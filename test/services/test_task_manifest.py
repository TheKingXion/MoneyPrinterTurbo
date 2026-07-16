import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.task_manifest import MANIFEST_NAME, TaskManifest


class TestTaskManifest(unittest.TestCase):
    def test_restore_hits_when_inputs_and_artifacts_match(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "audio.mp3"
            artifact.write_bytes(b"audio")
            manifest = TaskManifest("task-1", directory)
            manifest.complete(
                "audio", {"script": "same"}, {"duration": 3}, {"audio": artifact}
            )

            restored = manifest.restore("audio", {"script": "same"})

        self.assertEqual(restored["outputs"], {"duration": 3})
        self.assertEqual(restored["artifacts"]["audio"], str(artifact.resolve()))

    def test_restore_misses_for_changed_inputs_or_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "script.json"
            artifact.write_text("original", encoding="utf-8")
            manifest = TaskManifest("task-1", directory)
            manifest.complete(
                "script_terms", {"subject": "one"}, {"script": "text"}, {"data": artifact}
            )

            self.assertIsNone(
                manifest.restore("script_terms", {"subject": "different"})
            )
            artifact.write_text("changed", encoding="utf-8")
            self.assertIsNone(manifest.restore("script_terms", {"subject": "one"}))

    def test_corrupt_json_is_ignored_and_recovered_on_write(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / MANIFEST_NAME
            manifest_path.write_text("{broken", encoding="utf-8")
            artifact = Path(directory) / "subtitle.srt"
            artifact.write_text("subtitle", encoding="utf-8")
            manifest = TaskManifest("task-1", directory)

            self.assertIsNone(manifest.restore("subtitle", {"provider": "edge"}))
            manifest.complete(
                "subtitle", {"provider": "edge"}, {}, {"subtitle": artifact}
            )

            recovered = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(recovered["version"], 1)
        self.assertEqual(recovered["stages"]["subtitle"]["status"], "complete")

    def test_write_fsyncs_temporary_file_before_atomic_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "audio.mp3"
            artifact.write_bytes(b"audio")
            manifest = TaskManifest("task-1", directory)
            real_replace = os.replace
            events = []

            def checked_replace(source, destination):
                events.append("replace")
                json.loads(Path(source).read_text(encoding="utf-8"))
                return real_replace(source, destination)

            with (
                patch("app.services.task_manifest.os.fsync", side_effect=lambda _: events.append("fsync")),
                patch("app.services.task_manifest.os.replace", side_effect=checked_replace) as replace,
            ):
                manifest.complete("audio", {}, {"duration": 1}, {"audio": artifact})

        replace.assert_called_once()
        self.assertIn("fsync", events)
        self.assertLess(events.index("fsync"), events.index("replace"))
