# HOL Light MCP Server

MCP server for the [HOL Light](https://github.com/jrh13/hol-light) theorem prover, designed for LLM-assisted proof search.

## Quick Start

After setup (below), here's a complete proof session:

```
set_goal  "`!n. EVEN n ==> EVEN(n * n)`"
→ {"goals":[{"hypotheses":[],"conclusion":"forall n. EVEN n ==> EVEN (n * n)"}], ...}

apply_tactic  "GEN_TAC THEN STRIP_TAC"
→ {"goals":[{"hypotheses":[{"label":"","term":"EVEN n"}],"conclusion":"EVEN (n * n)"}], ...}

search_theorems  name="EVEN_MULT"
→ [{"name":"EVEN_MULT","statement":"|- forall m n. EVEN (m * n) <=> EVEN m \\/ EVEN n"}]

apply_tactic  "ASM_REWRITE_TAC[EVEN_MULT]"
→ {"proved":true,"theorem":"|- forall n. EVEN n ==> EVEN (n * n)"}
```

Or as a one-shot proof:

```
prove  goal="`!n. EVEN n ==> EVEN(n * n)`"  tactic="GEN_TAC THEN STRIP_TAC THEN ASM_REWRITE_TAC[EVEN_MULT]"
→ {"proved":true,"theorem":"|- forall n. EVEN n ==> EVEN (n * n)"}
```

See [TUTORIAL.md](TUTORIAL.md) for more examples (including s2n-bignum ARM proofs) and [SKILL.md](SKILL.md) for a tactic reference.

## Tools

| Tool | Description | Output |
|------|-------------|--------|
| `eval` | Evaluate arbitrary OCaml/HOL Light code | Structured JSON (truncated) |
| `set_goal` | Set a proof goal, return initial state | Structured JSON |
| `goal_state` | Return current proof goals | Structured JSON |
| `apply_tactic` | Apply a tactic, return new state or proved theorem | Structured JSON |
| `apply_tactics` | Apply a list of tactics in one round-trip | Structured JSON |
| `prove` | One-shot prove: goal + tactic → theorem | Structured JSON |
| `backtrack` | Undo tactic steps | Structured JSON |
| `search_theorems` | Search theorem database by name | Structured JSON |
| `hol_type` | Get the type of a term | Raw text |
| `hol_load` | Load a HOL Light file via `needs` | Structured JSON |
| `hol_interrupt` | Cancel a long-running command | Status message |
| `hol_restart` | Reload config; optionally rebuild a checkpoint; restart HOL | Structured JSON |
| `hol_status` | Check process health, uptime, config, checkpoint | Structured JSON |
| `hol_help` | Return tactic reference and proof guide (SKILL.md) | Markdown text |
| `start_recording` | Start recording proof tactics to a JSONL file | Status message |
| `stop_recording` | Stop recording and return the file path | Status message |
| `definition_source` | Where a name is defined and its source, parsed without running HOL ([optional](#language-server-optional)) | Structured JSON |

`eval` returns `{"success", "output", "output_truncated", "full_output_chars", "time_seconds"}`. Large outputs are truncated to `max_output_chars` (default 4000, configurable). Override per-call with `max_output_chars=N`.

`hol_load` returns `{"success", "file", "time_seconds"}` (plus `"error"` on failure). Intermediate output is suppressed — use `eval` with `needs "file.ml"` if you need verbose output.

## Setup

Build HOL Light, then install the server:

```bash
git clone https://github.com/jrh13/hol-light.git
(cd hol-light && make switch && eval $(opam env) && make)

git clone https://github.com/hol-light-family/hol-light-mcp.git
cd hol-light-mcp && uv sync
```

The server needs Python 3.11+. The optional `definition_source` tool needs
Python 3.12+ and an extra: see [Language Server](#language-server-optional).

The server needs to know where HOL Light is. It checks, in order:

1. `--hol-dir /path/to/hol-light` on the command line
2. the `HOL_LIGHT_DIR` environment variable
3. `hol_dir = "/path/to/hol-light"` in `hol-mcp.toml` (relative paths are resolved against the config file)

If none is set, the server exits with an error naming these options.

## Checkpointing (optional, recommended)

HOL Light takes ~75s to cold start. DMTCP checkpointing reduces this to ~2s.

Requires DMTCP installed (`dmtcp_launch` and `dmtcp_restart` on PATH). To build from source:

```bash
git clone https://github.com/dmtcp/dmtcp.git /tmp/dmtcp
cd /tmp/dmtcp && ./configure --prefix=$HOME/.local && make -j$(nproc) && make install
```

Create named checkpoints:

```bash
export LD_LIBRARY_PATH="$HOME/.local/lib:$LD_LIBRARY_PATH"
export HOL_LIGHT_DIR=/path/to/hol-light   # or pass --hol-dir

# Base HOL Light (~75s)
python3 make_checkpoint.py --name base

# With s2n-bignum ARM infrastructure (~5-10min)
python3 make_checkpoint.py --name s2n -I /path/to/s2n-bignum 'needs "arm/proofs/base.ml"'
```

Checkpoints are written to `$HOL_LIGHT_DIR/hol-<name>.ckpt/`.

## Configuration

By default, the server loads `hol-mcp.toml` from the current directory, falling back to the one in this repo. To use a different config, pass `--config /absolute/path/to/hol-mcp.toml` or set `HOL_MCP_CONFIG`.

```toml
# HOL Light checkout (overridden by --hol-dir or HOL_LIGHT_DIR).
hol_dir = "/path/to/hol-light"

# Which checkpoint to use (looks for <hol_dir>/hol-<name>.ckpt/).
checkpoint = "s2n"

# Timeout in seconds for HOL Light commands.
timeout = 600

# Maximum characters for eval output before truncation.
max_output_chars = 4000

# Checkpoint recipes for hol_restart(rebuild_hol_and_checkpoint=true).
# "base" is vanilla HOL Light and needs no recipe; other checkpoints require one.
# Example recipe with s2n-bignum ARM infrastructure (uncomment to enable):
# [checkpoint_recipes.gcm]
# include_dirs = ["/path/to/s2n-bignum"]
# loads = ['needs "arm/proofs/base.ml"', 'needs "common/gcm.ml"']
```

`hol_restart(checkpoint="gcm")` selects `gcm` for one invocation without
modifying the config. `hol_restart(rebuild_hol_and_checkpoint=true)`
synchronously runs `make clean` and `make` to rebuild HOL Light from source,
then rebuilds the selected checkpoint recipe, before restarting HOL. Because it
rebuilds HOL Light itself, it commonly takes several minutes and must be
requested explicitly. The `base` checkpoint can be rebuilt without a recipe;
any other checkpoint requires an explicit `[checkpoint_recipes.<name>]` entry,
otherwise the rebuild fails rather than replacing a custom checkpoint with bare
HOL. If the build or checkpoint creation fails, the MCP server remains
available but stops HOL and rejects proof work until the build is fixed and
`hol_restart(rebuild_hol_and_checkpoint=true)` succeeds.

The restart response reports the active and configured checkpoints, process
IDs, startup mode, changed config values, and failure diagnostics. Use
`hol_status` to inspect the active checkpoint, its captured file fingerprint,
whether its files have changed since startup, and whether a rebuild is
required.

## Language Server (optional)

The `definition_source` tool comes from [hol-language-server](https://github.com/hol-light-family/hol-language-server).
It parses HOL Light and project sources and never talks to the HOL process, so
it answers while HOL is starting or down. It is off by default.

- Install it with `uv sync --extra language-server`. It needs Python 3.12+.
- Enable it with `--enable-language-server`, or `enabled = true` under
  `[language_server]` in `hol-mcp.toml` (other keys: see that file).
- Set the project to index with `--language-server-workspace /path/to/project`,
  or `workspace` under `[language_server]`.
  - Without one, only the core HOL Light files are indexed.
  - It is not the current directory: clients often start the server in the
    hol-light-mcp checkout.
- `needs` paths are searched in the loading file's directory, HOL Light, the
  workspace, then `$HOLLIGHT_LOAD_PATH` (or `load_path` in the config).
- Indexing runs in the background at startup.
  - A call made before it finishes waits up to 20 seconds, then reports that
    indexing is still running.
  - `hol_status` reports its progress and any error under `language_server`.
- Each call parses again the files that define the name if they changed. A
  call with `file` does the same for that file and the files it loads.
- A problem such as a missing extra or a bad workspace does not stop the
  server: the tool and `hol_status` report it.
- The settings are read once at startup; `hol_restart` does not reload them.

To use it from a client, add `--extra language-server` and the flags to the
`args` of the [client configuration](#client-configuration). For example, for
s2n-bignum:

```json
"args": ["run", "--extra", "language-server", "--directory", "/path/to/hol-light-mcp",
         "hol-light-mcp", "--enable-language-server",
         "--language-server-workspace", "/path/to/s2n-bignum"]
```

Indexing a project the size of s2n-bignum can take half a minute or more.
A call made sooner reports that indexing is still running; try it again.

The tool does not answer while another HOL tool such as `eval` runs. It
answers when that tool returns.

`definition_source(name, file=None, max_chars=None)` returns, for each indexed
definition of `name`, its file, the 1-based line and column of the name, its
source, and for theorems and constant definitions their statement. It does
not follow the names a definition uses; look those up by name. With `file`,
only the definitions that file sees are returned, and `unresolved_loads`
lists the loads that could not be found. With `max_chars`, a longer source or
statement is cut and marked; without it, both are returned whole.

## Usage

```bash
uv run hol-light-mcp --hol-dir /path/to/hol-light
uv run hol-light-mcp --config /path/to/hol-mcp.toml
```

The server speaks MCP over stdio.

## Client Configuration

### Kiro CLI (`~/.kiro/settings/mcp.json`)

```json
{
  "mcpServers": {
    "hol-light": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/hol-light-mcp", "hol-light-mcp",
               "--config", "/path/to/your/project/hol-mcp.toml"],
      "env": {
        "PATH": "/home/user/.local/bin:/usr/bin:/bin",
        "HOL_LIGHT_DIR": "/path/to/hol-light"
      }
    }
  }
}
```

### Skill / Tactic Guide

The server includes a built-in `hol_help` tool that returns the full tactic reference and proof guide ([SKILL.md](SKILL.md)). The LLM can call it before its first proof — no extra configuration needed.

## Tests

```bash
export HOL_LIGHT_DIR=/path/to/hol-light
uv run pytest test_server.py test_restart.py test_hol_locate.py -v   # 72 unit tests
uv run python smoke_test.py                                          # 37 MCP integration checks
uv run --extra language-server pytest test_language_server.py -v   # needs no HOL Light
```

First run includes HOL Light startup (~75s cold, ~2s with checkpoint).

## Security Considerations

The `eval` tool executes arbitrary OCaml code, which has full access to the host system. This is inherent to theorem proving. However, the server uses stdio transport (no network sockets), so it is only accessible to the MCP client process that spawned it. It is not exposed to the network, even on an EC2 instance with open ports.

Treat DMTCP checkpoint files (`hol-<name>.ckpt/`) as executable code---don't restore untrusted checkpoints.

## Related Work

[mkannwischer/mcp-hol-light](https://github.com/mkannwischer/mcp-hol-light) is another MCP server for HOL Light. It connects to a separate [hol-server](https://github.com/monadius/hol_server) TCP process and exposes a similar set of tools (`hol_eval`, `hol_tactic`, `hol_back`, `hol_search`, etc.). It implements the MCP protocol directly rather than using an SDK, and returns raw REPL text from all tools.

We take a different approach: we embed HOL Light as a subprocess (no external server needed), use OCaml-side JSON serialization to return structured goal states and search results, and support DMTCP checkpointing for instant startup. We find that structured output is particularly useful for automated proof search, where an LLM agent needs to programmatically inspect goals, detect proof completion, and implement backtracking strategies without parsing REPL text.

## Acknowledgments

Thanks to kceren for advice on HOL Light tactics and proof patterns, to nebeid for suggesting named checkpoints, and to the s2n-bignum team for encouragement and tutorial examples.

## License

BSD 2-Clause, the same license as HOL Light. See [LICENSE](LICENSE).
