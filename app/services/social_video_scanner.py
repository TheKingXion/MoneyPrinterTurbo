import glob
import json
import os
import re
from datetime import datetime
from typing import Any, Callable

from app.utils import utils


def read_task_subject(task_dir: str, metadata_parser: Callable[[str], dict[str, Any]]) -> str:
    script_path = os.path.join(task_dir, "script.json")
    try:
        with open(script_path, "r", encoding="utf-8") as file:
            data = json.load(file)
        params = data.get("params") or {}
        if isinstance(params, dict) and params.get("video_subject"):
            return str(params["video_subject"]).strip()
    except (OSError, json.JSONDecodeError, TypeError):
        pass

    metadata_path = os.path.join(task_dir, "METADATOS.md")
    metadata = metadata_parser(metadata_path) if os.path.isfile(metadata_path) else {}
    return str(metadata.get("title") or os.path.basename(task_dir)).strip()


def scan_generated_videos(
    tracker_entries: list[dict[str, Any]],
    metadata_parser: Callable[[str], dict[str, Any]],
    platform: str,
    status_filter: str = "",
) -> dict[str, Any]:
    entries_by_task = {}
    for entry in tracker_entries:
        try:
            key = (entry.get("task_id"), int(entry.get("index", 1)))
        except (TypeError, ValueError):
            continue
        entries_by_task[key] = entry
    videos: list[dict[str, Any]] = []
    for task_dir in glob.glob(os.path.join(utils.task_dir(), "*")):
        if not os.path.isdir(task_dir):
            continue
        task_id = os.path.basename(task_dir)
        video_files = glob.glob(os.path.join(task_dir, "final-*.mp4"))
        video_files.sort(
            key=lambda path: int(match.group(1))
            if (match := re.search(r"final-(\d+)\.mp4$", os.path.basename(path), re.IGNORECASE))
            else 0
        )
        if not video_files:
            fallback = os.path.join(task_dir, "final.mp4")
            video_files = [fallback] if os.path.isfile(fallback) else []
        if not video_files:
            continue
        metadata_path = os.path.join(task_dir, "METADATOS.md")
        subject = read_task_subject(task_dir, metadata_parser)
        for fallback_index, video_path in enumerate(video_files, start=1):
            filename_match = re.search(r"final-(\d+)\.mp4$", os.path.basename(video_path), re.IGNORECASE)
            index = int(filename_match.group(1)) if filename_match else fallback_index
            try:
                size_bytes = os.path.getsize(video_path)
            except OSError:
                continue
            if size_bytes <= 0:
                continue
            entry = entries_by_task.get((task_id, index)) or {}
            status = entry.get("status") or "pending"
            if status_filter and status != status_filter:
                continue
            videos.append(
                {
                    "task_id": task_id,
                    "index": index,
                    "subject": subject,
                    "video_path": video_path,
                    "video_size_mb": round(size_bytes / 1024 / 1024, 2),
                    "metadata_path": metadata_path if os.path.isfile(metadata_path) else "",
                    "has_metadata": os.path.isfile(metadata_path),
                    "generated_at": datetime.fromtimestamp(os.path.getmtime(video_path)).isoformat(timespec="seconds"),
                    f"{platform}_status": status,
                    f"{platform}_url": entry.get(f"{platform}_url", ""),
                    "publish_id": entry.get("publish_id", ""),
                    "scheduled_at": entry.get("scheduled_at", ""),
                    "provider": entry.get("provider", ""),
                    "error": entry.get("error", ""),
                }
            )

    order = {
        "pending": 0,
        "failed": 1,
        "scheduled": 2,
        "scheduled_retry": 3,
        "uploading": 4,
        "processing": 5,
        "reconcile_required": 6,
        "published": 7,
        "cancelled": 8,
    }
    status_key = f"{platform}_status"
    videos.sort(key=lambda item: (order.get(item[status_key], 9), item["generated_at"]))
    statuses = [item[status_key] for item in videos]
    return {
        "total": len(videos),
        "pending": statuses.count("pending"),
        "scheduled": statuses.count("scheduled") + statuses.count("scheduled_retry"),
        "processing": statuses.count("uploading") + statuses.count("processing"),
        "published": statuses.count("published"),
        "failed": statuses.count("failed") + statuses.count("reconcile_required"),
        "videos": videos,
    }
