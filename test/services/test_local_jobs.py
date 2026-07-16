import contextvars
import threading

import pytest
from loguru import logger

from app.config import config
from app.services.local_jobs import JobAlreadyRunningError, LocalJobRunner


def test_job_propagates_context_and_config_snapshot():
    runner = LocalJobRunner(max_workers=1)
    marker = contextvars.ContextVar("local_job_test_marker", default="missing")
    marker.set("captured")
    snapshot = config.snapshot_runtime_config()
    snapshot["app"]["local_job_snapshot_test"] = "snapshot"
    try:
        job = runner.submit(
            "session",
            "job",
            lambda: (marker.get(), config.app["local_job_snapshot_test"]),
            config_snapshot=snapshot,
        )
        snapshot["app"]["local_job_snapshot_test"] = "changed-after-submit"

        assert job.result() == ("captured", "snapshot")
        assert job.state == "completed"
        assert "local_job_snapshot_test" not in config.app
    finally:
        runner.shutdown()


def test_job_rejects_second_active_submit_for_same_session():
    runner = LocalJobRunner(max_workers=2)
    release = threading.Event()
    started = threading.Event()

    def wait_for_release():
        started.set()
        release.wait(timeout=2)

    try:
        first = runner.submit(
            "session", "first", wait_for_release, config_snapshot={}
        )
        assert started.wait(timeout=1)
        with pytest.raises(JobAlreadyRunningError):
            runner.submit("session", "second", lambda: None, config_snapshot={})
        release.set()
        first.result(timeout=1)
    finally:
        release.set()
        runner.shutdown()


def test_job_log_queue_is_bounded_and_keeps_latest_records():
    runner = LocalJobRunner(max_workers=1, max_logs=2)

    def emit_logs():
        logger.info("one")
        logger.info("two")
        logger.info("three")

    try:
        job = runner.submit("session", "job", emit_logs, config_snapshot={})
        job.result()
        assert job.drain_logs() == ["two", "three"]
        assert job.drain_logs() == []
    finally:
        runner.shutdown()


def test_global_job_capacity_is_bounded_across_sessions():
    runner = LocalJobRunner(max_workers=1, max_jobs=1)
    release = threading.Event()
    started = threading.Event()

    def wait_for_release():
        started.set()
        release.wait(timeout=2)

    try:
        first = runner.submit(
            "session-1", "first", wait_for_release, config_snapshot={}
        )
        assert started.wait(timeout=1)
        with pytest.raises(JobAlreadyRunningError, match="queue is full"):
            runner.submit("session-2", "second", lambda: None, config_snapshot={})
        release.set()
        first.result(timeout=1)
    finally:
        release.set()
        runner.shutdown()


def test_failed_job_exposes_exception_and_can_be_discarded():
    runner = LocalJobRunner(max_workers=1)

    def fail():
        raise ValueError("broken")

    try:
        job = runner.submit("session", "job", fail, config_snapshot={})
        with pytest.raises(ValueError, match="broken"):
            job.result()
        assert job.state == "failed"
        assert runner.discard("session", "job")
        assert runner.get("session") is None
    finally:
        runner.shutdown()
