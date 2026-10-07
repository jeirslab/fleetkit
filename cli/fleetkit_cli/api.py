"""HTTP API over the same pipeline as the CLI.

  POST /v1/deploys                 start a deploy (DeployRequest) -> 202 job
  GET  /v1/deploys                 recent jobs
  GET  /v1/deploys/{id}            one job
  GET  /v1/deploys/{id}/events     events with seq >= ?after (long-polls ?wait s)
  GET  /v1/deploys/{id}/stream     the same as server-sent events, to the end
  POST /v1/deploys/{id}/cancel     ask a running job to stop
  GET  /v1/estates                 estates and their stacks (evaluates the repo)
  GET  /healthz                    liveness, no auth

Every /v1 route needs `Authorization: Bearer <token>`. A deploy of an estate
that is already deploying is 409, with the running job's id.
"""
from __future__ import annotations

import asyncio
import hmac
import json
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from . import render
from .jobs import TERMINAL, BusyError, Job, JobManager
from .pipeline import DeployRequest
from .settings import Settings


def create_app(manager: JobManager, settings: Optional[Settings], token: Optional[str]) -> FastAPI:
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
            return render.estates(settings)
        except render.RenderError as e:
            raise HTTPException(500, str(e))

    return app
