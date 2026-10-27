"""Custom tools of a repository: `fleetkit tools ...`.

A tools directory (default `tools/`, see tools_env.tools_dir) holds one folder
per tool:

    <tools dir>/<name>/README.md          what it is; the first line is its summary
    <tools dir>/<name>/requirements.txt   PyPI packages it needs (may be empty)
    <tools dir>/<name>/__init__.py        exposes a click command or group `cli`
    (or main.py)                          instead of __init__.py, when it has one

A shell tool is a folder whose `cli` is a small click command that runs its
script. Folders starting with `_` or `.` are not tools. `tools list` and
`tools check` are built in, so no tool may be called `list` or `check`.

This file is both imported by fleetkit (for `list`, which needs no packages)
and run by the tools' own interpreter (tools_env.py): there it imports only the
standard library and click, so it works in a venv without fleetkit in it.
A directory's own `main.py` that exposes a click group `tools` replaces the
group built here.

    python tools_main.py --tools-dir DIR [tools arguments ...]
"""
from __future__ import annotations

import importlib
import importlib.util
import re
import sys
import types
from pathlib import Path

RESERVED = ("list", "check")


def tool_names(tools_dir: Path) -> list[str]:
    if not tools_dir.is_dir():
        return []
    return sorted(p.name for p in tools_dir.iterdir()
                  if p.is_dir() and not p.name.startswith(("_", ".")))


def summary(tool_dir: Path) -> str:
    """The README's first line, without Markdown heading marks; '' when there is none."""
    readme = tool_dir / "README.md"
    if not readme.is_file():
        return ""
    for line in readme.read_text().splitlines():
        if line.strip():
            return line.strip().lstrip("#").strip()
    return ""


def requirement_lines(tool_dir: Path) -> list[str]:
    f = tool_dir / "requirements.txt"
    if not f.is_file():
        return []
    out = []
    for line in f.read_text().splitlines():
        line = line.split(" #")[0].strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def load(tools_dir: Path, name: str):
    """The tool's click command. The folder is loaded as a package, so its modules
    import each other relatively. Raises on any failure."""
    import click

    folder = tools_dir / name
    pkg = "fleet_tool_" + re.sub(r"\W", "_", name)
    if pkg in sys.modules:
        mod = sys.modules[pkg]
    else:
        init = folder / "__init__.py"
        if init.is_file():
            spec = importlib.util.spec_from_file_location(pkg, init, submodule_search_locations=[str(folder)])
            mod = importlib.util.module_from_spec(spec)
            sys.modules[pkg] = mod
            spec.loader.exec_module(mod)
        elif (folder / "main.py").is_file():
            mod = types.ModuleType(pkg)
            mod.__path__ = [str(folder)]
            sys.modules[pkg] = mod
        else:
            raise ValueError("no __init__.py or main.py")
    if (folder / "main.py").is_file():
        mod = importlib.import_module(pkg + ".main")
    cmd = getattr(mod, "cli", None)
    if not isinstance(cmd, click.Command):
        raise ValueError("no click command `cli` in %s" % ("main.py" if (folder / "main.py").is_file() else "__init__.py"))
    return cmd


def problems(tools_dir: Path, name: str, importable: bool = True) -> list[str]:
    folder = tools_dir / name
    found = []
    if name in RESERVED:
        found.append("the name is reserved")
    if not summary(folder):
        found.append("README.md is missing or empty")
    if not (folder / "requirements.txt").is_file():
        found.append("requirements.txt is missing")
    if importable:
        try:
            load(tools_dir, name)
        except Exception as e:  # noqa: BLE001 - any failure of a tool is a finding
            found.append("%s: %s" % (type(e).__name__, e))
    return found


def make_group(tools_dir: Path):
    import click

    class ToolsGroup(click.Group):
        """The tools of the directory, each loaded only when asked for, so one
        broken tool does not stop the others."""

        def list_commands(self, ctx):
            return sorted(set(super().list_commands(ctx)) | (set(tool_names(tools_dir)) - set(RESERVED)))

        def get_command(self, ctx, name):
            cmd = super().get_command(ctx, name)
            if cmd or name not in tool_names(tools_dir):
                return cmd
            try:
                return load(tools_dir, name)
            except Exception as e:  # noqa: BLE001
                raise click.ClickException("tool %s does not load: %s: %s" % (name, type(e).__name__, e))

    @click.group("tools", cls=ToolsGroup, help="The custom tools of this repository (%s)." % tools_dir)
    def tools():
        pass

    @tools.command("list")
    def list_cmd():
        """List the tools with the first line of their README."""
        for line in listing(tools_dir):
            click.echo(line)

    @tools.command("check")
    def check_cmd():
        """Validate every tool folder: README, requirements.txt, an importable click command."""
        bad = check(tools_dir)
        for line in bad:
            click.echo(line, err=True)
        if bad:
            raise click.ClickException("%d problem(s) in %s" % (len(bad), tools_dir))
        click.echo("%d tool(s) in %s: ok" % (len(tool_names(tools_dir)), tools_dir))

    return tools


def listing(tools_dir: Path) -> list[str]:
    names = tool_names(tools_dir)
    width = max((len(n) for n in names), default=0)
    return ["%-*s  %s" % (width, n, summary(tools_dir / n)) for n in names]


def check(tools_dir: Path, importable: bool = True) -> list[str]:
    return ["%s: %s" % (n, p) for n in tool_names(tools_dir) for p in problems(tools_dir, n, importable)]


def build(tools_dir: Path):
    """A directory's own main.py (a click group `tools`) wins over the built-in one."""
    own = tools_dir / "main.py"
    if own.is_file():
        spec = importlib.util.spec_from_file_location("fleet_tools_main", own)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.tools
    return make_group(tools_dir)


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2 or argv[0] != "--tools-dir":
        sys.exit("usage: tools_main.py --tools-dir DIR [arguments]")
    build(Path(argv[1]).resolve())(argv[2:], prog_name="fleetkit tools")


if __name__ == "__main__":
    main()
