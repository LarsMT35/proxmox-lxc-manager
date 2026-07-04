"""Guided update wizard.

A wizard job is a sequence of steps executed against one container.
Safety model:
  * Per-container lock  – one job per CT at a time.
  * Exclusive-group lock – e.g. both DNS containers share one lock, so
    AdGuard and Unbound can never be updated/rebooted simultaneously.
  * Critical semaphore  – at most N (default 1) critical containers at once.
  * confirm-steps        – the wizard STOPS and waits for the user before
    any state-changing command; the exact command is shown first.

Client protocol (WebSocket, JSON):
  server -> client : {type: state|line|step|confirm_required|done, ...}
  client -> server : {type: confirm|pause|resume|abort|ack}
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from .config import Config, ContainerMeta
from .proxmox import APT_ENV, Proxmox
from .store import Store


@dataclass
class Step:
    key: str
    title: str
    command: str | None = None          # command shown & executed inside CT
    confirm: bool = False               # requires explicit user confirmation
    checkbox: str | None = None         # extra confirmation text (e.g. backup)
    optional: bool = False              # user may skip
    host_action: str | None = None      # "reboot" | "snapshot" instead of exec
    destructive: bool = False


@dataclass
class Job:
    id: str
    ctid: int
    meta: ContainerMeta
    steps: list[Step]
    state: str = "pending"              # pending|running|paused|waiting_confirm|done|failed|aborted
    current: int = -1
    step_states: list[str] = field(default_factory=list)
    subscribers: list[Any] = field(default_factory=list)   # asyncio.Queue
    _confirm: asyncio.Event = field(default_factory=asyncio.Event)
    _resume: asyncio.Event = field(default_factory=asyncio.Event)
    _skip: bool = False
    _aborted: bool = False

    def __post_init__(self) -> None:
        self.step_states = ["pending"] * len(self.steps)
        self._resume.set()

    # ----- events to UI ----------------------------------------------------
    async def emit(self, msg: dict) -> None:
        msg["job_id"] = self.id
        for q in list(self.subscribers):
            await q.put(msg)

    def snapshot(self) -> dict:
        return {
            "id": self.id, "ctid": self.ctid, "state": self.state,
            "current": self.current,
            "steps": [{"key": s.key, "title": s.title, "command": s.command,
                       "confirm": s.confirm, "checkbox": s.checkbox,
                       "optional": s.optional, "state": st}
                      for s, st in zip(self.steps, self.step_states)],
        }

    # ----- controls from UI -------------------------------------------------
    def confirm(self, skip: bool = False) -> None:
        self._skip = skip
        self._confirm.set()

    def pause(self) -> None:
        self._resume.clear()
        self.state = "paused"

    def resume(self) -> None:
        self._resume.set()
        if self.state == "paused":
            self.state = "running"

    def abort(self) -> None:
        self._aborted = True
        self._confirm.set()
        self._resume.set()


def build_update_steps(meta: ContainerMeta, snapshot_hint: bool) -> list[Step]:
    steps: list[Step] = [
        Step("status", "System-Status prüfen",
             command="uptime -p && df -h / | tail -1 && . /etc/os-release && echo $PRETTY_NAME"),
    ]
    if snapshot_hint and meta.is_critical:
        steps.append(Step(
            "snapshot", "Snapshot erstellen (empfohlen)",
            host_action="snapshot", confirm=True, optional=True,
            checkbox=f"Ich habe verstanden: {meta.name} ist KRITISCH. "
                     "Snapshot vor dem Update wird dringend empfohlen."))
    steps.append(Step("apt_update", "Paketquellen aktualisieren (apt update)",
                      command=f"{APT_ENV} apt-get update"))
    steps.append(Step("list", "Verfügbare Upgrades anzeigen",
                      command="apt list --upgradable 2>/dev/null"))

    checkbox = None
    if "require_explicit_confirm" in meta.flags or "backup_reminder" in meta.flags:
        checkbox = (f"BACKUP-ERINNERUNG: Vor dem Update von {meta.name} muss ein "
                    "aktuelles Backup/Snapshot existieren. Ich bestätige das ausdrücklich.")
    steps.append(Step(
        "upgrade", "Upgrade ausführen (apt full-upgrade)",
        command=f"{APT_ENV} apt-get full-upgrade -y", confirm=True,
        checkbox=checkbox, destructive=True))

    steps.append(Step("autoremove", "Aufräumen (apt autoremove)",
                      command=f"{APT_ENV} apt-get autoremove -y", confirm=True,
                      destructive=True))

    if "validate_config" in meta.flags and meta.validate_command:
        steps.append(Step("validate", f"Konfiguration validieren ({meta.validate_command})",
                          command=meta.validate_command))

    steps.append(Step("reboot", "Container neu starten (optional)",
                      host_action="reboot", confirm=True, optional=True,
                      destructive=True))
    steps.append(Step("verify", "Abschluss-Verifikation",
                      command=". /etc/os-release && echo $PRETTY_NAME && "
                              "apt list --upgradable 2>/dev/null | tail -n +2 | wc -l"))
    return steps


class WizardManager:
    def __init__(self, cfg: Config, px: Proxmox, store: Store):
        self.cfg, self.px, self.store = cfg, px, store
        self.jobs: dict[str, Job] = {}
        self.ct_locks: dict[int, asyncio.Lock] = {}
        self.group_locks: dict[str, asyncio.Lock] = {}
        n = int(cfg.safety.get("max_parallel_critical", 1))
        self.critical_sem = asyncio.Semaphore(max(1, n))

    # ----- lock helpers ------------------------------------------------------
    def _ct_lock(self, ctid: int) -> asyncio.Lock:
        return self.ct_locks.setdefault(ctid, asyncio.Lock())

    def _group_lock(self, group: str | None) -> asyncio.Lock | None:
        if not group:
            return None
        return self.group_locks.setdefault(group, asyncio.Lock())

    def busy_reason(self, ctid: int) -> str | None:
        meta = self.cfg.meta(ctid)
        if self._ct_lock(ctid).locked():
            return f"CT {ctid} hat bereits einen laufenden Job."
        gl = self._group_lock(meta.group)
        if gl and gl.locked():
            members = self.cfg.group_members(meta.group)
            return (f"Gruppe '{meta.group}' ({members}) ist gesperrt – "
                    "Container derselben Gruppe werden nie gleichzeitig bearbeitet.")
        return None

    # ----- job lifecycle ------------------------------------------------------
    def start_update(self, ctid: int) -> Job:
        reason = self.busy_reason(ctid)
        if reason:
            raise RuntimeError(reason)
        meta = self.cfg.meta(ctid)
        job = Job(id=uuid.uuid4().hex[:12], ctid=ctid, meta=meta,
                  steps=build_update_steps(
                      meta, bool(self.cfg.safety.get("snapshot_before_critical", True))))
        self.jobs[job.id] = job
        self.store.job_create(job.id, ctid, "update")
        asyncio.create_task(self._run(job))
        return job

    async def _run(self, job: Job) -> None:
        meta = job.meta
        gl = self._group_lock(meta.group)
        async with self._ct_lock(job.ctid):
            if gl:
                await gl.acquire()
            if meta.is_critical:
                await self.critical_sem.acquire()
            try:
                await self._run_steps(job)
            finally:
                if meta.is_critical:
                    self.critical_sem.release()
                if gl:
                    gl.release()

    async def _run_steps(self, job: Job) -> None:
        job.state = "running"
        await job.emit({"type": "state", **job.snapshot()})
        failed = False
        for i, step in enumerate(job.steps):
            if job._aborted:
                break
            await job._resume.wait()
            job.current = i
            job.step_states[i] = "running"
            self.store.log(job.id, step.key, f"### STEP: {step.title}")

            # 1) show command, wait for confirmation if required
            shown = step.command or f"pct {step.host_action} {job.ctid}"
            await job.emit({"type": "step", "index": i, "key": step.key,
                            "title": step.title, "command": shown,
                            "state": "running"})
            if step.confirm:
                job.state = "waiting_confirm"
                job._confirm.clear()
                await job.emit({"type": "confirm_required", "index": i,
                                "command": shown, "checkbox": step.checkbox,
                                "optional": step.optional})
                await job._confirm.wait()
                job.state = "running"
                if job._aborted:
                    break
                if job._skip:
                    job._skip = False
                    job.step_states[i] = "skipped"
                    self.store.log(job.id, step.key, "-> übersprungen")
                    await job.emit({"type": "step", "index": i, "state": "skipped"})
                    continue

            # 2) execute
            loop = asyncio.get_running_loop()

            def on_line(line: str, _i: int = i, _k: str = step.key) -> None:
                self.store.log(job.id, _k, line)
                loop.create_task(job.emit({"type": "line", "index": _i, "line": line}))

            self.store.log(job.id, step.key, f"$ {shown}")
            await job.emit({"type": "line", "index": i, "line": f"$ {shown}"})

            if step.host_action == "reboot":
                res = await self.px.reboot(job.ctid)
                for line in (res.stdout + res.stderr).splitlines():
                    on_line(line)
            elif step.host_action == "snapshot":
                name = f"pre-update-{time.strftime('%Y%m%d-%H%M%S')}"
                res = await self.px.snapshot(job.ctid, name)
                on_line(f"Snapshot: {name}")
                for line in (res.stdout + res.stderr).splitlines():
                    on_line(line)
            else:
                res = await self.px.exec_stream(job.ctid, step.command or "true", on_line)

            ok = res.ok
            job.step_states[i] = "success" if ok else "failed"
            await job.emit({"type": "step", "index": i,
                            "state": job.step_states[i], "rc": res.returncode})
            if not ok and not step.optional:
                failed = True
                break

        job.state = ("aborted" if job._aborted else
                     "failed" if failed else "done")
        summary = {"steps": job.step_states}
        self.store.job_finish(job.id, job.state, summary)
        self.store.history_add(job.ctid, 0, 0, job.state)
        await job.emit({"type": "done", "state": job.state, **job.snapshot()})
