"""Render stage: evaluate the estate repo's Pulumi programs with Nix and lay
each out as a Pulumi project directory. This is the only place the model is
read, and it is read as the evaluated output, never interpreted.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .events import Emitter
from .settings import Settings


class RenderError(Exception):
    pass


def nix_eval_json(s: Settings, attr: str, apply: str | None = None) -> object:
    cmd = [s.nix, "eval", "--json", f"{s.flake}#{attr}"]
    if apply:
        cmd += ["--apply", apply]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        err = "\n".join(line for line in p.stderr.splitlines() if "Git tree" not in line)
        raise RenderError(f"nix eval {attr} failed:\n{err.strip()[-4000:]}")
    return json.loads(p.stdout)


def stacks_of(s: Settings, estate: str) -> list[str]:
    names = nix_eval_json(s, "pulumi", "builtins.mapAttrs (_: builtins.attrNames)")
    if estate not in names:
        raise RenderError(f"no estate {estate!r} under pulumi (have: {', '.join(sorted(names))})")
    return sorted(names[estate])


def estates(s: Settings) -> dict[str, list[str]]:
    return nix_eval_json(s, "pulumi", "builtins.mapAttrs (_: builtins.attrNames)")


# Splits a program in Nix: the project part (what `pulumi install` and the CLI
# read) and the rest written to the store as Main.json by builtins.toFile, so
# what runs is an immutable, content-addressed file whose path a deploy can
# record. No build and no pkgs: toFile writes during the eval.
SPLIT = """p:
let
  projectKeys = [ "name" "runtime" "packages" "description" ];
  invokes = builtins.filter (v: v ? "fn::invoke") (builtins.attrValues (p.variables or { }));
in
{
  project = builtins.intersectAttrs (builtins.listToAttrs (map (n: { name = n; value = null; }) projectKeys)) p;
  main = builtins.toFile "Main.json" (builtins.toJSON (removeAttrs p projectKeys));
  secrets = builtins.filter (x: x != null) (map (v: v."fn::invoke".arguments.sourceFile or null) invokes);
}"""


def _link_secrets(wd: Path, sources: list[str], roots: list[Path]) -> None:
    """The sops provider resolves a relative source_file against its working
    directory (the project dir); link each file there from the first secret
    root that has it. Per file, so two roots may share a directory name."""
    for src in sources:
        if Path(src).is_absolute():
            continue
        hit = next((r / src for r in roots if (r / src).is_file()), None)
        if hit is None:
            raise RenderError(
                f"secrets file {src} is under none of {', '.join(map(str, roots))} "
                f"(FLEETKIT_SECRET_ROOTS adds more)")
        link = wd / src
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(hit)


def render(s: Settings, estate: str, stack: str, ev: Emitter) -> tuple[Path, str]:
    """-> (project dir, the program's store path)."""
    ev.emit("render", "start", estate=estate, stack=stack)
    out = nix_eval_json(s, f"pulumi.{estate}.{stack}", SPLIT)
    project, main = out["project"], out["main"]
    if project.get("runtime") != "yaml":
        raise RenderError(f"pulumi.{estate}.{stack} is not a Pulumi YAML program")
    wd = s.state_dir / "work" / estate / stack
    wd.mkdir(parents=True, exist_ok=True)
    # The project file is named .json; the YAML runtime reads the program from
    # Main.json beside it. The packages must be in the project file only: the
    # bridge's parameters are not seen when they are in the program.
    for stale in ("Pulumi.yaml", "Pulumi.yml", "Main.yaml"):
        (wd / stale).unlink(missing_ok=True)
    (wd / "Pulumi.json").write_text(json.dumps(project, indent=1, sort_keys=True) + "\n")
    # Main.json is a GC root on the program, so a collection during a long
    # deploy cannot take it away (nix build of a store path only links it).
    m = wd / "Main.json"
    if m.is_symlink() or m.exists():
        m.unlink()
    p = subprocess.run([s.nix, "build", "--out-link", str(m), main], capture_output=True, text=True)
    if p.returncode != 0:
        raise RenderError(f"cannot root the program {main}: {p.stderr.strip()[-2000:]}")
    _link_secrets(wd, out["secrets"], s.secret_roots)
    ev.emit("render", "done", estate=estate, stack=stack, project=project["name"],
            program=main, workdir=str(wd))
    return wd, main
