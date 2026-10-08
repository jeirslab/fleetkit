"""The plan of a stack, per resource, and the guard every `up` passes.

A count ("replace: 1") does not say what is replaced. The plan is built from
the engine's step events of a preview: one entry per resource that is not
`same`, with the program's key, the op, the changed property paths and the
paths that force a replacement. The guard then refuses an `up` whose plan
would replace, delete or update a guest (a Proxmox container or VM) that the
request did not name for that op, and any `up` of a program that carries an
`import` option (issue #61: a leftover `import` on an adopted container made
the next `up` destroy it, although it was protected). HA resources are gated
the same way, their create too (guests.py says why). The create of a guest is
gated when the stack declares it as already existing, by an adoption id
(`--allow-create`): a guest that could not be adopted would otherwise be
created over the live one. Two more refusals take no name: a resource of a `proxmox*` package whose type the runner does not
know (unknown_refusals), and the delete of a guest whose id a second state
entry holds as well (Held).

Three layers keep an `up` to what the guard read (infra.py runs them):
  1. the refusals below, on the plan of the preview;
  2. the engine itself, bound to that preview by an update plan
     (`preview --save-plan`, `up --plan`): a program or a state that changed
     in between is refused by Pulumi, step by step;
  3. `protect`. The engine refuses to delete a resource the STATE protects,
     and to replace one that the state AND the program protect (seen with
     Pulumi 3.247: `protect` in the program alone does not stop a replace of
     a resource the state does not protect yet). So both are set: before the
     first plan of a deploy every guest in state that the request did not
     name for a replace or a delete is protected in state (to_protect, run by
     infra.plan), and in the program the runner runs, a copy, the same guests
     carry options.protect (protect, below). After the up, whatever became of
     it, every guest still in state is protected again (infra.apply).
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from pydantic import BaseModel, Field

# guests.py says how these lists are kept complete.
from .guests import CONTAINER_TYPES, GUEST_TYPES, HA_TYPES, KNOWN_TYPES, VM_TYPES

# Gated op family -> the request field (and CLI flag) that names a resource for it.
FLAGS = {"replace": "allow_replace", "delete": "allow_delete", "update": "allow_update", "create": "allow_create"}

# Resources that exist only in Pulumi's state: creating one touches nothing real.
STATE_ONLY = ("pulumi:providers:", "pulumi:pulumi:Stack")

UNKNOWN_WHY = ("its type is of a proxmox package and is in none of the runner's lists (guests.py): it may be "
               "a guest, or something that stops one, that nothing here gates. Nothing is applied while the "
               "program or the state has it: update fleetkit (the list is checked against the pinned provider), "
               "or use a type the runner knows")

IMPORT_WHY = ("the program sets options.import; with it still present after an adoption the next up "
              "replaces (destroys) the resource (issue #61). Adopt with `fleetkit adopt`, which never "
              "leaves import in a program")

# Pulumi's marker of a secret value in an event or a state file.
SECRET_SIG = "4dabf18193072939515e22adb298388d"

_RANK = ("replace", "delete", "import", "update", "create")


# What the engine says when `protect` stops it (a preview fails with these too).
PROTECT_RE = re.compile(r"cannot be deleted because it is protected|"
                        r"unable to replace resource .* as it is currently marked for protection")
# What it says when an `up` leaves its update plan.
VIOLATION_RE = re.compile(r"violates plan|not allowed by the plan|is constrained to")
# Un-attributed lines that only repeat that the run failed.
_GENERIC_RE = re.compile(r"^(preview|update) failed$")


class Allow(BaseModel):
    """Resources a deploy may replace, delete, update or create. A name is
    `<stack>/<key>`, a URN, or a bare program key; a bare key that names a
    resource in more than one stack of the request is refused (`ambiguous`)."""
    replace: list[str] = Field(default_factory=list)
    delete: list[str] = Field(default_factory=list)
    update: list[str] = Field(default_factory=list)
    create: list[str] = Field(default_factory=list)


def _split(name: str) -> tuple[Optional[str], str]:
    """An allow name -> (stack or None, key or URN)."""
    if name.startswith("urn:pulumi:") or "/" not in name:
        return None, name
    stack, _, key = name.partition("/")
    return stack, key


def scope(allow: Allow, stack: str) -> Allow:
    """The names of `allow` that can mean a resource of `stack`, without the
    stack prefix: `<stack>/<key>` for this stack, URNs, and bare keys."""
    def mine(names: list[str]) -> list[str]:
        return [key for st, key in map(_split, names) if st in (None, stack)]
    return Allow(replace=mine(allow.replace), delete=mine(allow.delete), update=mine(allow.update),
                 create=mine(allow.create))


def ambiguous(allow: Allow, names: dict[str, set[str]]) -> list[str]:
    """Why the names of a request cannot be used as they are: a bare key that
    is a resource (in the program or in state) of more than one stack, or a
    `<stack>/` that is not a stack of the request. `names`: stack -> keys."""
    out = []
    for fam in FLAGS:
        for name in getattr(allow, fam):
            st, key = _split(name)
            if st is not None and st not in names:
                out.append(f"{flag(fam)} {name}: no stack {st} in this request (stacks: {', '.join(sorted(names))})")
            elif st is None and not key.startswith("urn:pulumi:"):
                hits = sorted(n for n, keys in names.items() if key in keys)
                if len(hits) > 1:
                    out.append(f"{flag(fam)} {key} is ambiguous: {key} is a resource of {', '.join(hits)}; "
                               f"name it with its stack: " + " or ".join(f"{flag(fam)} {n}/{key}" for n in hits))
    return out


def gated(type_: str) -> bool:
    """Whether the guard gates this type: a guest, or an HA resource."""
    return type_ in GUEST_TYPES or type_ in HA_TYPES


def gate(type_: str, op: str, declared: bool = False) -> Optional[str]:
    """The gated family of an engine op on a resource of this type. A
    `create` is gated for an HA resource (it puts the guest it names under
    the HA manager, which starts, stops or moves it to match) and for a guest
    the stack declares as already existing (`declared`: it has an adoption
    id). A `create` step is of a resource that is not in state; a
    create-replacement is of one that is, and is a replace."""
    if op == "create":
        return "create" if type_ in HA_TYPES or (declared and type_ in GUEST_TYPES) else None
    return family(op)


def unknown(type_: str) -> bool:
    """A type of a `proxmox*` package that is in none of the runner's lists."""
    return type_.split(":", 1)[0].startswith("proxmox") and type_ not in KNOWN_TYPES and not gated(type_)


def ident(type_: str, rid: Any) -> tuple[str, str]:
    """What real resource an id of this type names: (family, id). A guest's
    adoption id is `<node>/<vmid>` while its state id is the vmid, and a vmid
    is one container or one VM whichever of the provider's tokens declares
    it: guests compare by family and vmid."""
    if type_ in GUEST_TYPES:
        fam = "proxmox container" if type_ in CONTAINER_TYPES else "proxmox vm" if type_ in VM_TYPES else type_
        return fam, str(rid).rsplit("/", 1)[-1]
    return type_, str(rid)


def state_ids(r: dict[str, Any]) -> set[str]:
    """The ids a state resource is known by: its id and what it was imported by."""
    return {str(r[k]) for k in ("id", "importID") if r.get(k) not in (None, "")}


def same_resource(type_: str, rid: Any, r: dict[str, Any]) -> bool:
    """Whether the state resource `r` is the real resource that the id `rid`
    of a resource of `type_` names."""
    want = ident(type_, rid)
    return any(ident(r.get("type") or "", i) == want for i in state_ids(r))


class Held:
    """The states of every stack of a run (stack -> state resources), asked
    whether a guest's id is in them more than once. Two state entries for one
    real guest (an adoption under a second key, a guest moved between stacks
    without `pulumi state move`) make the delete of either destroy the guest
    the other still manages."""

    def __init__(self, states: dict[str, list[dict[str, Any]]], stack: str):
        self.states, self.stack = states, stack

    def __call__(self, urn: str) -> Optional[str]:
        """Why the delete of `urn` (of this stack) must not run, or None."""
        me = next((r for r in self.states.get(self.stack) or [] if r["urn"] == urn and not r.get("delete")), None)
        if me is None or me.get("type") not in GUEST_TYPES:
            return None
        for n, resources in self.states.items():
            for r in resources:
                if (n, r["urn"]) == (self.stack, urn) or r.get("delete"):
                    continue
                if any(same_resource(me["type"], i, r) for i in state_ids(me)):
                    return (f"its id {me.get('id')} is also in the state of {n} as {r['urn']}: one real guest has two "
                            f"state entries, and deleting this one destroys the guest the other still manages. No "
                            f"flag allows it. Remove this state entry instead, which touches nothing real: "
                            f"`pulumi state unprotect '{urn}'`, then `pulumi state delete '{urn}'` (with the "
                            f"backend and passphrase of {self.stack}), and deploy again")
        return None


def to_protect(state: list[dict[str, Any]], allow: Allow, key: Callable[[str], str],
               protected: Iterable[str] = ()) -> list[str]:
    """URNs of the gated resources in `state` that it does not protect and
    that `allow` (scoped to the stack) does not name for a replace or a
    delete. `key`: URN -> the name a request would use. `protected`: URNs the
    program that runs protects; one of those is protected in state although
    it is named (the estate's own `protect` is never removed, and the engine
    only honours it once the state has it). An entry that waits to be deleted
    (the old half of an interrupted replacement) is left alone."""
    named, kept = {*allow.replace, *allow.delete}, set(protected)
    return [r["urn"] for r in state if gated(r.get("type") or "") and not r.get("protect") and not r.get("delete")
            and (r["urn"] in kept or (r["urn"] not in named and key(r["urn"]) not in named))]


def protected_urns(stack: str, program: dict[str, Any]) -> set[str]:
    """URNs of the resources the program protects."""
    return {urn_of(stack, program, k) for k, r in (program.get("resources") or {}).items()
            if ((r or {}).get("options") or {}).get("protect") is True}


def protect(stack: str, program: dict[str, Any], allow: Allow) -> tuple[dict[str, Any], list[str]]:
    """-> (a copy of the program in which every guest (and HA resource) that
    `allow` does not name for a replace or a delete has options.protect =
    true, the keys it was set on). With the state protecting them too
    (to_protect), the engine refuses to replace or delete those itself. Never
    written to the estate's files; a protect the estate set is never removed."""
    out = json.loads(json.dumps(program))
    named = {*allow.replace, *allow.delete}
    added = []
    for key, r in (out.get("resources") or {}).items():
        if not gated((r or {}).get("type") or "") or key in named or urn_of(stack, out, key) in named:
            continue
        opts = r.setdefault("options", {})
        if opts.get("protect") is not True:
            opts["protect"] = True
            added.append(key)
    return out, added


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


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

    def step(self, m: Any, phase: str = "pre") -> dict[str, Any]:
        """Record one step: the metadata of a pre, outputs (`done`) or `failed` event."""
        r = self._r.setdefault(m.urn, {"key": self.key(m.urn), "urn": m.urn, "type": m.type, "steps": [],
                                       "done": [], "failed": [], "diff": [], "kinds": {}, "replaceReasons": [],
                                       "live": None, "liveOutputs": None, "declared": None,
                                       "declaredOutputs": None})
        op = str(getattr(m.op, "value", m.op))
        if op not in r["steps"]:
            r["steps"].append(op)
        if phase in ("done", "failed") and op not in r[phase]:
            r[phase].append(op)
        detailed = m.detailed_diff or {}
        for p in [*detailed, *([] if detailed else (m.diffs or []))]:
            if p not in r["diff"]:
                r["diff"].append(p)
        for p, d in detailed.items():
            kind = d.get("diffKind", "") if isinstance(d, dict) else str(getattr(d.diff_kind, "value", d.diff_kind))
            r["kinds"][p] = kind
            if kind.endswith("-replace") and p not in r["replaceReasons"]:
                r["replaceReasons"].append(p)
        for p in m.keys or []:
            if p not in r["replaceReasons"]:
                r["replaceReasons"].append(p)
        # What Pulumi 3.247 sends for an adoption (seen with a real engine): an
        # `import` step whose outputs event has the live resource as `old` AND
        # as `new`, then, when the declaration differs, an `update` (or the
        # steps of a replacement) with the live resource as `old`, the
        # declaration as `new` and the differing keys in `diffs`. So the
        # declaration is only taken from a step that is not the import.
        # `inputs` of an imported resource leave out what equals the provider's
        # zero value (a false, a 0): `outputs` has those, so both are kept.
        if m.old is not None:
            if m.old.inputs is not None:
                r["live"] = m.old.inputs
            if getattr(m.old, "outputs", None):
                r["liveOutputs"] = m.old.outputs
        if m.new is not None and (op != "import" or m.diffs or detailed):
            if m.new.inputs is not None:
                r["declared"] = m.new.inputs
            if getattr(m.new, "outputs", None):
                r["declaredOutputs"] = m.new.outputs
        return r

    def error(self, urn: str, message: str) -> None:
        self.errors.setdefault(urn or "", []).append(" ".join(message.split()))

    def protected(self) -> set[str]:
        """URNs the engine refused to replace or delete because of `protect`."""
        return {u for u, msgs in self.errors.items() if u and any(PROTECT_RE.search(m) for m in msgs)}

    def only_protect_errors(self) -> bool:
        """Whether every error of the run is `protect` stopping the engine (the
        steps of such a run are still all there: seen with a real engine)."""
        real = [m for u, msgs in self.errors.items() for m in msgs
                if not PROTECT_RE.search(m) and not (not u and _GENERIC_RE.match(m))]
        return bool(self.protected()) and not real

    def violations(self) -> list[str]:
        """What the engine refused because it left the update plan."""
        return [m for msgs in self.errors.values() for m in msgs if VIOLATION_RE.search(m)]

    def counts(self) -> dict[str, int]:
        """Steps by op, `same` included: what the engine's summary would say
        (used when a preview failed and gave none)."""
        out: dict[str, int] = {}
        for r in self._r.values():
            for op in r["steps"]:
                out[op] = out.get(op, 0) + 1
        return out

    def progress(self) -> dict[str, list[dict[str, str]]]:
        """What is known of a run that stopped: the steps that finished, and
        those that had started and had not (they may or may not have happened)."""
        done, flying = [], []
        for r in self._r.values():
            for op in r["steps"]:
                if op in ("same", "refresh", "read"):
                    continue
                if op in r["done"]:
                    done.append({"op": op, "key": r["key"]})
                elif op not in r["failed"]:
                    flying.append({"op": op, "key": r["key"]})
        return {"completed": done, "in_flight": flying}

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
            c = {"key": r["key"], "urn": r["urn"], "type": r["type"], "op": op, "steps": steps,
                 "diff": list(r["diff"]), "replaceReasons": list(r["replaceReasons"])}
            if r["urn"] in self.protected():
                c["protected"] = True  # the engine refuses it: protect is set
            out.append(c)
        return out


def refusals(changes: list[dict[str, Any]], allow: Allow, label: Optional[Callable[[str], str]] = None,
             held: Optional[Callable[[str], Optional[str]]] = None,
             adopt_ids: Optional[dict[str, str]] = None) -> list[dict[str, Any]]:
    """What a deploy refuses of a plan: [{key, urn, type, op, flag}], one per
    guest (or HA resource) and gated op that the request did not name. `allow`
    is already scoped to the stack (`scope`); `label` writes the key in the
    flag (the pipeline adds the stack when the key exists in another stack
    too). `held` (Held): the delete of a guest whose id a second state entry
    holds is refused whatever the request names, with `why` and no flag.
    `adopt_ids`: the stack's adoption ids (program key -> the id of the
    existing resource the declaration describes); the create of a guest that
    has one is refused unless named for a create, with the id (`id`)."""
    label = label or (lambda key: key)
    ids = adopt_ids or {}
    out = []
    for c in changes:
        if not gated(c["type"]):
            continue
        declared = c["key"] in ids
        for fam in dict.fromkeys(f for f in (gate(c["type"], s, declared) for s in c["steps"]) if f):
            steps = [s for s in c["steps"] if gate(c["type"], s, declared) == fam]
            base = {"key": c["key"], "urn": c["urn"], "type": c["type"], "steps": steps, "op": fam}
            if fam == "create" and declared:
                base["id"] = ids[c["key"]]
            why = held(c["urn"]) if held and fam == "delete" else None
            named = getattr(allow, fam)
            if why:
                out.append({**base, "flag": None, "why": why})
            elif c["key"] not in named and c["urn"] not in named:
                out.append({**base, "flag": f"{flag(fam)} {label(c['key'])}"})
    return out


def unknown_refusals(program: dict[str, Any], changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resources of the program, and resources the plan changes (one the
    program no longer has is deleted from state), whose type is of a proxmox
    package and unknown to the runner. Refused whatever the request names."""
    res = program.get("resources") or {}
    out = [{"key": k, "type": r.get("type", ""), "op": "unknown-type", "flag": None, "why": UNKNOWN_WHY}
           for k, r in res.items() if unknown((r or {}).get("type") or "")]
    out += [{"key": c["key"], "urn": c["urn"], "type": c["type"], "op": "unknown-type", "flag": None,
             "why": UNKNOWN_WHY} for c in changes if unknown(c["type"]) and c["key"] not in res]
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
    if r["op"] == "unknown-type":
        return f"{r['key']} ({r['type']}): {r['why']}"
    if r["type"] in HA_TYPES:
        return (f"{r['op']} of HA resource {r['key']} ({r['type']}) (the HA manager starts, stops or moves the "
                f"guest it names to match): needs {r['flag']}")
    if not r.get("flag"):
        return f"{r['op']} of guest {r['key']} ({r['type']}): {r['why']}"
    if r["op"] == "create":
        return (f"create of guest {r['key']} ({r['type']}): declared as already existing ({r.get('id')}): "
                f"adopt it (`fleetkit adopt`), or pass {r['flag']}")
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
