"""Tests for the optional language server (definition_source).

They write small HOL Light and project sources to a temporary directory, so
they need neither HOL Light nor a running HOL process. Tests of lookups need
the language-server extra (`uv sync --extra language-server`, Python 3.12+)
and are skipped without it. The others run everywhere.
"""

import asyncio
import importlib.util
import json
import os
import sys

import pytest
from mcp.server.fastmcp import FastMCP

import language_server

needs_extra = pytest.mark.skipif(
    importlib.util.find_spec("hol_language_server_core") is None,
    reason="needs the language-server extra")

HOL_ML = """\
let ADD_SYM = prove
 (`!m n. m + n = n + m`,
  ARITH_TAC);;

let EVEN = new_definition `EVEN n <=> ?m. n = 2 * m`;;
"""

PROJECT = {
    "lib/util.ml": """\
let ADD_SYM_ALT = prove
 (`!m n. n + m = m + n`,
  REWRITE_TAC[ADD_SYM]);;
""",
    "tactics.ml": """\
needs "lib/util.ml";;

(* A tactic with a parameter *)
let HELPER_TAC th =
  REWRITE_TAC[th; ADD_SYM_ALT];;

let OTHER_TAC = HELPER_TAC ADD_SYM_ALT;;
""",
    "main.ml": """\
needs "lib/util.ml";;
needs "missing.ml";;

let MY_THM = prove
 (`!n. EVEN n ==> EVEN (n + n)`,
  REWRITE_TAC[EVEN; ADD_SYM_ALT] THEN ARITH_TAC);;
""",
    # The same tactic twice in one file: reported once.
    "dup.ml": "let DUP_TAC x = x;;\nlet DUP_TAC x = x;;\n",
    # The same text in two files: reported twice.
    "a/step.ml": "let STEP_TAC = ALL_TAC;;\n",
    "b/step.ml": "let STEP_TAC = ALL_TAC;;\n",
    # Found only through the load path.
    "uses_extra.ml": 'needs "extra.ml";;\n',
}


def write_tree(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return str(root)


@pytest.fixture(scope="module")
def tree(tmp_path_factory):
    root = tmp_path_factory.mktemp("ls")
    hol = write_tree(root / "hol", {"hol.ml": HOL_ML})
    project = write_tree(root / "project", PROJECT)
    extra = write_tree(root / "extra", {"extra.ml": "let EXTRA_TAC = ALL_TAC;;\n"})
    return {"hol": hol, "project": project, "extra": extra}


@pytest.fixture
def hol(tree):
    return tree["hol"]


@pytest.fixture
def project(tree):
    return tree["project"]


def ls_config(workspace, **section):
    table = {"enabled": True, **section}
    if workspace is not None:
        table["workspace"] = workspace
    return language_server.language_server_config({"language_server": table}, None)


def start(hol_dir, config, hol_dir_error=None):
    server = FastMCP("test")
    ls = language_server.LanguageServer(hol_dir, config, hol_dir_error)
    language_server.register(server, ls)
    ls.start_indexing()
    return server, ls


def call(mcp, **args):
    _, result = asyncio.run(mcp.call_tool("definition_source", args))
    return json.loads(result["result"])


@pytest.fixture(scope="module")
def mcp(tree):
    server, _ = start(tree["hol"], ls_config(tree["project"]))
    return server


# --- config ---

def test_disabled_by_default():
    assert language_server.language_server_config({}, None) is None
    assert language_server.language_server_config(
        {"language_server": {"enabled": False}}, None) is None


def test_defaults_when_enabled():
    cfg = language_server.language_server_config({"language_server": {"enabled": True}}, None)
    # Not the current directory: the server often runs in the hol-light-mcp checkout.
    assert cfg["workspace"] is None
    assert cfg["root_paths"] is None
    assert cfg["load_path"] is None
    assert cfg["index_workspace"] is True


def test_workspace_relative_to_config_file(tmp_path):
    config_path = str(tmp_path / "hol-mcp.toml")
    cfg = language_server.language_server_config(
        {"language_server": {"enabled": True, "workspace": "proj"}}, config_path)
    assert cfg["workspace"] == str(tmp_path / "proj")


def test_workspace_flag_beats_config(tmp_path):
    config_path = str(tmp_path / "hol-mcp.toml")
    cfg = language_server.language_server_config(
        {"language_server": {"enabled": True, "workspace": "proj"}}, config_path, workspace_flag="flagged")
    assert cfg["workspace"] == os.path.join(os.getcwd(), "flagged")


def test_workspace_flag_does_not_enable(tmp_path):
    assert language_server.language_server_config({}, None, workspace_flag=str(tmp_path)) is None


def test_enable_flag_without_config():
    cfg = language_server.language_server_config({}, None, enable_flag=True)
    assert cfg["workspace"] is None


def test_enable_flag_overrides_enabled_false():
    assert language_server.language_server_config(
        {"language_server": {"enabled": False}}, None, enable_flag=True) is not None


@pytest.mark.parametrize("section, message", [
    ({"enabled": "yes"}, "enabled"),
    ({"enabled": True, "root_paths": "."}, "root_paths"),
    ({"enabled": True, "load_path": [1]}, "load_path"),
    ({"enabled": True, "typo": 1}, "Unknown"),
])
def test_bad_config_is_rejected(section, message):
    with pytest.raises(ValueError, match=message):
        language_server.language_server_config({"language_server": section}, None)


# --- problems are reported by the tool, and the server keeps running ---

def test_missing_extra_is_reported(monkeypatch, tmp_path):
    # None in sys.modules makes the import fail, as when the extra is not installed.
    monkeypatch.setitem(sys.modules, "hol_language_server_core", None)
    server, ls = start(str(tmp_path), ls_config(str(tmp_path)))
    with pytest.raises(Exception, match="uv sync --extra language-server"):
        call(server, name="X")
    assert ls.status() == {"workspace": str(tmp_path), "indexing": False,
                           "error": language_server.INSTALL_HINT}


@needs_extra
def test_missing_hol_dir_is_reported(project):
    server, ls = start(None, ls_config(project))
    with pytest.raises(Exception, match="HOL Light directory not configured"):
        call(server, name="HELPER_TAC")
    assert ls.status()["indexing"] is False
    assert ls.status()["error"]


@needs_extra
def test_the_real_hol_dir_error_is_reported(project):
    server, _ = start(None, ls_config(project), "HOL_LIGHT_DIR points at /x, which does not look")
    with pytest.raises(Exception, match="HOL_LIGHT_DIR points at /x"):
        call(server, name="HELPER_TAC")


@needs_extra
def test_missing_workspace_is_reported(hol, tmp_path):
    server, ls = start(hol, ls_config(str(tmp_path / "typo")))
    with pytest.raises(Exception, match="typo is not a directory"):
        call(server, name="HELPER_TAC")
    assert "not a directory" in ls.status()["error"]


@needs_extra
def test_bad_root_path_is_reported(hol, project):
    server, _ = start(hol, ls_config(project, root_paths=["{nope}"]))
    with pytest.raises(Exception, match="placeholder"):
        call(server, name="HELPER_TAC")


@needs_extra
def test_a_call_during_indexing_does_not_wait_forever(hol, project, monkeypatch):
    monkeypatch.setattr(language_server, "WAIT_SECONDS", 0.01)
    server = FastMCP("test")
    # Indexing not started: the call must give up.
    language_server.register(server, language_server.LanguageServer(hol, ls_config(project)))
    with pytest.raises(Exception, match="still indexing"):
        call(server, name="HELPER_TAC")


# --- definition_source ---

@needs_extra
def test_tool_is_read_only(mcp):
    [tool] = asyncio.run(mcp.list_tools())
    assert tool.name == "definition_source"
    assert tool.annotations.readOnlyHint is True


@needs_extra
def test_definition_with_location(mcp, project):
    [helper] = call(mcp, name="HELPER_TAC")["definitions"]
    assert helper == {
        "name": "HELPER_TAC",
        "kind": "value",
        "file": os.path.join(project, "tactics.ml"),
        "line": 4,
        "column": 5,
        "source": "let HELPER_TAC th =\n  REWRITE_TAC[th; ADD_SYM_ALT];;",
    }


@needs_extra
def test_theorem_has_its_statement(mcp):
    [thm] = call(mcp, name="ADD_SYM_ALT")["definitions"]
    assert thm["kind"] == "theorem"
    assert thm["statement"] == "!m n. n + m = m + n"


@needs_extra
def test_used_names_are_not_followed(mcp):
    result = call(mcp, name="OTHER_TAC")
    assert [d["name"] for d in result["definitions"]] == ["OTHER_TAC"]
    assert "references" not in result["definitions"][0]


@needs_extra
def test_cut_source_is_marked_truncated(mcp):
    [cut] = call(mcp, name="HELPER_TAC", max_chars=25)["definitions"]
    assert len(cut["source"]) == 25
    assert cut["truncated"] is True


@needs_extra
def test_cut_statement_is_marked_truncated(mcp):
    [thm] = call(mcp, name="ADD_SYM_ALT", max_chars=5)["definitions"]
    assert thm["statement"] == "!m n."
    assert thm["statement_truncated"] is True
    assert thm["statement_chars"] == len("!m n. n + m = m + n")


@needs_extra
def test_no_limit_by_default(mcp):
    [helper] = call(mcp, name="HELPER_TAC")["definitions"]
    assert "truncated" not in helper
    assert helper["source"].endswith(";;")


@needs_extra
def test_a_repeat_in_one_file_is_reported_once(mcp):
    assert len(call(mcp, name="DUP_TAC")["definitions"]) == 1


@needs_extra
def test_the_same_text_in_two_files_is_reported_twice(mcp, project):
    files = sorted(d["file"] for d in call(mcp, name="STEP_TAC")["definitions"])
    assert files == [os.path.join(project, "a", "step.ml"), os.path.join(project, "b", "step.ml")]


@needs_extra
def test_unknown_name_is_an_error(mcp):
    with pytest.raises(Exception, match="No definition of REWRITE_TAC is indexed"):
        call(mcp, name="REWRITE_TAC")


@needs_extra
def test_unresolved_loads_are_reported(mcp, project):
    result = call(mcp, name="MY_THM", file=os.path.join(project, "main.ml"))
    assert result["unresolved_loads"] == ["missing.ml"]


@needs_extra
def test_file_relative_to_workspace(mcp):
    [thm] = call(mcp, name="MY_THM", file="main.ml")["definitions"]
    assert thm["kind"] == "theorem"


@needs_extra
def test_missing_file_is_an_error(mcp):
    with pytest.raises(Exception, match="No such file"):
        call(mcp, name="MY_THM", file="nope.ml")


# --- no workspace ---

@needs_extra
def test_without_a_workspace_only_hol_light_is_indexed(hol, project):
    server, ls = start(hol, ls_config(None))
    [thm] = call(server, name="ADD_SYM")["definitions"]
    assert thm["file"] == os.path.join(hol, "hol.ml")
    with pytest.raises(Exception, match="No workspace is set"):
        call(server, name="HELPER_TAC")
    with pytest.raises(Exception, match="no workspace is set"):
        call(server, name="HELPER_TAC", file="tactics.ml")
    # An absolute file still works.
    [helper] = call(server, name="HELPER_TAC", file=os.path.join(project, "tactics.ml"))["definitions"]
    assert helper["line"] == 4
    assert ls.status()["workspace"] is None


# --- load path ---

@needs_extra
def test_load_path_from_config(hol, tree):
    server, _ = start(hol, ls_config(tree["project"], load_path=[tree["extra"]]))
    result = call(server, name="EXTRA_TAC", file="uses_extra.ml")
    assert "unresolved_loads" not in result
    assert result["definitions"][0]["file"] == os.path.join(tree["extra"], "extra.ml")


@needs_extra
def test_load_path_from_environment(hol, tree, monkeypatch):
    monkeypatch.setenv("HOLLIGHT_LOAD_PATH", tree["extra"])
    server, _ = start(hol, ls_config(tree["project"]))
    result = call(server, name="EXTRA_TAC", file="uses_extra.ml")
    assert "unresolved_loads" not in result


# --- edits after indexing ---

@needs_extra
def test_source_follows_an_edit(hol, tmp_path):
    workspace = write_tree(tmp_path, {"t.ml": "let T_TAC = ALL_TAC;;\n"})
    server, _ = start(hol, ls_config(workspace))
    assert call(server, name="T_TAC")["definitions"][0]["line"] == 1
    (tmp_path / "t.ml").write_text("(* new *)\n\n\nlet T_TAC = NO_TAC;;\n")
    [entry] = call(server, name="T_TAC")["definitions"]
    assert entry["line"] == 4
    assert entry["source"] == "let T_TAC = NO_TAC;;"


@needs_extra
def test_a_removed_file_is_left_out(hol, tmp_path):
    workspace = write_tree(tmp_path, {"a.ml": "let R_TAC = ALL_TAC;;\n",
                                      "b.ml": "let R_TAC = NO_TAC;;\n"})
    server, _ = start(hol, ls_config(workspace))
    assert len(call(server, name="R_TAC")["definitions"]) == 2
    (tmp_path / "a.ml").unlink()
    [entry] = call(server, name="R_TAC")["definitions"]
    assert entry["file"] == os.path.join(workspace, "b.ml")
