"""Deterministic storyboard and final-render quality gates."""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from moviepy.video.io.VideoFileClip import VideoFileClip

from app.services.video_storyboard import VideoPlan


def validate_storyboard(plan: VideoPlan, minimum_score: float = 0.235) -> dict[str, Any]:
    issues = []
    selected_paths = []
    covered_duration = 0.0
    protagonists = Counter()
    locations = Counter()
    for scene in plan.scenes:
        selected = scene.selected_clip or {}
        local_path = selected.get("local_path")
        if local_path:
            selected_paths.append(local_path)
            covered_duration += scene.duration
        else:
            issues.append({"scene_id": scene.scene_id, "code": "missing_clip"})
        score = float(scene.fidelity_score or 0)
        if score < minimum_score:
            issues.append(
                {
                    "scene_id": scene.scene_id,
                    "code": "low_fidelity",
                    "score": score,
                }
            )
        if "repeated_clip" in scene.warnings:
            issues.append({"scene_id": scene.scene_id, "code": "repeated_clip"})
        if scene.protagonist:
            protagonists[scene.protagonist] += 1
        if scene.location:
            locations[scene.location] += 1

    canonical_protagonist = protagonists.most_common(1)[0][0] if protagonists else ""
    canonical_location = locations.most_common(1)[0][0] if locations else ""
    for scene in plan.scenes:
        if (
            canonical_protagonist
            and scene.protagonist
            and scene.protagonist != canonical_protagonist
        ):
            issues.append(
                {
                    "scene_id": scene.scene_id,
                    "code": "protagonist_continuity",
                    "expected": canonical_protagonist,
                    "actual": scene.protagonist,
                }
            )
        if (
            canonical_location
            and scene.location
            and scene.location != canonical_location
            and scene.continuity_key
            and scene.continuity_key
            == plan.scenes[max(0, scene.index - 1)].continuity_key
        ):
            issues.append(
                {
                    "scene_id": scene.scene_id,
                    "code": "location_continuity",
                    "expected": canonical_location,
                    "actual": scene.location,
                }
            )

    duplicates = len(selected_paths) - len(set(selected_paths))
    return {
        "version": 1,
        "scene_count": len(plan.scenes),
        "covered_duration": round(covered_duration, 3),
        "audio_duration": round(plan.audio_duration, 3),
        "coverage_ratio": round(
            min(1.0, covered_duration / max(plan.audio_duration, 0.001)), 4
        ),
        "duplicate_clip_count": duplicates,
        "duplicate_ratio": round(duplicates / max(len(selected_paths), 1), 4),
        "canonical_protagonist": canonical_protagonist,
        "canonical_location": canonical_location,
        "issues": issues,
        "passed": not any(
            issue["code"] in {"missing_clip", "protagonist_continuity"}
            for issue in issues
        ),
    }


def validate_final_videos(
    report: dict[str, Any], video_paths: list[str], expected_duration: float
) -> dict[str, Any]:
    renders = []
    for video_path in video_paths:
        duration = 0.0
        error = ""
        clip = None
        try:
            clip = VideoFileClip(video_path, audio=False)
            duration = float(clip.duration or 0)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if clip is not None:
                clip.close()
        renders.append(
            {
                "path": video_path,
                "duration": round(duration, 3),
                "duration_delta": round(duration - expected_duration, 3),
                "exists": os.path.isfile(video_path),
                "error": error,
                "passed": not error
                and os.path.isfile(video_path)
                and duration + 0.15 >= expected_duration,
            }
        )
    result = dict(report)
    result["renders"] = renders
    result["passed"] = bool(report.get("passed")) and all(
        item["passed"] for item in renders
    )
    return result


def save_quality_report(report: dict[str, Any], file_path: str) -> str:
    target = Path(file_path)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            json.dump(report, temp_file, ensure_ascii=False, indent=2)
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, target)
        return str(target)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
