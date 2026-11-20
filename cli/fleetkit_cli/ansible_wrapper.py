"""`ansible-playbook` as the ansible/ansible provider calls it (docs/addons.md).

The provider writes a playbook's variables into a temporary INI inventory,
`/tmp/.inventory-N.ini`, as `name='<raw value>'`. Ansible reads a quoted INI
value as a Python literal: an apostrophe in the value ends it, and a `\\n` in
it becomes a newline. This wrapper moves those lines into an extra-vars JSON
file (`-e @file`), where any string survives, and runs the real
ansible-playbook with the rest. Stdlib only: it runs under the ansible env's
python (flake.nix: ansibleBin).

    ansible_wrapper.py <real ansible-playbook> <its arguments>

shortcut: a value with a real newline spans lines of the INI and is not
recovered; the provider's own values do not have one. A provider that
writes JSON or YAML inventories makes this wrapper unnecessary.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_INVENTORY = re.compile(r"^\.inventory-.*\.ini$")
_VAR = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)='(.*)'$")


def split_inventory(text: str) -> tuple[str, dict[str, str]]:
    """(the inventory without its `name='value'` lines, {name: raw value})."""
    keep, values = [], {}
    for line in text.splitlines():
        m = _VAR.match(line.rstrip("\r"))
        if m:
            values[m.group(1)] = m.group(2)
        else:
            keep.append(line)
    return "\n".join(keep) + "\n", values


def rewrite(args: list[str], workdir: Path) -> list[str]:
    """args with the provider's inventory's variables moved to `-e @file`."""
    out, extra, i = [], {}, 0
    while i < len(args):
        a = args[i]
        if a in ("-i", "--inventory") and i + 1 < len(args) and _INVENTORY.match(os.path.basename(args[i + 1])):
            src = Path(args[i + 1])
            text, values = split_inventory(src.read_text())
            dst = workdir / src.name
            dst.write_text(text)
            extra.update(values)
            out += [a, str(dst)]
            i += 2
        else:
            out.append(a)
            i += 1
    if extra:
        vars_file = workdir / "vars.json"
        # __ansible_unsafe: a `{{` in a value is text, not a template
        vars_file.write_text(json.dumps({k: {"__ansible_unsafe": v} for k, v in extra.items()}))
        out += ["-e", f"@{vars_file}"]
    return out


def main(argv: list[str]) -> int:
    real, args = argv[0], argv[1:]
    with tempfile.TemporaryDirectory(prefix="fleetkit-ansible-") as d:
        return subprocess.call([real, *rewrite(args, Path(d))])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
