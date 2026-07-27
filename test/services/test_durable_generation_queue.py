import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from app.models.schema import VideoParams
from app.services.durable_generation_queue import DurableGenerationQueue


def test_durable_queue_executes_and_persists_result():
    with tempfile.TemporaryDirectory() as directory, patch(
        "app.services.durable_generation_queue.utils.storage_dir",
        return_value=str(Path(directory) / "queue.db"),
    ), patch("app.services.task.start", return_value={"videos": ["final.mp4"]}):
        queue = DurableGenerationQueue(max_workers=1, max_jobs=5)
        try:
            job = queue.submit(
                "session-1",
                "task-1",
                VideoParams(video_subject="test"),
                config_snapshot={},
            )
            deadline = time.monotonic() + 5
            while not job.done and time.monotonic() < deadline:
                time.sleep(0.05)
            assert job.result() == {"videos": ["final.mp4"]}
            assert queue.get_record("task-1")["state"] == "completed"
        finally:
            queue.shutdown()


def test_campaign_reports_aggregate_progress():
    with tempfile.TemporaryDirectory() as directory, patch(
        "app.services.durable_generation_queue.utils.storage_dir",
        return_value=str(Path(directory) / "queue.db"),
    ), patch("app.services.task.start", return_value={"videos": ["final.mp4"]}):
        queue = DurableGenerationQueue(max_workers=1, max_jobs=150)
        try:
            campaign_id = queue.submit_campaign(
                "Campaign",
                [
                    (f"task-{index}", VideoParams(video_subject=str(index)), {})
                    for index in range(3)
                ],
            )
            deadline = time.monotonic() + 5
            status = queue.campaign_status(campaign_id)
            while status["finished"] < 3 and time.monotonic() < deadline:
                time.sleep(0.05)
                status = queue.campaign_status(campaign_id)
            assert status["progress"] == 100
            assert status["counts"]["completed"] == 3
        finally:
            queue.shutdown()
