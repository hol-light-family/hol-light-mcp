"""Optional static source lookup for HOL Light, from hol-language-server
(a submodule and path dependency): the `definition_source` tool.

Finds where a named theorem, definition, or tactic is defined, with its
source and statement. Parses files and the files they load with
needs/loads/loadt, plus the core HOL Light sources. Never talks to the HOL
Light process, so it works while HOL is starting, busy, or down.

Enabled by `[language_server] enabled = true` in hol-mcp.toml or by the
--enable-language-server flag.
"""

import asyncio
import json
import logging
import os
import threading
from typing import Annotated

from hol_language_server_core import config as core_config
from hol_language_server_core import parser as core_parser
from hol_language_server_core import service as core_service
from mcp.types import ToolAnnotations
from pydantic import Field

import hol_locate

logger = logging.getLogger(__name__)


def _string_list(value, name):
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"language_server.{name} must be an array of strings")
    return list(value)


def language_server_config(config, config_path, enable_flag=False, workspace_flag=None):
    """Validate the [language_server] table of hol-mcp.toml. Returns None when
    the language server is disabled (neither enabled in the table nor by
    --enable-language-server), else its effective settings. The workspace
    is, in order: --language-server-workspace (relative to the current
    directory), workspace in the table (relative to the config file), the
    current directory."""
    section = config.get("language_server", {})
    if not isinstance(section, dict):
        raise ValueError("language_server must be a TOML table")
    known = {"enabled", "workspace", "root_paths", "index_workspace",
             "custom_imports", "custom_theorems", "custom_definitions"}
    unknown = sorted(set(section) - known)
    if unknown:
        raise ValueError(f"Unknown language_server settings: {', '.join(unknown)}")
    enabled = section.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("language_server.enabled must be true or false")
    if not (enabled or enable_flag):
        return None
    index_workspace = section.get("index_workspace", True)
    if not isinstance(index_workspace, bool):
        raise ValueError("language_server.index_workspace must be true or false")
    workspace = section.get("workspace")
    if workspace_flag:
        workspace = os.path.expanduser(workspace_flag)
    elif workspace is not None:
        if not isinstance(workspace, str) or not workspace:
            raise ValueError("language_server.workspace must be a non-empty string")
        workspace = os.path.expanduser(workspace)
        if config_path and not os.path.isabs(workspace):
            workspace = os.path.join(os.path.dirname(config_path), workspace)
    return {
        "workspace": os.path.abspath(workspace or os.getcwd()),
        "root_paths": _string_list(section.get("root_paths", core_config.DEFAULT_ROOT_PATHS), "root_paths"),
        "index_workspace": index_workspace,
        "custom_imports": _string_list(section.get("custom_imports", []), "custom_imports"),
        "custom_theorems": _string_list(section.get("custom_theorems", []), "custom_theorems"),
        "custom_definitions": _string_list(section.get("custom_definitions", []), "custom_definitions"),
    }


class LanguageServer:
    """The index, built in a background thread so the server starts at once.
    Calls made during indexing wait until it is done."""

    def __init__(self, hol_dir, ls_config):
        self.settings = core_config.Settings(
            hol_path=hol_dir or "",
            root_paths=ls_config["root_paths"],
            workspace=ls_config["workspace"],
            index_workspace=ls_config["index_workspace"],
            custom_commands=core_parser.CustomCommands(
                imports=core_config._command_names(
                    ls_config["custom_imports"], "language_server.custom_imports"),
                theorems=core_config._command_names(
                    ls_config["custom_theorems"], "language_server.custom_theorems"),
                definitions=core_config._command_names(
                    ls_config["custom_definitions"], "language_server.custom_definitions"),
            ),
        )
        # A bad search path is a config error: report it at startup. Without
        # a HOL Light directory, the tool reports that instead.
        if hol_dir:
            core_config.expand_root_paths(self.settings)
        self.service = core_service.HolLanguageService(self.settings)
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._error = None
        self._thread = None

    def start_indexing(self):
        self._thread = threading.Thread(target=self._index, daemon=True)
        self._thread.start()

    def _index(self):
        try:
            if not self.settings.hol_path:
                raise ValueError(hol_locate.USAGE)
            with self._lock:
                self.service.database.index_base_hol_light_files(
                    self.settings.hol_path, self.settings.custom_commands)
                if self.settings.index_workspace:
                    unresolved = self.service.index_workspace()
                    if unresolved:
                        logger.warning("Loads not found in the workspace: %s",
                                       ", ".join(sorted({dep.name for dep in unresolved})))
        except Exception as exc:
            logger.error("Language server indexing failed: %s", exc)
            self._error = str(exc)
        finally:
            self._ready.set()

    def status(self):
        return {
            "workspace": self.settings.workspace,
            "indexing": not self._ready.is_set(),
            "error": self._error,
        }

    def definition_source(self, name, file=None, max_chars=None):
        self._ready.wait()
        if self._error:
            raise RuntimeError(f"Language server indexing failed: {self._error}")
        with self._lock:
            return self._definition_source(name, file, max_chars)

    def _definition_source(self, name, file, max_chars):
        service = self.service
        unresolved = []
        deps = None
        if file is not None:
            # Indexes the file and its dependencies. Unchanged files are not parsed again.
            file_path = core_service.resolve_path(file, self.settings.workspace)
            unresolved = sorted({dep.name for dep in service.index(file_path, True)})
            deps = service.database.all_dependencies(file_path)
        symbols = [s for s in service.find_symbols(name, deps) if s.kind != "module"]
        if not symbols:
            raise ValueError(
                f"No definition of {name} is indexed"
                + (f" in {file} or its dependencies" if file else "")
                + ". Try the qualified name (Module.name)."
                + (f" Loads that could not be found: {', '.join(unresolved)}." if unresolved else "")
            )
        definitions = []
        seen = set()
        for symbol in symbols:
            source = service.source(symbol)
            text = source.doc.text[source.start:source.end] if source else ""
            # A value's content is its type, not a statement.
            statement = symbol.content if symbol.kind != "value" and symbol.content else None
            # The same file can reach the index under two paths; collapse
            # byte-identical definitions so each is reported once.
            identity = json.dumps([symbol.full_name, symbol.kind, text, statement])
            if identity in seen:
                continue
            seen.add(identity)
            entry = {
                "name": symbol.full_name,
                "kind": symbol.kind,
                "file": symbol.path,
                # 1-based, of the defined name.
                "line": symbol.position.line + 1,
                "column": symbol.position.character + 1,
            }
            if text:
                entry["source"] = text[:max_chars]
            if max_chars is not None and len(text) > max_chars:
                entry["truncated"] = True
                entry["chars"] = len(text)
            if statement:
                entry["statement"] = statement[:max_chars]
            if statement and max_chars is not None and len(statement) > max_chars:
                entry["statement_truncated"] = True
                entry["statement_chars"] = len(statement)
            definitions.append(entry)
        result = {"definitions": definitions}
        if unresolved:
            result["unresolved_loads"] = unresolved
        return json.dumps(result, indent=1, ensure_ascii=False)


DESCRIPTION = " ".join([
    "Static lookup, without running HOL Light: where a tactic, conversion, rule or other OCaml value "
    "(or a theorem or constant definition) is defined, and its source code.",
    "Each entry gives the file and the 1-based line and column of the defined name, the source of the "
    "whole definition, and for theorems and constant definitions their statement.",
    "With max_chars, a longer source or statement is cut and marked truncated or "
    "statement_truncated; without it, both are returned whole.",
    "Proofs and the names a definition uses are not followed: look those up by name with this tool.",
    "A name defined in several files (e.g., a helper tactic each proof defines for itself) gives one "
    "entry per definition: pass file to get the one a proof uses.",
    "With file, `unresolved_loads` names the files it or its dependencies load that could not be "
    "found; definitions from them are missing from the result.",
    "Results are parsed, not type-checked: whether a statement or proof runs must be checked with "
    "the HOL Light tools.",
])


def register(mcp, language_server):
    """Add the definition_source tool to the FastMCP server."""

    @mcp.tool(
        name="definition_source",
        description=DESCRIPTION,
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    )
    async def definition_source(
        name: Annotated[str, Field(description="The name to look up, e.g. ARM_STEPS_TAC "
                                    "(may be qualified with modules: Module.name)")],
        file: Annotated[str | None, Field(description="Only definitions visible from this file "
                                           "(the file and its dependencies). Absolute, or relative "
                                           "to the workspace. Without it, every indexed "
                                           "definition with this name")] = None,
        max_chars: Annotated[int | None, Field(description="Maximum characters of each source and "
                                                "statement (longer ones are cut and marked truncated). "
                                                "Default: no limit",
                                                ge=1)] = None,
    ) -> str:
        # In a thread: indexing may still be running, and HOL tools keep the event loop.
        return await asyncio.to_thread(language_server.definition_source, name, file, max_chars)

    return definition_source
