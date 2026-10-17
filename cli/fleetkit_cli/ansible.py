"""What the playbook runs of an `up` did (the Ansible add-on, docs/addons.md).

A playbook run is an `ansible:index/playbook:Playbook` resource of the
ansible/ansible provider. Its provider runs ansible-playbook when it creates
the resource (and a change to it is a replacement: lib/pulumi.nix), and keeps
the output as `ansiblePlaybookStdout`. The engine's op says whether the
playbook ran; whether it changed anything is in the output's PLAY RECAP, and
only the recap's counts are read from it: the output itself (what a task
printed) is never passed on.
"""
from __future__ import annotations

import re
from typing import Any

TYPE = "ansible:index/playbook:Playbook"

# Ops whose step creates the resource, and so runs the playbook.
RUN_OPS = ("create", "create-replacement", "replace")

# "<host> : ok=1 changed=0 unreachable=0 failed=0 skipped=0 rescued=0 ignored=0"
_LINE = re.compile(r"^(\S+)\s*:\s*((?:[a-z]+=\d+\s*)+)$")
_COUNT = re.compile(r"([a-z]+)=(\d+)")


def recap(stdout: str) -> dict[str, dict[str, int]]:
    """{host: {ok, changed, unreachable, failed, ...}} from the PLAY RECAP of
    an ansible-playbook output; {} if it has none (the run did not get that far)."""
    out: dict[str, dict[str, int]] = {}
    after = False
    for line in (stdout or "").splitlines():
        if line.startswith("PLAY RECAP"):
            after, out = True, {}  # the last recap is the run's
            continue
        m = _LINE.match(line.strip()) if after else None
        if m:
            out[m.group(1)] = {k: int(v) for k, v in _COUNT.findall(m.group(2))}
    return out


def runs(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One record per playbook resource of a run (guard.Plan.entries()):
    {key, ran, changed, recap}. `ran`: its step created it, so the playbook
    ran; `changed`: a host of its recap changed something (False when it did
    not run). A failed run is an engine error, reported as such."""
    out = []
    for r in entries:
        if r["type"] != TYPE:
            continue
        ran = any(op in r["done"] for op in RUN_OPS)
        counts = recap(str((r.get("declaredOutputs") or {}).get("ansiblePlaybookStdout") or "")) if ran else {}
        out.append({"key": r["key"], "ran": ran,
                    "changed": any(c.get("changed", 0) > 0 for c in counts.values()), "recap": counts})
    return out
