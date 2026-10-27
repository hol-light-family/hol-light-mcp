"""Tests for the optional language server (definition_source).

They use the hol-language-server submodule's fixture checkouts, so they need
neither HOL Light nor a running HOL process.
"""

import asyncio
import json
import os

import pytest
from mcp.server.fastmcp import FastMCP

import language_server

EXAMPLES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "hol-language-server", "tests", "examples")
HOL = os.path.join(EXAMPLES, "hol")
PROJECT = os.path.join(EXAMPLES, "project")


def ls_config(**section):
    return language_server.language_server_config(
        {"language_server": {"enabled": True, "workspace": PROJECT, **section}}, None)


@pytest.fixture(scope="module")
def mcp():
    server = FastMCP("test")
    ls = language_server.LanguageServer(HOL, ls_config())
    language_server.register(server, ls)
    ls.start_indexing()
    return server


def call(mcp, **args):
    _, result = asyncio.run(mcp.call_tool("definition_source", args))
    return json.loads(result["result"])


# --- config ---

def test_disabled_by_default():
    assert language_server.language_server_config({}, None) is None
    assert language_server.language_server_config(
        {"language_server": {"enabled": False}}, None) is None


def test_defaults_when_enabled():
    cfg = language_server.language_server_config({"language_server": {"enabled": True}}, None)
    assert cfg["workspace"] == os.getcwd()
    assert cfg["root_paths"] == [".", "{hol}", "{workspace}"]
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


def test_workspace_flag_does_not_enable():
    assert language_server.language_server_config({}, None, workspace_flag=PROJECT) is None


def test_enable_flag_without_config():
    cfg = language_server.language_server_config({}, None, enable_flag=True)
    assert cfg["workspace"] == os.getcwd()


def test_enable_flag_overrides_enabled_false():
    assert language_server.language_server_config(
        {"language_server": {"enabled": False}}, None, enable_flag=True) is not None


@pytest.mark.parametrize("section, message", [
    ({"enabled": "yes"}, "enabled"),
    ({"enabled": True, "root_paths": "."}, "root_paths"),
    ({"enabled": True, "typo": 1}, "Unknown"),
])
def test_bad_config_is_rejected(section, message):
    with pytest.raises(ValueError, match=message):
        language_server.language_server_config({"language_server": section}, None)


def test_bad_root_path_is_rejected_at_startup():
    with pytest.raises(ValueError, match="placeholder"):
        language_server.LanguageServer(HOL, ls_config(root_paths=["{nope}"]))


def test_missing_hol_dir_is_reported_by_the_tool():
    server = FastMCP("test")
    ls = language_server.LanguageServer(None, ls_config())
    language_server.register(server, ls)
    ls.start_indexing()
    with pytest.raises(Exception, match="HOL Light directory not configured"):
        call(server, name="HELPER_TAC")
    assert ls.status()["indexing"] is False
    assert ls.status()["error"]


# --- definition_source ---

def test_tool_is_read_only(mcp):
    [tool] = asyncio.run(mcp.list_tools())
    assert tool.name == "definition_source"
    assert tool.annotations.readOnlyHint is True


def test_definition_with_location(mcp):
    [helper] = call(mcp, name="HELPER_TAC")["definitions"]
    assert helper == {
        "name": "HELPER_TAC",
        "kind": "value",
        "file": os.path.join(PROJECT, "tactics.ml"),
        "line": 12,
        "column": 5,
        "source": "let HELPER_TAC th =\n  REWRITE_TAC[th; ADD_SYM_ALT];;",
    }


def test_theorem_has_its_statement(mcp):
    [thm] = call(mcp, name="ADD_SYM_ALT")["definitions"]
    assert thm["kind"] == "theorem"
    assert thm["statement"] == "!m n. n + m = m + n"


def test_used_names_are_not_followed(mcp):
    result = call(mcp, name="OTHER_TAC")
    assert [d["name"] for d in result["definitions"]] == ["OTHER_TAC"]
    assert "references" not in result["definitions"][0]


def test_cut_source_is_marked_truncated(mcp):
    [cut] = call(mcp, name="HELPER_TAC", max_chars=25)["definitions"]
    assert len(cut["source"]) == 25
    assert cut["truncated"] is True


def test_cut_statement_is_marked_truncated(mcp):
    [thm] = call(mcp, name="ADD_SYM_ALT", max_chars=5)["definitions"]
    assert thm["statement"] == "!m n."
    assert thm["statement_truncated"] is True
    assert thm["statement_chars"] == len("!m n. n + m = m + n")


def test_no_limit_by_default(mcp):
    [helper] = call(mcp, name="HELPER_TAC")["definitions"]
    assert "truncated" not in helper
    assert helper["source"].endswith(";;")


def test_byte_identical_definitions_reported_once(mcp):
    assert len(call(mcp, name="DUP_TAC")["definitions"]) == 1


def test_unknown_name_is_an_error(mcp):
    with pytest.raises(Exception, match="No definition of REWRITE_TAC is indexed"):
        call(mcp, name="REWRITE_TAC")


def test_unresolved_loads_are_reported(mcp):
    result = call(mcp, name="MY_THM", file=os.path.join(PROJECT, "main.ml"))
    assert result["unresolved_loads"] == ["missing.ml"]
