"""Optional static source lookup for HOL Light, from hol-language-server:
the `definition_source` tool.

Finds where a named theorem, definition, or tactic is defined, with its
source and statement. Parses files and the files they load with
needs/loads/loadt, plus the core HOL Light sources. Never talks to the HOL
Light process, so it works while HOL is starting or down. It does not answer
while another HOL tool such as eval runs: those block the event loop.

Enabled by `[language_server] enabled = true` in hol-mcp.toml or by the
--enable-language-server flag. hol-language-server comes from the
`language-server` extra (Python 3.12+). It is imported only when the tool is
enabled, so the server runs without it.
"""

import asyncio
import json
import logging
import os
import threading
from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

import hol_locate

logger = logging.getLogger(__name__)

# How long a call waits for the startup index before it reports that
# indexing is still running.
WAIT_SECONDS = 20.0

INSTALL_HINT = (
    "definition_source needs the language-server extra, which needs Python 3.12+: "
    "run `uv sync --extra language-server` in the hol-light-mcp checkout."
)

NO_WORKSPACE_HINT = (
    "No workspace is set, so only the HOL Light sources are indexed. Set one with "
    "--language-server-workspace /path/to/project or workspace under [language_server]."
)


def _string_list(section, name):
    """The array of strings under `name`, or None when it is absent."""
    if name not in section:
        return None
    value = section[name]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"language_server.{name} must be an array of strings")
    return list(value)


def language_server_config(config, config_path, enable_flag=False, workspace_flag=None):
    """Validate the [language_server] table of hol-mcp.toml. Returns None when
    the language server is disabled (neither enabled in the table nor by
    --enable-language-server), else its effective settings. The workspace
    is --language-server-workspace (relative to the current directory), else
    workspace in the table (relative to the config file), else None: then no
    project is indexed, only HOL Light."""
    section = config.get("language_server", {})
    if not isinstance(section, dict):
        raise ValueError("language_server must be a TOML table")
    known = {"enabled", "workspace", "root_paths", "load_path", "index_workspace",
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
        "workspace": os.path.abspath(workspace) if workspace else None,
        # None: hol-language-server's defaults.
        "root_paths": _string_list(section, "root_paths"),
        # None: $HOLLIGHT_LOAD_PATH, as HOL Light reads it.
        "load_path": _string_list(section, "load_path"),
        "index_workspace": index_workspace,
        "custom_imports": _string_list(section, "custom_imports") or [],
        "custom_theorems": _string_list(section, "custom_theorems") or [],
        "custom_definitions": _string_list(section, "custom_definitions") or [],
    }


class LanguageServer:
    """The index, built in a background thread so the server starts at once.
    A call made during indexing waits up to WAIT_SECONDS for it.

    A problem that stops the index (no hol-language-server, no HOL Light
    directory, a bad workspace or setting) does not stop the MCP server: the
    tool and hol_status report it."""

    def __init__(self, hol_dir, ls_config, hol_dir_error=None):
        self.workspace = ls_config["workspace"]
        self.settings = None
        self.service = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._error = None
        self._thread = None
        try:
            self._setup(hol_dir, hol_dir_error, ls_config)
        except ImportError:
            self._fail(INSTALL_HINT)
        except ValueError as exc:
            self._fail(str(exc))

    def _setup(self, hol_dir, hol_dir_error, ls_config):
        from hol_language_server_core import config as core_config
        from hol_language_server_core import service as core_service

        if hol_dir is None:
            raise ValueError(hol_dir_error or hol_locate.USAGE)
        workspace = self.workspace
        if workspace is not None and not os.path.isdir(workspace):
            raise ValueError(f"The language server workspace {workspace} is not a directory")
        root_paths = ls_config["root_paths"]
        if root_paths is None:
            root_paths = [path for path in core_config.DEFAULT_ROOT_PATHS
                          if workspace is not None or path != "{workspace}"]
        load_path = ls_config["load_path"]
        if load_path is None:
            load_path = core_config.split_load_path(os.environ.get("HOLLIGHT_LOAD_PATH", ""))
        base = core_config.Settings(workspace=workspace or "", load_path=load_path)
        try:
            self.settings = core_config.settings_from_options({
                "path": hol_dir,
                "rootPaths": root_paths,
                "indexWorkspace": ls_config["index_workspace"] and workspace is not None,
                "customImports": ls_config["custom_imports"],
                "customTheorems": ls_config["custom_theorems"],
                "customDefinitions": ls_config["custom_definitions"],
            }, base)
            # Raises on a bad search path.
            core_config.expand_root_paths(self.settings)
        except ValueError as exc:
            raise ValueError(f"Invalid [language_server] settings: {exc}") from exc
        self.service = core_service.HolLanguageService(self.settings)
        self._resolve_path = core_service.resolve_path

    def _fail(self, message):
        logger.error("Language server unavailable: %s", message)
        self._error = message
        self._ready.set()

    def start_indexing(self):
        if self._ready.is_set():
            return
        self._thread = threading.Thread(target=self._index, daemon=True)
        self._thread.start()

    def _index(self):
        try:
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
            self._error = f"Indexing failed: {exc}"
        finally:
            self._ready.set()

    def status(self):
        return {
            "workspace": self.workspace,
            "indexing": not self._ready.is_set(),
            "error": self._error,
        }

    def definition_source(self, name, file=None, max_chars=None):
        if not self._ready.wait(WAIT_SECONDS):
            raise RuntimeError(
                "The language server is still indexing the workspace. Try again shortly; "
                "hol_status shows language_server.indexing = false when it is done.")
        if self._error:
            raise RuntimeError(f"Language server unavailable: {self._error}")
        with self._lock:
            return self._definition_source(name, file, max_chars)

    def _fresh_symbols(self, name, deps):
        """The definitions of name, after indexing again each project file
        that holds one, so their offsets match the file as it is now. A file
        that can no longer be read is left out."""
        service = self.service
        database = service.database

        def find(gone=frozenset()):
            return [s for s in service.find_symbols(name, deps)
                    if s.kind != "module" and s.path not in gone]

        gone = set()
        for path in sorted({s.path for s in find()}):
            # The core HOL Light files are indexed once, at startup.
            if database.is_base_hol_light_file(path):
                continue
            try:
                # Parses the file again only if its text changed.
                service.index(path, False)
            except OSError:
                gone.add(path)
        return find(gone)

    def _definition_source(self, name, file, max_chars):
        service = self.service
        workspace = self.settings.workspace
        unresolved = []
        deps = None
        if file is not None:
            file = os.path.expanduser(file)
            if not workspace and not os.path.isabs(file):
                raise ValueError(f"{file} is relative, but no workspace is set: pass an absolute path.")
            file_path = self._resolve_path(file, workspace)
            if not os.path.isfile(file_path):
                raise ValueError(f"No such file: {file_path}")
            # Indexes the file and its dependencies. Unchanged files are not parsed again.
            unresolved = sorted({dep.name for dep in service.index(file_path, True)})
            deps = service.database.all_dependencies(file_path)
        symbols = self._fresh_symbols(name, deps)
        if not symbols:
            raise ValueError(
                f"No definition of {name} is indexed"
                + (f" in {file} or its dependencies" if file else "")
                + ". Try the qualified name (Module.name)."
                + (f" Loads that could not be found: {', '.join(unresolved)}." if unresolved else "")
                + ("" if workspace else " " + NO_WORKSPACE_HINT)
            )
        definitions = []
        seen = set()
        texts = {}
        for symbol in symbols:
            text = ""
            if symbol.source_start is not None and symbol.source_end is not None:
                if symbol.path not in texts:
                    try:
                        texts[symbol.path] = service.get_document(symbol.path).text
                    except OSError:
                        # Removed since _fresh_symbols read it.
                        texts[symbol.path] = None
                if texts[symbol.path] is None:
                    continue
                text = texts[symbol.path][symbol.source_start:symbol.source_end]
            # A value's content is its type, not a statement.
            statement = symbol.content if symbol.kind != "value" and symbol.content else None
            # The same file can reach the index under two paths, and a file
            # can repeat a definition word for word: report those once.
            # Definitions in different files stay separate.
            identity = json.dumps([os.path.realpath(symbol.path), symbol.full_name,
                                   symbol.kind, text, statement])
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
        if not definitions:
            raise ValueError(f"Every file that defined {name} has been removed.")
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
