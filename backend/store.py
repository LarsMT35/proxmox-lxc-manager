"""SQLite persistence: wizard jobs, per-step logs, update history."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

DB_PATH = os.environ.get("LXC_COMMANDER_DB", "/var/lib/lxc-commander/commander.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    ctid INTEGER NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    created REAL NOT NULL,
    finished REAL,
    summary TEXT
);
CREATE TABLE IF NOT EXISTS job_log (
    job_id TEXT NOT NULL,
    ts REAL NOT NULL,
    step TEXT,
    line TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS update_history (
    ctid INTEGER NOT NULL,
    ts REAL NOT NULL,
    packages INTEGER,
    security INTEGER,
    result TEXT
);
CREATE INDEX IF NOT EXISTS idx_log_job ON job_log(job_id);
"""


class Store:
    def __init__(self, path: str = DB_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(_SCHEMA)
        self.db.commit()

    def _exec(self, sql: str, args: tuple = ()) -> None:
        with self._lock:
            self.db.execute(sql, args)
            self.db.commit()

    # jobs ---------------------------------------------------------------
    def job_create(self, job_id: str, ctid: int, kind: str) -> None:
        self._exec("INSERT INTO jobs VALUES (?,?,?,?,?,NULL,NULL)",
                   (job_id, ctid, kind, "running", time.time()))

    def job_finish(self, job_id: str, state: str, summary: dict) -> None:
        self._exec("UPDATE jobs SET state=?, finished=?, summary=? WHERE id=?",
                   (state, time.time(), json.dumps(summary), job_id))

    def log(self, job_id: str, step: str, line: str) -> None:
        self._exec("INSERT INTO job_log VALUES (?,?,?,?)",
                   (job_id, time.time(), step, line))

    def job_log(self, job_id: str) -> list[dict]:
        cur = self.db.execute(
            "SELECT ts, step, line FROM job_log WHERE job_id=? ORDER BY ts", (job_id,))
        return [{"ts": r[0], "step": r[1], "line": r[2]} for r in cur.fetchall()]

    def jobs_for(self, ctid: int, limit: int = 20) -> list[dict]:
        cur = self.db.execute(
            "SELECT id, kind, state, created, finished, summary FROM jobs "
            "WHERE ctid=? ORDER BY created DESC LIMIT ?", (ctid, limit))
        return [{"id": r[0], "kind": r[1], "state": r[2], "created": r[3],
                 "finished": r[4], "summary": json.loads(r[5]) if r[5] else None}
                for r in cur.fetchall()]

    def history_add(self, ctid: int, packages: int, security: int, result: str) -> None:
        self._exec("INSERT INTO update_history VALUES (?,?,?,?,?)",
                   (ctid, time.time(), packages, security, result))
