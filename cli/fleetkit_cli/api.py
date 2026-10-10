"""HTTP API over the same pipeline as the CLI.

  POST /v1/deploys                 start a deploy (DeployRequest) -> 202 job
  GET  /v1/deploys                 recent jobs
  GET  /v1/deploys/{id}            one job
  GET  /v1/deploys/{id}/events     events with seq >= ?after (long-polls ?wait s)
  GET  /v1/deploys/{id}/stream     the same as server-sent events, to the end
  POST /v1/deploys/{id}/cancel     ask a running job to stop (again: at once)
  GET  /v1/estates                 estates and their stacks (evaluates the repo)
  GET  /healthz                    liveness, no auth

With a repo (gitops.py):
  GET  /v1/gitops                  repo, branch, head, what each estate last had submitted
  POST /v1/gitops/sync             fetch now and deploy what changed
  POST /v1/hooks/github            GitHub webhook, push and pull_request (HMAC-signed, no bearer)

Every other /v1 route needs `Authorization: Bearer <token>`. A deploy of an
estate that is already deploying is 409, with the running job's id.

A token is the one unscoped token (everything) or a named, scoped one
(tokens.py). A scoped token's request is checked here, before a job exists:
outside its scope is 403 with the field that was refused, and nothing is
created. It sees the jobs and estates of its estates only (another estate's
job is 404, as if it did not exist), and the GitOps routes are closed to it.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import render
from . import tokens as tk
from .jobs import TERMINAL, BusyError, Job, JobManager
from .pipeline import DeployRequest
from .settings import Settings
from .tokens import Authenticator, Principal, Token


class RefusalDetail(BaseModel):
    error: str = Field(description="Why the request was refused.")
    field: Optional[str] = Field(
        default=None, description="The request field the token's scope refused (none: the route itself).")


class Refusal(BaseModel):
    detail: RefusalDetail


_REFUSED: dict[int | str, dict[str, Any]] = {403: {
    "model": Refusal,
    "description": "The token is scoped and its scope does not allow this request. `detail.field` names the "
                   "field that was refused. Nothing was done: no job was created.",
}}


def create_app(manager: JobManager, settings: Optional[Settings], token: Optional[str],
               gitops: Any = None, scoped: Optional[list[Token]] = None) -> FastAPI:
    """`token`: the unscoped token (None: none). `scoped`: the named tokens of
    FLEETKIT_API_TOKENS_FILE (tokens.load). Neither: no auth."""
    authn = Authenticator(token, scoped)
    app = FastAPI(title="fleetkit", version="0.1.0",
                  description="Deploy an estate: Pulumi for what exists, Colmena for what runs on it.")

    def auth(request: Request) -> Principal:
        p = authn.authenticate(request.headers.get("authorization", ""))
        if p is None:
            raise HTTPException(401, "bad or missing bearer token")
        return p

    def refuse(p: Principal, field: Optional[str], why: str) -> HTTPException:
        return HTTPException(403, {"error": f"token {p.name}: {why}", "field": field})

    def unscoped(p: Principal = Depends(auth)) -> Principal:
        if p.scope is not None:
            raise refuse(p, None, "a scoped token may not use the GitOps routes")
        return p

    def get(jid: str, p: Principal) -> Job:
        j = manager.jobs.get(jid)
        # Another estate's job does not exist for a scoped token: the same
        # answer as for an id that was never a job.
        if j is None or not p.sees(j.request.estate):
            raise HTTPException(404, f"no job {jid}")
        return j

    def pin(rev: Optional[str]) -> Optional[str]:
        return gitops.repo.commit_on(rev, [gitops.g.branch, *gitops.g.preview_branches])

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "running": len(manager.active)}

    @app.post("/v1/deploys", status_code=202, responses=_REFUSED)
    def deploy(req: DeployRequest, p: Principal = Depends(auth)) -> dict[str, Any]:
        try:
            req = tk.check(p, req, pin if gitops is not None else None)
        except tk.Refused as e:
            raise refuse(p, e.field, e.why)
        except Exception:  # noqa: BLE001 - only a scoped token gets here: git's own words stay with the server
            raise HTTPException(502, "the repo could not be fetched, so the rev was not checked; no job was created")
        try:
            return manager.submit(req, by=p.name).record()
        except BusyError as e:
            raise HTTPException(409, {"error": f"estate {req.estate} is deploying", "job": str(e)})

    @app.get("/v1/deploys")
    def deploys(limit: int = 50, p: Principal = Depends(auth)) -> list[dict[str, Any]]:
        return [j.record() for j in [j for j in manager.list() if p.sees(j.request.estate)][:limit]]

    @app.get("/v1/deploys/{jid}")
    def one(jid: str, p: Principal = Depends(auth)) -> dict[str, Any]:
        return get(jid, p).record()

    @app.get("/v1/deploys/{jid}/events")
    async def events(jid: str, after: int = 0, wait: float = 0, p: Principal = Depends(auth)) -> dict[str, Any]:
        j = get(jid, p)
        evs = await asyncio.to_thread(j.wait_events, after, min(max(wait, 0), 30))
        return {"state": j.state, "events": evs, "next": after + len(evs)}

    @app.get("/v1/deploys/{jid}/stream")
    async def stream(jid: str, after: int = 0, p: Principal = Depends(auth)) -> StreamingResponse:
        j = get(jid, p)

        async def gen():
            seq = after
            while True:
                evs = await asyncio.to_thread(j.wait_events, seq, 15)
                for e in evs:
                    yield f"id: {e['seq']}\ndata: {json.dumps(e)}\n\n"
                seq += len(evs)
                if not evs:
                    yield ": keepalive\n\n"
                if j.state in TERMINAL and seq >= len(j.events):
                    yield f"event: end\ndata: {json.dumps(j.record())}\n\n"
                    return

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/v1/deploys/{jid}/cancel")
    def cancel(jid: str, p: Principal = Depends(auth)) -> dict[str, Any]:
        get(jid, p)
        j = manager.cancel(jid)
        assert j is not None
        return j.record()

    @app.get("/v1/estates")
    def estates(p: Principal = Depends(auth)) -> dict[str, list[str]]:
        if settings is None:
            raise HTTPException(503, "no estate repo configured")
        try:
            s = settings
            if gitops is not None:
                s = settings.at(gitops.repo.checkout(gitops.repo.resolve(None)))
            return {e: st for e, st in render.estates(s).items() if p.sees(e)}
        except Exception as e:  # noqa: BLE001 - reported to the caller
            # An evaluation error can quote any part of the repo: not to a scoped token.
            raise HTTPException(500, str(e) if p.scope is None else "the estates could not be evaluated")

    if gitops is not None:
        g = gitops

        @app.get("/v1/gitops", dependencies=[Depends(unscoped)], responses=_REFUSED)
        def gitops_status() -> dict[str, Any]:
            return g.status()

        @app.post("/v1/gitops/sync", dependencies=[Depends(unscoped)], responses=_REFUSED)
        async def gitops_sync() -> dict[str, Any]:
            try:
                return await asyncio.to_thread(g.sync, "api")
            except Exception as e:  # noqa: BLE001 - reported to the caller
                raise HTTPException(502, str(e))

        @app.post("/v1/hooks/github")
        async def github_hook(request: Request) -> dict[str, Any]:
            secret = g.g.webhook_secret
            if not secret:
                raise HTTPException(404, "no webhook secret configured")
            body = await request.body()
            want = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(request.headers.get("x-hub-signature-256", ""), want):
                raise HTTPException(401, "bad signature")
            event = request.headers.get("x-github-event", "")
            if event == "ping":
                return {"ok": True}
            if event == "pull_request":
                try:
                    return await asyncio.to_thread(g.on_pull_request, json.loads(body))
                except Exception as e:  # noqa: BLE001 - reported to the caller
                    raise HTTPException(502, str(e))
            if event != "push":
                return {"ignored": f"event {event}"}
            payload = json.loads(body)
            if payload.get("ref") != f"refs/heads/{g.g.branch}":
                return {"ignored": f"ref {payload.get('ref')}"}
            try:
                return await asyncio.to_thread(g.sync, "github push", payload.get("after"))
            except Exception as e:  # noqa: BLE001 - reported to the caller
                raise HTTPException(502, str(e))

    return app
