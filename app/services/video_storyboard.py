"""Structured, time-aligned storyboard models and persistence."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.services import subtitle


class ScenePlan(BaseModel):
    scene_id: str
    index: int = Field(ge=0)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    narration: str
    query: str
    fallback_queries: list[str] = Field(default_factory=list)
    action: str = ""
    protagonist: str = ""
    location: str = ""
    required_objects: list[str] = Field(default_factory=list)
    excluded_elements: list[str] = Field(default_factory=list)
    shot_type: str = "medium shot"
    continuity_key: str = ""
    selected_clip: dict[str, Any] | None = None
    fidelity_score: float | None = None
    warnings: list[str] = Field(default_factory=list)
    locked: bool = False

    @field_validator("end")
    @classmethod
    def validate_end(cls, value: float, info):
        start = float(info.data.get("start", 0))
        if value <= start:
            raise ValueError("scene end must be greater than start")
        return value

    @property
    def duration(self) -> float:
        return max(0.05, self.end - self.start)

    def material_query(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "query": self.query,
            "required_objects": self.required_objects,
            "excluded_elements": self.excluded_elements,
        }


class VideoPlan(BaseModel):
    version: int = 1
    subject: str
    script: str
    audio_duration: float = Field(gt=0)
    protagonist: str = ""
    central_location: str = ""
    continuity_objects: list[str] = Field(default_factory=list)
    scenes: list[ScenePlan]

    @field_validator("scenes")
    @classmethod
    def validate_scenes(cls, value: list[ScenePlan]):
        if not value:
            raise ValueError("storyboard requires at least one scene")
        previous_end = 0.0
        for expected_index, scene in enumerate(value):
            if scene.index != expected_index:
                raise ValueError("scene indices must be contiguous")
            if abs(scene.start - previous_end) > 0.051:
                raise ValueError("storyboard timeline must be continuous")
            previous_end = scene.end
        return value


def _srt_seconds(value: str) -> float:
    hours, minutes, seconds = value.strip().replace(",", ".").split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _subtitle_segments(subtitle_path: str) -> list[dict[str, Any]]:
    segments = []
    for _, timing, text in subtitle.file_to_subtitles(subtitle_path):
        start_text, end_text = timing.split(" --> ", 1)
        segments.append(
            {
                "start": _srt_seconds(start_text),
                "end": _srt_seconds(end_text),
                "narration": re.sub(r"\s+", " ", text).strip(),
            }
        )
    return segments


def _script_segments(script: str, audio_duration: float, count: int) -> list[dict]:
    sentences = [
        value.strip()
        for value in re.split(r"(?<=[.!?。！？])\s+|\n+", script)
        if value.strip()
    ]
    if not sentences:
        sentences = [script.strip() or "Narration"]
    target_count = max(1, min(max(count, 1), len(sentences)))
    groups: list[list[str]] = [[] for _ in range(target_count)]
    for index, sentence in enumerate(sentences):
        groups[min(target_count - 1, index * target_count // len(sentences))].append(
            sentence
        )
    weights = [max(1, len(" ".join(group))) for group in groups]
    total_weight = sum(weights)
    cursor = 0.0
    result = []
    for index, group in enumerate(groups):
        end = (
            audio_duration
            if index == len(groups) - 1
            else cursor + audio_duration * weights[index] / total_weight
        )
        result.append(
            {"start": cursor, "end": end, "narration": " ".join(group).strip()}
        )
        cursor = end
    return result


def _merge_short_segments(
    segments: list[dict[str, Any]], audio_duration: float, minimum: float = 1.8
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for segment in segments:
        if (
            merged
            and segment["end"] - merged[-1]["start"] <= 5.5
            and merged[-1]["end"] - merged[-1]["start"] < minimum
        ):
            merged[-1]["end"] = segment["end"]
            merged[-1]["narration"] = (
                f"{merged[-1]['narration']} {segment['narration']}".strip()
            )
        else:
            merged.append(dict(segment))
    if len(merged) > 1 and merged[-1]["end"] - merged[-1]["start"] < minimum:
        last = merged.pop()
        merged[-1]["end"] = last["end"]
        merged[-1]["narration"] = (
            f"{merged[-1]['narration']} {last['narration']}".strip()
        )
    boundaries = [0.0]
    for index in range(1, len(merged)):
        boundary = max(boundaries[-1], float(merged[index]["start"]))
        boundaries.append(boundary)
    boundaries.append(audio_duration)
    for index, segment in enumerate(merged):
        segment["start"] = boundaries[index]
        segment["end"] = max(boundaries[index] + 0.05, boundaries[index + 1])
    merged[-1]["end"] = audio_duration
    return merged


def _query_for_scene(terms: list[str], index: int, scene_count: int) -> str:
    if not terms:
        return "person performing visible action"
    term_index = min(len(terms) - 1, index * len(terms) // max(scene_count, 1))
    return str(terms[term_index]).strip()


def build_video_plan(
    subject: str,
    script: str,
    terms: list[str],
    subtitle_path: str,
    audio_duration: float,
    enrichment: list[dict[str, Any]] | None = None,
) -> VideoPlan:
    raw_segments = _subtitle_segments(subtitle_path) if subtitle_path else []
    if not raw_segments:
        raw_segments = _script_segments(script, audio_duration, len(terms))
    segments = _merge_short_segments(raw_segments, audio_duration)
    enrichment = enrichment or []
    scenes = []
    for index, segment in enumerate(segments):
        extra = enrichment[index] if index < len(enrichment) else {}
        base_query = _query_for_scene(terms, index, len(segments))
        query = str(extra.get("query") or base_query).strip()
        fallback_queries = [
            str(item).strip()
            for item in extra.get("fallback_queries", [])
            if str(item).strip() and str(item).strip() != query
        ][:3]
        if base_query and base_query != query and base_query not in fallback_queries:
            fallback_queries.append(base_query)
        scenes.append(
            ScenePlan(
                scene_id=f"scene-{index + 1:03d}",
                index=index,
                start=round(float(segment["start"]), 3),
                end=round(float(segment["end"]), 3),
                narration=str(segment["narration"]),
                query=query,
                fallback_queries=fallback_queries,
                action=str(extra.get("action") or ""),
                protagonist=str(extra.get("protagonist") or ""),
                location=str(extra.get("location") or ""),
                required_objects=[
                    str(item).strip()
                    for item in extra.get("required_objects", [])
                    if str(item).strip()
                ][:8],
                excluded_elements=[
                    str(item).strip()
                    for item in extra.get("excluded_elements", [])
                    if str(item).strip()
                ][:8],
                shot_type=str(extra.get("shot_type") or "medium shot"),
                continuity_key=str(extra.get("continuity_key") or ""),
            )
        )
    protagonist = next((scene.protagonist for scene in scenes if scene.protagonist), "")
    central_location = next(
        (scene.location for scene in scenes if scene.location), ""
    )
    continuity_objects = list(
        dict.fromkeys(
            item for scene in scenes for item in scene.required_objects if item
        )
    )[:12]
    return VideoPlan(
        subject=subject,
        script=script,
        audio_duration=audio_duration,
        protagonist=protagonist,
        central_location=central_location,
        continuity_objects=continuity_objects,
        scenes=scenes,
    )


def save_video_plan(plan: VideoPlan, file_path: str | os.PathLike[str]) -> str:
    target = Path(file_path)
    target.parent.mkdir(parents=True, exist_ok=True)
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
            json.dump(
                plan.model_dump(mode="json"),
                temp_file,
                ensure_ascii=False,
                indent=2,
            )
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, target)
        return str(target)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def load_video_plan(file_path: str | os.PathLike[str]) -> VideoPlan:
    return VideoPlan.model_validate_json(Path(file_path).read_text(encoding="utf-8"))
