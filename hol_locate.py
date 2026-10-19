"""Locate the HOL Light checkout used by the MCP server and make_checkpoint.py."""

import os

ENV_VAR = "HOL_LIGHT_DIR"
USAGE = (
    "HOL Light directory not configured. Set it with one of: "
    "--hol-dir /path/to/hol-light, "
    f"{ENV_VAR}=/path/to/hol-light, "
    'or hol_dir = "/path/to/hol-light" in hol-mcp.toml.'
)


def cli_arg(argv, name):
    """Return the value following `name` in argv, or None."""
    for i, arg in enumerate(argv):
        if arg == name and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


def resolve_hol_dir(flag=None, config_value=None, config_path=None, environ=None):
    """Resolve the HOL Light directory. Precedence:
    1. --hol-dir flag  2. HOL_LIGHT_DIR env  3. hol_dir in hol-mcp.toml
    (relative to the config file). Raises ValueError if unset or invalid."""
    environ = os.environ if environ is None else environ
    if flag:
        path, source = flag, "--hol-dir"
    elif environ.get(ENV_VAR):
        path, source = environ[ENV_VAR], ENV_VAR
    elif config_value is not None:
        if not isinstance(config_value, str) or not config_value:
            raise ValueError("hol_dir must be a non-empty string")
        path, source = config_value, "hol_dir"
        if config_path and not os.path.isabs(os.path.expanduser(path)):
            path = os.path.join(os.path.dirname(config_path), path)
    else:
        raise ValueError(USAGE)
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(os.path.join(path, "hol.ml")):
        raise ValueError(
            f"{source} points at {path}, which does not look like a HOL Light "
            "checkout (no hol.ml found)"
        )
    return path
