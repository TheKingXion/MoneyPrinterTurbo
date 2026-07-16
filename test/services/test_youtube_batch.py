import tempfile
import unittest
import json
import os
from datetime import datetime, timezone
from unittest.mock import patch

from app.services.youtube_batch import (
    SCHEMA_VERSION,
    YouTubeBatchStore,
    audit_story_idea,
    build_unique_batch_subjects,
    idea_similarity,
    normalize_idea_text,
    validate_unique_ideas,
    videos_per_day_for_days,
)
from app.services.youtube_batch_runner import (
    YouTubeBatchRunner,
    _default_upload,
    classify_upload_failure,
)
from app.services.youtube_uploader import build_publish_plan


class TestYouTubeBatch(unittest.TestCase):
    def test_story_quality_prefers_concrete_transformation_over_generic_mystery(self):
        concrete = audit_story_idea(
            "Una joven recolectó libros usados, creó una biblioteca para su comunidad y la reconstruyó después de que la lluvia dañara los estantes.",
            "Los libros que la lluvia no pudo borrar",
        )
        generic = audit_story_idea(
            "La historia desconocida de una ciudad olvidada con tecnología imposible",
            "El secreto que cambió la historia",
        )

        self.assertEqual(concrete["status"], "approved")
        self.assertEqual(generic["status"], "review")
        self.assertGreater(concrete["score"], generic["score"])

    def test_semantic_duplicate_detection_normalizes_accents_and_paraphrases(self):
        self.assertEqual(normalize_idea_text("  BIBLIOTÉCA, comunitaria! "), "biblioteca comunitaria")
        first = "Una joven convirtió libros usados en una biblioteca comunitaria"
        second = "Creó una estantería gratuita con libros desechados para unir a su barrio"

        audit = validate_unique_ideas([second], [first])

        self.assertTrue(audit[0]["duplicate"])
        self.assertGreaterEqual(idea_similarity(first, second), 0.78)

    def test_duplicate_normalization_supports_non_latin_text(self):
        self.assertEqual(normalize_idea_text("女孩修复社区图书馆"), "女孩修复社区图书馆")
        self.assertTrue(
            validate_unique_ideas(["女孩修复社区图书馆"], ["女孩修复社区图书馆"])[0]["duplicate"]
        )

    def test_shared_template_with_different_beneficiary_is_not_duplicate(self):
        rural = "Una médica crea una clínica móvil para niños rurales"
        urban = "Una médica crea una clínica móvil para presos urbanos"

        self.assertLess(idea_similarity(rural, urban), 0.74)
        self.assertFalse(validate_unique_ideas([urban], [rural])[0]["duplicate"])

    def test_bulk_duplicate_scan_skips_unrelated_sequence_comparisons(self):
        existing = [f"conceptoexclusivo{index}" for index in range(500)]
        candidates = [f"propuestaunica{index}" for index in range(200)]

        with patch("app.services.youtube_batch.SequenceMatcher") as matcher:
            results = validate_unique_ideas(candidates, existing)

        self.assertFalse(any(row["duplicate"] for row in results))
        matcher.assert_not_called()

    def test_store_persists_title_override_and_rejects_repeated_premise(self):
        with tempfile.TemporaryDirectory() as directory:
            store = YouTubeBatchStore(directory)
            first = store.create(
                ["Una mujer convierte ropa usada en abrigos para su comunidad"],
                [],
                {},
                title_overrides=["La ropa que volvió a proteger a todo un barrio"],
                idea_mode="ai",
            )
            with self.assertRaisesRegex(ValueError, "Duplicate batch idea"):
                store.create(
                    ["Con ropa desechada creó abrigos para ayudar a su barrio"],
                    [],
                    {},
                )

        self.assertEqual(first["schema_version"], SCHEMA_VERSION)
        self.assertEqual(first["idea_mode"], "ai")
        self.assertEqual(
            first["items"][0]["title_override"],
            "La ropa que volvió a proteger a todo un barrio",
        )
    def test_builds_exact_requested_count_after_preferred_pool_is_exhausted(self):
        preferred = ["Used title", "Available title"]
        subjects = build_unique_batch_subjects(
            24,
            existing_subjects={"Used title"},
            preferred_subjects=preferred,
        )
        self.assertEqual(len(subjects), 24)
        self.assertEqual(len({subject.casefold() for subject in subjects}), 24)
        self.assertEqual(subjects[0], "Available title")
        self.assertNotIn("Used title", subjects)

    def test_calculates_six_videos_per_day_for_twenty_four_over_four_days(self):
        self.assertEqual(videos_per_day_for_days(24, 4), 6)

        plan = build_publish_plan(
            total_videos=24,
            schedule_mode="daily_block",
            start_date="2030-07-14",
            schedule_at="21:00",
            videos_per_day=videos_per_day_for_days(24, 4),
            interval_minutes=10,
            timezone_name="UTC",
            now=datetime(2030, 7, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(len({item["local_date"] for item in plan}), 4)
        self.assertEqual(
            [item["local_time"] for item in plan[:6]],
            ["21:00", "21:10", "21:20", "21:30", "21:40", "21:50"],
        )

    def test_uses_partial_final_day(self):
        self.assertEqual(videos_per_day_for_days(10, 4), 3)

    def test_rejects_more_days_than_videos(self):
        with self.assertRaises(ValueError):
            videos_per_day_for_days(3, 4)

    def test_persists_batch_items_and_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            store = YouTubeBatchStore(directory)
            plan = [{"publish_at": "2030-01-01T00:00:00Z"}]
            batch = store.create(["Subject"], plan, {"total_days": 1})
            store.update_item(
                batch,
                0,
                generation_status="generated",
                video_path="video.mp4",
            )
            loaded = store.load(batch["batch_id"])
            listed = store.list_batches()

        self.assertEqual(loaded["requested"], 1)
        self.assertEqual(loaded["items"][0]["generation_status"], "generated")
        self.assertEqual(loaded["items"][0]["video_path"], "video.mp4")
        self.assertEqual(listed[0]["batch_id"], batch["batch_id"])

    def test_save_retries_transient_windows_replace_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            store = YouTubeBatchStore(directory)
            real_replace = os.replace
            attempts = 0

            def flaky_replace(source, destination):
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    error = PermissionError(13, "manifest is temporarily locked")
                    error.winerror = 5
                    raise error
                return real_replace(source, destination)

            with (
                patch(
                    "app.services.youtube_batch.os.replace",
                    side_effect=flaky_replace,
                ),
                patch("app.services.youtube_batch.time.sleep") as sleep,
            ):
                batch = store.create(["Subject"], [], {})

            self.assertEqual(attempts, 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(store.load(batch["batch_id"])["batch_id"], batch["batch_id"])
            self.assertFalse(any(name.endswith(".tmp") for name in os.listdir(directory)))

    def test_failed_replace_preserves_manifest_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            store = YouTubeBatchStore(directory)
            batch = store.create(["Subject"], [], {})
            batch["status"] = "generating"
            locked = PermissionError(13, "manifest remains locked")
            locked.winerror = 5

            with (
                patch(
                    "app.services.youtube_batch.os.replace",
                    side_effect=locked,
                ),
                patch("app.services.youtube_batch.time.sleep"),
                self.assertRaises(PermissionError),
            ):
                store.save(batch)

            self.assertEqual(store.load(batch["batch_id"])["status"], "pending")
            self.assertFalse(any(name.endswith(".tmp") for name in os.listdir(directory)))

    def test_manifest_has_compatible_and_durable_slots(self):
        with tempfile.TemporaryDirectory() as directory:
            store = YouTubeBatchStore(directory)
            slot = {"publish_at": "2030-01-01T00:00:00Z"}
            batch = store.create(["Subject"], [slot], {}, execution_mode="interleaved")
            store.update_item(batch, 0, current_publish_slot={"publish_at": "2030-01-02T00:00:00Z"})
            loaded = store.load(batch["batch_id"])

            self.assertEqual(loaded["schema_version"], SCHEMA_VERSION)
            self.assertEqual(loaded["items"][0]["original_publish_slot"], slot)
            self.assertEqual(loaded["items"][0]["publish_slot"], loaded["items"][0]["current_publish_slot"])
            with self.assertRaises(ValueError):
                store.update_item(loaded, 0, original_publish_slot={"publish_at": "changed"})

    def test_scheduled_batch_requires_slots_and_reserves_them(self):
        with tempfile.TemporaryDirectory() as directory:
            store = YouTubeBatchStore(directory)
            with self.assertRaisesRegex(ValueError, "one publish slot"):
                store.create(["First unique subject"], [], {"scheduled": True})

            store.create(
                ["First unique subject"],
                [{"publish_at": "2030-01-01T21:00:00Z"}],
                {"scheduled": True},
            )
            with self.assertRaisesRegex(ValueError, "already reserved"):
                store.create(
                    ["A completely different second subject"],
                    [{"publish_at": "2030-01-01T21:00:00Z"}],
                    {"scheduled": True},
                )

    def test_shared_publish_time_allows_multiple_items_at_same_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            store = YouTubeBatchStore(directory)
            batch = store.create(
                ["First distinct story", "Second unrelated story"],
                [
                    {"publish_at": "2030-01-01T21:00:00Z"},
                    {"publish_at": "2030-01-01T21:00:00Z"},
                ],
                {"scheduled": True, "allow_shared_publish_time": True},
            )

        self.assertEqual(
            [item["current_publish_slot"]["publish_at"] for item in batch["items"]],
            ["2030-01-01T21:00:00Z", "2030-01-01T21:00:00Z"],
        )

    def test_load_migrates_old_manifest_without_rewriting_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            store = YouTubeBatchStore(directory)
            batch_id = "legacy"
            legacy = {
                "batch_id": batch_id,
                "status": "pending",
                "settings": {},
                "items": [{"publish_slot": {"publish_at": "later"}}],
            }
            with open(store._path(batch_id), "w", encoding="utf-8") as file:
                json.dump(legacy, file)
            loaded = store.load(batch_id)

        self.assertEqual(loaded["schema_version"], SCHEMA_VERSION)
        self.assertEqual(loaded["execution_mode"], "interleaved")
        self.assertEqual(loaded["idea_mode"], "legacy")
        self.assertEqual(loaded["items"][0]["generation_status"], "pending")
        self.assertEqual(loaded["items"][0]["upload_status"], "pending")
        self.assertEqual(loaded["items"][0]["title_override"], "")
        self.assertEqual(loaded["items"][0]["original_publish_slot"], legacy["items"][0]["publish_slot"])

    def test_load_rejects_manifest_from_newer_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            store = YouTubeBatchStore(directory)
            with open(store._path("future"), "w", encoding="utf-8") as file:
                json.dump({"schema_version": SCHEMA_VERSION + 1, "batch_id": "future"}, file)
            with self.assertRaisesRegex(ValueError, "Unsupported YouTube batch schema"):
                store.load("future")

    def test_interleaved_generates_then_uploads_each_item(self):
        events = []

        def generate(task_id, params):
            events.append(("generate", params.video_subject))
            return {"videos": [f"{params.video_subject}.mp4"]}

        def upload(item, slot, settings):
            events.append(("upload", item["subject"]))
            return {"success": True, "video_id": item["subject"]}

        with tempfile.TemporaryDirectory() as directory:
            store = YouTubeBatchStore(directory)
            batch = store.create(["one", "two"], [], {"video_params": {}}, "interleaved")
            result = YouTubeBatchRunner(store, generate, upload).run(batch["batch_id"])

        self.assertEqual(events, [("generate", "one"), ("upload", "one"), ("generate", "two"), ("upload", "two")])
        self.assertEqual(result["status"], "completed")

    def test_batch_generation_forces_one_output_per_subject(self):
        counts = []

        def generate(task_id, params):
            counts.append(params.video_count)
            return {"videos": ["one.mp4"]}

        with tempfile.TemporaryDirectory() as directory:
            store = YouTubeBatchStore(directory)
            batch = store.create(
                ["one"], [], {"video_params": {"video_count": 5}}
            )
            YouTubeBatchRunner(
                store, generate, lambda item, slot, settings: {"success": True}
            ).run(batch["batch_id"])

        self.assertEqual(counts, [1])

    def test_batch_applies_shared_script_settings_to_each_subject(self):
        received = []

        def generate(task_id, params):
            received.append(
                (
                    params.video_subject,
                    params.video_script_prompt,
                    params.paragraph_number,
                    params.bgm_type,
                )
            )
            return {"videos": [f"{params.video_subject}.mp4"]}

        settings = {
            "video_params": {
                "video_subject": "",
                "video_script_prompt": "viral instructions",
                "paragraph_number": 6,
                "bgm_type": "",
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            store = YouTubeBatchStore(directory)
            batch = store.create(["one", "two"], [], settings)
            YouTubeBatchRunner(
                store, generate, lambda item, slot, config: {"success": True}
            ).run(batch["batch_id"])

        self.assertEqual(
            received,
            [
                ("one", "viral instructions", 6, ""),
                ("two", "viral instructions", 6, ""),
            ],
        )

    def test_default_upload_reads_generated_youtube_metadata(self):
        from pathlib import Path
        from unittest.mock import patch

        content = """# Metadatos

## YouTube Shorts

Título: Metadata title

Descripción: Metadata description

Hashtags: #one #two
"""
        with tempfile.TemporaryDirectory() as directory:
            task_dir = Path(directory)
            video_path = task_dir / "final-1.mp4"
            video_path.write_bytes(b"video")
            (task_dir / "METADATOS.md").write_text(content, encoding="utf-8")
            with patch(
                "app.services.youtube_uploader.youtube_uploader.upload_video",
                return_value={"success": True},
            ) as upload:
                _default_upload(
                    {
                        "video_path": str(video_path),
                        "subject": "Fallback subject",
                        "task_id": "task-1",
                        "upload_index": 1,
                    },
                    {},
                    {},
                )

        self.assertEqual(upload.call_args.kwargs["title"], "Metadata title")
        self.assertEqual(
            upload.call_args.kwargs["description"], "Metadata description"
        )
        self.assertEqual(upload.call_args.kwargs["tags"], ["#one", "#two"])

    def test_default_upload_prefers_edited_title_override(self):
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            video_path = Path(directory) / "final-1.mp4"
            video_path.write_bytes(b"video")
            with patch(
                "app.services.youtube_uploader.youtube_uploader.upload_video",
                return_value={"success": True},
            ) as upload:
                _default_upload(
                    {
                        "video_path": str(video_path),
                        "subject": "Generation subject",
                        "title_override": "Edited publishing title",
                        "task_id": "task-1",
                    },
                    {},
                    {},
                )

        self.assertEqual(upload.call_args.kwargs["title"], "Edited publishing title")

    def test_generate_all_first_and_upload_failure_continues(self):
        events = []

        def generate(task_id, params):
            events.append(("generate", params.video_subject))
            return {"videos": [f"{params.video_subject}.mp4"]}

        def upload(item, slot, settings):
            events.append(("upload", item["subject"]))
            if item["subject"] == "one":
                return {"success": False, "error": "503 temporary outage"}
            return {"success": True}

        with tempfile.TemporaryDirectory() as directory:
            store = YouTubeBatchStore(directory)
            batch = store.create(["one", "two"], [], {}, "generate_all_first")
            result = YouTubeBatchRunner(store, generate, upload, retry_attempts=1).run(batch["batch_id"])

        self.assertEqual(events, [("generate", "one"), ("generate", "two"), ("upload", "one"), ("upload", "two")])
        self.assertEqual(result["items"][0]["failure_type"], "transient")
        self.assertEqual(result["items"][1]["upload_status"], "uploaded")

    def test_interleaved_retries_transient_then_continues_in_exact_order(self):
        events = []
        attempts = {"one": 0}

        def generate(task_id, params):
            events.append(f"generate:{params.video_subject}")
            return {"videos": [f"{params.video_subject}.mp4"]}

        def upload(item, slot, settings):
            events.append(f"upload:{item['subject']}")
            if item["subject"] == "one" and attempts["one"] < 1:
                attempts["one"] += 1
                return {"success": False, "error": "503 temporary", "retryable": True}
            return {"success": True}

        with tempfile.TemporaryDirectory() as directory:
            store = YouTubeBatchStore(directory)
            batch = store.create(["one", "two"], [], {"upload_retry_backoff_seconds": 0})
            result = YouTubeBatchRunner(store, generate, upload).run(batch["batch_id"])

        self.assertEqual(
            events,
            ["generate:one", "upload:one", "upload:one", "generate:two", "upload:two"],
        )
        self.assertEqual(result["status"], "completed")

    def test_failure_metadata_controls_automatic_resume(self):
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        cases = [
            ({"error": "quotaExceeded"}, "waiting_quota", "quota"),
            ({"error": "401 invalid_grant"}, "failed", "auth"),
            ({"error": "timeout", "retryable": True, "outcome_unknown": True}, "needs_review", "outcome_unknown"),
        ]
        for failure, status, failure_type in cases:
            with self.subTest(failure_type=failure_type), tempfile.TemporaryDirectory() as directory:
                uploads = []
                store = YouTubeBatchStore(directory)
                batch = store.create(["one"], [], {"upload_retry_attempts": 1, "quota_retry_backoff_seconds": 60})
                runner = YouTubeBatchRunner(
                    store,
                    lambda task_id, params: {"videos": ["one.mp4"]},
                    lambda item, slot, settings: uploads.append(item["subject"]) or {"success": False, **failure},
                    now=lambda: now,
                )
                result = runner.run(batch["batch_id"])
                runner.run(batch["batch_id"])

                self.assertEqual(result["items"][0]["upload_status"], status)
                self.assertEqual(result["items"][0]["failure_type"], failure_type)
                self.assertEqual(len(uploads), 1)
                if failure_type == "quota":
                    self.assertEqual(result["items"][0]["next_retry_at"], "2030-01-01T00:01:00Z")
                if failure_type == "auth":
                    self.assertTrue(result["items"][0]["requires_resume"])

    def test_retry_monitor_detects_due_items(self):
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        runner = YouTubeBatchRunner(now=lambda: now)
        due = {"items": [{"upload_status": "waiting_retry", "next_retry_at": "2029-12-31T23:59:00Z"}]}
        future = {"items": [{"upload_status": "waiting_quota", "next_retry_at": "2030-01-01T01:00:00Z"}]}

        self.assertTrue(runner._has_due_retry(due))
        self.assertFalse(runner._has_due_retry(future))

    def test_expired_slot_is_rescheduled_without_changing_original(self):
        now = datetime(2030, 1, 2, tzinfo=timezone.utc)
        expired = {"publish_at": "2030-01-01T12:00:00Z"}
        replacement = {"publish_at": "2030-01-03T12:00:00Z"}
        uploaded_slots = []
        with tempfile.TemporaryDirectory() as directory:
            store = YouTubeBatchStore(directory)
            batch = store.create(["one"], [expired], {"scheduled": True})
            runner = YouTubeBatchRunner(
                store,
                lambda task_id, params: {"videos": ["one.mp4"]},
                lambda item, slot, settings: uploaded_slots.append(slot) or {"success": True, "scheduled": True},
                now=lambda: now,
                allocate_slot=lambda manifest, index: replacement,
            )
            result = runner.run(batch["batch_id"])

        item = result["items"][0]
        self.assertEqual(uploaded_slots, [replacement])
        self.assertEqual(item["original_publish_slot"], expired)
        self.assertEqual(item["current_publish_slot"], replacement)
        self.assertEqual(item["rescheduled_reason"], "publish slot expired before upload")

    def test_execution_lock_prevents_second_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            store = YouTubeBatchStore(directory)
            batch = store.create(["one"], [], {})
            uploads = []
            runner = YouTubeBatchRunner(
                store,
                lambda task_id, params: {"videos": ["one.mp4"]},
                lambda item, slot, settings: uploads.append(item["subject"]) or {"success": True},
            )
            with store.execution_lock(batch["batch_id"]) as acquired:
                self.assertTrue(acquired)
                blocked = runner.run(batch["batch_id"])
            completed = runner.run(batch["batch_id"])

        self.assertEqual(blocked["items"][0]["generation_status"], "pending")
        self.assertEqual(uploads, ["one"])
        self.assertEqual(completed["status"], "completed")

    def test_reconcile_uses_task_output_and_does_not_repeat_uncertain_upload(self):
        generated = []
        uploaded = []
        with tempfile.TemporaryDirectory() as directory:
            store = YouTubeBatchStore(directory)
            batch = store.create(["one", "two"], [], {})
            store.update_item(batch, 0, task_id="finished", generation_status="generating")
            store.update_item(batch, 1, generation_status="generated", video_path="two.mp4", upload_status="uploading")
            runner = YouTubeBatchRunner(
                store,
                lambda task_id, params: generated.append(task_id),
                lambda item, slot, settings: uploaded.append(item["subject"]),
            )
            original_get_task = __import__("app.services.youtube_batch_runner", fromlist=["sm"]).sm.state.get_task
            runner_module = __import__("app.services.youtube_batch_runner", fromlist=["sm"])
            runner_module.sm.state.get_task = lambda task_id: {"videos": ["finished.mp4"]}
            try:
                result = runner.run(batch["batch_id"])
            finally:
                runner_module.sm.state.get_task = original_get_task

        self.assertEqual(generated, [])
        self.assertEqual(uploaded, ["one"])
        self.assertEqual(result["items"][1]["upload_status"], "needs_review")

    def test_classifies_upload_failures(self):
        self.assertEqual(classify_upload_failure("quotaExceeded"), "quota")
        self.assertEqual(classify_upload_failure("401 invalid_grant"), "auth")
        self.assertEqual(classify_upload_failure("connection timeout"), "transient")
        self.assertEqual(classify_upload_failure("bad title"), "permanent")


if __name__ == "__main__":
    unittest.main()
