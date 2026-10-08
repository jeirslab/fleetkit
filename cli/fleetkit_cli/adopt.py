"""Adoption: bring resources that already exist into a stack's state.

`import` is never part of a rendered program (a leftover one made the next
`up` destroy the adopted guest, issue #61). It lives only here, for one run:

  1. render the stacks; read each selected stack's state. `todo` = resources
     with an adoption id (the stack's adoptIds, or --id) that are not in state.
     A resource already in state is never touched;
  2. a temporary project dir (outside the state dir) holds the stack's program
     with options.import set on the `todo` resources only;
  3. a preview of it, targeted at `todo` and what they depend on, gives the
     report: per resource `import` (the declaration equals the live resource),
     `import+update` (with each differing property, live and declared), or the
     provider's error;
  4. with apply: refused if a resource would be updated and is not accepted, or
     if the plan holds anything but import / update / same. Otherwise the
     temporary program is applied with the same targets, the temporary dir is
     removed, and a preview of the real program must show every adopted
     resource as `same`.

The temporary dir is removed on every way out, and nothing is written to the
real project dir but Pulumi's own stack settings file when the stack is new.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Optional

from . import backends, guard, infra, render
from .events import Cancelled, Emitter
from .settings import Settings

# Engine ops an adoption may contain.
ADOPT_OPS = ("same", "import", "update", "read", "refresh")


class AdoptError(Exception):
    pass


def with_imports(program: dict[str, Any], ids: dict[str, str]) -> dict[str, Any]:
    """A copy of the program with options.import on the resources of `ids`."""
    out = json.loads(json.dumps(program))
    for key, rid in ids.items():
        out["resources"][key].setdefault("options", {})["import"] = rid
    return out


def temp_project(s: Settings, stack: str, st: dict[str, Any], real: Path, program: dict[str, Any],
                 tmp: ExitStack) -> Path:
    """A project dir for one run, removed when `tmp` closes."""
    wd = Path(tmp.enter_context(tempfile.TemporaryDirectory(prefix=f"fleetkit-adopt-{stack}-")))
    (wd / "Pulumi.yaml").write_text(json.dumps(program))
    render.link_secrets(s, wd, st)
    # Pulumi's settings of the stack (the passphrase salt): the same stack, from here.
    settings = real / f"Pulumi.{s.stack}.yaml"
    if settings.is_file():
        shutil.copy(settings, wd / settings.name)
    return wd


def _state_urns(st: Any) -> set[str]:
    deployment = st.export_stack().deployment or {}
    return {r["urn"] for r in deployment.get("resources") or []}


def _state_only(type_: str) -> bool:
    return type_.startswith(guard.STATE_ONLY)


def _diff(ev: Emitter, entry: dict[str, Any], declared: dict[str, Any]) -> list[dict[str, Any]]:
    """Each differing property: its path, the live value and the declared one."""
    out = []
    for path in entry["diff"]:
        d: dict[str, Any] = {"path": path}
        for name, src in (("live", entry.get("live")), ("declared", entry.get("declared") or declared)):
            found, v = guard.lookup(src or {}, path)
            if found:
                d[name] = ev.redact(guard.scrub(v))
        out.append(d)
    return out


def _resource(ev: Emitter, p: guard.Plan, program: dict[str, Any], key: str, urn: str, rid: str,
              failed: Optional[str]) -> dict[str, Any]:
    r: dict[str, Any] = {"key": key, "type": program["resources"][key]["type"], "urn": urn, "id": rid}
    entry = p.entry(urn)
    errors = p.errors.get(urn) or []
    steps = [s for s in (entry or {}).get("steps", []) if s != "same"]
    if errors or not steps:
        why = "; ".join(errors) or failed or "the preview has no step for this resource"
        return {**r, "status": "error", "error": ev.redact(why)}
    assert entry is not None
    r["steps"] = steps
    if any(s not in ADOPT_OPS for s in steps):
        return {**r, "status": "other", "diff": _diff(ev, entry, program["resources"][key].get("properties") or {})}
    if "update" in steps or entry["diff"]:
        return {**r, "status": "import+update",
                "diff": _diff(ev, entry, program["resources"][key].get("properties") or {})}
    return {**r, "status": "import"}


def run(s: Settings, estate: str, ev: Emitter, stacks: list[str] | None = None,
        resources: list[str] | None = None, ids: dict[str, str] | None = None,
        accept_update: list[str] | None = None, apply: bool = False) -> dict[str, Any]:
    """-> the report: {estate, apply, applied, ok, stacks: {<stack>: {program,
    resources: [...], other: [...], error?, verify?}}, refused: [...]}."""
    ids, accept, only = dict(ids or {}), set(accept_update or []), list(resources or [])
    sts = render.stacks_of(s, estate, stacks)
    workdirs = {n: render.render(s, n, st, ev) for n, st in sts.items()}
    envs = {n: backends.env_for(s, n, st["backend"], ev) for n, st in sts.items()}
    programs = {n: guard.load_program(workdirs[n]) for n in sts}
    known = {k for p in programs.values() for k in (p.get("resources") or {})}
    for what, keys in (("--id", ids), ("--resource", only), ("--accept-update", accept)):
        missing = sorted(set(keys) - known)
        if missing:
            raise AdoptError(f"{what}: no resource {', '.join(missing)} in {', '.join(sorted(sts))}")
    for n, program in programs.items():
        if guard.imports(program):
            raise AdoptError(f"{n}: the rendered program already sets options.import on "
                             f"{', '.join(guard.imports(program))}; a program must not carry import "
                             f"(update fleetkit, or drop `adopt` from the stack)")

    report: dict[str, Any] = {"estate": estate, "apply": apply, "applied": False, "ok": True,
                              "stacks": {}, "refused": []}
    work: dict[str, dict[str, Any]] = {}
    with ExitStack() as tmp:
        for n, st in sts.items():
            ev.check()
            program = programs[n]
            res = program.get("resources") or {}
            out: dict[str, Any] = {"program": st["file"], "resources": [], "other": []}
            report["stacks"][n] = out
            have = {**{k: v for k, v in (st.get("adoptIds") or {}).items() if k in res},
                    **{k: v for k, v in ids.items() if k in res}}
            unresolved = {k: why for k, why in (st.get("adoptUnresolved") or {}).items()
                          if k in res and k not in have}
            wanted = [k for k in res if k in have or k in unresolved or k in only]
            if only:
                wanted = [k for k in wanted if k in only]
            if not wanted:
                continue
            real = infra.open_stack(s, workdirs[n], n, envs[n], ev)
            in_state = _state_urns(real)
            all_urns = guard.urns(s.stack, program)
            todo: dict[str, str] = {}
            for k in wanted:
                base = {"key": k, "type": res[k]["type"], "urn": all_urns[k]}
                if all_urns[k] in in_state:
                    out["resources"].append({**base, "status": "in-state",
                                             "note": "already in state; adopt does not touch it"})
                elif k in have:
                    todo[k] = have[k]
                elif k in unresolved:
                    out["resources"].append({**base, "status": "unresolved", "why": unresolved[k]})
                else:
                    out["resources"].append({**base, "status": "no-id",
                                             "why": "no adoption id: pass --id " + k + "=<provider id>"})
            if not todo:
                continue
            # The temporary program: the only place import is ever written.
            wd = temp_project(s, n, st, workdirs[n], with_imports(program, todo), tmp)
            deps = {d for k in todo for d in guard.depends_on(program, k)} - set(todo)
            targets = [all_urns[k] for k in [*todo, *sorted(deps)]]
            p = guard.Plan(s.stack, program)
            failed = None
            try:
                tst = infra.open_stack(s, wd, n, envs[n], ev)
                infra.engine(tst, ev, n, "preview", p, targets=targets)
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001 - the provider refusing an import is a result
                failed = f"{type(e).__name__}: {str(e).strip()[-1500:]}"
                out["error"] = ev.redact(failed)
            for k, rid in todo.items():
                out["resources"].append(_resource(ev, p, program, k, all_urns[k], rid, failed))
            todo_urns = {all_urns[k] for k in todo}
            out["other"] = [c for c in p.changes() if c["urn"] not in todo_urns]
            work[n] = {"wd": wd, "todo": todo, "targets": targets, "urns": todo_urns}
            ev.emit("adopt", "plan", stack=n, resources=out["resources"], other=out["other"])

        _judge(report, accept)
        if apply and report["ok"] and work:
            for n, w in work.items():
                ev.check()
                _up(s, n, w, programs[n], envs[n], accept, ev)
            report["applied"] = True
    # The temporary programs are gone. What was adopted must now be `same` in
    # a preview of the real program; if not, say what differs and stop.
    if report["applied"]:
        for n, w in work.items():
            report["stacks"][n]["verify"] = _verify(s, n, w, workdirs[n], envs[n], ev)
            if not report["stacks"][n]["verify"]["ok"]:
                report["ok"] = False
    return report


def _judge(report: dict[str, Any], accept: set[str]) -> None:
    """Fill report.refused (what stops an apply) and report.ok."""
    refused = report["refused"]
    for n, out in report["stacks"].items():
        for r in out["resources"]:
            if r["status"] == "error":
                refused.append({"stack": n, "key": r["key"], "kind": "error",
                                "why": f"cannot import: {r['error']}"})
            elif r["status"] == "other":
                refused.append({"stack": n, "key": r["key"], "kind": "plan",
                                "why": f"the plan would {', '.join(r['steps'])} it; adopt only imports"})
            elif r["status"] == "import+update" and r["key"] not in accept:
                paths = ", ".join(d["path"] for d in r["diff"]) or "see the preview"
                reboot = (" It is a guest: the update reboots it."
                          if r["type"] in guard.GUEST_TYPES else "")
                refused.append({"stack": n, "key": r["key"], "kind": "update", "why": (
                    f"the declaration differs from the live resource ({paths}), so importing it also "
                    f"updates it in place.{reboot} Make the declaration match, or pass "
                    f"--accept-update {r['key']}")})
        for c in out["other"]:
            if c["op"] == "create" and _state_only(c["type"]):
                continue  # a provider or the stack itself: state only
            refused.append({"stack": n, "key": c["key"], "kind": "plan",
                            "why": f"the plan would also {c['op']} {c['key']} ({c['type']}); adopt only imports"})
        if out.get("error") and not any(r["stack"] == n for r in refused):
            refused.append({"stack": n, "key": None, "kind": "error", "why": out["error"]})
    # Without apply, a difference is the report's content, not a failure;
    # a resource that cannot be imported at all is one either way.
    report["ok"] = not [r for r in refused if report["apply"] or r["kind"] == "error"]


def _up(s: Settings, n: str, w: dict[str, Any], program: dict[str, Any], env: dict[str, str],
        accept: set[str], ev: Emitter) -> None:
    p = guard.Plan(s.stack, program)

    def tripwire(entry: dict[str, Any], op: str) -> Optional[str]:
        if op == "create" and _state_only(entry["type"]):
            return None
        if op not in ADOPT_OPS or entry["urn"] not in w["urns"]:
            return f"{op} of {entry['key']}"
        if op == "update" and entry["key"] not in accept:
            return f"update of {entry['key']} (not accepted)"
        return None

    st = infra.open_stack(s, w["wd"], n, env, ev, install=False)
    infra.engine(st, ev, n, "up", p, targets=w["targets"], tripwire=tripwire)
    ev.emit("adopt", "applied", stack=n, resources=sorted(w["todo"]))


def _verify(s: Settings, n: str, w: dict[str, Any], real: Path, env: dict[str, str], ev: Emitter) -> dict[str, Any]:
    program = guard.load_program(real)
    if guard.imports(program):  # cannot happen: adopt never writes there
        raise AdoptError(f"{n}: the real program carries import")
    p = guard.Plan(s.stack, program)
    st = infra.open_stack(s, real, n, env, ev, install=False)
    try:
        infra.engine(st, ev, n, "preview", p)
    except Cancelled:
        raise
    except Exception as e:  # noqa: BLE001 - reported as the verification's failure
        return {"ok": False, "differs": [], "error": ev.redact(f"{type(e).__name__}: {str(e).strip()[-1500:]}")}
    differs = [c for c in p.changes() if c["urn"] in w["urns"]]
    v = {"ok": not differs, "differs": differs}
    ev.emit("adopt", "verify", stack=n, **v)
    return v


def text(report: dict[str, Any]) -> list[str]:
    """The report for a person."""
    out: list[str] = []
    for n, st in report["stacks"].items():
        out.append(f"{n}  (program {st['program']})")
        if not st["resources"]:
            out.append("  nothing to adopt")
        for r in st["resources"]:
            status = r["status"]
            if status == "import":
                out.append(f"  {r['key']}: import {r['id']}  ({r['type']}): clean, the declaration equals the live resource")
            elif status == "import+update":
                out.append(f"  {r['key']}: import {r['id']} + update  ({r['type']}): the declaration differs")
                for d in r["diff"]:
                    live = json.dumps(d["live"]) if "live" in d else "?"
                    decl = json.dumps(d["declared"]) if "declared" in d else "?"
                    out.append(f"      {d['path']}: live {live}, declared {decl}")
            elif status == "in-state":
                out.append(f"  {r['key']}: {r['note']}")
            elif status in ("unresolved", "no-id"):
                out.append(f"  {r['key']}: cannot adopt: {r['why']}")
            elif status == "other":
                out.append(f"  {r['key']}: the plan would {', '.join(r['steps'])} it  ({r['type']})")
            else:
                out.append(f"  {r['key']}: error: {r['error']}")
        for c in st["other"]:
            out.append(f"  also in the plan: {c['op']} {c['key']}  ({c['type']})")
        v = st.get("verify")
        if v is not None:
            if v["ok"]:
                out.append("  verified: a preview of the real program shows every adopted resource as same")
            else:
                out.append("  NOT VERIFIED: a preview of the real program (no import) still changes what was adopted:")
                out += [f"      {c['op']} {c['key']}" + (f" [{', '.join(c['diff'])}]" if c["diff"] else "")
                        for c in v["differs"]]
                if v.get("error"):
                    out.append(f"      {v['error']}")
    for r in report["refused"]:
        word = "refused" if report["apply"] else "an apply would refuse"
        out.append(f"{word}: {r['stack']}" + (f" {r['key']}" if r["key"] else "") + f": {r['why']}")
    if report["applied"]:
        out.append("adopted" if report["ok"] else "adopted, but not verified: fix the declaration (nothing was changed to fix it)")
    else:
        out.append("nothing changed" + ("" if report["apply"] else " (pass --apply to adopt)"))
    return out
