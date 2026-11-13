"""Custom tools (`fleetkit tools`): discovery, list, check, running a tool, the
tools directory setting, and the environment's cache key. Tools run here in the
test's own interpreter (FLEETKIT_TOOLS_PYTHON); building the venv with uv is
checked with uv replaced by a recorder."""
import subprocess
import sys

import pytest
from click.testing import CliRunner

from fleetkit_cli import tools_env, tools_main
from fleetkit_cli.main import cli


def make_tool(tdir, name, code=None, readme="# %s\n\nDoes a thing.\n", reqs="", entry="__init__.py"):
    d = tdir / name
    d.mkdir(parents=True)
    if readme is not None:
        (d / "README.md").write_text(readme % name if "%s" in readme else readme)
    if reqs is not None:
        (d / "requirements.txt").write_text(reqs)
    if code is None:
        code = ("import click\n\n@click.command()\n@click.argument('who')\n"
                "def cli(who):\n    click.echo('hello ' + who)\n")
    (d / entry).write_text(code)
    return d


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "flake.nix").write_text("{ }\n")
    monkeypatch.setenv("FLEETKIT_TOOLS_PYTHON", sys.executable)
    monkeypatch.delenv("FLEETKIT_TOOLS_DIR", raising=False)
    return tmp_path


def fleetkit(repo, *args):
    """Runs the command line in a subprocess: `tools` replaces the process."""
    return subprocess.run([sys.executable, "-m", "fleetkit_cli.main", "--flake", str(repo), *args],
                          capture_output=True, text=True)


def test_discovery_skips_underscore_dot_and_files(tmp_path):
    for n in ("b", "a", "_shared", ".hidden"):
        (tmp_path / n).mkdir()
    (tmp_path / "file.txt").write_text("x")
    assert tools_main.tool_names(tmp_path) == ["a", "b"]
    assert tools_main.tool_names(tmp_path / "missing") == []


def test_list_shows_readme_first_line(repo):
    make_tool(repo / "tools", "hello")
    make_tool(repo / "tools", "plain", readme="\n\nJust text\n")
    r = CliRunner().invoke(cli, ["--flake", str(repo), "tools", "list"])
    assert r.exit_code == 0, r.output
    assert r.output.splitlines() == ["hello  hello", "plain  Just text"]


def test_check_passes_for_good_tools(repo):
    make_tool(repo / "tools", "hello")
    make_tool(repo / "tools", "pkg", code="", entry="__init__.py")  # replaced below
    (repo / "tools" / "pkg" / "__init__.py").unlink()
    (repo / "tools" / "pkg" / "main.py").write_text("import click\n\n@click.group()\ndef cli():\n    pass\n")
    assert tools_main.check(repo / "tools") == []
    r = fleetkit(repo, "tools", "check")
    assert r.returncode == 0, r.stderr
    assert "2 tool(s)" in r.stdout


def test_check_reports_each_problem(repo):
    t = repo / "tools"
    make_tool(t, "norm", readme=None)
    make_tool(t, "noreq", reqs=None)
    make_tool(t, "empty", readme="\n")
    make_tool(t, "nocli", code="x = 1\n")
    make_tool(t, "broken", code="raise RuntimeError('boom')\n")
    make_tool(t, "check")
    (t / "bare").mkdir()
    (t / "bare" / "README.md").write_text("bare\n")
    (t / "bare" / "requirements.txt").write_text("")
    found = tools_main.check(t)
    text = "\n".join(found)
    assert "norm: README.md is missing or empty" in text
    assert "noreq: requirements.txt is missing" in text
    assert "empty: README.md is missing or empty" in text
    assert "nocli: ValueError: no click command `cli`" in text
    assert "broken: RuntimeError: boom" in text
    assert "check: the name is reserved" in text
    assert "bare: ValueError: no __init__.py or main.py" in text
    r = fleetkit(repo, "tools", "check")
    assert r.returncode != 0
    assert "problem(s)" in r.stderr


def test_run_python_tool_with_relative_import(repo):
    d = make_tool(repo / "tools", "hello", code=(
        "import click\nfrom .helper import greet\n\n@click.command()\n@click.argument('who')\n"
        "def cli(who):\n    click.echo(greet(who))\n"))
    (d / "helper.py").write_text("def greet(w):\n    return 'hi ' + w\n")
    r = fleetkit(repo, "tools", "hello", "bob")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "hi bob\n"


def test_run_shell_tool(repo):
    d = make_tool(repo / "tools", "say", code=(
        "import subprocess, sys\nfrom pathlib import Path\nimport click\n\n"
        "@click.command(context_settings={'ignore_unknown_options': True})\n"
        "@click.argument('args', nargs=-1)\n"
        "def cli(args):\n"
        "    sys.exit(subprocess.run([str(Path(__file__).with_name('say.sh')), *args]).returncode)\n"))
    (d / "say.sh").write_text("#!/bin/sh\necho \"shell: $*\"\nexit 3\n")
    (d / "say.sh").chmod(0o755)
    r = fleetkit(repo, "tools", "say", "a", "b")
    assert r.stdout == "shell: a b\n"
    assert r.returncode == 3


def test_a_broken_tool_does_not_stop_the_others(repo):
    make_tool(repo / "tools", "good")
    make_tool(repo / "tools", "bad", code="raise RuntimeError('boom')\n")
    assert fleetkit(repo, "tools", "good", "x").stdout == "hello x\n"
    r = fleetkit(repo, "tools", "bad")
    assert r.returncode != 0 and "does not load" in r.stderr


def test_own_main_py_replaces_the_group(repo):
    (repo / "tools").mkdir()
    (repo / "tools" / "main.py").write_text(
        "import click\n\n@click.group()\ndef tools():\n    pass\n\n"
        "@tools.command()\ndef mine():\n    click.echo('custom main')\n")
    assert fleetkit(repo, "tools", "mine").stdout == "custom main\n"


def test_tools_dir_setting(repo, monkeypatch):
    assert tools_env.tools_dir(repo) == repo / "tools"
    (repo / "fleetkit.toml").write_text('tools_dir = "dev/tools"\n')
    assert tools_env.tools_dir(repo) == repo / "dev" / "tools"
    monkeypatch.setenv("FLEETKIT_TOOLS_DIR", "env")
    assert tools_env.tools_dir(repo) == repo / "env"
    assert tools_env.tools_dir(repo, "flag") == repo / "flag"
    make_tool(repo / "dev" / "tools", "hello")
    monkeypatch.delenv("FLEETKIT_TOOLS_DIR")
    assert fleetkit(repo, "tools", "hello", "z").stdout == "hello z\n"


def test_venv_is_keyed_by_requirements_and_built_by_uv(repo, monkeypatch):
    make_tool(repo / "tools", "a", reqs="requests==2.0\n# note\n")
    make_tool(repo / "tools", "b", reqs="requests==2.0\nrich\n")
    reqs = tools_env.requirements(repo / "tools")
    assert reqs.splitlines()[1:] == ["requests==2.0", "rich"]
    assert reqs.startswith("click==")
    key = tools_env.venv_dir(repo, reqs)
    assert tools_env.venv_dir(repo, reqs + "x") != key
    assert key.parent == repo / ".fleet" / "tools-venv"

    calls = []

    def fake_run(cmd, check):
        calls.append(cmd)
        if cmd[1] == "venv":
            (key / "bin").mkdir(parents=True)

    monkeypatch.setattr(subprocess, "run", fake_run)
    python = tools_env.ensure_venv(repo, repo / "tools")
    assert python == key / "bin" / "python"
    assert [c[:2] for c in calls] == [["uv", "venv"], ["uv", "pip"]]
    assert "requests==2.0" in key.with_suffix(".txt").read_text()
    tools_env.ensure_venv(repo, repo / "tools")  # cached: uv is not run again
    assert len(calls) == 2
