import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from app.services.tiktok_scheduler import TikTokScheduler
from app.services.social_video_scanner import scan_generated_videos
from app.services.tiktok_uploader import TikTokUploadTracker, TikTokUploader, parse_tiktok_metadata_file


class TestTikTokMetadata(unittest.TestCase):
    def test_parser_uses_tiktok_section_only(self):
        content = """# Metadatos

## TikTok

Título: Título TikTok

Descripción: Caption TikTok

Hashtags: #viral #FYP #viral

## YouTube Shorts

Título: Título YouTube
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "METADATOS.md"
            path.write_text(content, encoding="utf-8")
            metadata = parse_tiktok_metadata_file(str(path))
        self.assertEqual(metadata["title"], "Título TikTok")
        self.assertIn("Caption TikTok", metadata["caption"])
        self.assertEqual(metadata["hashtags"], ["#viral", "#FYP"])


class TestTikTokTracker(unittest.TestCase):
    def test_platform_log_is_persistent_and_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = TikTokUploadTracker(str(Path(directory) / "tiktok.json"))
            tracker.add_entry("task-1", 1, "Subject", "video.mp4", "processing", publish_id="pub-1")
            entry = tracker.get_by_task_id("task-1")
        self.assertEqual(entry["publish_id"], "pub-1")
        self.assertEqual(entry["status"], "processing")

    def test_two_tracker_instances_allow_only_one_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "tiktok.json")
            trackers = [TikTokUploadTracker(path), TikTokUploadTracker(path)]
            barrier = threading.Barrier(2)
            results = []

            def claim(tracker):
                barrier.wait()
                results.append(tracker.claim("task-1", 1, "Subject", "video.mp4", "official"))

            threads = [threading.Thread(target=claim, args=(tracker,)) for tracker in trackers]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(sorted(results), [False, True])

    def test_corrupt_tracker_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tiktok.json"
            path.write_text("{invalid", encoding="utf-8")
            tracker = TikTokUploadTracker(str(path))
            with self.assertRaisesRegex(RuntimeError, "corrupt"):
                tracker.load()


class TestTikTokOAuth(unittest.TestCase):
    def test_authorization_url_has_required_scopes_and_state(self):
        uploader = TikTokUploader()
        uploader.client_key = "client-key"
        uploader.client_secret = "client-secret"
        uploader.provider = "official"
        uploader.redirect_uri = "https://example.com/callback"
        uploader.sync_from_config = MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            uploader.redirect_uri = "http://127.0.0.1:8080/callback"
            uploader.state_dir = str(Path(directory) / "states")
            result = uploader.authorization_url()
            state_data = json.loads(Path(uploader._state_file(result["state"])).read_text(encoding="utf-8"))
        query = parse_qs(urlparse(result["authorization_url"]).query)
        self.assertEqual(query["client_key"], ["client-key"])
        self.assertIn("video.publish", query["scope"][0])
        self.assertTrue(result["state"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        expected_challenge = hashlib.sha256(state_data["code_verifier"].encode("ascii")).hexdigest()
        self.assertEqual(query["code_challenge"], [expected_challenge])
        self.assertRegex(query["code_challenge"][0], r"^[0-9a-f]{64}$")

    @patch("app.services.tiktok_uploader.requests.post")
    def test_exchange_code_sends_pkce_verifier(self, mock_post):
        uploader = TikTokUploader()
        uploader.client_key = "client-key"
        uploader.client_secret = "client-secret"
        uploader.provider = "official"
        uploader.redirect_uri = "http://127.0.0.1:8080/callback"
        uploader.sync_from_config = MagicMock()
        uploader.sync_from_disk = MagicMock()
        response = MagicMock()
        response.ok = True
        response.json.return_value = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "open_id": "open-id",
        }
        mock_post.return_value = response

        with tempfile.TemporaryDirectory() as directory:
            uploader.state_dir = str(Path(directory) / "states")
            uploader.token_path = str(Path(directory) / "token.json")
            uploader.token_lock_path = f"{uploader.token_path}.lock"
            authorization = uploader.authorization_url()
            verifier = json.loads(Path(uploader._state_file(authorization["state"])).read_text(encoding="utf-8"))["code_verifier"]
            result = uploader.exchange_code("authorization-code", authorization["state"])
            with self.assertRaisesRegex(RuntimeError, "already used"):
                uploader.exchange_code("authorization-code", authorization["state"])

        self.assertTrue(result["authorized"])
        self.assertEqual(mock_post.call_args.kwargs["data"]["code_verifier"], verifier)

    def test_token_is_rejected_after_client_key_change(self):
        uploader = TikTokUploader()
        uploader.provider = "official"
        uploader.client_key = "old-key"
        with tempfile.TemporaryDirectory() as directory:
            uploader.token_path = str(Path(directory) / "token.json")
            uploader.token_lock_path = f"{uploader.token_path}.lock"
            uploader._save_token({"access_token": "token", "expires_in": 3600})
            uploader.client_key = "new-key"
            self.assertFalse(uploader.is_authorized())

    def test_oauth_state_is_one_time_and_supports_parallel_flows(self):
        uploader = TikTokUploader()
        uploader.client_key = "client-key"
        uploader.client_secret = "client-secret"
        uploader.provider = "official"
        uploader.redirect_uri = "http://127.0.0.1:8080/callback"
        uploader.sync_from_config = MagicMock()
        uploader.sync_from_disk = MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            uploader.state_dir = str(Path(directory) / "states")
            first = uploader.authorization_url()
            second = uploader.authorization_url()
            self.assertTrue(Path(uploader._state_file(first["state"])).is_file())
            self.assertTrue(Path(uploader._state_file(second["state"])).is_file())

    @patch("app.services.tiktok_uploader.requests.put")
    @patch("app.services.tiktok_uploader.requests.post")
    def test_official_file_upload_returns_publish_id(self, mock_post, mock_put):
        uploader = TikTokUploader()
        uploader.sync_from_config = MagicMock()
        uploader.enabled = True
        uploader.provider = "official"
        uploader.privacy_level = "SELF_ONLY"
        uploader.allow_comments = True
        uploader.allow_duet = False
        uploader.allow_stitch = False
        uploader.remaining_upload_slots = MagicMock(return_value=5)
        uploader.creator_info = MagicMock(return_value={"privacy_level_options": ["SELF_ONLY"]})
        uploader._access_token = MagicMock(return_value="token")
        response = MagicMock()
        response.json.return_value = {
            "data": {"upload_url": "https://upload.example/video", "publish_id": "pub-1"},
            "error": {"code": "ok"},
        }
        mock_post.return_value = response
        mock_put.return_value = MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "video.mp4"
            video.write_bytes(b"video-bytes")
            result = uploader.upload_video(str(video), "Caption")
        self.assertTrue(result["success"])
        self.assertEqual(result["publish_id"], "pub-1")
        mock_put.assert_called_once()

    @patch("app.services.tiktok_uploader.requests.put")
    @patch("app.services.tiktok_uploader.requests.post")
    def test_trailing_bytes_are_merged_into_final_chunk(self, mock_post, mock_put):
        uploader = TikTokUploader()
        uploader.sync_from_config = MagicMock()
        uploader.enabled = True
        uploader.provider = "official"
        uploader.privacy_level = "SELF_ONLY"
        uploader.remaining_upload_slots = MagicMock(return_value=5)
        uploader.creator_info = MagicMock(return_value={"privacy_level_options": ["SELF_ONLY"]})
        uploader._access_token = MagicMock(return_value="token")
        response = MagicMock()
        response.json.return_value = {
            "data": {"upload_url": "https://upload.example/video", "publish_id": "pub-2"},
            "error": {"code": "ok"},
        }
        mock_post.return_value = response
        mock_put.return_value = MagicMock()
        size = 10 * 1024 * 1024 + 1
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "video.mp4"
            with video.open("wb") as file:
                file.seek(size - 1)
                file.write(b"x")
            result = uploader.upload_video(str(video), "Caption")
        source_info = mock_post.call_args.kwargs["json"]["source_info"]
        self.assertTrue(result["success"])
        self.assertEqual(source_info["total_chunk_count"], 1)
        self.assertEqual(mock_put.call_args.kwargs["headers"]["Content-Length"], str(size))

    def test_upload_post_completed_failure_is_not_published(self):
        result = TikTokUploader._normalize_upload_post_status(
            {
                "status": "completed",
                "results": [{"platform": "tiktok", "success": False, "error": "rejected"}],
            }
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "rejected")

    def test_upload_post_status_without_success_is_processing(self):
        result = TikTokUploader._normalize_upload_post_status({"status": "in_progress", "results": []})
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "processing")

    def test_upload_post_aggregate_failure_is_failed(self):
        result = TikTokUploader._normalize_upload_post_status({"status": "failed", "error": "provider down"})
        self.assertEqual(result["status"], "failed")

    @patch("app.services.tiktok_uploader.requests.post")
    def test_refresh_preserves_rotating_refresh_token_when_omitted(self, mock_post):
        uploader = TikTokUploader()
        uploader.client_key = "client-key"
        uploader.client_secret = "client-secret"
        response = MagicMock()
        response.ok = True
        response.json.return_value = {"access_token": "new-access", "expires_in": 3600}
        mock_post.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            uploader.token_path = str(Path(directory) / "token.json")
            uploader.token_lock_path = f"{uploader.token_path}.lock"
            uploader._save_token({"access_token": "old-access", "refresh_token": "keep-me", "expires_in": 1})
            refreshed = uploader.refresh_token()
        self.assertEqual(refreshed["refresh_token"], "keep-me")


class TestSocialVideoScanner(unittest.TestCase):
    @patch("app.services.social_video_scanner.utils.task_dir")
    def test_scanner_keeps_small_videos_and_sorts_numeric_indexes(self, task_dir):
        with tempfile.TemporaryDirectory() as directory:
            task_dir.return_value = directory
            generated = Path(directory) / "task-1"
            generated.mkdir()
            (generated / "final-10.mp4").write_bytes(b"ten")
            (generated / "final-2.mp4").write_bytes(b"two")
            result = scan_generated_videos([], lambda _: {}, "tiktok")
        self.assertEqual([video["index"] for video in result["videos"]], [2, 10])


class TestTikTokScheduler(unittest.TestCase):
    @patch("app.services.tiktok_scheduler.tiktok_upload_tracker")
    def test_schedule_can_be_added_and_cancelled(self, tracker):
        with tempfile.TemporaryDirectory() as directory:
            scheduler = TikTokScheduler(str(Path(directory) / "schedule.json"))
            job = scheduler.add_job(
                task_id="task-1",
                subject="Subject",
                video_path="video.mp4",
                caption="Caption",
                scheduled_at="2030-01-01T20:00:00+00:00",
                provider="official",
                privacy_level="SELF_ONLY",
                allow_comment=True,
                allow_duet=False,
                allow_stitch=False,
            )
            cancelled = scheduler.cancel(job["job_id"])
            saved = scheduler.load()[0]
        self.assertTrue(cancelled)
        self.assertEqual(saved["status"], "cancelled")
        tracker.update_status.assert_called_once()

    @patch("app.services.tiktok_scheduler.tiktok_uploader")
    @patch("app.services.tiktok_scheduler.tiktok_upload_tracker")
    def test_retry_remains_reserved_in_tracker(self, tracker, uploader):
        tracker.reserve_schedule.return_value = True
        uploader.upload_video.return_value = {"success": False, "error": "temporary", "retryable": True}
        with tempfile.TemporaryDirectory() as directory:
            scheduler = TikTokScheduler(str(Path(directory) / "schedule.json"))
            scheduler.add_job(
                task_id="task-1",
                subject="Subject",
                video_path="video.mp4",
                caption="Caption",
                scheduled_at="2020-01-01T20:00:00+00:00",
                provider="official",
                privacy_level="SELF_ONLY",
                allow_comment=True,
                allow_duet=False,
                allow_stitch=False,
            )
            scheduler.run_due_jobs()
            saved = scheduler.load()[0]
        self.assertEqual(saved["status"], "pending")
        statuses = [call.args[4] for call in tracker.add_entry.call_args_list]
        self.assertIn("scheduled_retry", statuses)


if __name__ == "__main__":
    unittest.main()
