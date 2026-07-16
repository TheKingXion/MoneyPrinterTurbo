import threading
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.config import config
from app.models.llm_provider import LLM_PROVIDER_REGISTRY


class TestConfigPersistence:
    @staticmethod
    def _load_example_config():
        config_path = Path(__file__).resolve().parents[2] / "config.example.toml"
        return tomllib.loads(config_path.read_text(encoding="utf-8"))

    def test_example_config_documents_runtime_settings(self):
        example_config = self._load_example_config()
        app_config = example_config["app"]
        assert app_config["video_source"] in {"pexels", "pixabay", "coverr", "local"}
        assert example_config["whisper"]["device"].lower() == "cpu"
        assert config.listen_host == "127.0.0.1"
        assert config.listen_port == 8080
        assert config.log_level == "DEBUG"

    def test_example_config_covers_llm_provider_registry(self):
        app_config = self._load_example_config()["app"]
        missing_fields = set()
        for provider in LLM_PROVIDER_REGISTRY:
            if provider.show_api_key:
                if provider.config_key("api_key") not in app_config:
                    missing_fields.add(provider.config_key("api_key"))
            if provider.show_base_url:
                if provider.config_key("base_url") not in app_config:
                    missing_fields.add(provider.config_key("base_url"))
            if provider.requires_model_name:
                if provider.config_key("model_name") not in app_config:
                    missing_fields.add(provider.config_key("model_name"))
            for field in provider.extra_fields:
                if provider.config_key(field.config_suffix) not in app_config:
                    missing_fields.add(provider.config_key(field.config_suffix))

        assert missing_fields == set()

    def test_upload_post_settings_belong_to_app_section(self):
        example_config = self._load_example_config()
        upload_post_keys = {
            "upload_post_enabled", "upload_post_api_key", "upload_post_username",
            "upload_post_platforms", "upload_post_auto_upload",
            "upload_post_youtube_privacy_status",
        }
        assert upload_post_keys <= example_config["app"].keys()
        assert upload_post_keys.isdisjoint(example_config.get("ui", {}).keys())

    def test_save_config_uses_parseable_atomic_output(self):
        original_cfg = dict(config._cfg)
        original_app = dict(config.app)
        original_youtube = dict(config.youtube)
        original_tiktok = dict(config.tiktok)
        try:
            with TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "config.toml"
                config.app["atomic_save_test"] = "ok"
                config.youtube["enabled"] = True
                config.tiktok["enabled"] = True
                with (
                    patch.object(config, "root_dir", temp_dir),
                    patch.object(config, "config_file", str(config_path)),
                ):
                    config.save_config()

                saved_config = tomllib.loads(config_path.read_text(encoding="utf-8"))
                assert saved_config["app"]["atomic_save_test"] == "ok"
                assert saved_config["youtube"]["enabled"] is True
                assert saved_config["tiktok"]["enabled"] is True
                assert list(Path(temp_dir).glob(".config-*.toml.tmp")) == []
        finally:
            config.app.clear()
            config.app.update(original_app)
            config.youtube.clear()
            config.youtube.update(original_youtube)
            config.tiktok.clear()
            config.tiktok.update(original_tiktok)
            config._cfg.clear()
            config._cfg.update(original_cfg)

    def test_runtime_config_lock_blocks_concurrent_config_writes(self):
        write_started = threading.Event()
        write_finished = threading.Event()

        def update_config():
            write_started.set()
            config.app["runtime_lock_test"] = "updated"
            write_finished.set()

        config.app.pop("runtime_lock_test", None)
        with config.runtime_config_lock():
            worker = threading.Thread(target=update_config)
            worker.start()
            assert write_started.wait(timeout=1)
            assert not write_finished.wait(timeout=0.05)

        worker.join(timeout=1)
        assert write_finished.is_set()
        config.app.pop("runtime_lock_test", None)

    def test_runtime_config_snapshot_is_deep_and_context_local(self):
        original = config.app.get("runtime_snapshot_test")
        had_original = "runtime_snapshot_test" in config.app
        try:
            config.app["runtime_snapshot_test"] = {"nested": ["captured"]}
            snapshot = config.snapshot_runtime_config()
            config.app["runtime_snapshot_test"]["nested"].append("global-change")

            with config.use_runtime_config(snapshot):
                assert config.app["runtime_snapshot_test"] == {
                    "nested": ["captured"]
                }

            assert config.app["runtime_snapshot_test"] == {
                "nested": ["captured", "global-change"]
            }
        finally:
            if had_original:
                config.app["runtime_snapshot_test"] = original
            else:
                config.app.pop("runtime_snapshot_test", None)

    def test_runtime_config_snapshot_context_does_not_hold_write_lock(self):
        snapshot = config.snapshot_runtime_config()
        write_finished = threading.Event()

        def update_config():
            config.app["runtime_snapshot_lock_test"] = "updated"
            write_finished.set()

        try:
            with config.use_runtime_config(snapshot):
                worker = threading.Thread(target=update_config)
                worker.start()
                assert write_finished.wait(timeout=1)
                assert "runtime_snapshot_lock_test" not in config.app

            worker.join(timeout=1)
            assert config.app["runtime_snapshot_lock_test"] == "updated"
        finally:
            config.app.pop("runtime_snapshot_lock_test", None)
