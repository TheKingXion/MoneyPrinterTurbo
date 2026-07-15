import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.models.schema import MaterialInfo, VideoAspect, VideoParams


class TestVideoAspect(unittest.TestCase):
    def test_to_resolution_known_aspects(self):
        self.assertEqual(VideoAspect.landscape.to_resolution(), (1920, 1080))
        self.assertEqual(VideoAspect.portrait.to_resolution(), (1080, 1920))
        self.assertEqual(VideoAspect.square.to_resolution(), (1080, 1080))

    def test_to_resolution_rejects_unsupported_value(self):
        with self.assertRaises(ValueError):
            VideoAspect.to_resolution("4:5")


class TestVideoSchemaCompatibility(unittest.TestCase):
    def test_material_info_accepts_historical_and_clip_ranker_fields(self):
        historical = MaterialInfo(provider="pexels", url="clip.mp4", duration=5)
        enriched = MaterialInfo(
            provider="pixabay",
            url="ranked.mp4",
            duration=7,
            thumbnail_url="thumb.jpg",
            search_term="city skyline",
            score=0.9,
            scene_index=2,
        )

        self.assertEqual(historical.thumbnail_url, "")
        self.assertEqual(enriched.search_term, "city skyline")
        self.assertEqual(enriched.scene_index, 2)

    def test_video_params_preserves_multiple_source_contract(self):
        params = VideoParams(
            video_subject="multiple sources",
            video_source="pexels",
            video_sources=["pexels", "pixabay"],
        )

        self.assertEqual(params.video_source, "pexels")
        self.assertEqual(params.video_sources, ["pexels", "pixabay"])


if __name__ == "__main__":
    unittest.main()
