#!/usr/bin/env python3
"""Run a tool that moved from fleetkit's tools/ to secrets.nix (ADR-0009).

fleetkit's tools/sops-config.py, tools/deploy-key, tools/github-app-bootstrap
and tools/hooks/secrets-scan.sh are thin wrappers kept at their old paths:
each prints one notice line on stderr and execs the tool of the same name in
secrets.nix's tools/ directory. Arguments, stdin, stdout and the exit status
are the tool's own.

Where secrets.nix's tools/ is, in this order:
  1. $FLEETKIT_SECRETS_TOOLS, a directory laid out as secrets.nix's tools/;
  2. the secrets-nix input locked in fleetkit's flake.lock (beside this
     directory), fetched with `nix eval` and builtins.fetchTree.

    secrets_nix_tool.py <tool> [args...]   exec the tool (python3 or bash)
    secrets_nix_tool.py --dir              print the tools directory

Standard-library Python; needs `nix` on the PATH unless (1) is set.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
INPUT = "secrets-nix"


def die(msg):
    sys.stderr.write(f"fleetkit: {msg}\n")
    sys.exit(2)


def tools_dir():
    env = os.environ.get("FLEETKIT_SECRETS_TOOLS")
    if env:
        return env
    lock_path = os.path.join(ROOT, "flake.lock")
    try:
        with open(lock_path) as f:
            lock = json.load(f)
        nodes = lock["nodes"]
        name = nodes[lock["root"]]["inputs"][INPUT]
        locked = nodes[name]["locked"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        die(f"cannot read the {INPUT} input from {lock_path} ({e}); set FLEETKIT_SECRETS_TOOLS")
    # The locked attributes as a Nix string literal (JSON string escapes are
    # Nix's, except "${", which is escaped).
    literal = json.dumps(json.dumps(locked)).replace("${", "\\${")
    p = subprocess.run(
        [
            "nix", "--extra-experimental-features", "nix-command flakes",
            "eval", "--raw",
            "--expr", f"(builtins.fetchTree (builtins.fromJSON {literal})).outPath",
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if p.returncode != 0 or not p.stdout.strip():
        sys.stderr.write(p.stderr[-2000:])
        die(f"cannot fetch {INPUT} as locked in {lock_path}; set FLEETKIT_SECRETS_TOOLS")
    return os.path.join(p.stdout.strip(), "tools")


def run(tool, args):
    d = tools_dir()
    path = os.path.join(d, tool)
    if not os.path.isfile(path):
        die(f"{tool} not found in {d}")
    sys.stderr.write(f"fleetkit: tools/{tool} moved to secrets.nix (ADR-0009); running {path}\n")
    sys.stderr.flush()
    if tool.endswith(".sh"):
        os.execvp("bash", ["bash", path, *args])
    os.execv(sys.executable, [sys.executable, path, *args])


if __name__ == "__main__":
    if len(sys.argv) < 2:
        die("usage: secrets_nix_tool.py <tool> [args...] | --dir")
    if sys.argv[1] == "--dir":
        print(tools_dir())
    else:
        run(sys.argv[1], sys.argv[2:])
