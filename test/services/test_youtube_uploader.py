import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.services.youtube_uploader import (
    UploadTracker,
    YouTubeUploader,
    build_publish_plan,
    move_uploaded_task,
    scan_pending_videos,
)


class TestYouTubeTracker(unittest.TestCase):
    def test_two_tracker_instances_allow_only_one_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "youtube.json")
            trackers = [UploadTracker(path), UploadTracker(path)]
            barrier = threading.Barrier(2)
            results = []

            def claim(tracker):
                barrier.wait()
                results.append(bool(tracker.claim("task-1", 2, "Subject", "final-2.mp4")))

            threads = [threading.Thread(target=claim, args=(tracker,)) for tracker in trackers]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            data = json.loads(Path(path).read_text(encoding="utf-8"))

        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(data["uploads"]), 1)
        self.assertEqual(data["uploads"][0]["status"], "uploading")

    def test_corrupt_tracker_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "youtube.json"
            path.write_text("{invalid", encoding="utf-8")
            tracker = UploadTracker(str(path))
            with self.assertRaisesRegex(RuntimeError, "corrupt"):
                tracker.load()
            with self.assertRaisesRegex(RuntimeError, "corrupt"):
                tracker.claim("task-1", 1, "Subject", "final.mp4")

    def test_only_claim_owner_can_release_or_finalize(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = UploadTracker(str(Path(directory) / "youtube.json"))
            token = tracker.claim("task-1", 1, "Subject", "final.mp4")
            self.assertIsNotNone(token)
            self.assertFalse(tracker.release("task-1", 1, "wrong", "failure"))
            self.assertIsNone(tracker.finalize("task-1", 1, "wrong", "completed"))
            entry = tracker.finalize("task-1", 1, token, "completed", youtube_id="video-1")

        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["youtube_id"], "video-1")
        self.assertNotIn("claim_token", entry)


class TestYouTubeTaskMove(unittest.TestCase):
    @patch("app.services.youtube_uploader.utils.storage_dir")
    @patch("app.services.youtube_uploader.utils.task_dir")
    def test_rejects_task_path_traversal(self, task_dir, storage_dir):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = root / "tasks"
            uploaded = root / "uploaded"
            tasks.mkdir()
            uploaded.mkdir()
            task_dir.return_value = str(tasks)
            storage_dir.return_value = str(uploaded)

            with self.assertRaisesRegex(ValueError, "invalid task_id"):
                move_uploaded_task("../outside")

            self.assertTrue(tasks.is_dir())


class TestYouTubePublishPlan(unittest.TestCase):
    def test_twenty_five_videos_over_five_days_can_share_21_hours(self):
        plan = build_publish_plan(
            total_videos=25,
            schedule_mode="daily_block",
            start_date="2030-07-20",
            schedule_at="21:00",
            videos_per_day=5,
            interval_minutes=0,
            timezone_name="UTC",
            now=datetime(2030, 7, 1, tzinfo=timezone.utc),
            allow_shared_publish_time=True,
        )

        self.assertEqual(len(plan), 25)
        self.assertEqual(len({item["local_date"] for item in plan}), 5)
        for day_offset in range(5):
            daily = plan[day_offset * 5 : (day_offset + 1) * 5]
            self.assertEqual([item["local_time"] for item in daily], ["21:00"] * 5)

    def test_daily_block_restarts_selected_hour_on_each_date(self):
        plan = build_publish_plan(
            total_videos=10,
            schedule_mode="daily_block",
            start_date="2030-07-20",
            schedule_at="21:00",
            videos_per_day=5,
            interval_minutes=5,
            timezone_name="UTC",
            now=datetime(2030, 7, 1, tzinfo=timezone.utc),
        )

        self.assertEqual(
            [(item["local_date"], item["local_time"]) for item in plan],
            [
                ("2030-07-20", "21:00"),
                ("2030-07-20", "21:05"),
                ("2030-07-20", "21:10"),
                ("2030-07-20", "21:15"),
                ("2030-07-20", "21:20"),
                ("2030-07-21", "21:00"),
                ("2030-07-21", "21:05"),
                ("2030-07-21", "21:10"),
                ("2030-07-21", "21:15"),
                ("2030-07-21", "21:20"),
            ],
        )

    def test_daily_block_keeps_new_videos_on_same_date_when_first_slot_is_occupied(self):
        plan = build_publish_plan(
            total_videos=3,
            schedule_mode="daily_block",
            start_date="2030-07-14",
            schedule_at="21:00",
            videos_per_day=3,
            interval_minutes=5,
            timezone_name="UTC",
            now=datetime(2030, 7, 1, tzinfo=timezone.utc),
            occupied_publish_at={"2030-07-14T21:00:00Z"},
            occupied_counts_toward_daily_capacity=False,
        )

        self.assertEqual(
            [(item["local_date"], item["local_time"]) for item in plan],
            [
                ("2030-07-14", "21:05"),
                ("2030-07-14", "21:10"),
                ("2030-07-14", "21:15"),
            ],
        )

    def test_daily_block_uses_selected_time_between_videos(self):
        plan = build_publish_plan(
            total_videos=3,
            schedule_mode="daily_block",
            start_date="2030-07-14",
            schedule_at="21:00",
            videos_per_day=3,
            interval_minutes=5,
            timezone_name="UTC",
            now=datetime(2030, 7, 1, tzinfo=timezone.utc),
        )

        self.assertEqual(
            [item["local_time"] for item in plan],
            ["21:00", "21:05", "21:10"],
        )

    def test_daily_block_distributes_sixteen_videos_over_four_days(self):
        plan = build_publish_plan(
            total_videos=16,
            schedule_mode="daily_block",
            start_date="2030-07-14",
            schedule_at="21:00",
            videos_per_day=4,
            timezone_name="UTC",
            now=datetime(2030, 7, 1, tzinfo=timezone.utc),
        )

        self.assertEqual(len(plan), 16)
        self.assertEqual(
            [(item["local_date"], item["local_time"]) for item in plan[:4]],
            [
                ("2030-07-14", "21:00"),
                ("2030-07-14", "21:15"),
                ("2030-07-14", "21:30"),
                ("2030-07-14", "21:45"),
            ],
        )
        self.assertEqual(plan[-1]["local_date"], "2030-07-17")
        self.assertEqual(plan[-1]["local_time"], "21:45")
        self.assertTrue(all(item["videos_on_date"] == 4 for item in plan))

    def test_daily_block_keeps_cadence_on_partial_last_day(self):
        plan = build_publish_plan(
            total_videos=10,
            schedule_mode="daily_block",
            start_date="2030-07-14",
            schedule_at="21:00",
            videos_per_day=4,
            timezone_name="UTC",
            now=datetime(2030, 7, 1, tzinfo=timezone.utc),
        )

        self.assertEqual([(item["local_date"], item["local_time"]) for item in plan[-2:]], [
            ("2030-07-16", "21:00"),
            ("2030-07-16", "21:15"),
        ])
        self.assertEqual(plan[-1]["videos_on_date"], 2)

    def test_interval_mode_preserves_continuous_spacing(self):
        plan = build_publish_plan(
            total_videos=4,
            schedule_mode="interval",
            start_date="2030-07-14",
            schedule_at="21:00",
            interval_minutes=30,
            timezone_name="UTC",
            now=datetime(2030, 7, 1, tzinfo=timezone.utc),
        )
        self.assertEqual([item["local_time"] for item in plan], ["21:00", "21:30", "22:00", "22:30"])

    def test_existing_timestamp_is_skipped(self):
        plan = build_publish_plan(
            total_videos=2,
            schedule_mode="daily_block",
            start_date="2030-07-14",
            schedule_at="21:00",
            videos_per_day=4,
            timezone_name="UTC",
            now=datetime(2030, 7, 1, tzinfo=timezone.utc),
            occupied_publish_at={"2030-07-14T21:00:00Z"},
        )
        self.assertEqual([item["local_time"] for item in plan], ["21:15", "21:30"])

    def test_explicit_past_start_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "future"):
            build_publish_plan(
                total_videos=1,
                schedule_mode="daily_block",
                start_date="2030-07-14",
                schedule_at="21:00",
                timezone_name="UTC",
                now=datetime(2030, 7, 15, tzinfo=timezone.utc),
            )

    def test_santiago_timezone_observes_seasonal_offsets(self):
        winter = build_publish_plan(
            total_videos=1,
            schedule_mode="daily_block",
            start_date="2030-07-14",
            schedule_at="21:00",
            timezone_name="America/Santiago",
            now=datetime(2030, 7, 1, tzinfo=timezone.utc),
        )[0]
        summer = build_publish_plan(
            total_videos=1,
            schedule_mode="daily_block",
            start_date="2030-01-14",
            schedule_at="21:00",
            timezone_name="America/Santiago",
            now=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )[0]

        self.assertEqual(winter["publish_at"], "2030-07-15T01:00:00Z")
        self.assertEqual(summer["publish_at"], "2030-01-15T00:00:00Z")

    @patch("googleapiclient.http.MediaFileUpload")
    def test_scheduled_upload_is_private_and_contains_publish_at(self, media_upload):
        uploader = YouTubeUploader()
        service = MagicMock()
        service.videos.return_value.insert.return_value.execute.return_value = {"id": "video-1"}
        uploader._get_service = MagicMock(return_value=service)
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "video.mp4"
            video.write_bytes(b"video")
            result = uploader.upload_video(
                str(video), "Title", publish_at="2030-07-15T01:00:00Z", privacy_status="public"
            )

        body = service.videos.return_value.insert.call_args.kwargs["body"]
        self.assertTrue(result["success"])
        self.assertEqual(body["status"]["privacyStatus"], "private")
        self.assertEqual(body["status"]["publishAt"], "2030-07-15T01:00:00Z")

    @patch("googleapiclient.http.MediaFileUpload")
    @patch("app.services.youtube_uploader.utils.task_dir")
    def test_managed_video_is_claimed_and_finalized_inside_upload(self, task_dir, media_upload):
        uploader = YouTubeUploader()
        service = MagicMock()
        service.videos.return_value.insert.return_value.execute.return_value = {"id": "video-2"}
        uploader._get_service = MagicMock(return_value=service)
        with tempfile.TemporaryDirectory() as directory:
            tasks = Path(directory) / "tasks"
            generated = tasks / "task-1"
            generated.mkdir(parents=True)
            video = generated / "final-2.mp4"
            video.write_bytes(b"video")
            task_dir.return_value = str(tasks)
            tracker = UploadTracker(str(Path(directory) / "youtube.json"))
            with patch("app.services.youtube_uploader.upload_tracker", tracker):
                first = uploader.upload_video(str(video), "Title")
                second = uploader.upload_video(str(video), "Title")
                entry = tracker.get_by_task_id("task-1", 2)

        self.assertTrue(first["success"])
        self.assertFalse(second["success"])
        self.assertTrue(second["skipped"])
        self.assertEqual(second["error"], "already_claimed")
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["youtube_id"], "video-2")
        self.assertEqual(service.videos.return_value.insert.return_value.execute.call_count, 1)

    def test_nonexistent_dst_time_does_not_create_duplicate_utc_slots(self):
        plan = build_publish_plan(
            total_videos=3,
            schedule_mode="interval",
            start_date="2027-03-14",
            schedule_at="01:30",
            interval_minutes=60,
            timezone_name="America/New_York",
            now=datetime(2027, 3, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(len({item["publish_at"] for item in plan}), 3)
        self.assertEqual([item["local_time"] for item in plan], ["01:30", "03:30", "04:30"])

    def test_noncanonical_existing_time_consumes_daily_capacity(self):
        plan = build_publish_plan(
            total_videos=2,
            schedule_mode="daily_block",
            start_date="2030-07-14",
            schedule_at="21:00",
            videos_per_day=2,
            timezone_name="UTC",
            now=datetime(2030, 7, 1, tzinfo=timezone.utc),
            occupied_publish_at={"2030-07-14T21:07:00+00:00"},
        )
        self.assertEqual([item["local_date"] for item in plan], ["2030-07-14", "2030-07-15"])

    def test_explicit_batch_rejects_collision_instead_of_adding_another_day(self):
        with self.assertRaisesRegex(ValueError, "already occupied"):
            build_publish_plan(
                total_videos=24,
                schedule_mode="daily_block",
                start_date="2030-07-14",
                schedule_at="21:00",
                videos_per_day=6,
                timezone_name="UTC",
                now=datetime(2030, 7, 1, tzinfo=timezone.utc),
                occupied_publish_at={"2030-07-14T21:00:00Z"},
                occupied_counts_toward_daily_capacity=False,
                collision_policy="error",
            )

    @patch("app.services.youtube_uploader.utils.task_dir")
    def test_scanner_returns_every_output_index(self, task_dir):
        with tempfile.TemporaryDirectory() as directory:
            task_dir.return_value = directory
            generated = Path(directory) / "task-1"
            generated.mkdir()
            for index in (1, 2):
                with (generated / f"final-{index}.mp4").open("wb") as file:
                    file.seek(1024 * 1024)
                    file.write(b"x")
            tracker = UploadTracker(str(Path(directory) / "youtube-log.json"))
            with patch("app.services.youtube_uploader.upload_tracker", tracker):
                result = scan_pending_videos()
        self.assertEqual([video["index"] for video in result["videos"]], [1, 2])


if __name__ == "__main__":
    unittest.main()
