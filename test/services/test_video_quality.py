from app.services import video_quality, video_storyboard


def test_quality_report_detects_repeated_and_missing_scenes():
    plan = video_storyboard.build_video_plan(
        subject="Test",
        script="One action. Another action.",
        terms=["person first action", "person second action"],
        subtitle_path="",
        audio_duration=4,
    )
    plan.scenes[0].selected_clip = {"local_path": "same.mp4"}
    plan.scenes[0].fidelity_score = 0.3
    if len(plan.scenes) == 1:
        plan.scenes.append(
            video_storyboard.ScenePlan(
                scene_id="scene-002",
                index=1,
                start=2,
                end=4,
                narration="Another action.",
                query="person second action",
            )
        )
        plan.scenes[0].end = 2
    report = video_quality.validate_storyboard(plan)

    assert report["coverage_ratio"] < 1
    assert not report["passed"]
    assert any(issue["code"] == "missing_clip" for issue in report["issues"])
