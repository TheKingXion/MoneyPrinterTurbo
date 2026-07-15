import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.models.schema import VideoParams
from webui.components import tiktok_panel, youtube_panel


class TestSocialPanelRuntime(unittest.TestCase):
    def test_youtube_progress_can_render_active_batch_and_history(self):
        app = AppTest.from_string(
            """
from unittest.mock import patch
from webui.components import youtube_panel

batch = {
    "batch_id": "batch-123",
    "requested": 1,
    "status": "running",
    "control": "running",
    "items": [],
}
with patch.object(youtube_panel.youtube_batch_store, "load", return_value=batch):
    youtube_panel._render_progress(lambda value: value, "batch-123", "active")
    youtube_panel._render_progress(lambda value: value, "batch-123", "history")
"""
        ).run()

        self.assertEqual(len(app.exception), 0)
        self.assertIsNotNone(app.button(key="yt_batch_pause_active_batch-123"))
        self.assertIsNotNone(app.button(key="yt_batch_pause_history_batch-123"))

    def test_youtube_batch_renders_instruction_defaults(self):
        app = AppTest.from_string(
            """
from unittest.mock import patch
import streamlit as st
from app.models.schema import VideoParams
from webui.components import youtube_panel

st.session_state["yt_batch_schedule_interval"] = 5
publish_plan = patch.object(
    youtube_panel.youtube_uploader,
    "create_publish_plan",
    return_value=[{"publish_at_local": "later"}] * 10,
)
with (
    patch.object(youtube_panel.youtube_batch_store, "list_batches", return_value=[]),
    patch.object(youtube_panel, "scan_pending_videos", return_value={"videos": []}),
    patch.object(youtube_panel.upload_tracker, "load", return_value=[]),
    publish_plan as create_publish_plan,
):
    youtube_panel._render_batch(lambda value: value, VideoParams(video_subject=""))
    st.session_state["yt_batch_plan_interval"] = create_publish_plan.call_args.kwargs["interval_minutes"]
    st.session_state["yt_batch_collision_policy"] = create_publish_plan.call_args.kwargs["collision_policy"]
    st.session_state["yt_batch_allow_shared"] = create_publish_plan.call_args.kwargs["allow_shared_publish_time"]
"""
        ).run()

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.slider(key="yt_batch_paragraph_number").value, 6)
        self.assertEqual(app.selectbox(key="yt_batch_bgm_type").value, "")
        self.assertEqual(app.number_input(key="yt_batch_schedule_interval").value, 5)
        self.assertEqual(app.number_input(key="yt_batch_days").value, 5)
        self.assertFalse(app.checkbox(key="yt_batch_interval_enabled").value)
        self.assertEqual(app.session_state["yt_batch_plan_interval"], 0)
        self.assertTrue(app.session_state["yt_batch_allow_shared"])
        self.assertEqual(app.session_state["yt_batch_collision_policy"], "skip")
        self.assertIn(
            "Escribe un guion viral",
            app.text_area(key="yt_batch_script_prompt").value,
        )

    def test_youtube_batch_overrides_script_and_music_settings(self):
        base = VideoParams(
            video_subject="base",
            video_script="old script",
            video_terms="old terms",
            video_count=4,
            video_script_prompt="old prompt",
            paragraph_number=1,
            bgm_type="random",
            bgm_file="old.mp3",
            voice_name="narrator",
            video_language="es-ES",
            video_sources=["pexels", "pixabay"],
        )

        params = youtube_panel._build_batch_video_params(
            base,
            "  instrucciones virales  ",
            6,
            "",
        )

        self.assertEqual(params.video_subject, "")
        self.assertEqual(params.video_script, "")
        self.assertEqual(params.video_terms, "")
        self.assertEqual(params.video_count, 1)
        self.assertEqual(params.video_script_prompt, "instrucciones virales")
        self.assertEqual(params.paragraph_number, 6)
        self.assertEqual(params.bgm_type, "")
        self.assertEqual(params.bgm_file, "")
        self.assertEqual(params.voice_name, "narrator")
        self.assertEqual(params.video_language, "es-ES")
        self.assertEqual(params.video_sources, ["pexels", "pixabay"])

    def test_settings_render_uses_persisted_privacy_without_saving(self):
        app = AppTest.from_string(
            """
import streamlit as st
from unittest.mock import MagicMock
from app.config import config
from webui.components.youtube_panel import _render_settings
from app.services.youtube_uploader import youtube_uploader

original = dict(config.youtube)
original_save = config.save_config
config.youtube.update({
    "enabled": False,
    "privacy_status": "unlisted",
    "client_id": "",
    "client_secret": "",
})
save = MagicMock()
config.save_config = save
try:
    _render_settings(lambda value: value)
    st.session_state["save_calls"] = save.call_count
finally:
    config.youtube.clear()
    config.youtube.update(original)
    config.save_config = original_save
    youtube_uploader.sync_from_config()
"""
        ).run()

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.selectbox(key="yt_settings_privacy").value, "unlisted")
        self.assertEqual(app.session_state["save_calls"], 0)

    def test_tiktok_slot_offset_counts_only_pending_jobs(self):
        jobs = [
            {"status": "pending"},
            {"status": "completed"},
            {"status": "cancelled"},
            {"status": "pending"},
        ]
        with patch.object(tiktok_panel.tiktok_scheduler, "load", return_value=jobs):
            self.assertEqual(tiktok_panel._active_pending_jobs(), 2)

    def test_tiktok_settings_use_persisted_privacy_without_saving(self):
        app = AppTest.from_string(
            """
import streamlit as st
from unittest.mock import MagicMock
from app.config import config
from webui.components.tiktok_panel import _render_settings
from app.services.tiktok_uploader import tiktok_uploader

original = dict(config.tiktok)
original_save = config.save_config
config.tiktok.update({
    "enabled": False,
    "provider": "official",
    "privacy_level": "FOLLOWER_OF_CREATOR",
})
save = MagicMock()
config.save_config = save
try:
    _render_settings(lambda value: value)
    st.session_state["save_calls"] = save.call_count
finally:
    config.tiktok.clear()
    config.tiktok.update(original)
    config.save_config = original_save
    tiktok_uploader.sync_from_config()
"""
        ).run()

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(
            app.selectbox(key="tt_settings_privacy").value,
            "FOLLOWER_OF_CREATOR",
        )
        self.assertEqual(app.session_state["save_calls"], 0)


if __name__ == "__main__":
    unittest.main()
