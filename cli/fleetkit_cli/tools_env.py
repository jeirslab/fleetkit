"""Where a repository's custom tools are and what runs them (`fleetkit tools`).

The tools directory is, in order: --tools-dir / FLEETKIT_TOOLS_DIR, `tools_dir`
in the repository's `fleetkit.toml`, then `tools`. Tools run in one venv built
with uv from the union of their requirements.txt files plus the click fleetkit
itself runs, kept in <repo>/.fleet/tools-venv/<hash of those>/ and rebuilt only
when that union changes (or the Python does).
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tomllib
from importlib.metadata import version
from pathlib import Path

from . import tools_main


def tools_dir(repo: Path, option: str | None = None) -> Path:
    name = option or os.environ.get("FLEETKIT_TOOLS_DIR")
    conf = repo / "fleetkit.toml"
    if not name and conf.is_file():
        name = tomllib.loads(conf.read_text()).get("tools_dir")
    return repo / (name or "tools")


def requirements(tdir: Path) -> str:
    """click pinned to fleetkit's own, then each tool's lines, once each."""
    lines = ["click==" + version("click")]
    for n in tools_main.tool_names(tdir):
        lines += [r for r in tools_main.requirement_lines(tdir / n) if r not in lines]
    return "\n".join(lines) + "\n"


def venv_dir(repo: Path, reqs: str) -> Path:
    key = hashlib.sha256((reqs + "\0" + sys.version).encode()).hexdigest()[:16]
    return repo / ".fleet" / "tools-venv" / key


def ensure_venv(repo: Path, tdir: Path) -> Path:
    """The venv's python, built if this requirement set has none yet."""
    reqs = requirements(tdir)
    venv = venv_dir(repo, reqs)
    python = venv / "bin" / "python"
    if (venv / ".ok").is_file():
        return python
    venv.parent.mkdir(parents=True, exist_ok=True)
    req_file = venv.with_suffix(".txt")
    req_file.write_text(reqs)
    try:
        subprocess.run(["uv", "venv", "--quiet", "--clear", "--python", sys.executable, str(venv)], check=True)
        subprocess.run(["uv", "pip", "install", "--quiet", "--python", str(python), "-r", str(req_file)], check=True)
    except FileNotFoundError:
        raise RuntimeError("uv is not on PATH; it builds the tools' environment")
    except subprocess.CalledProcessError as e:
        raise RuntimeError("building the tools' environment failed: %s" % " ".join(e.cmd))
    (venv / ".ok").write_text("")
    return python


def run(repo: Path, tdir: Path, args: list[str]) -> None:
    """Replace this process by the tools command in the tools' environment
    (FLEETKIT_TOOLS_PYTHON names an interpreter to use instead of building one)."""
    python = os.environ.get("FLEETKIT_TOOLS_PYTHON") or str(ensure_venv(repo, tdir))
    script = tools_main.__file__
    os.chdir(repo)
    os.execv(python, [python, "-I", script, "--tools-dir", str(tdir), *args])
