import unittest
from unittest.mock import patch

from webui.components.youtube_panel import (
    IDEA_GENERATION_CHUNK_SIZE,
    MAX_BATCH_IDEAS,
    _clear_batch_draft,
    _generate_unique_idea_rows,
    _regenerate_unlocked_idea_rows,
    _score_idea_rows,
)


class YouTubePanelStateTests(unittest.TestCase):
    def test_large_idea_generation_uses_bounded_chunks(self):
        counter = 0
        requested = []

        def generate(topic, amount, language, existing):
            nonlocal counter
            requested.append(amount)
            rows = []
            for _ in range(amount):
                counter += 1
                rows.append(
                    {
                        "subject": f"Idea concreta {counter}",
                        "title_override": f"Titulo concreto {counter}",
                    }
                )
            return rows

        def unique(subjects, existing):
            return [
                {
                    "subject": subject,
                    "duplicate": False,
                    "duplicate_of": "",
                    "similarity": 0.0,
                }
                for subject in subjects
            ]

        with (
            patch(
                "webui.components.youtube_panel.llm.generate_batch_ideas",
                side_effect=generate,
            ),
            patch(
                "webui.components.youtube_panel.validate_unique_ideas",
                side_effect=unique,
            ),
        ):
            rows = _generate_unique_idea_rows("tema", 25, "es", [])

        self.assertEqual(len(rows), 25)
        self.assertEqual(
            requested,
            [IDEA_GENERATION_CHUNK_SIZE, IDEA_GENERATION_CHUNK_SIZE, 1],
        )
        self.assertGreaterEqual(MAX_BATCH_IDEAS, 200)

    def test_regeneration_preserves_only_locked_perfect_rows(self):
        perfect = {
            "subject": "Una joven construyo una biblioteca comunitaria tras superar una inundacion y ayudo a sus vecinos con libros recuperados",
            "title_override": "Los libros que sobrevivieron a la gran lluvia",
            "locked": True,
        }
        weak = {
            "subject": "Una idea breve",
            "title_override": "Breve",
            "locked": True,
        }
        replacement = {
            "subject": "Un mecanico reparo bicicletas abandonadas y creo una red de transporte para trabajadores de toda su comunidad",
            "title_override": "El taller que devolvio el movimiento al barrio",
            "locked": False,
        }

        with patch(
            "webui.components.youtube_panel._generate_unique_idea_rows",
            return_value=_score_idea_rows([replacement]),
        ) as generate:
            rows = _regenerate_unlocked_idea_rows(
                "tema", 2, "es", [], [perfect, weak]
            )

        self.assertEqual(rows[0]["subject"], perfect["subject"])
        self.assertTrue(rows[0]["locked"])
        self.assertEqual(rows[1]["subject"], replacement["subject"])
        self.assertEqual(generate.call_args.args[1], 1)

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
