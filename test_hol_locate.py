"""Tests for locating the HOL Light checkout."""

import json
import os
import subprocess
import sys

import pytest

import hol_locate
import server


def make_hol(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / "hol.ml").write_text("")
    return path


def test_flag_beats_env_and_config(tmp_path):
    flag = make_hol(tmp_path / "flag")
    env = make_hol(tmp_path / "env")
    cfg = make_hol(tmp_path / "cfg")
    assert hol_locate.resolve_hol_dir(
        flag=str(flag), config_value=str(cfg),
        environ={hol_locate.ENV_VAR: str(env)},
    ) == str(flag)


def test_env_beats_config(tmp_path):
    env = make_hol(tmp_path / "env")
    cfg = make_hol(tmp_path / "cfg")
    assert hol_locate.resolve_hol_dir(
        config_value=str(cfg), environ={hol_locate.ENV_VAR: str(env)},
    ) == str(env)


def test_config_relative_to_config_file(tmp_path):
    make_hol(tmp_path / "hol-light")
    config_path = tmp_path / "proj" / "hol-mcp.toml"
    assert hol_locate.resolve_hol_dir(
        config_value="../hol-light", config_path=str(config_path), environ={},
    ) == str(tmp_path / "hol-light")


def test_unset_raises_usage():
    with pytest.raises(ValueError) as exc:
        hol_locate.resolve_hol_dir(environ={})
    assert "--hol-dir" in str(exc.value)
    assert hol_locate.ENV_VAR in str(exc.value)
    assert "hol_dir" in str(exc.value)


def test_missing_hol_ml_raises(tmp_path):
    with pytest.raises(ValueError, match="no hol.ml"):
        hol_locate.resolve_hol_dir(flag=str(tmp_path), environ={})


def test_non_string_config_raises():
    with pytest.raises(ValueError, match="hol_dir must be"):
        hol_locate.resolve_hol_dir(config_value=42, environ={})


def test_cli_arg_forms():
    assert hol_locate.cli_arg(["x", "--hol-dir", "/a"], "--hol-dir") == "/a"
    assert hol_locate.cli_arg(["x", "--hol-dir=/b"], "--hol-dir") == "/b"
    assert hol_locate.cli_arg(["x", "--hol-dir"], "--hol-dir") is None


def test_server_unconfigured_reports_error(monkeypatch):
    monkeypatch.setattr(server, "HOL_DIR", None)
    monkeypatch.setattr(server, "HOL_DIR_ERROR", hol_locate.USAGE)
    monkeypatch.setattr(server, "_proc", None)
    with pytest.raises(RuntimeError, match="HOL Light directory not configured"):
        server._start_hol()
    status = json.loads(server.hol_status())
    assert status["hol_dir"] is None
    assert status["hol_dir_error"] == hol_locate.USAGE


def test_make_checkpoint_unconfigured_exits():
    r = subprocess.run(
        [sys.executable, "make_checkpoint.py"],
        cwd=os.path.dirname(os.path.abspath(hol_locate.__file__)),
        env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True,
    )
    assert r.returncode == 1
    assert "HOL Light directory not configured" in r.stderr
