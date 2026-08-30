"""
Background jobs, so a batch can outlive the HTTP request that started it.

WHY THIS IS NOT JUST "RAISE max_rows"
-------------------------------------
Resolution costs ~5-10 seconds per name, because upstream rate limits are
honoured deliberately. So:

      25 names  ~  2.5 min
     250 names  ~   25 min
     500 names  ~   50 min

against a 120s gunicorn timeout. No batch size setting survives that. The work
has to move off the request:

    POST /api/bulk/submit   -> job_id, returns immediately
    GET  /api/bulk/status   -> progress, poll this
    GET  /api/bulk/result   -> xlsx / csv when done

Progress is written after every row, so a job that dies half way still hands
back the rows it finished rather than nothing.

HONEST LIMITS
-------------
This is an in-process thread pool with a SQLite store. That is the right size
for one operator running a few hundred names, and it is deliberately not a
Celery/Redis deployment.

Two things will bite on Render's free tier and are not bugs:
  * the instance sleeps after ~15 minutes idle, and a sleeping instance is not
    running your job. Keep the tab polling, or use a paid instance.
  * the filesystem is ephemeral, so a redeploy loses in-flight jobs.

For 10-15k names, run this locally. There is no HTTP timeout, no sleep, no
ephemeral disk, and the SQLite cache makes re-runs cheap.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS job (
    job_id TEXT PRIMARY KEY,
    state TEXT,                -- queued | running | done | failed | cancelled
    filename TEXT,
    total INTEGER,
    processed INTEGER,
    started_at TEXT,
    finished_at TEXT,
    error TEXT,
    counts TEXT
);
CREATE TABLE IF NOT EXISTS job_row (
    job_id TEXT, seq INTEGER, payload TEXT,
    PRIMARY KEY (job_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_job_row ON job_row(job_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class JobHandle:
    job_id: str
    total: int
    state: str = "queued"


class JobStore:
    """SQLite-backed job state. Thread-safe by explicit lock."""

    def __init__(self, path: str = "jobs.db"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._lock = threading.RLock()

    def create(self, filename: str, total: int) -> str:
        job_id = uuid.uuid4().hex[:16]
        with self._lock:
            self.conn.execute(
                "INSERT INTO job VALUES (?,?,?,?,?,?,?,?,?)",
                (job_id, "queued", filename, total, 0, _now(), None, None, "{}"))
            self.conn.commit()
        return job_id

    def set_state(self, job_id: str, state: str, error: str = "") -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE job SET state=?, error=?, finished_at=? WHERE job_id=?",
                (state, error or None,
                 _now() if state in ("done", "failed", "cancelled") else None,
                 job_id))
            self.conn.commit()

    def add_row(self, job_id: str, seq: int, row: dict, counts: dict) -> None:
        """Written after EVERY row, so a died job still yields partial output."""
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO job_row VALUES (?,?,?)",
                              (job_id, seq, json.dumps(row)))
            self.conn.execute(
                "UPDATE job SET processed=?, counts=? WHERE job_id=?",
                (seq + 1, json.dumps(counts), job_id))
            self.conn.commit()

    def status(self, job_id: str) -> Optional[dict]:
        with self._lock:
            r = self.conn.execute(
                "SELECT job_id,state,filename,total,processed,started_at,"
                "finished_at,error,counts FROM job WHERE job_id=?",
                (job_id,)).fetchone()
        if not r:
            return None
        total, processed = r[3] or 0, r[4] or 0
        return {"job_id": r[0], "state": r[1], "filename": r[2],
                "total": total, "processed": processed,
                "percent": round(100.0 * processed / total, 1) if total else 0.0,
                "started_at": r[5], "finished_at": r[6], "error": r[7],
                "counts": json.loads(r[8] or "{}")}

    def rows(self, job_id: str) -> list[dict]:
        with self._lock:
            rs = self.conn.execute(
                "SELECT payload FROM job_row WHERE job_id=? ORDER BY seq",
                (job_id,)).fetchall()
        return [json.loads(r[0]) for r in rs]

    def cancel(self, job_id: str) -> None:
        self.set_state(job_id, "cancelled")

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rs = self.conn.execute(
                "SELECT job_id FROM job ORDER BY started_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [self.status(r[0]) for r in rs]


class JobRunner:
    """
    One worker thread per job, capped. Deliberately small.

    max_concurrent is 1 by default: the rate limiter is global, so running two
    jobs at once does not go faster, it just makes both slower and doubles the
    chance of a 429.
    """

    def __init__(self, store: JobStore, max_concurrent: int = 1):
        self.store = store
        self.sem = threading.Semaphore(max_concurrent)
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()

    def cancel(self, job_id: str) -> None:
        with self._lock:
            self._cancelled.add(job_id)
        self.store.cancel(job_id)

    def _is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._cancelled

    def submit(self, job_id: str, rows: list, resolve_one: Callable,
               flatten: Callable) -> None:
        t = threading.Thread(target=self._run, daemon=True,
                             args=(job_id, rows, resolve_one, flatten))
        t.start()

    def _run(self, job_id: str, rows: list, resolve_one: Callable,
             flatten: Callable) -> None:
        with self.sem:
            self.store.set_state(job_id, "running")
            counts: dict[str, int] = {}
            try:
                for i, pr in enumerate(rows):
                    if self._is_cancelled(job_id):
                        self.store.set_state(job_id, "cancelled")
                        return
                    try:
                        result = resolve_one(pr)
                    except Exception as exc:               # noqa: BLE001
                        result = {"decision": "error", "error": str(exc)[:200]}
                    row = flatten(pr, result)
                    d = str(row.get("decision", "?"))
                    counts[d] = counts.get(d, 0) + 1
                    self.store.add_row(job_id, i, row, counts)
                self.store.set_state(job_id, "done")
            except Exception as exc:                       # noqa: BLE001
                self.store.set_state(job_id, "failed", str(exc)[:300])
