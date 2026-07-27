"""SQLite-backed generation queue with restart recovery and bounded workers."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from loguru import logger

from app.config import config
from app.models.schema import VideoParams
from app.utils import utils


class JobAlreadyRunningError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DurableJob:
    queue: "DurableGenerationQueue"
    session_id: str
    job_id: str
    _last_log_id: int = field(default=0, repr=False)

    @property
    def _record(self) -> dict[str, Any]:
        record = self.queue.get_record(self.job_id)
        return record or {"state": "failed", "error": "durable job record missing"}

    @property
    def done(self) -> bool:
        return self._record["state"] in {"completed", "failed", "cancelled"}

    @property
    def state(self) -> str:
        return self._record["state"]

    def result(self, timeout: float | None = None) -> Any:
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.done:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(self.job_id)
            time.sleep(0.05)
        record = self._record
        if record["state"] != "completed":
            raise RuntimeError(record.get("error") or f"job {record['state']}")
        return json.loads(record.get("result_json") or "null")

    def drain_logs(self) -> list[str]:
        rows = self.queue.read_logs(self.job_id, after_id=self._last_log_id)
        if rows:
            self._last_log_id = rows[-1][0]
        return [message for _, message in rows]


class DurableGenerationQueue:
    def __init__(self, *, max_workers: int = 2, max_jobs: int = 150):
        self.db_path = Path(utils.storage_dir("generation_queue.db"))
        self.max_workers = max(1, int(max_workers))
        self.max_jobs = max(1, int(max_jobs))
        self.worker_id = f"{os.getpid()}-{uuid4().hex[:8]}"
        self._stop = threading.Event()
        self._wake = threading.Condition()
        self._jobs: dict[tuple[str, str], DurableJob] = {}
        self._initialize()
        self._recover_abandoned()
        self._workers = [
            threading.Thread(
                target=self._worker_loop,
                name=f"mpt-durable-worker-{index + 1}",
                daemon=True,
            )
            for index in range(self.max_workers)
        ]
        for worker in self._workers:
            worker.start()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS generation_jobs (
                    job_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    campaign_id TEXT,
                    state TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 0,
                    params_json TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    stop_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    worker_id TEXT,
                    lease_until REAL,
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_generation_jobs_claim
                ON generation_jobs(state, priority DESC, created_at);
                CREATE INDEX IF NOT EXISTS idx_generation_jobs_session
                ON generation_jobs(session_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS generation_job_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    message TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_generation_logs_job
                ON generation_job_logs(job_id, id);
                CREATE TABLE IF NOT EXISTS generation_campaigns (
                    campaign_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    total_jobs INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def _recover_abandoned(self) -> None:
        now = time.time()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE generation_jobs
                SET state = CASE WHEN attempts < max_attempts THEN 'queued' ELSE 'failed' END,
                    error = CASE WHEN attempts < max_attempts
                        THEN 'worker interrupted; queued for recovery'
                        ELSE 'maximum recovery attempts exceeded' END,
                    worker_id = NULL, lease_until = NULL, updated_at = ?
                WHERE state = 'running' AND (lease_until IS NULL OR lease_until < ?)
                """,
                (_utc_now(), now),
            )

    def submit(
        self,
        session_id: str,
        job_id: str,
        params: VideoParams,
        *,
        config_snapshot: dict,
        stop_at: str = "video",
        priority: int = 0,
        campaign_id: str | None = None,
    ) -> DurableJob:
        now = _utc_now()
        params_json = params.model_dump_json()
        config_json = json.dumps(config_snapshot, ensure_ascii=False, default=str)
        with self._connection() as connection:
            active = connection.execute(
                """
                SELECT job_id FROM generation_jobs
                WHERE session_id = ? AND state IN ('queued', 'running')
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if active and active["job_id"] != job_id:
                raise JobAlreadyRunningError(
                    f"session {session_id!r} already has an active job"
                )
            if active and active["job_id"] == job_id:
                return self._jobs.setdefault(
                    (session_id, job_id), DurableJob(self, session_id, job_id)
                )
            queued_count = connection.execute(
                "SELECT COUNT(*) FROM generation_jobs WHERE state IN ('queued', 'running')"
            ).fetchone()[0]
            if queued_count >= self.max_jobs and not active:
                raise JobAlreadyRunningError("durable generation queue is full")
            connection.execute(
                """
                INSERT INTO generation_jobs (
                    job_id, session_id, campaign_id, state, priority,
                    params_json, config_json, stop_at, created_at, updated_at
                ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    session_id=excluded.session_id,
                    campaign_id=excluded.campaign_id,
                    state='queued',
                    priority=excluded.priority,
                    params_json=excluded.params_json,
                    config_json=excluded.config_json,
                    stop_at=excluded.stop_at,
                    attempts=0,
                    result_json=NULL,
                    error=NULL,
                    worker_id=NULL,
                    lease_until=NULL,
                    updated_at=excluded.updated_at
                """,
                (
                    job_id,
                    session_id,
                    campaign_id,
                    int(priority),
                    params_json,
                    config_json,
                    stop_at,
                    now,
                    now,
                ),
            )
        job = self._jobs.setdefault(
            (session_id, job_id), DurableJob(self, session_id, job_id)
        )
        with self._wake:
            self._wake.notify_all()
        return job

    def _claim(self) -> sqlite3.Row | None:
        lease_until = time.time() + 300
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM generation_jobs
                WHERE state = 'queued'
                ORDER BY priority DESC, created_at
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            updated = connection.execute(
                """
                UPDATE generation_jobs
                SET state='running', attempts=attempts+1, worker_id=?,
                    lease_until=?, updated_at=?
                WHERE job_id=? AND state='queued'
                """,
                (self.worker_id, lease_until, _utc_now(), row["job_id"]),
            ).rowcount
            connection.execute("COMMIT")
            return self.get_row(row["job_id"]) if updated else None

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            row = self._claim()
            if row is None:
                with self._wake:
                    self._wake.wait(timeout=1.0)
                continue
            self._execute(row)

    def _execute(self, row: sqlite3.Row) -> None:
        job_id = row["job_id"]
        worker_thread_id = threading.get_ident()
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(job_id, heartbeat_stop),
            name=f"mpt-job-heartbeat-{job_id[:8]}",
            daemon=True,
        )
        heartbeat.start()
        handler_id = logger.add(
            lambda message: self.append_log(job_id, str(message)),
            format="{message}",
            filter=lambda record: record["thread"].id == worker_thread_id,
        )
        try:
            from app.services import task

            params = VideoParams.model_validate_json(row["params_json"])
            snapshot = json.loads(row["config_json"])
            with config.use_runtime_config(snapshot), logger.contextualize(
                durable_job_id=job_id
            ):
                result = task.start(
                    task_id=job_id,
                    params=params,
                    stop_at=row["stop_at"],
                    suppress_youtube_upload=params.suppress_youtube_upload,
                    suppress_tiktok_upload=params.suppress_tiktok_upload,
                )
            if result is None:
                self._finish(
                    job_id,
                    "failed",
                    error="generation pipeline finished without a result",
                )
            else:
                self._finish(job_id, "completed", result=result)
        except Exception as exc:
            self.append_log(job_id, traceback.format_exc())
            self._finish(job_id, "failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=2)
            try:
                logger.remove(handler_id)
            except ValueError:
                pass

    def _heartbeat_loop(self, job_id: str, stop: threading.Event) -> None:
        while not stop.wait(60):
            try:
                with self._connection() as connection:
                    connection.execute(
                        """
                        UPDATE generation_jobs SET lease_until=?, updated_at=?
                        WHERE job_id=? AND state='running' AND worker_id=?
                        """,
                        (time.time() + 300, _utc_now(), job_id, self.worker_id),
                    )
            except sqlite3.Error as exc:
                logger.warning(f"generation queue heartbeat failed: {exc}")

    def _finish(
        self, job_id: str, state: str, *, result: Any = None, error: str | None = None
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE generation_jobs
                SET state=?, result_json=?, error=?, worker_id=NULL,
                    lease_until=NULL, updated_at=?
                WHERE job_id=?
                """,
                (
                    state,
                    json.dumps(result, ensure_ascii=False, default=str),
                    error,
                    _utc_now(),
                    job_id,
                ),
            )

    def append_log(self, job_id: str, message: str) -> None:
        text = str(message).rstrip()
        if not text:
            return
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO generation_job_logs(job_id, created_at, message) VALUES (?, ?, ?)",
                (job_id, _utc_now(), text),
            )
            connection.execute(
                """
                DELETE FROM generation_job_logs
                WHERE job_id=? AND id NOT IN (
                    SELECT id FROM generation_job_logs
                    WHERE job_id=? ORDER BY id DESC LIMIT 1000
                )
                """,
                (job_id, job_id),
            )

    def read_logs(self, job_id: str, *, after_id: int = 0) -> list[tuple[int, str]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, message FROM generation_job_logs
                WHERE job_id=? AND id>? ORDER BY id
                """,
                (job_id, int(after_id)),
            ).fetchall()
        return [(int(row["id"]), str(row["message"])) for row in rows]

    def get_row(self, job_id: str) -> sqlite3.Row | None:
        with self._connection() as connection:
            return connection.execute(
                "SELECT * FROM generation_jobs WHERE job_id=?", (job_id,)
            ).fetchone()

    def get_record(self, job_id: str) -> dict[str, Any] | None:
        row = self.get_row(job_id)
        return dict(row) if row else None

    def get(self, session_id: str) -> DurableJob | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT job_id FROM generation_jobs
                WHERE session_id=?
                ORDER BY
                    CASE state WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,
                    updated_at DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        key = (session_id, row["job_id"])
        return self._jobs.setdefault(key, DurableJob(self, *key))

    def discard(self, session_id: str, job_id: str) -> bool:
        self._jobs.pop((session_id, job_id), None)
        return True

    def cancel(self, job_id: str) -> bool:
        with self._connection() as connection:
            changed = connection.execute(
                """
                UPDATE generation_jobs SET state='cancelled', updated_at=?
                WHERE job_id=? AND state='queued'
                """,
                (_utc_now(), job_id),
            ).rowcount
        return bool(changed)

    def submit_campaign(
        self,
        title: str,
        jobs: list[tuple[str, VideoParams, dict]],
        *,
        stop_at: str = "video",
        priority: int = 0,
        campaign_id: str | None = None,
    ) -> str:
        """Queue up to 150 independently recoverable videos as one campaign."""
        if not jobs or len(jobs) > self.max_jobs:
            raise ValueError(f"campaign size must be between 1 and {self.max_jobs}")
        campaign_id = campaign_id or str(uuid4())
        now = _utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO generation_campaigns(
                    campaign_id, title, total_jobs, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (campaign_id, str(title).strip() or campaign_id, len(jobs), now, now),
            )
        for index, (task_id, params, snapshot) in enumerate(jobs):
            self.submit(
                session_id=f"campaign:{campaign_id}:{index}",
                job_id=task_id,
                params=params,
                config_snapshot=snapshot,
                stop_at=stop_at,
                priority=priority,
                campaign_id=campaign_id,
            )
        return campaign_id

    def campaign_status(self, campaign_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            campaign = connection.execute(
                "SELECT * FROM generation_campaigns WHERE campaign_id=?",
                (campaign_id,),
            ).fetchone()
            if campaign is None:
                return None
            rows = connection.execute(
                """
                SELECT state, COUNT(*) AS count FROM generation_jobs
                WHERE campaign_id=? GROUP BY state
                """,
                (campaign_id,),
            ).fetchall()
        counts = {row["state"]: int(row["count"]) for row in rows}
        completed = counts.get("completed", 0)
        failed = counts.get("failed", 0) + counts.get("cancelled", 0)
        total = int(campaign["total_jobs"])
        return {
            **dict(campaign),
            "counts": counts,
            "finished": completed + failed,
            "progress": round((completed + failed) * 100 / max(total, 1), 2),
        }

    def shutdown(self) -> None:
        self._stop.set()
        with self._wake:
            self._wake.notify_all()
        for worker in self._workers:
            worker.join(timeout=5)


durable_generation_queue = DurableGenerationQueue(
    max_workers=max(1, min(4, int(config.app.get("durable_generation_workers", 2) or 2))),
    max_jobs=max(150, int(config.app.get("max_queued_tasks", 150) or 150)),
)
