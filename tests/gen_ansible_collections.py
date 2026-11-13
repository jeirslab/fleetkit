#!/usr/bin/env python3
"""Regenerate providers/ansible/collections.json: the Ansible collections the
runner's playbook runs have (packages.<system>.ansible: ansible-core and the
community package, from the flake's nixpkgs), with the version of each.

  gen_ansible_collections.py

Builds packages.<system>.ansible (a nix build: the first run downloads it)
and asks it, with nothing else on the collections path, what it has. The
model reads only the generated file (modules/addons.nix: a playbook's
`collections` must be in it); tests/addons.sh checks, by evaluation alone,
that its versions are the ones the flake's nixpkgs pins.
"""
import json
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "providers/ansible/collections.json"


def run(*cmd, env=None):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, env=env).stdout


def main():
    env_path = run("nix", "build", "--no-link", "--print-out-paths", f"{ROOT}#ansible").strip()
    site = next(pathlib.Path(env_path, "lib").glob("python3*/site-packages"))
    with tempfile.TemporaryDirectory() as home:
        env = {"HOME": home, "PATH": f"{env_path}/bin", "ANSIBLE_COLLECTIONS_PATH": str(site)}
        listed = json.loads(run(f"{env_path}/bin/ansible-galaxy", "collection", "list", "--format", "json", env=env))
        community = re.search(r"([0-9][0-9.]*)", run(f"{env_path}/bin/ansible-community", "--version", env=env))
        core = re.search(r"core ([0-9][0-9.]*)", run(f"{env_path}/bin/ansible", "--version", env=env))
    collections = {}
    for _, found in listed.items():
        for name, meta in found.items():
            collections.setdefault(name, meta["version"])
    out = {
        "ansible": community.group(1),
        "ansible-core": core.group(1),
        # ansible-core's own: always there, never listed by ansible-galaxy.
        "builtin": ["ansible.builtin"],
        "collections": dict(sorted(collections.items())),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1) + "\n")
    print(f"ansible {out['ansible']} (core {out['ansible-core']}): {len(collections)} collections", file=sys.stderr)


if __name__ == "__main__":
    main()
