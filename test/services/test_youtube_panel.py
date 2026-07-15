import unittest

from webui.components.youtube_panel import _clear_batch_draft


class YouTubePanelStateTests(unittest.TestCase):
    def test_clearing_started_draft_preserves_active_batch(self):
        state = {
            "yt_batch_draft_context": ("ai", 4),
            "yt_batch_idea_rows": [{"subject": "idea"}],
            "yt_batch_idea_editor": [{"subject": "idea"}],
            "yt_active_batch_id": "batch-123",
            "unrelated": True,
        }

        _clear_batch_draft(state)

        self.assertEqual(state["yt_active_batch_id"], "batch-123")
        self.assertTrue(state["unrelated"])
        self.assertNotIn("yt_batch_draft_context", state)
        self.assertNotIn("yt_batch_idea_rows", state)
        self.assertNotIn("yt_batch_idea_editor", state)


if __name__ == "__main__":
    unittest.main()
