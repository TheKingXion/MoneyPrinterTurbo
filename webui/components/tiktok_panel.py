import os

import streamlit as st
from uuid import uuid4

from app.config import config
from app.services import task as tm
from app.services.tiktok_scheduler import tiktok_scheduler
from app.services.tiktok_uploader import (
    parse_tiktok_metadata_file,
    scan_tiktok_videos,
    tiktok_upload_tracker,
    tiktok_uploader,
)


def _render_settings(tr) -> None:
    tiktok_uploader.sync_from_config()
    left, right = st.columns(2)
    with left:
        provider = st.selectbox(
            tr("Provider"), ["official", "upload_post"],
            index=0 if config.tiktok.get("provider", "official") == "official" else 1,
            key="tt_settings_provider",
        )
        client_key = config.tiktok.get("client_key", "")
        client_secret = config.tiktok.get("client_secret", "")
        redirect_uri = config.tiktok.get("redirect_uri", "")
        upload_post_api_key = config.tiktok.get("upload_post_api_key", "")
        upload_post_username = config.tiktok.get("upload_post_username", "")
        if provider == "official":
            client_key = st.text_input(tr("TikTok Client Key"), client_key, type="password", key="tt_settings_client_key")
            client_secret = st.text_input(tr("TikTok Client Secret"), client_secret, type="password", key="tt_settings_client_secret")
            redirect_uri = st.text_input(tr("TikTok Redirect URI"), redirect_uri, key="tt_settings_redirect_uri")
            if st.button(tr("Authorize TikTok"), key="tt_settings_authorize"):
                tiktok_uploader.sync_from_config()
                try:
                    if not tiktok_uploader.is_configured():
                        raise RuntimeError(tr("Save enabled TikTok settings before authorizing"))
                    authorization = tiktok_uploader.authorization_url()
                    st.link_button(tr("Open TikTok Authorization"), authorization["authorization_url"])
                except Exception as exc:
                    st.error(str(exc))
        else:
            upload_post_api_key = st.text_input(tr("Upload-Post API Key"), upload_post_api_key, type="password", key="tt_settings_upload_post_key")
            upload_post_username = st.text_input(tr("Upload-Post Username"), upload_post_username, key="tt_settings_upload_post_user")
    with right:
        enabled = st.checkbox(tr("Enable TikTok Upload"), bool(config.tiktok.get("enabled", False)), key="tt_settings_enabled")
        auto_upload = st.checkbox(tr("Auto-upload after generation"), bool(config.tiktok.get("auto_upload", False)), key="tt_settings_auto_upload")
        schedule_enabled = st.checkbox(tr("Schedule uploads"), bool(config.tiktok.get("schedule_enabled", False)), key="tt_settings_schedule")
        privacy_options = ["SELF_ONLY", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "PUBLIC_TO_EVERYONE"]
        saved_privacy = config.tiktok.get("privacy_level", "SELF_ONLY")
        privacy_level = st.selectbox(
            tr("TikTok Privacy"), privacy_options,
            index=privacy_options.index(saved_privacy) if saved_privacy in privacy_options else 0,
            key="tt_settings_privacy",
        )
        flags = st.columns(3)
        allow_comments = flags[0].checkbox(tr("Comments"), bool(config.tiktok.get("allow_comments", True)), key="tt_settings_comments")
        allow_duet = flags[1].checkbox(tr("Duet"), bool(config.tiktok.get("allow_duet", False)), key="tt_settings_duet")
        allow_stitch = flags[2].checkbox(tr("Stitch"), bool(config.tiktok.get("allow_stitch", False)), key="tt_settings_stitch")
        schedule_at = st.text_input(tr("Schedule Time"), config.tiktok.get("schedule_at", "21:00"), key="tt_settings_time")
        schedule_interval_minutes = st.number_input(tr("Schedule Interval Minutes"), 1, 240, int(config.tiktok.get("schedule_interval_minutes", 30)), key="tt_settings_interval")
        daily_upload_limit = st.number_input(tr("Local Daily Upload Limit"), 1, 100, int(config.tiktok.get("daily_upload_limit", 10)), key="tt_settings_limit")
        if st.button(tr("Save TikTok Settings"), key="tt_settings_save", type="primary", use_container_width=True):
            config.tiktok.update({
                "provider": provider,
                "client_key": client_key,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "upload_post_api_key": upload_post_api_key,
                "upload_post_username": upload_post_username,
                "enabled": enabled,
                "auto_upload": auto_upload,
                "schedule_enabled": schedule_enabled,
                "privacy_level": privacy_level,
                "allow_comments": allow_comments,
                "allow_duet": allow_duet,
                "allow_stitch": allow_stitch,
                "schedule_at": schedule_at,
                "schedule_interval_minutes": schedule_interval_minutes,
                "daily_upload_limit": daily_upload_limit,
            })
            config.save_config()
            tiktok_uploader.sync_from_config()
            st.success(tr("TikTok settings saved"))


def _schedule(item: dict, slot: int, privacy: str) -> dict:
    metadata = parse_tiktok_metadata_file(item.get("metadata_path", ""))
    return tiktok_scheduler.add_job(
        task_id=item["task_id"],
        subject=item.get("subject", ""),
        video_path=item["video_path"],
        caption=metadata.get("caption") or item.get("subject", ""),
        scheduled_at=tiktok_scheduler.calculate_scheduled_at(slot),
        provider=tiktok_uploader.provider,
        privacy_level=privacy,
        allow_comment=tiktok_uploader.allow_comments,
        allow_duet=tiktok_uploader.allow_duet,
        allow_stitch=tiktok_uploader.allow_stitch,
        index=int(item.get("index", 1)),
    )


def _upload(item: dict, privacy: str) -> dict:
    metadata = parse_tiktok_metadata_file(item.get("metadata_path", ""))
    caption = metadata.get("caption") or item.get("subject", "")
    index = int(item.get("index", 1))
    if not tiktok_upload_tracker.claim(
        item["task_id"], index, item.get("subject", ""), item["video_path"],
        tiktok_uploader.provider,
    ):
        return {"success": True, "skipped": True}
    result = tiktok_uploader.upload_video(
        item["video_path"], caption, privacy_level=privacy,
        idempotency_key=f"{item['task_id']}-{index}",
    )
    tiktok_upload_tracker.add_entry(
        item["task_id"], index, item.get("subject", ""), item["video_path"],
        result.get("status", "processing") if result.get("success") else "failed",
        provider=result.get("provider", tiktok_uploader.provider),
        publish_id=result.get("publish_id", ""),
        tiktok_url=result.get("tiktok_url", ""),
        error=result.get("error", ""),
    )
    return result


def _active_pending_jobs() -> int:
    return sum(1 for job in tiktok_scheduler.load() if job.get("status") == "pending")


def _preflight(tr, required_slots: int = 1) -> bool:
    tiktok_uploader.sync_from_config()
    if not tiktok_uploader.is_configured():
        st.error(tr("TikTok must be enabled and configured before starting"))
        return False
    if not tiktok_uploader.is_authorized():
        st.error(tr("Authorize TikTok before starting"))
        return False
    if required_slots > tiktok_uploader.remaining_upload_slots():
        st.error(tr("Not enough TikTok upload slots available"))
        return False
    return True


def _render_scanner(tr, batch=False, base_params=None) -> None:
    scanned = scan_tiktok_videos()
    pending = [item for item in scanned.get("videos", []) if item.get("tiktok_status") in {"pending", "failed"}]
    privacy_options = ["SELF_ONLY", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "PUBLIC_TO_EVERYONE"]
    saved_privacy = config.tiktok.get("privacy_level", "SELF_ONLY")
    privacy = st.selectbox(
        tr("TikTok Privacy"), privacy_options,
        index=privacy_options.index(saved_privacy) if saved_privacy in privacy_options else 0,
        key="tt_batch_privacy" if batch else "tt_scanner_privacy",
    )
    if batch:
        count = int(st.number_input(tr("Total videos"), 1, 20, 5, key="tt_batch_count"))
        subjects_text = st.text_area(
            tr("TikTok Batch Subjects"),
            value="\n".join(item.get("subject", "") for item in pending[:count]),
            height=130,
            key="tt_batch_subjects",
        )
        schedule_batch = st.checkbox(tr("Schedule uploads"), True, key="tt_batch_schedule")
        subjects = [line.strip() for line in subjects_text.splitlines() if line.strip()][:count]
        st.dataframe([{"#": i + 1, tr("Video Subject"): subject} for i, subject in enumerate(subjects)], hide_index=True, use_container_width=True)
        if st.button(tr("Start TikTok Batch"), key="tt_batch_start", type="primary", use_container_width=True, disabled=not subjects):
            if not _preflight(tr, len(subjects)):
                return
            queue_size = _active_pending_jobs()
            completed = 0
            with st.status(tr("Batch in progress..."), expanded=True) as status:
                for index, subject in enumerate(subjects):
                    status.write(f"{tr('Generating')}: {subject}")
                    task_id = str(uuid4())
                    params = base_params.model_copy(deep=True)
                    params.video_subject = subject
                    params.video_script = ""
                    params.video_terms = ""
                    params.video_count = 1
                    try:
                        result = tm.start(
                            task_id=task_id,
                            params=params,
                            suppress_tiktok_upload=True,
                            suppress_youtube_upload=True,
                        )
                        videos = result.get("videos", []) if result else []
                        if not videos:
                            raise RuntimeError("video generation returned no output")
                        item = {
                            "task_id": task_id,
                            "index": 1,
                            "subject": subject,
                            "video_path": videos[0],
                            "metadata_path": os.path.join(os.path.dirname(videos[0]), "METADATOS.md"),
                        }
                        action = _schedule(item, queue_size + index, privacy) if schedule_batch else _upload(item, privacy)
                        if action.get("error") or (not schedule_batch and not action.get("success")):
                            raise RuntimeError(action.get("error", "TikTok upload failed"))
                        completed += 1
                    except Exception as exc:
                        status.write(f"{tr('Failed')}: {subject} - {exc}")
                status.update(label=tr("Batch finished"), state="complete")
            st.success(f"{tr('Batch finished')}: {completed}/{len(subjects)}")
            st.rerun()
        return
    metrics = st.columns(5)
    for column, label, field in zip(metrics, ("Total", "Pending", "Scheduled", "Published", "Failed"), ("total", "pending", "scheduled", "published", "failed")):
        column.metric(tr(label), scanned.get(field, 0))
    if st.button(tr("Refresh Scanner"), key="tt_scanner_refresh"):
        st.rerun()
    for item in scanned.get("videos", []):
        with st.container(border=True):
            cols = st.columns([4, 1, 1])
            cols[0].write(f"**{item.get('subject') or item.get('task_id')}**")
            cols[1].write(item.get("tiktok_status", "pending"))
            if item in pending:
                actions = cols[2].columns(2)
                if actions[0].button(tr("Upload"), key=f"tt_scanner_upload_{item['task_id']}_{item.get('index', 1)}"):
                    if _preflight(tr):
                        result = _upload(item, privacy)
                        st.success(tr("Uploading")) if result.get("success") else st.error(result.get("error", "error"))
                        st.rerun()
                if actions[1].button(tr("Schedule"), key=f"tt_scanner_schedule_{item['task_id']}_{item.get('index', 1)}"):
                    if _preflight(tr):
                        _schedule(item, _active_pending_jobs(), privacy)
                        st.rerun()


def _render_queue(tr) -> None:
    jobs = tiktok_scheduler.load()
    for job in sorted(jobs, key=lambda value: value.get("scheduled_at", "")):
        with st.container(border=True):
            cols = st.columns([4, 2, 1])
            cols[0].write(f"**{job.get('subject') or job.get('task_id')}**")
            cols[1].write(f"{job.get('status', '')} | {job.get('scheduled_at', '')}")
            if job.get("status") == "pending" and cols[2].button(tr("Cancel"), key=f"tt_queue_cancel_{job['job_id']}"):
                tiktok_scheduler.cancel(job["job_id"])
                st.rerun()
    if not jobs:
        st.info(tr("No scheduled TikTok uploads"))


def render(tr, base_params) -> None:
    with st.expander(tr("TikTok Uploads and Scanner"), expanded=False):
        settings, scanner, batch, queue, log = st.tabs([tr("TikTok Settings"), tr("Scanner"), tr("Batch Generator"), tr("Schedule Queue"), tr("Upload Log")])
        with settings:
            _render_settings(tr)
        with scanner:
            _render_scanner(tr)
        with batch:
            _render_scanner(tr, batch=True, base_params=base_params)
        with queue:
            _render_queue(tr)
        with log:
            entries = tiktok_upload_tracker.load()
            st.dataframe(entries, hide_index=True, use_container_width=True) if entries else st.info(tr("No TikTok uploads yet"))
