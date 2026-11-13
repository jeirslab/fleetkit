# Custom tools

A repository can keep its own small command-line tools, written in Python or shell,
and run them as `fleetkit tools <name> ...`. They are for tools that concern the
estate (a report, a one-off migration, a wrapper around a vendor API), not MCP
servers or skills.

## Layout

```
tools/                       the tools directory (configurable, below)
  deploy-report/             one folder per tool; the folder name is the command
    README.md                what it is; its first line is shown by `tools list`
    requirements.txt         PyPI packages it needs (the file must exist; may be empty)
    __init__.py              exposes a click command or group named `cli`
                             (or main.py, when the folder has one, instead)
```

A shell tool is a folder whose `cli` is a small click command that runs the script:

```python
import subprocess, sys
from pathlib import Path
import click

@click.command(context_settings={"ignore_unknown_options": True})
@click.argument("args", nargs=-1)
def cli(args):
    """Report on something."""
    sys.exit(subprocess.run([str(Path(__file__).with_name("report.sh")), *args]).returncode)
```

A tool is loaded as a package, so its modules import each other relatively
(`from .helper import x`). Folders starting with `_` or `.` are ignored, and
`list` and `check` are not allowed as tool names.

## Commands

```
fleetkit tools list          every tool and the first line of its README
fleetkit tools check         README non-empty, requirements.txt present, `cli` importable;
                             exit 1 with one line per problem (for CI and gates)
fleetkit tools NAME ...      run the tool
```

## The tools directory

In order: `--tools-dir` (or `FLEETKIT_TOOLS_DIR`), `tools_dir` in `fleetkit.toml` at the
repository root, then `tools`. A repository whose `tools/` holds other things sets
`tools_dir = "fleet-tools"` (or any path) in `fleetkit.toml`.

## Environment

Tools run in one venv per repository, built with `uv` from `click` (pinned to the
version fleetkit itself runs) plus the union of the tools' `requirements.txt` files. It
lives in `<repo>/.fleet/tools-venv/<hash>/` (`.fleet/` is ignored), is keyed by a hash of
that union and the Python, and is rebuilt only when either changes. `list` needs no venv.
The fleetkit package ships `uv`. `FLEETKIT_TOOLS_PYTHON` names an interpreter to use
instead (tests, or an environment the repo already has).

## main.py

fleetkit provides the `tools` group (`fleetkit_cli/tools_main.py`): it discovers the folders and
loads each tool only when it is asked for, so a broken tool breaks only itself (and
`check`). A repository that wants something else puts a `main.py` in the tools directory
that exposes a click group named `tools`; it replaces the built-in one.
