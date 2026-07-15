from datetime import datetime, timedelta

import streamlit as st

from app.config import config
from app.models.schema import VideoParams
from app.services import llm
from app.services.youtube_batch import (
    audit_story_idea,
    collect_task_subjects,
    validate_unique_ideas,
    youtube_batch_store,
)
from app.services.youtube_batch_runner import youtube_batch_runner
from app.services.youtube_uploader import (
    parse_metadata_file,
    scan_pending_videos,
    upload_tracker,
    youtube_uploader,
)


DEFAULT_BATCH_SCRIPT_PROMPT = """Escribe un guion viral, original y sorprendente para YouTube Shorts.

Comienza con un gancho muy fuerte en la primera frase, capaz de generar curiosidad inmediata y retener al espectador. Escribe entre 2 y 4 oraciones en cada párrafo.

Desarrolla la historia a partir del tema preparado, con tensión narrativa creciente, ritmo rápido y frases fáciles de escuchar. Destaca un detalle inesperado solo si está incluido explícitamente en el tema preparado; nunca inventes uno.

Usa el idioma configurado con un tono natural, claro y emocional. Evita el relleno, las repeticiones, las introducciones lentas y las frases genéricas.

Termina con un cierre memorable, contundente y sorprendente, que deje una reflexión o sensación fuerte. No incluyas llamados a seguir cuentas, comentar, compartir ni suscribirse."""

_BATCH_DRAFT_STATE_KEYS = (
    "yt_batch_draft_context",
    "yt_batch_idea_rows",
    "yt_batch_idea_editor",
)


def _clear_batch_draft(state) -> None:
    for key in _BATCH_DRAFT_STATE_KEYS:
        state.pop(key, None)


def _build_batch_video_params(
    base_params: VideoParams,
    script_prompt: str,
    paragraph_number: int,
    bgm_type: str,
    bgm_file: str = "",
) -> VideoParams:
    params = base_params.model_copy(deep=True)
    params.video_subject = ""
    params.video_script = ""
    params.video_terms = ""
    params.video_count = 1
    params.video_script_prompt = script_prompt.strip()
    params.paragraph_number = paragraph_number
    params.bgm_type = bgm_type
    params.bgm_file = bgm_file.strip() if bgm_type == "custom" else ""
    return params


def _status_counts(batch: dict) -> dict:
    items = batch.get("items", [])
    return {
        "generated": sum(i.get("generation_status") == "generated" for i in items),
        "generation_failed": sum(i.get("generation_status") == "failed" for i in items),
        "uploaded": sum(i.get("upload_status") in {"uploaded", "scheduled"} for i in items),
        "upload_failed": sum(i.get("upload_status") in {"failed", "waiting_quota", "needs_review"} for i in items),
    }


@st.fragment(run_every="2s")
def _render_progress(tr, batch_id: str) -> None:
    batch = youtube_batch_store.load(batch_id)
    if not batch:
        st.warning("Batch manifest is unavailable")
        return
    counts = _status_counts(batch)
    total = max(1, int(batch.get("requested", 0)))
    st.progress((counts["generated"] + counts["uploaded"]) / (total * 2))
    metrics = st.columns(4)
    metrics[0].metric(tr("Generated"), f'{counts["generated"]}/{total}')
    metrics[1].metric(tr("Uploaded"), f'{counts["uploaded"]}/{total}')
    metrics[2].metric(tr("Failed"), counts["generation_failed"] + counts["upload_failed"])
    metrics[3].metric(tr("Status"), batch.get("status", "pending"))
    controls = st.columns(4)
    control = batch.get("control", "running")
    if controls[0].button(tr("Pause"), key=f"yt_batch_pause_{batch_id}", disabled=control != "running"):
        youtube_batch_runner.pause(batch_id)
        st.rerun(scope="fragment")
    if controls[1].button(tr("Resume"), key=f"yt_batch_resume_{batch_id}", disabled=control != "paused"):
        youtube_batch_runner.start(batch_id, explicit_resume=True)
        st.rerun(scope="fragment")
    if controls[2].button(tr("Retry"), key=f"yt_batch_retry_{batch_id}"):
        youtube_batch_runner.retry(batch_id)
        st.rerun(scope="fragment")
    if controls[3].button(tr("Cancel"), key=f"yt_batch_cancel_{batch_id}", disabled=control == "cancelled"):
        youtube_batch_runner.cancel(batch_id)
        st.rerun(scope="fragment")
    st.dataframe(
        [{
            "#": item.get("index", 0) + 1,
            tr("Video Subject"): item.get("subject", ""),
            tr("Publishing Title"): item.get("title_override") or item.get("generated_title", ""),
            tr("Generated"): item.get("generation_status", "pending"),
            tr("Uploaded"): item.get("upload_status", "pending"),
            tr("Schedule Time"): item.get("current_publish_slot", {}).get("publish_at_local", ""),
            tr("Failed"): item.get("error", ""),
        } for item in batch.get("items", [])],
        hide_index=True,
        use_container_width=True,
    )


def _render_settings(tr) -> None:
    youtube_uploader.sync_from_config()
    cols = st.columns(2)
    with cols[0]:
        client_id = st.text_input(tr("YouTube Client ID"), config.youtube.get("client_id", ""), type="password", key="yt_settings_client_id")
        client_secret = st.text_input(tr("YouTube Client Secret"), config.youtube.get("client_secret", ""), type="password", key="yt_settings_client_secret")
        if st.button(tr("Authorize YouTube"), key="yt_settings_authorize"):
            youtube_uploader.sync_from_config()
            try:
                if not youtube_uploader.is_configured():
                    raise RuntimeError(tr("Save enabled YouTube settings before authorizing"))
                youtube_uploader.authorize()
                st.success(tr("YouTube authorization completed"))
            except Exception as exc:
                st.error(str(exc))
    with cols[1]:
        enabled = st.checkbox(tr("Enable YouTube Upload"), bool(config.youtube.get("enabled", False)), key="yt_settings_enabled")
        auto_upload = st.checkbox(tr("Auto-upload after generation"), bool(config.youtube.get("auto_upload", False)), key="yt_settings_auto_upload")
        privacy_options = ["private", "unlisted", "public"]
        saved_privacy = config.youtube.get("privacy_status", "private")
        privacy_status = st.selectbox(
            tr("YouTube Privacy"),
            privacy_options,
            index=privacy_options.index(saved_privacy) if saved_privacy in privacy_options else 0,
            key="yt_settings_privacy",
        )
        daily_api_limit = st.number_input(tr("Daily API Upload Limit"), 1, 100, int(config.youtube.get("daily_api_limit", 7)), key="yt_settings_daily_limit")
        if st.button(tr("Save YouTube Settings"), key="yt_settings_save", type="primary", use_container_width=True):
            config.youtube.update({
                "client_id": client_id,
                "client_secret": client_secret,
                "enabled": enabled,
                "auto_upload": auto_upload,
                "privacy_status": privacy_status,
                "daily_api_limit": daily_api_limit,
            })
            config.save_config()
            youtube_uploader.sync_from_config()
            st.success(tr("YouTube settings saved"))


def _preflight(tr) -> bool:
    youtube_uploader.sync_from_config()
    if not youtube_uploader.is_configured():
        st.error(tr("YouTube must be enabled and configured before starting"))
        return False
    if not youtube_uploader.is_authorized():
        st.error(tr("Authorize YouTube before starting"))
        return False
    return True


def _upload_scanned(item: dict, publish_at: str = "") -> dict:
    metadata = parse_metadata_file(item.get("metadata_path", ""))
    subject = item.get("subject", "")
    return youtube_uploader.upload_video(
        video_path=item["video_path"],
        title=metadata.get("title") or subject,
        description=metadata.get("description") or subject,
        tags=metadata.get("tags") or ["#shorts"],
        publish_at=publish_at,
        privacy_status=config.youtube.get("privacy_status", "private"),
        task_id=item.get("task_id", ""),
        index=int(item.get("index", 1)),
    )


def _render_scanner(tr) -> None:
    scanned = scan_pending_videos()
    metrics = st.columns(4)
    for column, label, field in zip(metrics, ("Total", "Pending", "Scheduled", "Uploaded"), ("total", "pending", "scheduled", "completed")):
        column.metric(tr(label), scanned.get(field, 0))
    if st.button(tr("Refresh Scanner"), key="yt_scanner_refresh"):
        st.rerun()
    for item in scanned.get("videos", []):
        with st.container(border=True):
            cols = st.columns([4, 1, 1])
            cols[0].write(f"**{item.get('subject') or item.get('task_id')}**")
            cols[0].caption(item.get("video_path", ""))
            cols[1].write(item.get("youtube_status", "pending"))
            if item.get("youtube_url"):
                cols[1].link_button("YouTube", item["youtube_url"])
            if item.get("youtube_status") in {"pending", "failed"}:
                actions = cols[2].columns(2)
                action_key = f"{item['task_id']}_{item.get('index', 1)}"
                if actions[0].button(tr("Upload"), key=f"yt_scanner_upload_{action_key}"):
                    if _preflight(tr):
                        result = _upload_scanned(item)
                        st.success(tr("Uploading")) if result.get("success") else st.error(result.get("error", "error"))
                        st.rerun()
                if actions[1].button(tr("Schedule"), key=f"yt_scanner_schedule_{action_key}"):
                    if _preflight(tr):
                        try:
                            slot = youtube_uploader.create_publish_plan(1)[0]
                            result = _upload_scanned(item, slot.get("publish_at", ""))
                            st.success(tr("Uploading")) if result.get("success") else st.error(result.get("error", "error"))
                            st.rerun()
                        except Exception as exc:
                            st.error(str(exc))


def _existing_batch_subjects() -> list[str]:
    existing = collect_task_subjects()
    existing.extend(entry.get("video_subject", "") for entry in upload_tracker.load())
    for batch in youtube_batch_store.list_batches(10000):
        existing.extend(item.get("subject", "") for item in batch.get("items", []))
    return [value for value in existing if value]


def _render_batch_history(tr) -> None:
    recent = youtube_batch_store.list_batches(10)
    selected_id = st.selectbox(
        tr("Recent YouTube batches"),
        [batch["batch_id"] for batch in recent],
        format_func=lambda batch_id: next((f"{b.get('created_at', '')[:16]} | {b.get('status')} | {b.get('requested')}" for b in recent if b["batch_id"] == batch_id), batch_id),
        key="yt_batch_manifest_select",
        placeholder=tr("None"),
    ) if recent else None
    if selected_id:
        _render_progress(tr, selected_id)
    elif not recent:
        st.info(tr("None"))


def _generate_unique_idea_rows(topic: str, total: int, language: str, existing: list[str]) -> list[dict]:
    accepted = []
    blocked = list(existing)
    for _ in range(3):
        missing = total - len(accepted)
        if missing <= 0:
            break
        generated = llm.generate_batch_ideas(topic, missing, language, blocked)
        audit = validate_unique_ideas(
            [row.get("subject", "") for row in generated],
            blocked,
        )
        for row, result in zip(generated, audit):
            if result["duplicate"]:
                continue
            accepted.append(
                {
                    "subject": result["subject"],
                    "title_override": row.get("title_override", ""),
                }
            )
            blocked.append(result["subject"])
    if len(accepted) != total:
        raise RuntimeError("Unable to prepare the requested number of unique ideas")
    return accepted


def _render_batch(tr, base_params: VideoParams) -> None:
    active_batch_id = st.session_state.get("yt_active_batch_id", "")
    if active_batch_id:
        active_batch = youtube_batch_store.load(active_batch_id)
        if active_batch:
            # The editor widget existed in the previous run, so clear its state
            # here before any widgets are instantiated.
            _clear_batch_draft(st.session_state)
            st.success(f"{tr('Batch in progress...')} {active_batch_id}")
            _render_progress(tr, active_batch_id)
            if st.button(tr("Prepare batch ideas"), key="yt_batch_prepare_another"):
                st.session_state.pop("yt_active_batch_id", None)
                _clear_batch_draft(st.session_state)
                st.rerun()
            return
        st.session_state.pop("yt_active_batch_id", None)

    st.markdown(f"### 1. {tr('Prepare batch ideas')}")
    top_columns = st.columns([1, 1])
    total = int(top_columns[0].number_input(tr("Total videos"), 1, 100, 10, key="yt_batch_total"))
    idea_mode = top_columns[1].radio(
        tr("Idea preparation mode"),
        ["ai", "manual"],
        format_func=lambda value: tr("Generate ideas with AI") if value == "ai" else tr("Paste my own subjects"),
        horizontal=True,
        key="yt_batch_idea_mode",
    )
    draft_context = (idea_mode, total)
    if st.session_state.get("yt_batch_draft_context") not in {None, draft_context}:
        st.session_state.pop("yt_batch_idea_rows", None)
        st.session_state.pop("yt_batch_idea_editor", None)
    st.session_state["yt_batch_draft_context"] = draft_context
    existing = _existing_batch_subjects()
    if idea_mode == "ai":
        idea_prompt = st.text_area(
            tr("Batch story direction"),
            value="Historias realistas e inspiradoras sobre personas comunes que resuelven un problema concreto y ayudan a su comunidad.",
            height=100,
            key="yt_batch_idea_prompt",
        )
        if st.button(tr("Generate unique ideas"), key="yt_batch_generate_ideas", type="primary"):
            try:
                with st.spinner(tr("Generating Video Script and Keywords")):
                    st.session_state["yt_batch_idea_rows"] = _generate_unique_idea_rows(
                        idea_prompt,
                        total,
                        base_params.video_language or "Spanish (Latin America)",
                        existing,
                    )
                    st.session_state.pop("yt_batch_idea_editor", None)
                    st.rerun()
            except Exception as exc:
                st.error(str(exc))
    else:
        manual_subjects = st.text_area(
            tr("Prepared subjects"),
            height=150,
            key="yt_batch_subjects",
            help="One subject per line.",
        )
        if st.button(tr("Prepare subjects"), key="yt_batch_prepare_manual"):
            st.session_state["yt_batch_idea_rows"] = [
                {"subject": value.strip(), "title_override": ""}
                for value in manual_subjects.splitlines()
                if value.strip()
            ]
            st.session_state.pop("yt_batch_idea_editor", None)
            st.rerun()

    rows = st.session_state.setdefault("yt_batch_idea_rows", [])
    edited_rows = st.data_editor(
        rows,
        column_order=["subject", "title_override"],
        column_config={
            "subject": st.column_config.TextColumn(tr("Story brief"), required=True, width="large"),
            "title_override": st.column_config.TextColumn(tr("YouTube title optional"), max_chars=100, width="medium"),
        },
        num_rows="dynamic",
        hide_index=True,
        use_container_width=True,
        key="yt_batch_idea_editor",
    )
    st.session_state["yt_batch_idea_rows"] = edited_rows
    subjects = [str(row.get("subject", "")).strip() for row in edited_rows if str(row.get("subject", "")).strip()]
    title_overrides = [str(row.get("title_override", "")).strip() for row in edited_rows if str(row.get("subject", "")).strip()]
    idea_audit = validate_unique_ideas(subjects, existing)
    duplicate_rows = [row for row in idea_audit if row["duplicate"]]
    if duplicate_rows:
        for duplicate in duplicate_rows[:5]:
            st.error(
                f"{tr('Duplicate idea')}: {duplicate['subject']} -> {duplicate['duplicate_of']} "
                f"({duplicate['similarity']:.0%})"
            )
    st.caption(f"{tr('Prepared subjects')}: {len(subjects)}/{total}")
    quality_rows = [
        audit_story_idea(subject, title_overrides[index])
        for index, subject in enumerate(subjects)
    ]
    if subjects:
        st.dataframe(
            [
                {
                    "#": index + 1,
                    tr("Quality Score"): quality["score"],
                    tr("Review Notes"): "; ".join(quality["issues"]) or tr("Approved"),
                }
                for index, quality in enumerate(quality_rows)
            ],
            hide_index=True,
            use_container_width=True,
        )

    st.markdown(f"### 2. {tr('Configure batch videos')}")
    st.session_state.setdefault(
        "yt_batch_script_prompt",
        base_params.video_script_prompt or DEFAULT_BATCH_SCRIPT_PROMPT,
    )
    script_prompt = st.text_area(
        tr("Custom Script Requirements"),
        height=180,
        max_chars=2000,
        help=tr("Custom Script Requirements Placeholder"),
        key="yt_batch_script_prompt",
    ).strip()
    instruction_columns = st.columns(2)
    st.session_state.setdefault("yt_batch_paragraph_number", 6)
    paragraph_number = int(instruction_columns[0].slider(tr("Script Paragraph Number"), 1, 10, key="yt_batch_paragraph_number"))
    bgm_options = [(tr("No Background Music"), ""), (tr("Random Background Music"), "random"), (tr("Custom Background Music"), "custom")]
    st.session_state.setdefault("yt_batch_bgm_type", "")
    bgm_type = instruction_columns[1].selectbox(
        tr("Background Music Source"),
        [value for _, value in bgm_options],
        format_func=lambda value: dict((v, label) for label, v in bgm_options)[value],
        key="yt_batch_bgm_type",
    )
    bgm_file = ""
    if bgm_type == "custom":
        st.session_state.setdefault("yt_batch_bgm_file", base_params.bgm_file or "")
        bgm_file = st.text_input(tr("Custom Background Music File"), key="yt_batch_bgm_file").strip()

    st.markdown(f"### 3. {tr('Schedule and review')}")
    columns = st.columns(5)
    minimum_days = (total + 14) // 15
    current_days = int(st.session_state.get("yt_batch_days", min(5, total)) or 1)
    if current_days < minimum_days or current_days > total:
        st.session_state["yt_batch_days"] = max(minimum_days, min(5, total))
    days = int(
        columns[0].number_input(
            tr("Number of days"),
            min_value=minimum_days,
            max_value=total,
            value=max(minimum_days, min(5, total)),
            key="yt_batch_days",
        )
    )
    per_day = (total + days - 1) // days
    start_date = columns[1].date_input(tr("Start date"), datetime.now().date() + timedelta(days=1), key="yt_batch_start_date")
    schedule_at = columns[2].time_input(tr("Schedule Time"), key="yt_batch_start_time")
    interval_enabled = columns[3].checkbox(
        tr("Use interval between videos"),
        value=False,
        key="yt_batch_interval_enabled",
    )
    selected_interval_minutes = int(
        columns[4].number_input(
            tr("Upload Interval Minutes"),
            min_value=1,
            max_value=240,
            value=int(config.youtube.get("schedule_interval_minutes", 5) or 5),
            disabled=not interval_enabled,
            key="yt_batch_schedule_interval",
        )
    )
    schedule_interval_minutes = selected_interval_minutes if interval_enabled else 0
    allow_shared_publish_time = not interval_enabled
    execution_mode = st.radio(
        tr("Execution mode"),
        ["interleaved", "generate_all_first"],
        format_func=lambda value: tr("Generate and schedule one by one") if value == "interleaved" else tr("Generate all before scheduling"),
        horizontal=True,
        key="yt_batch_execution_mode",
    )
    st.caption(f"{tr('Scheduled days')}: {days} · {tr('Videos per day')}: {per_day}")
    try:
        plan = youtube_uploader.create_publish_plan(
            total,
            start_date=start_date,
            schedule_mode="daily_block",
            schedule_at=schedule_at.strftime("%H:%M"),
            videos_per_day=per_day,
            interval_minutes=schedule_interval_minutes,
            occupied_publish_at=youtube_batch_store.reserved_publish_slots(),
            occupied_counts_toward_daily_capacity=False,
            collision_policy="skip",
            allow_shared_publish_time=allow_shared_publish_time,
        )
    except Exception as exc:
        plan = []
        st.error(str(exc))
    preview = []
    for index, subject in enumerate(subjects[: len(plan)]):
        preview.append({
            "#": index + 1,
            tr("Video Subject"): subject,
            tr("Publishing Title"): title_overrides[index] or tr("Auto Detect"),
            tr("Schedule Time"): f"{plan[index].get('local_date', '')} {plan[index].get('local_time', '')}",
        })
    st.dataframe(preview, hide_index=True, use_container_width=True)
    low_quality_rows = [quality for quality in quality_rows if quality["status"] != "approved"]
    ready = (
        len(subjects) == total
        and len(plan) == total
        and not duplicate_rows
        and not low_quality_rows
    )
    if st.button(tr("Start Batch"), key="yt_batch_start", type="primary", use_container_width=True, disabled=not ready):
        if not _preflight(tr):
            return
        params = _build_batch_video_params(
            base_params,
            script_prompt,
            paragraph_number,
            bgm_type,
            bgm_file,
        )
        settings = {
            "total_videos": total,
            "total_days": days,
            "videos_per_day": per_day,
            "start_date": start_date.isoformat(),
            "schedule_at": schedule_at.strftime("%H:%M"),
            "schedule_interval_minutes": schedule_interval_minutes,
            "allow_shared_publish_time": allow_shared_publish_time,
            "schedule_mode": "daily_block",
            "scheduled": True,
            "privacy_status": config.youtube.get("privacy_status", "private"),
            "timezone": youtube_uploader.schedule_timezone,
            "video_params": params.model_dump(mode="json"),
            "editorial_profile": "realistic_inspiring_balanced_v1",
        }
        try:
            manifest = youtube_batch_store.create(
                subjects,
                plan,
                settings,
                execution_mode=execution_mode,
                title_overrides=title_overrides,
                idea_mode=idea_mode,
                blocked_subjects=existing,
            )
            st.session_state["yt_active_batch_id"] = manifest["batch_id"]
            youtube_batch_runner.start(manifest["batch_id"])
            st.rerun()
        except (ValueError, RuntimeError) as exc:
            st.error(str(exc))


def render(tr, base_params: VideoParams) -> None:
    with st.expander(tr("YouTube Uploads and Scanner"), expanded=False):
        batch, history, scanner, settings, log = st.tabs([tr("Batch Generator"), tr("Batch History"), tr("Scanner"), tr("YouTube Settings"), tr("Upload Log")])
        with batch:
            _render_batch(tr, base_params)
        with history:
            _render_batch_history(tr)
        with settings:
            _render_settings(tr)
        with scanner:
            _render_scanner(tr)
        with log:
            entries = upload_tracker.load()
            st.dataframe(entries, hide_index=True, use_container_width=True) if entries else st.info(tr("No YouTube uploads yet"))
