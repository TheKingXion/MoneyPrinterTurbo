import os
import secrets
import time

from fastapi import Depends, Path, Query, Request
from pydantic import BaseModel, Field

from app.config import config
from app.controllers import base
from app.controllers.v1.base import new_router
from app.models.exception import HttpException
from app.services.youtube_uploader import (
    move_uploaded_task,
    parse_metadata_file,
    scan_pending_videos,
    upload_tracker,
    youtube_uploader,
)
from app.services.youtube_batch import (
    EXECUTION_MODES,
    collect_task_subjects,
    youtube_batch_store,
)
from app.services.youtube_batch_runner import youtube_batch_runner
from app.utils import utils


SAFE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"


def _require_youtube_api_access(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host in {"127.0.0.1", "::1", "localhost", "testclient"}:
        return
    expected_token = os.getenv("YOUTUBE_API_TOKEN", "")
    supplied_token = request.headers.get("X-YouTube-API-Token", "")
    if bool(config.youtube.get("allow_remote_api", False)) and expected_token and secrets.compare_digest(
        supplied_token, expected_token
    ):
        return
    raise HttpException("youtube-access", status_code=403, message="YouTube API is restricted to localhost")


router = new_router(dependencies=[Depends(_require_youtube_api_access)])


class YouTubeBatchCreateRequest(BaseModel):
    subjects: list[str] = Field(min_length=1)
    title_overrides: list[str] = Field(default_factory=list)
    idea_mode: str = "manual"
    publish_plan: list[dict] = Field(default_factory=list)
    settings: dict = Field(default_factory=dict)
    execution_mode: str = "interleaved"
    auto_start: bool = False


class YouTubeBatchRetryRequest(BaseModel):
    publish_slots: dict[int, dict] = Field(default_factory=dict)


def _find_scanned_video(task_id: str, index: int = 1) -> dict:
    scanned = scan_pending_videos()
    for video in scanned["videos"]:
        if video["task_id"] == task_id and int(video.get("index", 1)) == index:
            return video
    return {}


def _metadata_for_video(video: dict) -> dict:
    metadata = parse_metadata_file(video.get("metadata_path", ""))
    if not metadata.get("title"):
        metadata["title"] = video.get("subject") or video.get("task_id") or "Short video"
    if not metadata.get("description"):
        metadata["description"] = video.get("subject", "")
    if not metadata.get("tags"):
        metadata["tags"] = ["#shorts"]
    return metadata


def _batch_or_404(batch_id: str) -> dict:
    batch = youtube_batch_store.load(batch_id)
    if not batch:
        raise HttpException(batch_id, status_code=404, message="YouTube batch not found")
    return batch


@router.post("/youtube/batches", summary="Create a durable YouTube batch")
def youtube_batch_create(request: Request, body: YouTubeBatchCreateRequest):
    if body.execution_mode not in EXECUTION_MODES:
        raise HttpException("youtube-batch", status_code=400, message="Invalid execution_mode")
    try:
        settings = dict(body.settings)
        settings["runner_managed"] = True
        blocked_subjects = collect_task_subjects()
        blocked_subjects.extend(
            entry.get("video_subject", "") for entry in upload_tracker.load()
        )
        batch = youtube_batch_store.create(
            body.subjects,
            body.publish_plan,
            settings,
            execution_mode=body.execution_mode,
            title_overrides=body.title_overrides or None,
            idea_mode=body.idea_mode,
            blocked_subjects=blocked_subjects,
        )
    except ValueError as exc:
        raise HttpException("youtube-batch", status_code=400, message=str(exc)) from exc
    if body.auto_start:
        batch = youtube_batch_runner.start(batch["batch_id"])
    return utils.get_response(200, batch)


@router.get("/youtube/batches", summary="List durable YouTube batches")
def youtube_batch_list(request: Request, limit: int = Query(20, ge=1, le=1000)):
    return utils.get_response(200, {"batches": youtube_batch_store.list_batches(limit)})


@router.get("/youtube/batches/{batch_id}", summary="Get a durable YouTube batch")
def youtube_batch_get(request: Request, batch_id: str = Path(..., pattern=SAFE_ID_PATTERN)):
    return utils.get_response(200, _batch_or_404(batch_id))


@router.post("/youtube/batches/{batch_id}/pause", summary="Pause a YouTube batch")
def youtube_batch_pause(request: Request, batch_id: str = Path(..., pattern=SAFE_ID_PATTERN)):
    _batch_or_404(batch_id)
    return utils.get_response(200, youtube_batch_runner.pause(batch_id))


@router.post("/youtube/batches/{batch_id}/resume", summary="Resume a YouTube batch")
def youtube_batch_resume(request: Request, batch_id: str = Path(..., pattern=SAFE_ID_PATTERN)):
    _batch_or_404(batch_id)
    return utils.get_response(200, youtube_batch_runner.start(batch_id, explicit_resume=True))


@router.post("/youtube/batches/{batch_id}/retry", summary="Retry failed YouTube batch items")
def youtube_batch_retry(
    request: Request,
    body: YouTubeBatchRetryRequest | None = None,
    batch_id: str = Path(..., pattern=SAFE_ID_PATTERN),
):
    batch = _batch_or_404(batch_id)
    publish_slots = body.publish_slots if body else {}
    allow_shared_publish_time = bool(
        batch.get("settings", {}).get("allow_shared_publish_time", False)
    )
    reserved_slots = (
        youtube_batch_store.reserved_publish_slots(batch_id) if publish_slots else set()
    )
    for index, slot in publish_slots.items():
        if index < 0 or index >= len(batch.get("items", [])):
            raise HttpException(batch_id, status_code=400, message=f"Invalid item index: {index}")
        publish_at = str(slot.get("publish_at", "")).strip()
        if not publish_at or (
            publish_at in reserved_slots and not allow_shared_publish_time
        ):
            raise HttpException(
                batch_id,
                status_code=409,
                message="YouTube publish slot is missing or already reserved",
            )
        reserved_slots.add(publish_at)
        youtube_batch_store.update_item(batch, index, current_publish_slot=slot)
    return utils.get_response(200, youtube_batch_runner.retry(batch_id))


@router.post("/youtube/batches/{batch_id}/cancel", summary="Cancel a YouTube batch")
def youtube_batch_cancel(request: Request, batch_id: str = Path(..., pattern=SAFE_ID_PATTERN)):
    _batch_or_404(batch_id)
    return utils.get_response(200, youtube_batch_runner.cancel(batch_id))


def _upload_scanned_video(
    task_id: str,
    schedule: bool = False,
    slot_index: int | None = None,
    move_after: bool = False,
    privacy_status: str = "",
    publish_slot: dict | None = None,
    index: int = 1,
) -> dict:
    youtube_uploader.sync_from_disk()
    video = _find_scanned_video(task_id, index)
    if not video:
        raise HttpException(task_id=task_id, status_code=404, message="video not found")

    if video.get("youtube_status") in {"completed", "scheduled"}:
        return {"success": True, "skipped": True, "reason": "already uploaded", "video": video}

    if not youtube_uploader.is_configured():
        raise HttpException(
            task_id=task_id,
            status_code=400,
            message="YouTube is not configured. Fill [youtube] in config.toml.",
        )

    metadata = _metadata_for_video(video)
    publish_at = ""
    if schedule:
        if publish_slot is None:
            publish_slot = (
                youtube_uploader.create_publish_plan(slot_index + 1, avoid_existing=False)[slot_index]
                if slot_index is not None
                else youtube_uploader.next_publish_slot()
            )
        publish_at = publish_slot["publish_at"]

    result = youtube_uploader.upload_video(
        video_path=video["video_path"],
        title=metadata["title"],
        description=metadata["description"],
        tags=metadata["tags"],
        publish_at=publish_at,
        privacy_status=privacy_status,
    )
    status = "scheduled" if result.get("scheduled") else "completed"
    if not result.get("success"):
        status = "failed"

    upload_tracker.add_entry(
        task_id=task_id,
        index=index,
        subject=video.get("subject", ""),
        video_path=video["video_path"],
        status=status,
        youtube_id=result.get("video_id", ""),
        youtube_url=result.get("url", ""),
        publish_at=result.get("publish_at", ""),
        publish_at_local=(publish_slot or {}).get("publish_at_local", ""),
        schedule_mode=(publish_slot or {}).get("schedule_mode", ""),
        error=result.get("error", ""),
    )

    moved_to = ""
    if result.get("success") and move_after:
        moved_to = move_uploaded_task(task_id)

    return {"success": bool(result.get("success")), "result": result, "moved_to": moved_to}


@router.get("/youtube/scanner", summary="Scan generated videos for YouTube upload")
def youtube_scanner(
    request: Request,
    status: str = Query("", description="Optional status filter: pending, scheduled, completed, failed"),
):
    data = scan_pending_videos(status_filter=status)
    data["api_slots_remaining"] = youtube_uploader.remaining_api_slots()
    data["api_slots_max"] = youtube_uploader.daily_api_limit
    return utils.get_response(200, data)


@router.get("/youtube/uploads", summary="List YouTube upload log")
def youtube_uploads(request: Request):
    uploads = upload_tracker.load()
    return utils.get_response(200, {"total": len(uploads), "uploads": uploads})


@router.post("/youtube/authorize", summary="Authorize YouTube OAuth")
def youtube_authorize(request: Request):
    request_id = base.get_task_id(request)
    try:
        return utils.get_response(200, youtube_uploader.authorize())
    except Exception as exc:
        raise HttpException(request_id, status_code=400, message=str(exc))


@router.post("/youtube/upload/{task_id}", summary="Upload one generated video to YouTube")
def youtube_upload_task(
    request: Request,
    task_id: str = Path(..., description="Task ID", pattern=SAFE_ID_PATTERN),
    move_after: bool = Query(False, description="Move task folder to storage/uploaded after success"),
    privacy_status: str = Query("", description="Override privacy: public, private, unlisted"),
    index: int = Query(1, ge=1, description="Generated video index"),
):
    return utils.get_response(
        200,
        _upload_scanned_video(
            task_id,
            schedule=False,
            move_after=move_after,
            privacy_status=privacy_status,
            index=index,
        ),
    )


@router.post("/youtube/schedule/{task_id}", summary="Schedule one generated video on YouTube")
def youtube_schedule_task(
    request: Request,
    task_id: str = Path(..., description="Task ID", pattern=SAFE_ID_PATTERN),
    slot_index: int | None = Query(None, ge=0, description="Optional index among the next available schedule slots"),
    move_after: bool = Query(False, description="Move task folder to storage/uploaded after success"),
    privacy_status: str = Query("", description="Ignored for scheduled uploads; YouTube requires private until publishAt"),
    schedule_mode: str = Query("", description="interval or daily_block"),
    start_date: str = Query("", description="Optional local start date: YYYY-MM-DD"),
    schedule_at: str = Query("", description="Local start time: HH:MM"),
    videos_per_day: int | None = Query(None, ge=1, le=60),
    interval_minutes: int | None = Query(None, ge=1, le=1440),
    index: int = Query(1, ge=1, description="Generated video index"),
):
    youtube_uploader.sync_from_disk()
    try:
        plan_size = (slot_index + 1) if slot_index is not None else 1
        publish_plan = youtube_uploader.create_publish_plan(
            plan_size,
            start_date=start_date or None,
            schedule_mode=schedule_mode,
            schedule_at=schedule_at,
            videos_per_day=videos_per_day,
            interval_minutes=interval_minutes,
            avoid_existing=True,
        )
        publish_slot = publish_plan[slot_index or 0]
    except (ValueError, RuntimeError) as exc:
        raise HttpException(task_id, status_code=400, message=str(exc)) from exc
    return utils.get_response(
        200,
        _upload_scanned_video(
            task_id,
            schedule=True,
            slot_index=slot_index,
            move_after=move_after,
            privacy_status=privacy_status,
            publish_slot=publish_slot,
            index=index,
        ),
    )


@router.post("/youtube/upload-pending", summary="Upload or schedule all pending videos")
def youtube_upload_pending(
    request: Request,
    schedule: bool = Query(False, description="Schedule instead of immediate upload"),
    move_after: bool = Query(False, description="Move task folders after successful upload"),
    limit: int = Query(0, ge=0, description="0 means no limit"),
    interval_minutes: int = Query(5, ge=0, description="Minutes to wait between uploads"),
    privacy_status: str = Query("", description="Override privacy: public, private, unlisted"),
    schedule_mode: str = Query("", description="interval or daily_block"),
    start_date: str = Query("", description="Optional local start date: YYYY-MM-DD"),
    schedule_at: str = Query("", description="Local start time: HH:MM"),
    videos_per_day: int | None = Query(None, ge=1, le=60),
    publish_interval_minutes: int | None = Query(None, ge=1, le=1440),
):
    youtube_uploader.sync_from_disk()
    pending = scan_pending_videos(status_filter="pending")["videos"]
    if limit:
        pending = pending[:limit]

    publish_plan = []
    if schedule:
        try:
            publish_plan = youtube_uploader.create_publish_plan(
                len(pending),
                start_date=start_date or None,
                schedule_mode=schedule_mode,
                schedule_at=schedule_at,
                videos_per_day=videos_per_day,
                interval_minutes=publish_interval_minutes,
            )
        except (ValueError, RuntimeError) as exc:
            raise HttpException("youtube-schedule", status_code=400, message=str(exc)) from exc

    results = []
    schedule_cursor = 0
    for index, video in enumerate(pending):
        if youtube_uploader.remaining_api_slots() <= 0:
            results.append(
                {
                    "task_id": video["task_id"],
                    "success": False,
                    "skipped": True,
                    "reason": "daily_api_limit_reached",
                }
            )
            continue

        if index > 0 and interval_minutes:
            time.sleep(interval_minutes * 60)

        upload_result = _upload_scanned_video(
            video["task_id"],
            schedule=schedule,
            move_after=move_after,
            privacy_status=privacy_status,
            publish_slot=publish_plan[schedule_cursor] if schedule else None,
            index=int(video.get("index", 1)),
        )
        results.append({"task_id": video["task_id"], **upload_result})
        if schedule and upload_result.get("success"):
            schedule_cursor += 1
    return utils.get_response(
        200,
        {
            "total": len(results),
            "schedule": schedule,
            "interval_minutes": interval_minutes,
            "publish_plan": publish_plan,
            "api_slots_remaining": youtube_uploader.remaining_api_slots(),
            "api_slots_max": youtube_uploader.daily_api_limit,
            "results": results,
        },
    )
