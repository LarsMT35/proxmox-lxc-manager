"""LXC Commander – FastAPI backend.

REST:
  GET  /api/containers                    inventory + status + criticality
  POST /api/containers/{id}/check         apt update + upgradable list
  POST /api/containers/{id}/snapshot      pct snapshot
  POST /api/containers/{id}/wizard        start guided update wizard
  POST /api/containers/{id}/adopt         persist a discovered container
  POST /api/update-all-safe               staged safe-mode batch update
  GET  /api/jobs/{job_id}                 job state
  GET  /api/jobs/{job_id}/log             persisted log
  GET  /api/containers/{id}/history       past jobs

WS:
  /ws/jobs/{job_id}?token=…               live steps/output + controls
"""
from __future__ import annotations

import asyncio
import os
import re
import time

from fastapi import (Depends, FastAPI, HTTPException, WebSocket,
                     WebSocketDisconnect)
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import ContainerMeta, load_config
from .proxmox import Proxmox
from .store import Store
from .wizard import WizardManager

cfg = load_config()
px = Proxmox(cfg)
store = Store()
wizards = WizardManager(cfg, px, store)
app = FastAPI(title="LXC Commander", version="1.0")

_update_cache: dict[int, dict] = {}
_bearer = HTTPBearer(auto_error=False)
TOKEN = cfg.server.get("auth_token") or ""


def auth(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    if not TOKEN or TOKEN == "CHANGE-ME-LONG-RANDOM-TOKEN":
        raise HTTPException(500, "auth_token in containers.yaml ist nicht gesetzt")
    if cred is None or cred.credentials != TOKEN:
        raise HTTPException(401, "invalid token")


# --------------------------------------------------------------------------
@app.get("/api/containers", dependencies=[Depends(auth)])
async def containers() -> list[dict]:
    base = await px.list_containers()
    known = {c["ctid"] for c in base}
    # also show configured containers that pct didn't list (e.g. host offline)
    for ctid in cfg.containers:
        if ctid not in known:
            base.append({"ctid": ctid, "status": "unknown", "name": None})

    async def enrich(c: dict) -> dict:
        meta = cfg.meta(c["ctid"])
        configured = c["ctid"] in cfg.containers
        out = {
            "ctid": c["ctid"],
            # discovered (unconfigured) containers keep their pct hostname
            "name": meta.name if configured else (c["name"] or meta.name),
            "configured": configured,
            "role": meta.role,
            "status": c["status"],
            "criticality": meta.criticality,
            "group": meta.group,
            "flags": meta.flags,
            "os": None, "uptime": None, "usage": None,
            "updates": _update_cache.get(c["ctid"], {"state": "unknown"}),
            "busy": wizards.busy_reason(c["ctid"]) is not None,
        }
        if c["status"] == "running":
            os_r, up, usage = await asyncio.gather(
                px.os_release(c["ctid"]), px.uptime(c["ctid"]),
                px.resource_usage(c["ctid"]))
            out.update(os=os_r, uptime=up, usage=usage)
        return out

    return list(await asyncio.gather(*(enrich(c) for c in base)))


@app.post("/api/containers/{ctid}/check", dependencies=[Depends(auth)])
async def check_updates(ctid: int) -> dict:
    result = await px.check_updates(ctid)
    result["checked_at"] = time.time()
    _update_cache[ctid] = result
    return result


@app.post("/api/containers/{ctid}/snapshot", dependencies=[Depends(auth)])
async def snapshot(ctid: int) -> dict:
    name = f"manual-{time.strftime('%Y%m%d-%H%M%S')}"
    res = await px.snapshot(ctid, name)
    if not res.ok:
        raise HTTPException(500, res.stderr[-500:] or "snapshot failed")
    return {"snapshot": name}


@app.post("/api/containers/{ctid}/wizard", dependencies=[Depends(auth)])
async def start_wizard(ctid: int) -> dict:
    try:
        job = wizards.start_update(ctid)
    except RuntimeError as e:            # lock / group conflict
        raise HTTPException(409, str(e))
    return job.snapshot()


class AdoptBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    role: str = Field(default="", max_length=64)
    criticality: str = "normal"
    group: str | None = None
    backup_reminder: bool = False
    validate_command: str | None = Field(default=None, max_length=200)


@app.post("/api/containers/{ctid}/adopt", dependencies=[Depends(auth)])
async def adopt_container(ctid: int, body: AdoptBody) -> dict:
    """Persist a container discovered via `pct list` into containers.yaml
    so criticality, groups and flags survive restarts."""
    if ctid in cfg.containers:
        raise HTTPException(409, f"CT {ctid} ist bereits konfiguriert.")
    if body.criticality not in ("critical", "normal", "low"):
        raise HTTPException(422, "criticality muss critical|normal|low sein")
    group = (body.group or "").strip() or None
    if group and not re.fullmatch(r"[a-z0-9_\-]{1,32}", group):
        raise HTTPException(422, "Gruppenname: nur a-z 0-9 _ - (max. 32 Zeichen)")
    flags: list[str] = []
    if body.backup_reminder:
        flags += ["require_explicit_confirm", "backup_reminder"]
    validate_command = (body.validate_command or "").strip() or None
    if validate_command:
        flags.append("validate_config")
    meta = ContainerMeta(ctid=ctid, name=body.name.strip(),
                         role=body.role.strip(), criticality=body.criticality,
                         group=group, flags=flags,
                         validate_command=validate_command)
    try:
        cfg.adopt_container(meta)
    except OSError as e:
        raise HTTPException(500, f"Konfiguration konnte nicht gespeichert werden: {e}")
    return {"ctid": ctid, "name": meta.name, "criticality": meta.criticality,
            "group": meta.group, "flags": meta.flags, "configured": True}


@app.post("/api/update-all-safe", dependencies=[Depends(auth)])
async def update_all_safe() -> dict:
    """Staged plan: non-critical first, then critical – strictly sequential,
    DNS containers never in the same stage. Returns the plan; each container
    still runs through its own wizard with all confirmations."""
    non_crit = sorted(c for c, m in cfg.containers.items() if not m.is_critical)
    crit = sorted(c for c, m in cfg.containers.items() if m.is_critical)
    # Entdeckte, (noch) nicht konfigurierte Container zählen als unkritisch –
    # aber nur wenn sie laufen.
    listed = await px.list_containers()
    discovered = sorted(
        c["ctid"] for c in listed
        if c["ctid"] not in cfg.containers and c["status"] == "running")
    non_crit = sorted(set(non_crit) | set(discovered))
    names = {c["ctid"]: c["name"] for c in listed if c["name"]}

    def name_of(ctid: int) -> str:
        return cfg.meta(ctid).name if ctid in cfg.containers \
            else names.get(ctid, f"CT {ctid}")

    # Stage 1: alle nicht-kritischen (sequenziell). Danach bekommt JEDER
    # kritische Container eine eigene Stufe – dadurch sind Container einer
    # exclusive_group (z.B. beide DNS) automatisch nie in derselben Stufe.
    stages: list[list[int]] = [non_crit] + [[c] for c in crit]
    return {"plan": [{"stage": i + 1,
                      "containers": [{"ctid": c, "name": name_of(c)} for c in s]}
                     for i, s in enumerate(stages) if s],
            "note": "Jede Stufe einzeln per Wizard starten – Bestätigungen bleiben Pflicht."}


@app.get("/api/jobs/{job_id}", dependencies=[Depends(auth)])
async def job_state(job_id: str) -> dict:
    job = wizards.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return job.snapshot()


@app.get("/api/jobs/{job_id}/log", dependencies=[Depends(auth)])
async def job_log(job_id: str) -> list[dict]:
    return store.job_log(job_id)


@app.get("/api/containers/{ctid}/history", dependencies=[Depends(auth)])
async def history(ctid: int) -> list[dict]:
    return store.jobs_for(ctid)


# ----- WebSocket ------------------------------------------------------------
@app.websocket("/ws/jobs/{job_id}")
async def ws_job(ws: WebSocket, job_id: str) -> None:
    if ws.query_params.get("token") != TOKEN:
        await ws.close(code=4401)
        return
    job = wizards.jobs.get(job_id)
    if not job:
        await ws.close(code=4404)
        return
    await ws.accept()
    q: asyncio.Queue = asyncio.Queue()
    job.subscribers.append(q)
    await ws.send_json({"type": "state", **job.snapshot()})

    async def sender() -> None:
        while True:
            await ws.send_json(await q.get())

    async def receiver() -> None:
        while True:
            msg = await ws.receive_json()
            t = msg.get("type")
            if t == "confirm":
                job.confirm(skip=bool(msg.get("skip")))
            elif t == "pause":
                job.pause()
            elif t == "resume":
                job.resume()
            elif t == "abort":
                job.abort()

    try:
        await asyncio.gather(sender(), receiver())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if q in job.subscribers:
            job.subscribers.remove(q)


# ----- static frontend --------------------------------------------------------
_frontend = os.path.join(os.path.dirname(__file__), "..", "frontend")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(_frontend, "index.html"))


app.mount("/static", StaticFiles(directory=_frontend), name="static")
