"""Adoption: bring resources that already exist into a stack's state.

`import` is never part of a rendered program (a leftover one made the next
`up` destroy the adopted guest, issue #61). It lives only here, for one run:

  1. render the stacks into this run's own directory; read each selected
     stack's state. `todo` = resources with an adoption id (the stack's
     adoptIds, or --id) that are not in state. A resource already in state is
     never touched, and a resource whose id is already in state under another
     URN (a renamed key), in this stack or in any other stack of the estate,
     is refused: importing it again would leave two state entries for one
     guest, and the next deploy would delete the live one. So are two
     resources of the run that resolve to one real resource. A guest is the
     same guest under every type token of its family (guard.ident);
  2. a temporary project dir (the system's, outside the state dir) holds the
     stack's program with options.import set on the `todo` resources only,
     and `protect` on every other resource that is in state exactly as the
     state has it: the adoption changes nothing of what is already there;
  3. a preview of it, targeted at `todo` and what they depend on, gives the
     report, per resource: `import` (the declaration equals the live
     resource), `import+update` (with each differing property, live and
     declared), `import+unrecorded` (the import did not record some
     properties, so the engine plans an update for them: a reboot of a
     guest), `absent` (the engine says that nothing exists by this id, in
     exactly its own words: a deploy will create it; for a guest that is a
     refusal of --apply unless accepted, since a guest that does exist
     elsewhere would be created a second time), or the provider's error (any
     other failure, whatever its text says). A resource that fails stops the engine before it has
     compared the others, so the preview is run again without it until the
     rest is complete;
  4. with apply: refused if a resource would be updated and is not accepted, or
     if the plan holds anything but import / update / same. Otherwise the
     temporary program is applied with the same targets, bound to the update
     plan of that preview, the temporary dir is removed, and a preview of the
     real program must show every adopted resource as `same` and no delete or
     replace of any guest or of anything that was adopted; and a preview of
     every other stack of the estate that has state must not delete or
     replace anything that holds an adopted id.

The temporary dir is removed on every way out (SIGTERM and SIGHUP included:
main.py turns them into a cancel), and one a kill left behind is swept by the
next run.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Optional

from . import backends, guard, infra, render
from .events import Cancelled, Emitter
from .settings import Settings

# Engine ops an adoption may contain.
ADOPT_OPS = ("same", "import", "update", "read", "refresh")

# Resources whose value the provider's import cannot read back (GitHub never
# returns a secret): adopting one always writes the declared value. They get
# their own status and are adopted only when named with --resource.
SECRET_TYPES = frozenset({
    "github:index/actionsSecret:ActionsSecret",
    "github:index/actionsOrganizationSecret:ActionsOrganizationSecret",
    "github:index/actionsEnvironmentSecret:ActionsEnvironmentSecret",
    "github:index/dependabotSecret:DependabotSecret",
    "github:index/dependabotOrganizationSecret:DependabotOrganizationSecret",
    "github:index/codespacesSecret:CodespacesSecret",
    "github:index/codespacesOrganizationSecret:CodespacesOrganizationSecret",
    "github:index/codespacesUserSecret:CodespacesUserSecret",
})

# Properties of a bpg container or VM that its import is known not to record
# (seen on real guests): the engine then plans an update for them whatever
# the declaration says. Named in the report; the classification itself is by
# data (no live value in what the import read).
KNOWN_UNRECORDED = ("cpu", "memory", "vmId", "timeoutStart", "scsiHardware")

# The engine's words when the provider's read of an import id returns nothing:
# "resource '<id>' does not exist" ("Preview failed: " before it in a preview),
# and the `import failed` of the step. Nothing else means `absent`. It used to
# be any message with "does not exist", "not found" or "404" in it, which a
# container on another node ("Configuration file 'nodes/pve2/lxc/105.conf'
# does not exist"), a proxy ("404 page not found") and a missing provider
# binary all matched: each was reported as "absent, a deploy will create it".
ABSENT_FORM = r"(?:Preview failed: )?resource '{id}' does not exist"
STEP_FAILED = "import failed"


def absent(errors: list[str], rid: str) -> bool:
    """Whether the errors of a resource's import say, in the engine's own
    exact form and nothing besides, that nothing exists by the id `rid`."""
    form = re.compile(ABSENT_FORM.format(id=re.escape(" ".join(str(rid).split()))))
    said = [e for e in errors if form.fullmatch(e)]
    other = [e for e in errors if not form.fullmatch(e) and e != STEP_FAILED]
    return bool(said) and not other

TEMP_PREFIX = "fleetkit-adopt-"


class AdoptError(Exception):
    pass


def with_imports(program: dict[str, Any], ids: dict[str, str]) -> dict[str, Any]:
    """A copy of the program with options.import on the resources of `ids`."""
    out = json.loads(json.dumps(program))
    for key, rid in ids.items():
        out["resources"][key].setdefault("options", {})["import"] = rid
    return out


def temp_program(program: dict[str, Any], ids: dict[str, str], in_state: list[dict[str, Any]],
                 urns: dict[str, str]) -> dict[str, Any]:
    """The program of an adoption: `import` on the resources of `ids`, and on
    every other resource that is in state the `protect` the state has, no more
    and no less. The program a deploy runs protects every guest (guard.protect);
    run as it is, an adoption that targets a guest's dependency would write
    that flag into the state of a resource it only depends on."""
    out = with_imports(program, ids)
    protected = {r["urn"]: bool(r.get("protect")) for r in in_state if not r.get("delete")}
    for key, r in out["resources"].items():
        if key in ids or urns[key] not in protected:
            continue
        opts = r.setdefault("options", {})
        if protected[urns[key]]:
            opts["protect"] = True
        else:
            opts.pop("protect", None)
    return out


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def sweep_temp(max_age: float = 3600) -> list[str]:
    """Remove temporary programs (they carry `import`) that a killed run left
    in the system's temporary directory: those of a process that is gone, and
    those of before the pid was in the name once they are an hour old."""
    gone = []
    for d in Path(tempfile.gettempdir()).glob(TEMP_PREFIX + "*"):
        try:
            if not d.is_dir() or d.is_symlink() or d.stat().st_uid != os.getuid():
                continue
            pid = d.name[len(TEMP_PREFIX):].split("-", 1)[0]
            if pid.isdigit():
                if int(pid) == os.getpid() or _alive(int(pid)):
                    continue
            elif time.time() - d.stat().st_mtime < max_age:
                continue
        except OSError:
            continue
        shutil.rmtree(d, ignore_errors=True)
        gone.append(str(d))
    return gone


def temp_project(s: Settings, stack: str, st: dict[str, Any], tmp: ExitStack) -> Path:
    """A project dir for one run, removed when `tmp` closes."""
    wd = Path(tmp.enter_context(tempfile.TemporaryDirectory(prefix=f"{TEMP_PREFIX}{os.getpid()}-{stack}-")))
    render.link_secrets(s, wd, st)
    # Pulumi's settings of the stack (the passphrase salt): the same stack, from here.
    render.settings_in(s, stack, wd)
    return wd


def _state_only(type_: str) -> bool:
    return type_.startswith(guard.STATE_ONLY)


def _ids(r: dict[str, Any]) -> set[str]:
    return {str(r[k]) for k in ("id", "importID") if r.get(k) not in (None, "")}


def _same_resource(type_: str, rid: str, r: dict[str, Any]) -> bool:
    """Whether the state resource `r` is the real resource the id `rid` names.
    A guest's adoption id is `<node>/<vmid>` while its state id is the vmid, so
    guests are compared by vmid (unique in a cluster), and across the type
    tokens of one family: every VM token of the provider addresses the same VM
    (guard.ident)."""
    return guard.same_resource(type_, rid, r)


class Estate:
    """Every stack of the estate, also those the run did not select: rendered
    and their state read once, when first asked for (a run with nothing to
    adopt opens no other stack). An id is looked for in all of them, and all
    that have state are previewed after an adoption."""

    def __init__(self, s: Settings, estate: str, selected: dict[str, dict[str, Any]],
                 projects: dict[str, render.Project], envs: dict[str, dict[str, str]], rundir: render.Run,
                 ev: Emitter):
        self.s, self.estate, self.rundir, self.ev = s, estate, rundir, ev
        self.sts, self.projects, self.envs = dict(selected), dict(projects), dict(envs)
        self._states: Optional[dict[str, list[dict[str, Any]]]] = None

    def states(self) -> dict[str, list[dict[str, Any]]]:
        if self._states is None:
            for n, st in render.stacks_of(self.s, self.estate).items():
                if n not in self.sts:
                    self.sts[n] = st
                    self.projects[n] = render.render(self.s, n, st, self.ev, self.rundir)
                    self.envs[n] = backends.env_for(self.s, n, st["backend"], self.ev)
            states = {}
            for n in self.sts:
                self.ev.check()
                try:
                    states[n] = infra.state(infra.open_stack(self.s, self.projects[n].wd, n, self.envs[n], self.ev,
                                                             install=False))
                except Cancelled:
                    raise
                except Exception as e:  # noqa: BLE001 - said with what it was needed for
                    raise AdoptError(
                        f"cannot read the state of {n} ({type(e).__name__}: {str(e).strip()[-800:]}). Every stack "
                        f"of {self.estate} is read before anything is adopted: an id that another stack already "
                        f"holds must not be imported a second time") from None
            self._states = states
        return self._states

    def others(self, n: str) -> dict[str, list[dict[str, Any]]]:
        return {m: rs for m, rs in self.states().items() if m != n}


def _diff(ev: Emitter, entry: dict[str, Any], declared: dict[str, Any]) -> list[dict[str, Any]]:
    """Each differing property: its path, the live value and the declared one,
    whenever the engine gave them (a false, a 0 and an empty value are values;
    `liveKnown` / `declaredKnown` say whether there is one), and its kind:
    `real` (the live value is known and the engine says it differs) or
    `unrecorded` (the import recorded no live value for it)."""
    out = []
    for path in entry["diff"]:
        d: dict[str, Any] = {"path": path}
        # The inputs of an imported resource leave out what equals the zero
        # value (false, 0, ""): what was read from the provider has them.
        for where, src in (("inputs", entry.get("live")), ("state", entry.get("liveOutputs"))):
            found, v = guard.lookup(src or {}, path)
            if found:
                d["live"], d["liveFrom"] = ev.redact(guard.scrub(v)), where
                break
        for where, src in (("declaration", entry.get("declared")), ("declaration", declared),
                           ("provider default", entry.get("declaredOutputs"))):
            found, v = guard.lookup(src or {}, path)
            if found:
                d["declared"], d["declaredFrom"] = ev.redact(guard.scrub(v)), where
                break
        d["liveKnown"], d["declaredKnown"] = "live" in d, "declared" in d
        d["kind"] = "real" if d["liveKnown"] else "unrecorded"
        if entry.get("kinds", {}).get(path):
            d["engine"] = entry["kinds"][path]  # the engine's detailedDiff kind, when it sends one
        out.append(d)
    return out


def _resource(ev: Emitter, p: guard.Plan, program: dict[str, Any], key: str, urn: str, rid: str,
              failed: Optional[str]) -> dict[str, Any]:
    type_ = program["resources"][key]["type"]
    r: dict[str, Any] = {"key": key, "type": type_, "urn": urn, "id": rid}
    entry = p.entry(urn)
    # `protect` refusing a replacement is not why it cannot be adopted: the
    # steps say that (status `other`).
    errors = [e for e in p.errors.get(urn) or [] if not guard.PROTECT_RE.search(e)]
    steps = [s for s in (entry or {}).get("steps", []) if s != "same"]
    if errors:
        why = ev.redact("; ".join(errors))
        if absent(errors, rid):
            return {**r, "status": "absent", "detail": why,
                    "note": "does not exist (nothing to import by this id); a deploy will create it"}
        return {**r, "status": "error", "error": why}
    if not steps:
        return {**r, "status": "error",
                "error": ev.redact(failed or "the preview has no step for this resource")}
    assert entry is not None
    r["steps"] = steps
    declared = program["resources"][key].get("properties") or {}
    if any(s not in ADOPT_OPS for s in steps):
        return {**r, "status": "other", "diff": _diff(ev, entry, declared)}
    if "import" in steps and "import" not in entry["done"]:
        # Only the start of its import was seen: the engine stopped (another
        # resource failed) before it compared this one. Never report it clean.
        return {**r, "status": "error",
                "error": ev.redact(failed or "the preview stopped before it compared this resource")}
    if "update" in steps or entry["diff"]:
        diff = _diff(ev, entry, declared)
        if type_ in SECRET_TYPES:
            return {**r, "status": "import+secret", "diff": diff,
                    "note": "a secret's value cannot be read back: adopting it writes the declared value"}
        if diff and all(d["kind"] == "unrecorded" for d in diff):
            return {**r, "status": "import+unrecorded", "diff": diff}
        return {**r, "status": "import+update", "diff": diff}
    return {**r, "status": "import"}


def _preview(s: Settings, n: str, st: dict[str, Any], program: dict[str, Any], env: dict[str, str],
             todo: dict[str, str], all_urns: dict[str, str], wd: Path, ev: Emitter,
             in_state: list[dict[str, Any]]) -> dict[str, Any]:
    """Preview the temporary program until the resources that are left all
    compare. -> {p, remaining, dropped: {key: report entry}, failed, targets}."""
    remaining, dropped = dict(todo), {}
    while True:
        ev.check()
        (wd / "Pulumi.yaml").write_text(json.dumps(temp_program(program, remaining, in_state, all_urns)))
        deps = {d for k in remaining for d in guard.depends_on(program, k)} - set(remaining)
        targets = [all_urns[k] for k in [*remaining, *sorted(deps)]]
        p = guard.Plan(s.stack, program)
        plan_file = wd / infra.PLAN_FILE
        plan_file.unlink(missing_ok=True)
        failed = None
        try:
            tst = infra.open_stack(s, wd, n, env, ev)
            infra.engine(tst, ev, n, "preview", p, targets=targets, plan_file=plan_file)
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001 - the provider refusing an import is a result
            failed = str(e).strip()[-1500:]
            if not isinstance(e, infra.EngineError):
                failed = f"{type(e).__name__}: {failed}"
        bad = [k for k in remaining
               if [e for e in p.errors.get(all_urns[k]) or [] if not guard.PROTECT_RE.search(e)]]
        if not bad:
            return {"p": p, "remaining": remaining, "dropped": dropped, "failed": failed, "targets": targets,
                    "plan_file": plan_file}
        # A resource that cannot be imported (it does not exist, or the
        # provider failed) ends the engine's run: the others were not, or not
        # all, compared. Report it, and preview again without it.
        for k in bad:
            dropped[k] = _resource(ev, p, program, k, all_urns[k], remaining[k], failed)
        remaining = {k: v for k, v in remaining.items() if k not in bad}
        if not remaining:
            plan_file.unlink(missing_ok=True)
            return {"p": guard.Plan(s.stack, program), "remaining": {}, "dropped": dropped, "failed": None,
                    "targets": [], "plan_file": plan_file}


def run(s: Settings, estate: str, ev: Emitter, stacks: list[str] | None = None,
        resources: list[str] | None = None, ids: dict[str, str] | None = None,
        accept_update: list[str] | None = None, apply: bool = False,
        accept_absent: list[str] | None = None) -> dict[str, Any]:
    """-> the report: {estate, apply, applied, ok, stacks: {<stack>: {program,
    resources: [...], other: [...], error?, verify?}}, refused: [...]}."""
    ids, accept, only = dict(ids or {}), set(accept_update or []), list(resources or [])
    gone = set(accept_absent or [])
    sweep_temp()
    sts = render.stacks_of(s, estate, stacks)
    report: dict[str, Any] = {"estate": estate, "apply": apply, "applied": False, "ok": True,
                              "stacks": {}, "refused": []}
    # The real programs go into this run's own directory (render.Run): nothing
    # here is written where a deploy reads, and no deploy writes here.
    with render.Run(s, "adopt") as rundir:
        projects = {n: render.render(s, n, st, ev, rundir) for n, st in sts.items()}
        envs = {n: backends.env_for(s, n, st["backend"], ev) for n, st in sts.items()}
        # The program the engine runs: every guest protected (guard.protect).
        programs = {n: guard.load_program(projects[n].wd) for n in sts}
        known = {k for p in programs.values() for k in (p.get("resources") or {})}
        for what, keys in (("--id", ids), ("--resource", only), ("--accept-update", accept),
                           ("--accept-absent", gone)):
            missing = sorted(set(keys) - known)
            if missing:
                raise AdoptError(f"{what}: no resource {', '.join(missing)} in {', '.join(sorted(sts))}")
            # A key names one resource: not the same key in two stacks.
            for k in sorted(set(keys)):
                hits = sorted(n for n, p in programs.items() if k in (p.get("resources") or {}))
                if len(hits) > 1:
                    raise AdoptError(f"{what} {k} is ambiguous: {k} is a resource of {', '.join(hits)}; "
                                     f"run one stack at a time (--stack)")
        for n, program in programs.items():
            if guard.imports(program):
                raise AdoptError(f"{n}: the rendered program already sets options.import on "
                                 f"{', '.join(guard.imports(program))}; a program must not carry import "
                                 f"(update fleetkit, or drop `adopt` from the stack)")

        work: dict[str, dict[str, Any]] = {}
        whole = Estate(s, estate, sts, projects, envs, rundir, ev)
        with ExitStack() as tmp:
            for n, st in sts.items():
                ev.check()
                work_n = _stack(s, n, st, programs[n], projects[n], envs[n], ids, only, tmp, ev, report, whole)
                if work_n:
                    work[n] = work_n
            _across_stacks(report)
            # A secret named with --resource is accepted by being named.
            accept |= {r["key"] for out in report["stacks"].values() for r in out["resources"]
                       if r["status"] == "import+secret"}
            _judge(report, accept, gone)
            if apply and report["ok"] and work:
                for n, w in work.items():
                    ev.check()
                    _up(s, n, w, programs[n], envs[n], accept, ev)
                report["applied"] = True
        # The temporary programs are gone. What was adopted must now be `same`
        # in a preview of the real program, and that preview must not delete
        # or replace a guest or anything that was adopted; if it does, say
        # what and stop.
        # The same id may sit in another stack of the estate under a form
        # the check before the import did not match: every other stack that
        # has state is previewed too, and must not delete or replace
        # anything that holds an id that was just adopted.
        if report["applied"]:
            adopted = [(programs[n]["resources"][k]["type"], rid) for n, w in work.items()
                       for k, rid in w["todo"].items()]
            for n, w in work.items():
                report["stacks"][n]["verify"] = _verify(s, n, w, projects[n], envs[n], ev, adopted)
            for n, resources_n in whole.states().items():
                if n in work or not resources_n:
                    continue
                out = report["stacks"].setdefault(n, {"program": whole.sts[n]["file"], "resources": [], "other": []})
                out["verify"] = {**_verify(s, n, None, whole.projects[n], whole.envs[n], ev, adopted),
                                 "scope": "adopted ids"}
            if not all(out["verify"]["ok"] for out in report["stacks"].values() if "verify" in out):
                report["ok"] = False
    return report


def _across_stacks(report: dict[str, Any]) -> None:
    """Two resources of the run, in different stacks, whose ids name one real
    resource: both become `duplicate` (within a stack: _stack). Nothing was
    applied yet, and _judge refuses a duplicate."""
    todo = [(n, r) for n, out in report["stacks"].items() for r in out["resources"]
            if "id" in r and r["status"] not in ("duplicate", "secret")]
    for n, r in todo:
        same = sorted(f"{m}/{o['key']}" for m, o in todo
                      if m != n and guard.ident(o["type"], o["id"]) == guard.ident(r["type"], r["id"]))
        if same:
            r.update(status="duplicate", same=same, why=_twice(r["id"], [f"{n}/{r['key']}", *same]))


def _twice(rid: str, keys: list[str]) -> str:
    return (f"{' and '.join(keys)} resolve to the same real resource (id {rid}): importing both would leave two "
            f"state entries for it, and the delete of either would destroy it. Declare it once (or correct the "
            f"id of the other), then run adopt again")


def _stack(s: Settings, n: str, st: dict[str, Any], program: dict[str, Any], project: render.Project,
           env: dict[str, str], ids: dict[str, str], only: list[str], tmp: ExitStack, ev: Emitter,
           report: dict[str, Any], whole: Estate) -> Optional[dict[str, Any]]:
    """The report of one stack; -> what an apply needs, or None."""
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
        return None
    real = infra.open_stack(s, project.wd, n, env, ev)
    in_state = infra.state(real)
    state_urns = {r["urn"] for r in in_state}
    all_urns = guard.urns(s.stack, program)
    todo: dict[str, str] = {}
    for k in wanted:
        base = {"key": k, "type": res[k]["type"], "urn": all_urns[k]}
        if all_urns[k] in state_urns:
            out["resources"].append({**base, "status": "in-state",
                                     "note": "already in state; adopt does not touch it"})
        elif k in have:
            twin = next((r for r in in_state if r["urn"] != all_urns[k]
                         and _same_resource(res[k]["type"], have[k], r)), None)
            # ... or in the state of another stack of the estate (selected or not).
            far = None if twin is not None else next(
                ((m, r) for m, rs in whole.others(n).items() for r in rs
                 if _same_resource(res[k]["type"], have[k], r)), None)
            if far is not None:
                out["resources"].append({**base, "id": have[k], "status": "duplicate", "twin": far[1]["urn"],
                                         "twinStack": far[0], "why": (
                    f"its id {have[k]} is already in the state of {far[0]} as {far[1]['urn']} (id "
                    f"{far[1].get('id')}): importing it here would leave two state entries for one real resource, "
                    f"and the next deploy of {far[0]} or of {n} that drops one would delete it. Move it instead: "
                    f"`pulumi state move` from {far[0]} (with the backends and passphrases of both), or remove "
                    f"the entry of the stack that no longer manages it (`pulumi state delete`, which touches "
                    f"nothing real), then run adopt again")})
            elif twin is not None:
                # The id is in state already, under another URN: a renamed key
                # (or project, or stack). A second import would give the state
                # two entries for one real resource, and the next deploy would
                # delete the one the program no longer has: the live guest.
                out["resources"].append({**base, "id": have[k], "status": "duplicate", "twin": twin["urn"], "why": (
                    f"its id {have[k]} is already in state as {twin['urn']} (id {twin.get('id')}): importing it "
                    f"again would leave two state entries for one real resource, and the next deploy would "
                    f"delete it. Rename it in state instead: `pulumi state rename '{twin['urn']}' {k}` (with "
                    f"the stack's backend and passphrase; `pulumi state move` for another project or stack), "
                    f"then run adopt again: it will find it in state")})
            elif res[k]["type"] in SECRET_TYPES and k not in only:
                out["resources"].append({**base, "id": have[k], "status": "secret", "note": (
                    f"a secret's value cannot be read back, so adopting it always writes the declared value; "
                    f"not adopted unless named: --resource {k}")})
            else:
                todo[k] = have[k]
        elif k in unresolved:
            out["resources"].append({**base, "status": "unresolved", "why": unresolved[k]})
        else:
            out["resources"].append({**base, "status": "no-id",
                                     "why": "no adoption id: pass --id " + k + "=<provider id>"})
    # Two of them that name one real resource: neither is imported.
    for k, rid in list(todo.items()):
        same = sorted(o for o, oid in todo.items()
                      if o != k and guard.ident(res[o]["type"], oid) == guard.ident(res[k]["type"], rid))
        if same:
            out["resources"].append({"key": k, "type": res[k]["type"], "urn": all_urns[k], "id": rid,
                                     "status": "duplicate", "same": same, "why": _twice(rid, [k, *same])})
    todo = {k: v for k, v in todo.items() if not any(r["key"] == k and r["status"] == "duplicate"
                                                     for r in out["resources"])}
    if not todo:
        return None
    # The temporary program: the only place import is ever written.
    wd = temp_project(s, n, st, tmp)
    pv = _preview(s, n, st, program, env, todo, all_urns, wd, ev, in_state)
    p, remaining = pv["p"], pv["remaining"]
    if pv["failed"] and not p.only_protect_errors():
        out["error"] = ev.redact(pv["failed"])
    for k, rid in todo.items():
        out["resources"].append(pv["dropped"].get(k) or _resource(ev, p, program, k, all_urns[k], rid, pv["failed"]))
    todo_urns = {all_urns[k] for k in remaining}
    out["other"] = [c for c in p.changes() if c["urn"] not in todo_urns]
    ev.emit("adopt", "plan", stack=n, resources=out["resources"], other=out["other"])
    if not remaining:
        return None
    plan_file = pv["plan_file"]
    bound = None
    if plan_file.is_file():
        os.chmod(plan_file, 0o600)
        bound = {"plan_sha256": guard.sha256(plan_file), "program_sha256": guard.sha256(wd / "Pulumi.yaml")}
    return {"wd": wd, "todo": remaining, "targets": pv["targets"], "urns": todo_urns, "plan_file": plan_file,
            "bound": bound}


def _judge(report: dict[str, Any], accept: set[str], accept_absent: frozenset[str] | set[str] = frozenset()) -> None:
    """Fill report.refused (what stops an apply) and report.ok."""
    refused = report["refused"]
    for n, out in report["stacks"].items():
        for r in out["resources"]:
            guest = r["type"] in guard.GUEST_TYPES
            if r["status"] == "error":
                refused.append({"stack": n, "key": r["key"], "kind": "error",
                                "why": f"cannot import: {r['error']}"})
            elif r["status"] == "duplicate":
                refused.append({"stack": n, "key": r["key"], "kind": "duplicate", "why": r["why"]})
            elif r["status"] == "absent" and guest and r["key"] not in accept_absent:
                refused.append({"stack": n, "key": r["key"], "kind": "absent", "why": (
                    f"nothing was found by the id {r['id']}, so a deploy would CREATE this guest. If the guest "
                    f"exists after all (on another node, under another vmid), that is a second one: correct the "
                    f"id (--id {r['key']}=<node>/<vmid>). If it really does not exist, say so: "
                    f"--accept-absent {r['key']}")})
            elif r["status"] == "other":
                refused.append({"stack": n, "key": r["key"], "kind": "plan",
                                "why": f"the plan would {', '.join(r['steps'])} it; adopt only imports"})
            elif r["status"] == "import+update" and r["key"] not in accept:
                paths = ", ".join(d["path"] for d in r["diff"]) or "see the preview"
                reboot = " It is a guest: the update reboots it." if guest else ""
                refused.append({"stack": n, "key": r["key"], "kind": "update", "why": (
                    f"the declaration differs from the live resource ({paths}), so importing it also "
                    f"updates it in place.{reboot} Make the declaration match, or pass "
                    f"--accept-update {r['key']}")})
            elif r["status"] == "import+unrecorded" and r["key"] not in accept:
                paths = ", ".join(d["path"] for d in r["diff"])
                reboot = (" It is a guest: that update REBOOTS it, even if every value already matches."
                          if guest else "")
                refused.append({"stack": n, "key": r["key"], "kind": "update", "why": (
                    f"the import does not record {paths}, so the engine cannot compare them with the "
                    f"declaration and plans an update in place for them.{reboot} The declaration cannot be "
                    f"made to match: check the values by hand, then pass --accept-update {r['key']}")})
        for c in out["other"]:
            if c["op"] == "create" and _state_only(c["type"]):
                continue  # a provider or the stack itself: state only
            refused.append({"stack": n, "key": c["key"], "kind": "plan",
                            "why": f"the plan would also {c['op']} {c['key']} ({c['type']}); adopt only imports"})
        if out.get("error") and not any(r["stack"] == n for r in refused):
            refused.append({"stack": n, "key": None, "kind": "error", "why": out["error"]})
    # Without apply, a difference is the report's content, not a failure; a
    # resource that cannot be imported at all, or that is in state under
    # another name, is one either way. A resource that does not exist is
    # neither: a deploy creates it; for a guest an apply wants that said
    # (--accept-absent).
    report["ok"] = not [r for r in refused if report["apply"] or r["kind"] in ("error", "duplicate")]


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

    # The up is bound to the plan of the preview the report was made from: the
    # engine refuses anything else. No plan (the preview failed), no up.
    bound, wd, plan_file = w["bound"], w["wd"], w["plan_file"]
    if not bound:
        raise guard.GuardError(f"refused: {n}: the adoption preview made no update plan; nothing was applied")
    if guard.sha256(wd / "Pulumi.yaml") != bound["program_sha256"] or not plan_file.is_file() \
            or guard.sha256(plan_file) != bound["plan_sha256"]:
        raise guard.GuardError(f"refused: {n}: the temporary program or its plan changed after the preview; "
                               f"nothing was applied")
    st = infra.open_stack(s, wd, n, env, ev, install=False)
    infra.engine(st, ev, n, "up", p, targets=w["targets"], tripwire=tripwire, plan_file=plan_file)
    ev.emit("adopt", "applied", stack=n, resources=sorted(w["todo"]))


def _destroys(c: dict[str, Any]) -> bool:
    return any(guard.family(x) in ("replace", "delete") for x in c["steps"])


def _verify(s: Settings, n: str, w: Optional[dict[str, Any]], project: render.Project, env: dict[str, str],
            ev: Emitter, adopted: list[tuple[str, str]] | None = None) -> dict[str, Any]:
    """Preview the real program (no import) after the adoption. ok only if
    every adopted resource is `same` AND the plan deletes or replaces no guest
    and nothing whose state id is one that was just adopted. `adopted`: (type,
    id) of everything the run adopted, in whatever stack. `w` is None for a
    stack in which nothing was adopted: there only what holds an adopted id
    counts (its guests are its own deploys' business)."""
    mine = w is not None
    w = w or {"todo": {}, "urns": set()}
    program = guard.load_program(project.wd)
    if guard.imports(program):  # cannot happen: adopt never writes there
        raise AdoptError(f"{n}: the real program carries import")
    try:
        project.verify()
        planned = infra.plan(s, project.wd, n, env, ev, True)
        now = infra.state(infra.open_stack(s, project.wd, n, env, ev, install=False))
    except Cancelled:
        raise
    except Exception as e:  # noqa: BLE001 - reported as the verification's failure
        return {"ok": False, "differs": [], "destroys": [],
                "error": ev.redact(f"{type(e).__name__}: {str(e).strip()[-1500:]}")}
    (project.wd / infra.PLAN_FILE).unlink(missing_ok=True)
    if adopted is None:
        adopted = [(program["resources"][k]["type"], rid) for k, rid in w["todo"].items()]
    ids = {rid for _, rid in adopted}
    hot = {r["urn"] for r in now
           if _ids(r) & ids or any(_same_resource(t, rid, r) for t, rid in adopted)} | w["urns"]
    differs = [c for c in planned["plan"] if c["urn"] in w["urns"]]
    destroys = [c for c in planned["plan"] if c not in differs and _destroys(c)
                and ((mine and c["type"] in guard.GUEST_TYPES) or c["urn"] in hot)]
    v = {"ok": not differs and not destroys, "differs": differs, "destroys": destroys}
    ev.emit("adopt", "verify", stack=n, **v)
    return v


def _value(d: dict[str, Any], side: str) -> str:
    if not d.get(f"{side}Known", side in d):
        return "not recorded by the import" if side == "live" else "not set"
    note = "" if d.get(f"{side}From") in (None, "inputs", "declaration") else f" ({d[side + 'From']})"
    return json.dumps(d[side]) + note


def text(report: dict[str, Any]) -> list[str]:
    """The report for a person."""
    out: list[str] = []
    for n, st in report["stacks"].items():
        out.append(f"{n}  (program {st['program']})")
        if not st["resources"]:
            out.append("  nothing to adopt")
        for r in st["resources"]:
            status = r["status"]
            guest = r["type"] in guard.GUEST_TYPES
            if status == "import":
                out.append(f"  {r['key']}: import {r['id']}  ({r['type']}): clean, the declaration equals the live resource")
            elif status in ("import+update", "import+unrecorded", "import+secret"):
                head = {"import+update": "+ update: the declaration differs",
                        "import+unrecorded": "+ update of what the import does not record"
                                             + (": it REBOOTS the guest" if guest else ""),
                        "import+secret": "+ write of the secret: its value cannot be read back"}[status]
                out.append(f"  {r['key']}: import {r['id']} {head}  ({r['type']})")
                for d in r["diff"]:
                    out.append(f"      {d['path']}: live {_value(d, 'live')}, declared {_value(d, 'declared')}"
                               + ("" if d["kind"] == "real" else "  [unrecorded]"))
            elif status == "absent":
                out.append(f"  {r['key']}: absent: {r['id']} {r['note']}  ({r['type']})"
                           + ("; a guest: check that it is not somewhere else under another id" if guest else ""))
            elif status == "secret":
                out.append(f"  {r['key']}: secret, not adopted: {r['note']}")
            elif status == "duplicate":
                out.append(f"  {r['key']}: REFUSED: {r['why']}")
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
            if v["ok"] and v.get("scope"):
                out.append("  verified: a preview of the real program deletes or replaces nothing that holds "
                           "an adopted id")
            elif v["ok"]:
                out.append("  verified: a preview of the real program shows every adopted resource as same, "
                           "and no delete or replace of a guest")
            else:
                out.append("  NOT VERIFIED: a preview of the real program (no import):")
                out += [f"      still changes what was adopted: {c['op']} {c['key']}"
                        + (f" [{', '.join(c['diff'])}]" if c["diff"] else "") for c in v["differs"]]
                out += [f"      WOULD {c['op'].upper()} {c['key']} ({c['type']}): do not deploy this stack "
                        f"until the state is fixed" for c in v.get("destroys") or []]
                if v.get("error"):
                    out.append(f"      {v['error']}")
    for r in report["refused"]:
        word = "refused" if report["apply"] or r["kind"] in ("error", "duplicate") else "an apply would refuse"
        out.append(f"{word}: {r['stack']}" + (f" {r['key']}" if r["key"] else "") + f": {r['why']}")
    if report["applied"]:
        out.append("adopted" if report["ok"] else "adopted, but not verified: fix the declaration (nothing was changed to fix it)")
    else:
        out.append("nothing changed" + ("" if report["apply"] else " (pass --apply to adopt)"))
    return out
