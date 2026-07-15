import html
import os
import secrets
import time
from datetime import datetime

from fastapi import Depends, Path, Query, Request
from fastapi.responses import HTMLResponse

from app.controllers.v1.base import new_router
from app.config import config
from app.models.exception import HttpException
from app.services.tiktok_scheduler import tiktok_scheduler
from app.services.tiktok_uploader import (
    parse_tiktok_metadata_file,
    scan_tiktok_videos,
    tiktok_upload_tracker,
    tiktok_uploader,
)
from app.utils import utils


def _require_tiktok_api_access(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host in {"127.0.0.1", "::1", "localhost", "testclient"}:
        return
    expected_token = os.getenv("TIKTOK_API_TOKEN", "")
    supplied_token = request.headers.get("X-TikTok-API-Token", "")
    if bool(config.tiktok.get("allow_remote_api", False)) and expected_token and secrets.compare_digest(
        supplied_token, expected_token
    ):
        return
    raise HttpException("tiktok-access", status_code=403, message="TikTok API is restricted to localhost")


router = new_router(dependencies=[Depends(_require_tiktok_api_access)])


def _find_video(task_id: str, index: int = 1) -> dict:
    return next(
        (item for item in scan_tiktok_videos()["videos"] if item["task_id"] == task_id and int(item.get("index", 1)) == index),
        {},
    )


def _metadata(video: dict) -> dict:
    metadata = parse_tiktok_metadata_file(video.get("metadata_path", ""))
    metadata["title"] = metadata.get("title") or video.get("subject") or "TikTok video"
    metadata["caption"] = metadata.get("caption") or metadata["title"]
    return metadata


def _upload(task_id: str, index: int = 1, caption: str = "", privacy_level: str = "") -> dict:
    tiktok_uploader.sync_from_disk()
    video = _find_video(task_id, index)
    if not video:
        raise HttpException(task_id, status_code=404, message="video not found")
    if video.get("tiktok_status") in {
        "scheduled", "scheduled_retry", "uploading", "processing", "reconcile_required", "published"
    }:
        return {"success": True, "skipped": True, "reason": "already uploaded", "video": video}
    if not tiktok_uploader.is_configured():
        raise HttpException(task_id, status_code=400, message="TikTok is not configured")
    metadata = _metadata(video)
    if not tiktok_upload_tracker.claim(
        task_id, index, video.get("subject", ""), video["video_path"], tiktok_uploader.provider
    ):
        return {"success": True, "skipped": True, "reason": "upload already active", "video": video}
    result = tiktok_uploader.upload_video(
        video["video_path"], caption or metadata["caption"], privacy_level,
        idempotency_key=f"{task_id}-{index}",
    )
    status = (
        result.get("status", "processing")
        if result.get("success")
        else "reconcile_required" if result.get("publish_id") else "failed"
    )
    tiktok_upload_tracker.add_entry(
        task_id, index, video.get("subject", ""), video["video_path"], status,
        provider=result.get("provider", tiktok_uploader.provider),
        publish_id=result.get("publish_id", ""), tiktok_url=result.get("tiktok_url", ""), error=result.get("error", ""),
    )
    return result


@router.get("/tiktok/status", summary="TikTok integration status")
def tiktok_status(request: Request):
    tiktok_uploader.sync_from_disk()
    return utils.get_response(200, {
        "configured": tiktok_uploader.is_configured(),
        "authorized": tiktok_uploader.is_authorized(),
        "provider": tiktok_uploader.provider,
        "remaining_upload_slots": tiktok_uploader.remaining_upload_slots(),
        "daily_upload_limit": tiktok_uploader.daily_upload_limit,
    })


@router.post("/tiktok/authorize", summary="Start TikTok OAuth")
def tiktok_authorize(request: Request):
    try:
        tiktok_uploader.sync_from_disk()
        return utils.get_response(200, tiktok_uploader.authorization_url())
    except Exception as exc:
        raise HttpException("tiktok-oauth", status_code=400, message=str(exc))


@router.get("/tiktok/callback", response_class=HTMLResponse, summary="TikTok OAuth callback")
def tiktok_callback(
    code: str = Query(""),
    state: str = Query(""),
    error: str = Query(""),
    error_description: str = Query(""),
):
    if error:
        detail = error_description or error
        return HTMLResponse(f"<h2>TikTok authorization failed</h2><p>{html.escape(detail)}</p>", status_code=400)
    try:
        tiktok_uploader.exchange_code(code, state)
        return HTMLResponse("<h2>TikTok authorization completed</h2><p>You can close this window.</p>")
    except Exception as exc:
        return HTMLResponse(f"<h2>TikTok authorization failed</h2><p>{html.escape(str(exc))}</p>", status_code=400)


@router.post("/tiktok/disconnect", summary="Remove TikTok OAuth token")
def tiktok_disconnect(request: Request):
    tiktok_uploader.disconnect()
    return utils.get_response(200, {"success": True})


@router.get("/tiktok/creator-info", summary="Get TikTok creator posting options")
def creator_info(request: Request):
    try:
        tiktok_uploader.sync_from_disk()
        return utils.get_response(200, tiktok_uploader.creator_info())
    except Exception as exc:
        raise HttpException("tiktok-creator", status_code=400, message=str(exc))


@router.get("/tiktok/scanner", summary="Scan generated videos for TikTok")
def scanner(request: Request, status: str = Query("")):
    data = scan_tiktok_videos(status)
    data["remaining_upload_slots"] = tiktok_uploader.remaining_upload_slots()
    data["daily_upload_limit"] = tiktok_uploader.daily_upload_limit
    return utils.get_response(200, data)


@router.get("/tiktok/uploads", summary="List TikTok upload log")
def uploads(request: Request):
    entries = tiktok_upload_tracker.load()
    return utils.get_response(200, {"total": len(entries), "uploads": entries})


@router.get("/tiktok/schedule", summary="List TikTok scheduled jobs")
def schedule(request: Request):
    jobs = tiktok_scheduler.load()
    return utils.get_response(200, {"total": len(jobs), "jobs": jobs})


@router.post("/tiktok/upload/{task_id}", summary="Upload one video to TikTok")
def upload_task(
    request: Request,
    task_id: str = Path(...),
    index: int = Query(1, ge=1),
    caption: str = Query("", max_length=2200),
    privacy_level: str = Query(""),
):
    return utils.get_response(200, _upload(task_id, index, caption, privacy_level))


@router.post("/tiktok/schedule/{task_id}", summary="Schedule one TikTok upload locally")
def schedule_task(
    request: Request,
    task_id: str = Path(...),
    index: int = Query(1, ge=1),
    scheduled_at: str = Query(""),
    slot_index: int = Query(0, ge=0),
    caption: str = Query("", max_length=2200),
    privacy_level: str = Query(""),
):
    tiktok_uploader.sync_from_disk()
    if not tiktok_uploader.is_configured():
        raise HttpException(task_id, status_code=400, message="TikTok is not configured")
    video = _find_video(task_id, index)
    if not video:
        raise HttpException(task_id, status_code=404, message="video not found")
    if video.get("tiktok_status") in {
        "scheduled", "scheduled_retry", "uploading", "processing", "reconcile_required", "published"
    }:
        raise HttpException(task_id, status_code=409, message="video is already scheduled or uploaded")
    metadata = _metadata(video)
    when = scheduled_at or tiktok_scheduler.calculate_scheduled_at(slot_index)
    try:
        datetime.fromisoformat(when.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HttpException(task_id, status_code=400, message="invalid scheduled_at") from exc
    job = tiktok_scheduler.add_job(
        task_id, video.get("subject", ""), video["video_path"], caption or metadata["caption"],
        when, tiktok_uploader.provider, privacy_level or tiktok_uploader.privacy_level,
        tiktok_uploader.allow_comments, tiktok_uploader.allow_duet, tiktok_uploader.allow_stitch, index=index,
    )
    return utils.get_response(200, job)


@router.delete("/tiktok/schedule/{job_id}", summary="Cancel scheduled TikTok upload")
def cancel_schedule(request: Request, job_id: str = Path(...)):
    if not tiktok_scheduler.cancel(job_id):
        raise HttpException(job_id, status_code=404, message="pending schedule not found")
    return utils.get_response(200, {"success": True})


@router.post("/tiktok/upload-pending", summary="Upload or schedule pending TikTok videos")
def upload_pending(
    request: Request,
    schedule: bool = Query(False),
    limit: int = Query(0, ge=0),
    interval_minutes: int = Query(5, ge=0, le=240),
    privacy_level: str = Query(""),
):
    if not tiktok_uploader.is_configured():
        raise HttpException("tiktok-pending", status_code=400, message="TikTok is not configured")
    pending = scan_tiktok_videos("pending")["videos"]
    if limit:
        pending = pending[:limit]
    results = []
    existing_pending_jobs = sum(1 for job in tiktok_scheduler.load() if job.get("status") == "pending")
    for index, video in enumerate(pending):
        if schedule:
            metadata = _metadata(video)
            when = tiktok_scheduler.calculate_scheduled_at(existing_pending_jobs + index)
            result = tiktok_scheduler.add_job(
                video["task_id"], video.get("subject", ""), video["video_path"], metadata["caption"], when,
                tiktok_uploader.provider, privacy_level or tiktok_uploader.privacy_level,
                tiktok_uploader.allow_comments, tiktok_uploader.allow_duet, tiktok_uploader.allow_stitch,
                index=int(video.get("index", 1)),
            )
        else:
            if index and interval_minutes:
                time.sleep(interval_minutes * 60)
            result = _upload(video["task_id"], int(video.get("index", 1)), privacy_level=privacy_level)
        results.append({"task_id": video["task_id"], "index": video.get("index", 1), "result": result})
    return utils.get_response(200, {"total": len(results), "schedule": schedule, "results": results})


@router.post("/tiktok/refresh-status/{task_id}", summary="Refresh TikTok publish status")
def refresh_status(request: Request, task_id: str = Path(...), index: int = Query(1, ge=1)):
    entry = tiktok_upload_tracker.get_by_task_id(task_id, index)
    if not entry or not entry.get("publish_id"):
        raise HttpException(task_id, status_code=404, message="TikTok publish ID not found")
    try:
        result = tiktok_uploader.fetch_status(entry["publish_id"], entry.get("provider", ""))
    except Exception as exc:
        raise HttpException(task_id, status_code=400, message=str(exc))
    if result.get("success") is False:
        raise HttpException(task_id, status_code=502, message=result.get("error") or "TikTok status provider unavailable")
    raw_status = str(result.get("status", "processing"))
    upper_status = raw_status.upper()
    if raw_status.lower() in {"published", "failed", "processing"}:
        normalized = raw_status.lower()
    else:
        normalized = "published" if upper_status in {"PUBLISH_COMPLETE", "PUBLISHED", "COMPLETED"} else "failed" if "FAIL" in upper_status else "processing"
    status_data = result.get("data") or {}
    post_ids = status_data.get("publicaly_available_post_id") or status_data.get("publicly_available_post_id") or []
    post_id = result.get("post_id") or (str(post_ids[0]) if isinstance(post_ids, list) and post_ids else "")
    tiktok_upload_tracker.update_status(
        task_id,
        normalized,
        index=index,
        publish_status=upper_status,
        post_id=post_id,
        tiktok_url=result.get("tiktok_url", ""),
        error=result.get("error", "") if normalized == "failed" else "",
    )
    return utils.get_response(200, {**result, "normalized_status": normalized})
