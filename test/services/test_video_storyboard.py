import tempfile
from pathlib import Path

from app.services import video_storyboard


def test_storyboard_uses_real_srt_timeline_and_is_continuous():
    with tempfile.TemporaryDirectory() as directory:
        subtitle = Path(directory) / "subtitle.srt"
        subtitle.write_text(
            "1\n00:00:00,000 --> 00:00:02,000\nA worker opens a box.\n\n"
            "2\n00:00:02,000 --> 00:00:05,000\nShe checks the product.\n\n",
            encoding="utf-8",
        )
        plan = video_storyboard.build_video_plan(
            subject="Product inspection",
            script="A worker opens a box. She checks the product.",
            terms=["worker opening box", "worker checking product"],
            subtitle_path=str(subtitle),
            audio_duration=5,
            enrichment=[
                {
                    "query": "warehouse worker opening cardboard box",
                    "action": "opens a cardboard box",
                    "protagonist": "warehouse worker",
                    "location": "warehouse",
                    "required_objects": ["cardboard box"],
                },
                {
                    "query": "warehouse worker inspecting small product",
                    "action": "inspects a product",
                    "protagonist": "warehouse worker",
                    "location": "warehouse",
                    "required_objects": ["product"],
                },
            ],
        )

    assert plan.scenes[0].start == 0
    assert plan.scenes[-1].end == 5
    assert plan.scenes[0].end == plan.scenes[1].start
    assert plan.protagonist == "warehouse worker"
    assert plan.central_location == "warehouse"


def test_storyboard_round_trip_preserves_selection():
    plan = video_storyboard.build_video_plan(
        subject="Test",
        script="A person walks.",
        terms=["person walking outdoors"],
        subtitle_path="",
        audio_duration=3,
    )
    plan.scenes[0].selected_clip = {
        "local_path": "clip.mp4",
        "start_time": 1.2,
        "duration": 3,
    }
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "storyboard.json"
        video_storyboard.save_video_plan(plan, target)
        restored = video_storyboard.load_video_plan(target)

    assert restored.scenes[0].selected_clip["start_time"] == 1.2
    assert restored.audio_duration == 3
