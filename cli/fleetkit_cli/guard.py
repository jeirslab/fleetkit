"""The plan of a stack, per resource, and the guard every `up` passes.

A count ("replace: 1") does not say what is replaced. The plan is built from
the engine's step events of a preview: one entry per resource that is not
`same`, with the program's key, the op, the changed property paths and the
paths that force a replacement. The guard then refuses an `up` whose plan
would replace, delete or update a guest (a Proxmox container or VM) that the
request did not name for that op, and any `up` of a program that carries an
`import` option (issue #61: a leftover `import` on an adopted container made
the next `up` destroy it, although it was protected).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Optional

from pydantic import BaseModel, Field

# The guests: what a replace or a delete destroys, and an in-place update
# reboots. Tokens of the bridged bpg/proxmox provider at the pinned version
# (providers/pulumi/names/bpg-proxmox-*.json).
GUEST_TYPES = frozenset({
    "proxmox:index/virtualEnvironmentContainer:VirtualEnvironmentContainer",
    "proxmox:index/virtualEnvironmentVm:VirtualEnvironmentVm",
})

# Gated op family -> the request field (and CLI flag) that names a resource for it.
FLAGS = {"replace": "allow_replace", "delete": "allow_delete", "update": "allow_update"}

# Resources that exist only in Pulumi's state: creating one touches nothing real.
STATE_ONLY = ("pulumi:providers:", "pulumi:pulumi:Stack")

IMPORT_WHY = ("the program sets options.import; with it still present after an adoption the next up "
              "replaces (destroys) the resource (issue #61). Adopt with `fleetkit adopt`, which never "
              "leaves import in a program")

# Pulumi's marker of a secret value in an event or a state file.
SECRET_SIG = "4dabf18193072939515e22adb298388d"

_RANK = ("replace", "delete", "import", "update", "create")


class Allow(BaseModel):
    """Resources (program keys, or URNs) a deploy may replace, delete or update."""
    replace: list[str] = Field(default_factory=list)
    delete: list[str] = Field(default_factory=list)
    update: list[str] = Field(default_factory=list)


class GuardError(Exception):
    """An `up` that was refused. `result` is the job's result so far (the plan
    and the refused entries), so a failed job still shows what it would do."""

    def __init__(self, message: str, result: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.result = result


def family(op: str) -> Optional[str]:
    """The gated family of an engine op: replace (replace, create-replacement,
    delete-replaced, and every other step of a replacement), delete, update."""
    if "replace" in op:
        return "replace"
    return op if op in ("delete", "update") else None


def flag(fam: str) -> str:
    return "--" + FLAGS[fam].replace("_", "-")


def load_program(wd: Path) -> dict[str, Any]:
    """The program of a project dir. The runner writes it as JSON (a YAML subset)."""
    return json.loads((wd / "Pulumi.yaml").read_text())


def imports(program: dict[str, Any]) -> list[str]:
    """Keys of the resources that carry an `import` option."""
    return sorted(k for k, r in (program.get("resources") or {}).items()
                  if ((r or {}).get("options") or {}).get("import") is not None)


def _ref(v: Any) -> Optional[str]:
    """`${key}` or `${key.prop}` -> key."""
    if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
        return v[2:-1].split(".")[0].split("[")[0]
    return None


def _qualified_type(program: dict[str, Any], key: str, seen: tuple[str, ...] = ()) -> str:
    r = program["resources"][key]
    parent = _ref((r.get("options") or {}).get("parent"))
    if parent and parent in program["resources"] and parent not in seen:
        return _qualified_type(program, parent, (*seen, key)) + "$" + r["type"]
    return r["type"]


def urn_of(stack: str, program: dict[str, Any], key: str) -> str:
    r = program["resources"][key]
    return f"urn:pulumi:{stack}::{program['name']}::{_qualified_type(program, key)}::{r.get('name') or key}"


def urns(stack: str, program: dict[str, Any]) -> dict[str, str]:
    """key -> URN of every resource of the program."""
    return {k: urn_of(stack, program, k) for k in (program.get("resources") or {})}


def depends_on(program: dict[str, Any], key: str) -> set[str]:
    """Keys of the resources `key` needs, transitively: its provider, parent,
    dependsOn, and every resource a property refers to."""
    res = program.get("resources") or {}
    out: set[str] = set()

    def refs(v: Any) -> Iterable[str]:
        if isinstance(v, str):
            i = 0
            while (a := v.find("${", i)) >= 0 and (b := v.find("}", a)) >= 0:
                yield v[a + 2:b].split(".")[0].split("[")[0]
                i = b + 1
        elif isinstance(v, dict):
            for x in v.values():
                yield from refs(x)
        elif isinstance(v, list):
            for x in v:
                yield from refs(x)

    def walk(k: str) -> None:
        r = res[k]
        opts = {o: v for o, v in (r.get("options") or {}).items() if o in ("provider", "parent", "dependsOn", "providers")}
        for d in {*refs(opts), *refs(r.get("properties") or {})}:
            if d in res and d not in out and d != key:
                out.add(d)
                walk(d)

    walk(key)
    return out


def lookup(v: Any, path: str) -> tuple[bool, Any]:
    """The value at a Pulumi property path (`a.b[0].c`, `a["k"]`) -> (found, value)."""
    parts: list[Any] = []
    buf = ""
    i = 0
    while i < len(path):
        c = path[i]
        if c == ".":
            if buf:
                parts.append(buf)
            buf = ""
        elif c == "[":
            if buf:
                parts.append(buf)
            buf = ""
            j = path.find("]", i)
            if j < 0:
                return False, None
            tok = path[i + 1:j]
            parts.append(tok[1:-1] if tok[:1] in ("'", '"') else int(tok) if tok.isdigit() else tok)
            i = j
        else:
            buf += c
        i += 1
    if buf:
        parts.append(buf)
    for p in parts:
        if isinstance(v, dict) and p in v:
            v = v[p]
        elif isinstance(v, list) and isinstance(p, int) and p < len(v):
            v = v[p]
        else:
            return False, None
    return True, v


def scrub(v: Any) -> Any:
    """A value with what Pulumi marks secret replaced by [secret]."""
    if isinstance(v, dict):
        return "[secret]" if SECRET_SIG in v else {k: scrub(x) for k, x in v.items()}
    if isinstance(v, list):
        return [scrub(x) for x in v]
    return v


class Plan:
    """Collects a run's engine events into one entry per resource."""

    def __init__(self, stack: str, program: dict[str, Any]):
        self.program = program
        self.by_urn = {u: k for k, u in urns(stack, program).items()}
        self._r: dict[str, dict[str, Any]] = {}
        self.errors: dict[str, list[str]] = {}

    def key(self, urn: str) -> str:
        """The program's key; for a resource the program no longer has, its name in state."""
        return self.by_urn.get(urn) or urn.rsplit("::", 1)[-1]

    def step(self, m: Any) -> dict[str, Any]:
        """Record one step (the metadata of a pre, outputs or failed event)."""
        r = self._r.setdefault(m.urn, {"key": self.key(m.urn), "urn": m.urn, "type": m.type, "steps": [],
                                       "diff": [], "replaceReasons": [], "live": None, "declared": None})
        op = str(getattr(m.op, "value", m.op))
        if op not in r["steps"]:
            r["steps"].append(op)
        detailed = m.detailed_diff or {}
        for p in [*detailed, *([] if detailed else (m.diffs or []))]:
            if p not in r["diff"]:
                r["diff"].append(p)
        for p, d in detailed.items():
            kind = d.get("diffKind", "") if isinstance(d, dict) else str(getattr(d.diff_kind, "value", d.diff_kind))
            if kind.endswith("-replace") and p not in r["replaceReasons"]:
                r["replaceReasons"].append(p)
        for p in m.keys or []:
            if p not in r["replaceReasons"]:
                r["replaceReasons"].append(p)
        # For an import the engine reports the live resource as `old` and the
        # declaration as `new`; the outputs event (after the read) is the full one.
        if m.old is not None and m.old.inputs is not None:
            r["live"] = m.old.inputs
        if m.new is not None and m.new.inputs is not None:
            r["declared"] = m.new.inputs
        return r

    def error(self, urn: str, message: str) -> None:
        self.errors.setdefault(urn or "", []).append(message)

    def entry(self, urn: str) -> Optional[dict[str, Any]]:
        return self._r.get(urn)

    def changes(self) -> list[dict[str, Any]]:
        """[{key, urn, type, op, steps, diff, replaceReasons}] of every resource
        that is not `same`. `op` is what happens to the resource (a replacement
        is three engine steps); `steps` are the engine's own ops."""
        out = []
        for r in self._r.values():
            steps = [s for s in r["steps"] if s != "same"]
            if not steps:
                continue
            fams = [family(s) or s for s in steps]
            op = next((o for o in _RANK if o in fams), fams[0])
            out.append({"key": r["key"], "urn": r["urn"], "type": r["type"], "op": op, "steps": steps,
                        "diff": list(r["diff"]), "replaceReasons": list(r["replaceReasons"])})
        return out


def refusals(changes: list[dict[str, Any]], allow: Allow) -> list[dict[str, Any]]:
    """What a deploy refuses of a plan: [{key, urn, type, op, flag}], one per
    guest and gated op that the request did not name."""
    out = []
    for c in changes:
        if c["type"] not in GUEST_TYPES:
            continue
        for fam in dict.fromkeys(f for f in map(family, c["steps"]) if f):
            named = getattr(allow, fam)
            if c["key"] not in named and c["urn"] not in named:
                out.append({"key": c["key"], "urn": c["urn"], "type": c["type"], "op": fam,
                            "steps": [s for s in c["steps"] if family(s) == fam], "flag": f"{flag(fam)} {c['key']}"})
    return out


def import_refusals(program: dict[str, Any]) -> list[dict[str, Any]]:
    res = program.get("resources") or {}
    return [{"key": k, "type": res[k].get("type", ""), "op": "import", "flag": None, "why": IMPORT_WHY}
            for k in imports(program)]


def by_op(changes: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    """op -> [{key, type}], for the summary."""
    out: dict[str, list[dict[str, str]]] = {}
    for c in changes:
        out.setdefault(c["op"], []).append({"key": c["key"], "type": c["type"]})
    return out


def _why(c: dict[str, Any]) -> str:
    bits = []
    if c.get("diff"):
        bits.append("changes " + ", ".join(c["diff"]))
    if c.get("replaceReasons"):
        bits.append("replaced because of " + ", ".join(c["replaceReasons"]))
    return f" [{'; '.join(bits)}]" if bits else ""


def _refusal(r: dict[str, Any]) -> str:
    if r["op"] == "import":
        return f"import on {r['key']} ({r['type']}): {r['why']}"
    note = " (an in-place update reboots a guest)" if r["op"] == "update" else ""
    return f"{r['op']} of guest {r['key']} ({r['type']}){note}: needs {r['flag']}"


def lines(stack: str, changes: list[dict[str, Any]], refused: list[dict[str, Any]], preview: bool) -> list[str]:
    """The plan of a stack as text: every resource by op, and what is refused."""
    out = [f"{stack}: " + (f"{len(changes)} resource(s) change" if changes else "no changes")]
    for c in changes:
        out.append(f"  {c['op']:<8} {c['key']}  ({c['type']}){_why(c)}")
    for r in refused:
        out.append(f"  {'a deploy would refuse' if preview else 'REFUSED'}: {_refusal(r)}")
    return out


def message(refused: dict[str, list[dict[str, Any]]]) -> str:
    out = ["refused, nothing was applied:"]
    for stack, rs in refused.items():
        out += [f"  {stack}: {_refusal(r)}" for r in rs]
    return "\n".join(out)


def markdown(plan: dict[str, list[dict[str, Any]]], refused: dict[str, list[dict[str, Any]]],
             preview: bool) -> list[str]:
    """The same list for a pull request comment."""
    out: list[str] = []
    for stack in sorted({*plan, *refused}):
        changes, rs = plan.get(stack) or [], refused.get(stack) or []
        if not changes and not rs:
            continue
        out.append(f"**`{stack}`**")
        for c in changes:
            out.append(f"- {c['op']}: `{c['key']}` (`{c['type']}`){_why(c)}")
        for r in rs:
            out.append(f"- ⛔ {'a deploy would refuse' if preview else 'refused'}: {_refusal(r)}")
        out.append("")
    return out
