"""HTTP API over the same pipeline as the CLI.

  POST /v1/deploys                 start a deploy (DeployRequest) -> 202 job
  GET  /v1/deploys                 recent jobs
  GET  /v1/deploys/{id}            one job
  GET  /v1/deploys/{id}/events     events with seq >= ?after (long-polls ?wait s)
  GET  /v1/deploys/{id}/stream     the same as server-sent events, to the end
  POST /v1/deploys/{id}/cancel     ask a running job to stop
  GET  /v1/estates                 estates and their stacks (evaluates the repo)
  GET  /healthz                    liveness, no auth

With a repo (gitops.py):
  GET  /v1/gitops                  repo, branch, head, what each estate last had submitted
  POST /v1/gitops/sync             fetch now and deploy what changed
  POST /v1/hooks/github            a GitHub push webhook (HMAC-signed, no bearer)

Every other /v1 route needs `Authorization: Bearer <token>`. A deploy of an
estate that is already deploying is 409, with the running job's id.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from . import render
from .jobs import TERMINAL, BusyError, Job, JobManager
from .pipeline import DeployRequest
from .settings import Settings


def create_app(manager: JobManager, settings: Optional[Settings], token: Optional[str],
               gitops: Any = None) -> FastAPI:
    app = FastAPI(title="fleetkit", version="0.1.0",
                  description="Deploy an estate: Pulumi for what exists, Colmena for what runs on it.")

    def auth(request: Request) -> None:
        if token is None:
            return
        got = request.headers.get("authorization", "")
        if not hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
            raise HTTPException(401, "bad or missing bearer token")

    def get(jid: str) -> Job:
        j = manager.jobs.get(jid)
        if j is None:
            raise HTTPException(404, f"no job {jid}")
        return j

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "running": len(manager.active)}

    @app.post("/v1/deploys", status_code=202, dependencies=[Depends(auth)])
    def deploy(req: DeployRequest) -> dict[str, Any]:
        try:
            return manager.submit(req).record()
        except BusyError as e:
            raise HTTPException(409, {"error": f"estate {req.estate} is deploying", "job": str(e)})

    @app.get("/v1/deploys", dependencies=[Depends(auth)])
    def deploys(limit: int = 50) -> list[dict[str, Any]]:
        return [j.record() for j in manager.list()[:limit]]

    @app.get("/v1/deploys/{jid}", dependencies=[Depends(auth)])
    def one(jid: str) -> dict[str, Any]:
        return get(jid).record()

    @app.get("/v1/deploys/{jid}/events", dependencies=[Depends(auth)])
    async def events(jid: str, after: int = 0, wait: float = 0) -> dict[str, Any]:
        j = get(jid)
        evs = await asyncio.to_thread(j.wait_events, after, min(max(wait, 0), 30))
        return {"state": j.state, "events": evs, "next": after + len(evs)}

    @app.get("/v1/deploys/{jid}/stream", dependencies=[Depends(auth)])
    async def stream(jid: str, after: int = 0) -> StreamingResponse:
        j = get(jid)

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

    @app.post("/v1/deploys/{jid}/cancel", dependencies=[Depends(auth)])
    def cancel(jid: str) -> dict[str, Any]:
        get(jid)
        j = manager.cancel(jid)
        assert j is not None
        return j.record()

    @app.get("/v1/estates", dependencies=[Depends(auth)])
    def estates() -> dict[str, list[str]]:
        if settings is None:
            raise HTTPException(503, "no estate repo configured")
        try:
            s = settings
            if gitops is not None:
                s = settings.at(gitops.repo.checkout(gitops.repo.resolve(None)))
            return render.estates(s)
        except Exception as e:  # noqa: BLE001 - reported to the caller
            raise HTTPException(500, str(e))

    if gitops is not None:
        g = gitops

        @app.get("/v1/gitops", dependencies=[Depends(auth)])
        def gitops_status() -> dict[str, Any]:
            return g.status()

        @app.post("/v1/gitops/sync", dependencies=[Depends(auth)])
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
