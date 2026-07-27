import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from app.models.schema import MaterialInfo, VideoAspect
from app.services import material_cache


def test_material_cache_preserves_thumbnail_and_resets_score():
    item = MaterialInfo(
        provider="pexels",
        url="https://cdn.example/video.mp4",
        duration=8,
        thumbnail_url="https://cdn.example/thumb.jpg",
        search_term="worker opens box",
        score=0.88,
        source_info={
            "asset_id": "42",
            "source_page": "https://pexels.com/video/42?token=secret",
        },
    )
    with tempfile.TemporaryDirectory() as directory, patch.object(
        material_cache, "_cache_dir", return_value=Path(directory)
    ):
        assert material_cache.save_search_results(
            "pexels", "worker opens box", 3, VideoAspect.portrait, [item]
        )
        restored = material_cache.load_search_results(
            "pexels", "worker opens box", 3, VideoAspect.portrait
        )

    assert restored[0].thumbnail_url == item.thumbnail_url
    assert restored[0].score == 0
    assert restored[0].source_info["source_page"] == "https://pexels.com/video/42"


def test_coverr_signed_results_are_not_cached():
    with tempfile.TemporaryDirectory() as directory, patch.object(
        material_cache, "_cache_dir", return_value=Path(directory)
    ):
        item = MaterialInfo(
            provider="coverr",
            url="https://coverr.example/video.mp4?jwt=secret",
            duration=5,
        )
        assert not material_cache.save_search_results(
            "coverr", "city", 3, VideoAspect.portrait, [item]
        )
        assert not os.listdir(directory)
