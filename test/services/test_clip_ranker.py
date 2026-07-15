import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.models.schema import MaterialInfo
from app.services import clip_ranker


class TestClipRanker(unittest.TestCase):
    def test_strict_demographic_query_rejects_unverifiable_candidates(self):
        candidates = [
            MaterialInfo(
                provider="pixabay",
                url="https://example.com/video.mp4",
                duration=8,
            )
        ]

        result = clip_ranker.rank_materials(
            candidates,
            query="teenage boy speaking into microphone",
        )

        self.assertEqual(result, [])

    def test_generic_query_preserves_unverifiable_provider_order(self):
        candidates = [
            MaterialInfo(provider="coverr", url="https://example.com/one.mp4"),
            MaterialInfo(provider="coverr", url="https://example.com/two.mp4"),
        ]

        result = clip_ranker.rank_materials(candidates, query="radio microphone", limit=1)

        self.assertEqual(result, candidates[:1])

    def test_young_adult_and_warehouse_queries_require_verification(self):
        self.assertTrue(
            clip_ranker.requires_strict_verification(
                "young adult woman labeling boxes warehouse"
            )
        )
        self.assertTrue(
            clip_ranker.requires_strict_verification("cardboard boxes inside warehouse")
        )
        self.assertFalse(clip_ranker.requires_strict_verification("colorful folders"))

    def test_relaxed_fallback_removes_invented_constraints(self):
        result = clip_ranker.relax_query_for_fallback(
            "young adult woman securing data on smartphone warehouse"
        )

        self.assertEqual(result, "person securing data on smartphone")


if __name__ == "__main__":
    unittest.main()
