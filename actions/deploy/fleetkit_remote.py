#!/usr/bin/env python3
"""The fleetkit deploy action's client: run estates on a fleetkit deploy server
from GitHub Actions. stdlib only (a runner has python3 and nothing else).

Per estate: POST /v1/deploys at the commit (waiting while the estate is busy),
follow /v1/deploys/<id>/events into the log, then write the step summary and,
on a pull request, one comment per estate (edited in place on later runs).
Exit 1 if any estate's job did not succeed.

Environment (set by action.yml): FLEETKIT_API_URL, FLEETKIT_API_TOKEN,
FLEETKIT_ESTATES, FLEETKIT_MODE (auto|preview|deploy), FLEETKIT_REV,
FLEETKIT_GOAL, FLEETKIT_HIVE, FLEETKIT_STACKS, FLEETKIT_INFRA, FLEETKIT_NIXOS,
FLEETKIT_ALLOW_REPLACE, FLEETKIT_ALLOW_DELETE, FLEETKIT_ALLOW_UPDATE (guests a
deploy may replace, delete, update; the server refuses otherwise),
FLEETKIT_TIMEOUT, FLEETKIT_COMMENT, GITHUB_TOKEN, and the runner's GITHUB_*.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any

TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")
OPS = ("create", "update", "replace", "delete", "same")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def flag(name: str, default: bool) -> bool:
    v = env(name)
    return default if not v else v.lower() in ("1", "true", "yes", "on")


def http(method: str, url: str, token: str, body: Any = None, timeout: float = 60) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "Accept": "application/json", "User-Agent": "fleetkit-action"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode(errors="replace")


def gh_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{name}<<__FLEETKIT__\n{value}\n__FLEETKIT__\n")


def summary(md: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(md + "\n")


def event_payload() -> dict[str, Any]:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if path and os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


class Server:
    def __init__(self, url: str, token: str):
        self.url, self.token = url.rstrip("/"), token

    def call(self, method: str, path: str, body: Any = None, timeout: float = 60) -> tuple[int, Any]:
        return http(method, f"{self.url}{path}", self.token, body, timeout)

    def wait_up(self, seconds: int = 90) -> None:
        """The tailnet takes a moment after `tailscale up` (routes, MagicDNS)."""
        last: Any = None
        end = time.time() + seconds
        while time.time() < end:
            try:
                code, _ = self.call("GET", "/healthz", timeout=10)
                if code == 200:
                    return
                last = code
            except OSError as e:
                last = e
            time.sleep(3)
        raise SystemExit(f"::error::fleetkit server {self.url} is not reachable ({last})")

    def submit(self, body: dict[str, Any], deadline: float) -> dict[str, Any]:
        while True:
            code, r = self.call("POST", "/v1/deploys", body)
            if code == 202:
                return r
            if code == 409 and time.time() < deadline:
                job = (r.get("detail") or {}).get("job") if isinstance(r, dict) else None
                print(f"estate {body['estate']} is busy (job {job}); waiting")
                time.sleep(15)
                continue
            raise SystemExit(f"::error::deploy request for {body['estate']} refused: {code} {r}")

    def follow(self, jid: str, deadline: float) -> dict[str, Any]:
        after = 0
        while time.time() < deadline:
            code, r = self.call("GET", f"/v1/deploys/{jid}/events?after={after}&wait=25", timeout=60)
            if code != 200:
                raise SystemExit(f"::error::events of job {jid}: {code} {r}")
            for e in r["events"]:
                show(e)
            after = r["next"]
            if r["state"] in TERMINAL and not r["events"]:
                break
        code, rec = self.call("GET", f"/v1/deploys/{jid}")
        if rec.get("state") not in TERMINAL:
            self.call("POST", f"/v1/deploys/{jid}/cancel")
            raise SystemExit(f"::error::job {jid} did not finish within the timeout; cancel requested")
        return rec


def show(e: dict[str, Any]) -> None:
    if e.get("kind") == "log":
        print(f"  {e.get('stage', ''):>6} | {e.get('line', '')}")
        return
    rest = {k: v for k, v in e.items() if k not in ("ts", "seq", "stage", "kind", "trace")}
    if e.get("kind") in ("error", "failed-step") or (e.get("kind") == "diagnostic" and rest.get("severity") == "error"):
        print(f"::error::[{e.get('stage')}] {json.dumps(rest)}")
    else:
        print(f"[{e.get('stage')}] {e.get('kind')} {json.dumps(rest) if rest else ''}")


def plan_lines(res: dict[str, Any], preview: bool) -> list[str]:
    """Every resource that changes, by op, and what the server's guard refuses
    (a failed, refused deploy carries the same lists in its result)."""
    plan, refused = res.get("plan") or {}, res.get("refused") or {}
    out: list[str] = []
    for stack in sorted({*plan, *refused}):
        changes, rs = plan.get(stack) or [], refused.get(stack) or []
        if not changes and not rs:
            continue
        out.append(f"**`{stack}`**")
        for c in changes:
            bits = []
            if c.get("diff"):
                bits.append("changes " + ", ".join(c["diff"]))
            if c.get("replaceReasons"):
                bits.append("replaced because of " + ", ".join(c["replaceReasons"]))
            out.append(f"- {c['op']}: `{c['key']}` (`{c['type']}`)" + (f" [{'; '.join(bits)}]" if bits else ""))
        for r in rs:
            word = "a deploy would refuse" if preview else "refused"
            need = f"needs `{r['flag']}`" if r.get("flag") else r.get("why", "")
            out.append(f"- ⛔ {word}: {r['op']} of `{r['key']}` (`{r['type']}`): {need}")
        out.append("")
    return out


def report(estate: str, rec: dict[str, Any], kind: str, sha: str, url: str | None) -> str:
    ok = rec.get("state") == "succeeded"
    res = rec.get("result") or {}
    lines = [f"### {'✅' if ok else '❌'} fleetkit {kind}: `{estate}` at `{sha[:10]}` ({rec.get('state')})", ""]
    infra = res.get("infra") or {}
    if infra:
        lines += ["| stack | " + " | ".join(OPS) + " | program |", "|---" * (len(OPS) + 2) + "|"]
        for st, c in sorted(infra.items()):
            prog = (res.get("programs") or {}).get(st, "")
            lines.append(f"| `{st}` | " + " | ".join(str(c.get(op, 0)) for op in OPS)
                         + f" | `{prog.rsplit('/', 1)[-1]}` |")
        lines.append("")
    lines += plan_lines(res, kind == "preview")
    if res.get("nixos"):
        lines.append("NixOS: " + ("built" if res["nixos"] == "built" else f"colmena apply {res['nixos']}"))
    if rec.get("error"):
        lines += ["", "```", str(rec["error"])[-1500:], "```"]
    lines += ["", f"job `{rec.get('id')}`" + (f" · [run]({url})" if url else "")]
    return "\n".join(lines)


def comment(repo: str, pr: int, estate: str, body: str) -> None:
    token = env("GITHUB_TOKEN")
    api = env("GITHUB_API_URL", "https://api.github.com")
    if not token:
        return
    tag = f"<!-- fleetkit:{estate} -->"
    text = f"{tag}\n{body}"
    code, comments = http("GET", f"{api}/repos/{repo}/issues/{pr}/comments?per_page=100", token)
    if code == 200:
        for c in comments:
            if tag in (c.get("body") or ""):
                code, r = http("PATCH", f"{api}/repos/{repo}/issues/comments/{c['id']}", token, {"body": text})
                break
        else:
            code, r = http("POST", f"{api}/repos/{repo}/issues/{pr}/comments", token, {"body": text})
    if code not in (200, 201):
        print(f"::warning::could not comment on #{pr} ({code}); does the workflow have pull-requests: write?")


def main() -> int:
    url, token = env("FLEETKIT_API_URL"), env("FLEETKIT_API_TOKEN")
    event_name = env("GITHUB_EVENT_NAME")
    ev = event_payload()
    pr = ev.get("pull_request") or {}
    if not token:
        fork = pr and (pr.get("head") or {}).get("repo", {}).get("full_name") != env("GITHUB_REPOSITORY")
        if fork:
            print("::notice::not previewed: GitHub gives no secrets to pull requests from forks")
            gh_output("result", "skipped")
            return 0
        print("::error::no api-token: set the FLEETKIT_API_TOKEN secret")
        return 1
    if not url:
        print("::error::no api-url")
        return 1
    mode = env("FLEETKIT_MODE", "auto")
    if mode == "auto":
        mode = "preview" if event_name in ("pull_request", "pull_request_target") else "deploy"
    if mode not in ("preview", "deploy"):
        print(f"::error::mode is {mode!r}; auto, preview or deploy")
        return 1
    sha = env("FLEETKIT_REV") or (pr.get("head") or {}).get("sha") or env("GITHUB_SHA")
    number = pr.get("number")
    estates = [e for e in re.split(r"[,\s]+", env("FLEETKIT_ESTATES")) if e]
    if not estates:
        print("::error::no estates")
        return 1
    stacks = [s for s in re.split(r"[,\s]+", env("FLEETKIT_STACKS")) if s] or None
    timeout = int(env("FLEETKIT_TIMEOUT", "3600") or 3600)
    run_url = None
    if env("GITHUB_RUN_ID"):
        run_url = f"{env('GITHUB_SERVER_URL', 'https://github.com')}/{env('GITHUB_REPOSITORY')}/actions/runs/{env('GITHUB_RUN_ID')}"

    srv = Server(url, token)
    srv.wait_up()
    records: dict[str, Any] = {}
    for estate in estates:
        deadline = time.time() + timeout
        body = {"estate": estate, "rev": sha, "preview": mode == "preview", "pr": number,
                "infra": flag("FLEETKIT_INFRA", True), "nixos": flag("FLEETKIT_NIXOS", True),
                "goal": env("FLEETKIT_GOAL", "switch") or "switch"}
        if env("FLEETKIT_HIVE"):
            body["hive"] = env("FLEETKIT_HIVE")
        if stacks:
            body["stacks"] = stacks
        for op in ("replace", "delete", "update"):
            keys = [k for k in re.split(r"[,\s]+", env(f"FLEETKIT_ALLOW_{op.upper()}")) if k]
            if keys:
                body[f"allow_{op}"] = keys
        print(f"::group::fleetkit {mode} {estate} at {sha[:12]}")
        job = srv.submit(body, deadline)
        print(f"job {job['id']}")
        rec = srv.follow(job["id"], deadline)
        print("::endgroup::")
        records[estate] = rec
        md = report(estate, rec, mode, sha, run_url)
        summary(md + "\n")
        if number and flag("FLEETKIT_COMMENT", True):
            comment(env("GITHUB_REPOSITORY"), number, estate, md)
        if rec.get("state") != "succeeded":
            print(f"::error::fleetkit {mode} of {estate}: {rec.get('state')}: {rec.get('error') or ''}"[:2000])
    ok = all(r.get("state") == "succeeded" for r in records.values())
    gh_output("result", "success" if ok else "failure")
    gh_output("jobs", json.dumps(records))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
