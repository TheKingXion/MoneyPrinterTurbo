"""Thread-safe local background jobs for UI integrations."""

import atexit
import copy
import contextvars
import queue
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from loguru import logger

from app.config import config


class JobAlreadyRunningError(RuntimeError):
    """Raised when a session tries to start a second active job."""


@dataclass
class LocalJob:
    session_id: str
    job_id: str
    _logs: queue.Queue[str]
    _future: Future | None = field(default=None, init=False, repr=False)
    created_at: float = field(default_factory=time.monotonic)
    completed_at: float | None = field(default=None, init=False)

    @property
    def future(self) -> Future | None:
        return self._future

    @property
    def done(self) -> bool:
        return self._future is not None and self._future.done()

    @property
    def state(self) -> str:
        future = self._future
        if future is None or future.running():
            return "running"
        if not future.done():
            return "pending"
        return "failed" if future.exception() is not None else "completed"

    def result(self, timeout: float | None = None) -> Any:
        if self._future is None:
            raise RuntimeError("job has not been submitted")
        return self._future.result(timeout=timeout)

    def append_log(self, message: object) -> None:
        text = str(message).rstrip()
        if not text:
            return
        while True:
            try:
                self._logs.put_nowait(text)
                return
            except queue.Full:
                try:
                    self._logs.get_nowait()
                except queue.Empty:
                    pass

    def drain_logs(self) -> list[str]:
        records = []
        while True:
            try:
                records.append(self._logs.get_nowait())
            except queue.Empty:
                return records


class LocalJobRunner:
    """Run at most one background job per local UI session."""

    def __init__(
        self, *, max_workers: int = 2, max_logs: int = 1000,
        max_jobs: int = 8, completed_ttl: float = 3600.0,
    ):
        if max_workers < 1 or max_logs < 1 or max_jobs < 1 or completed_ttl <= 0:
            raise ValueError("local job limits must be positive")
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="mpt-local-job"
        )
        self._max_logs = max_logs
        self._max_jobs = max_jobs
        self._completed_ttl = completed_ttl
        self._jobs: dict[str, LocalJob] = {}
        self._lock = threading.RLock()

    def submit(
        self,
        session_id: str,
        job_id: str,
        function: Callable[[], Any],
        *,
        config_snapshot: dict,
    ) -> LocalJob:
        """Submit a callable with the caller context and an isolated config view."""
        caller_context = contextvars.copy_context()
        snapshot = copy.deepcopy(config_snapshot)
        with self._lock:
            self._prune_locked()
            existing = self._jobs.get(session_id)
            if existing is not None and not existing.done:
                raise JobAlreadyRunningError(
                    f"session {session_id!r} already has an active job"
                )
            if len(self._jobs) >= self._max_jobs and session_id not in self._jobs:
                raise JobAlreadyRunningError("local generation queue is full")

            job = LocalJob(session_id, job_id, queue.Queue(maxsize=self._max_logs))
            self._jobs[session_id] = job
            job._future = self._executor.submit(
                caller_context.run,
                self._run,
                job,
                function,
                snapshot,
            )
            job._future.add_done_callback(
                lambda _future, tracked_job=job: self._mark_completed(tracked_job)
            )
            return job

    def _mark_completed(self, job: LocalJob) -> None:
        with self._lock:
            job.completed_at = time.monotonic()

    def _prune_locked(self) -> None:
        now = time.monotonic()
        expired = [
            session_id
            for session_id, job in self._jobs.items()
            if job.done and job.completed_at is not None
            and now - job.completed_at >= self._completed_ttl
        ]
        for session_id in expired:
            del self._jobs[session_id]

    @staticmethod
    def _run(job: LocalJob, function: Callable[[], Any], snapshot: dict) -> Any:
        def capture(message):
            job.append_log(message)

        with config.use_runtime_config(snapshot), logger.contextualize(
            local_job_id=job.job_id
        ):
            handler_id = logger.add(
                capture,
                format="{message}",
                filter=lambda record: record["extra"].get("local_job_id")
                == job.job_id,
            )
            try:
                return function()
            finally:
                try:
                    logger.remove(handler_id)
                except ValueError:
                    pass

    def get(self, session_id: str) -> LocalJob | None:
        with self._lock:
            self._prune_locked()
            return self._jobs.get(session_id)

    def discard(self, session_id: str, job_id: str) -> bool:
        """Forget a completed job after its result has been consumed."""
        with self._lock:
            job = self._jobs.get(session_id)
            if job is None or job.job_id != job_id or not job.done:
                return False
            del self._jobs[session_id]
            return True

    def shutdown(self, *, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=not wait)


local_job_runner = LocalJobRunner()
atexit.register(local_job_runner.shutdown, wait=False)
