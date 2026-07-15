import math
import os.path
import re
import time
from datetime import datetime, timezone
from os import path

from loguru import logger

from app.config import config
from app.models import const
from app.models.schema import VideoConcatMode, VideoParams
from app.services import llm, material, performance, subtitle, twelvelabs, video, voice, upload_post
from app.services import state as sm
from app.services.youtube_uploader import upload_tracker, youtube_uploader
from app.services.tiktok_scheduler import tiktok_scheduler
from app.services.tiktok_uploader import tiktok_upload_tracker, tiktok_uploader
from app.utils import file_security, utils


@performance.instrument_stage("script")
def generate_script(task_id, params):
    logger.info("\n\n## generating video script")
    video_script = params.video_script.strip()
    if not video_script:
        video_script = llm.generate_script(
            video_subject=params.video_subject,
            language=params.video_language,
            paragraph_number=params.paragraph_number,
            video_script_prompt=params.video_script_prompt,
            custom_system_prompt=params.custom_system_prompt,
        )
    else:
        logger.debug(f"video script: \n{video_script}")

    if not video_script:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        logger.error("failed to generate video script.")
        return None

    return video_script


@performance.instrument_stage("search_terms")
def generate_terms(task_id, params, video_script):
    logger.info("\n\n## generating video terms")
    video_terms = params.video_terms
    if not video_terms:
        # 开启素材按文案顺序匹配后，关键词本身也必须按脚本叙事顺序生成；
        # 否则后续即使顺序下载和顺序拼接，也只能复用一组全局主题词，
        # 无法改善“后面内容的画面提前出现”的问题。
        video_terms = llm.generate_terms(
            video_subject=params.video_subject,
            video_script=video_script,
            amount=12 if params.match_materials_to_script else 5,
            match_script_order=params.match_materials_to_script,
        )
    else:
        if isinstance(video_terms, str):
            video_terms = [term.strip() for term in re.split(r"[,，]", video_terms)]
        elif isinstance(video_terms, list):
            video_terms = [term.strip() for term in video_terms]
        else:
            raise ValueError("video_terms must be a string or a list of strings.")

        logger.debug(f"video terms: {utils.to_json(video_terms)}")

    if not video_terms:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        logger.error("failed to generate video terms.")
        return None

    # 可选的 TwelveLabs Marengo 语义重排：未启用时返回原顺序，无任何副作用。
    # 顺序匹配模式下关键词顺序本身就是脚本叙事顺序，必须保持原样，故跳过。
    if not params.match_materials_to_script:
        video_terms = twelvelabs.rerank_terms_by_subject(
            video_subject=params.video_subject,
            search_terms=video_terms,
        )

    return video_terms


def save_script_data(task_id, video_script, video_terms, params):
    script_file = path.join(utils.task_dir(task_id), "script.json")
    provider_id = str(config.app.get("llm_provider", "")).strip().lower()
    provider = llm.get_llm_provider(provider_id)
    model_name = provider.resolve_model_name(
        config.app.get(provider.config_key("model_name"), "")
    ) if provider else ""
    script_is_user_supplied = bool(params.video_script.strip())
    terms_are_user_supplied = bool(params.video_terms)
    script_data = {
        "script": video_script,
        "search_terms": video_terms,
        "params": params,
        "generation": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "llm_provider": provider_id,
            "model_name": model_name,
            "prompt_profile": "manual" if script_is_user_supplied else "realistic_grounded_v1",
            "script_source": "user" if script_is_user_supplied else "llm",
            "terms_source": "user" if terms_are_user_supplied else "llm",
            "quality_status": "unreviewed" if script_is_user_supplied else "passed",
        },
    }

    with open(script_file, "w", encoding="utf-8") as f:
        f.write(utils.to_json(script_data))


@performance.instrument_stage("social_metadata")
def save_social_metadata(task_id, params, video_script):
    """Create ready-to-publish TikTok and YouTube Shorts metadata per video task."""
    try:
        tiktok = llm.generate_social_metadata(
            video_subject=params.video_subject,
            video_script=video_script,
            language=params.video_language or "auto",
            platform="tiktok",
        )
        youtube = llm.generate_social_metadata(
            video_subject=params.video_subject,
            video_script=video_script,
            language=params.video_language or "auto",
            platform="youtube_shorts",
        )
        metadata_file = path.join(utils.task_dir(task_id), "METADATOS.md")
        content = "\n".join(
            [
                "# Metadatos",
                "",
                "## TikTok",
                "",
                f"Título: {tiktok.get('title', params.video_subject)}",
                "",
                f"Descripción: {tiktok.get('caption', '')}",
                "",
                f"Hashtags: {' '.join(tiktok.get('hashtags', []))}",
                "",
                "## YouTube Shorts",
                "",
                f"Título: {youtube.get('title', params.video_subject)}",
                "",
                f"Descripción: {youtube.get('caption', '')}",
                "",
                f"Hashtags: {' '.join(youtube.get('hashtags', []))}",
                "",
            ]
        )
        with open(metadata_file, "w", encoding="utf-8") as f:
            f.write(content)
        logger.success(f"saved social metadata: {metadata_file}")
        return {"tiktok": tiktok, "youtube_shorts": youtube}
    except Exception as exc:
        # Metadata must not make an already-rendered video task fail.
        logger.warning(f"failed to save social metadata for task {task_id}: {exc}")
        return {}


def resolve_custom_audio_file(task_id: str, custom_audio_file: str | None) -> str:
    requested_file = (custom_audio_file or "").strip()
    if not requested_file:
        return ""

    task_dir = utils.task_dir(task_id)
    try:
        return file_security.resolve_path_within_directory(
            task_dir,
            requested_file,
        )
    except ValueError as exc:
        task_dir_error = exc

    server_audio_file = path.realpath(
        requested_file
        if path.isabs(requested_file)
        else path.join(utils.root_dir(), requested_file)
    )
    if not path.isabs(requested_file):
        project_root = path.realpath(utils.root_dir())
        try:
            if path.commonpath([project_root, server_audio_file]) != project_root:
                raise ValueError(
                    "relative custom audio paths must stay within the project directory"
                )
        except ValueError as exc:
            raise ValueError(
                "custom audio file must be task-local or an existing server-side file"
            ) from exc

    if not path.isfile(server_audio_file):
        raise ValueError(
            "custom audio file does not exist or is not a file"
        ) from task_dir_error

    return server_audio_file


@performance.instrument_stage("audio")
def generate_audio(task_id, params, video_script):
    '''
    Generate audio for the video script.
    If a custom audio file is provided, it will be used directly.
    There will be no subtitle maker object returned in this case.
    Otherwise, TTS will be used to generate the audio.
    Returns:
        - audio_file: path to the generated or provided audio file
        - audio_duration: duration of the audio in seconds
        - sub_maker: subtitle maker object if TTS is used, None otherwise
    '''
    logger.info("\n\n## generating audio")
    # /audio 和 /subtitle 请求模型不包含 custom_audio_file，
    # 这里统一做兼容读取，避免直调接口时抛属性错误。
    requested_custom_audio_file = getattr(params, "custom_audio_file", None)
    try:
        custom_audio_file = resolve_custom_audio_file(
            task_id, requested_custom_audio_file
        )
    except ValueError as exc:
        logger.error(
            "custom audio file is invalid, "
            f"task_id: {task_id}, path: {requested_custom_audio_file}, error: {str(exc)}"
        )
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        return None, None, None

    if not custom_audio_file:
        logger.info("no custom audio file provided, using TTS to generate audio.")
        audio_file = path.join(utils.task_dir(task_id), "audio.mp3")
        sub_maker = voice.tts(
            text=video_script,
            voice_name=voice.parse_voice_name(params.voice_name),
            voice_rate=params.voice_rate,
            voice_file=audio_file,
        )
        if sub_maker is None:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error(
                """failed to generate audio:
1. check if the language of the voice matches the language of the video script.
2. check if the network is available. If you are in China, it is recommended to use a VPN and enable the global traffic mode.
            """.strip()
            )
            return None, None, None
        audio_duration = math.ceil(voice.get_audio_duration(sub_maker))
        if audio_duration == 0:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error("failed to get audio duration.")
            return None, None, None
        return audio_file, audio_duration, sub_maker
    else:
        logger.info(f"using custom audio file: {custom_audio_file}")
        audio_duration = voice.get_audio_duration(custom_audio_file)
        if audio_duration == 0:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error("failed to get audio duration from custom audio file.")
            return None, None, None
        return custom_audio_file, audio_duration, None

@performance.instrument_stage("subtitles")
def generate_subtitle(task_id, params, video_script, sub_maker, audio_file):
    '''
    Generate subtitle for the video script.
    If subtitle generation is disabled or no subtitle maker is provided, it will return an empty string.
    Otherwise, it will generate the subtitle using the specified provider.
    Returns:
        - subtitle_path: path to the generated subtitle file
    '''
    logger.info("\n\n## generating subtitle")
    if not params.subtitle_enabled:
        return ""

    subtitle_path = path.join(utils.task_dir(task_id), "subtitle.srt")
    subtitle_provider = config.app.get("subtitle_provider", "edge").strip().lower()
    logger.info(f"\n\n## generating subtitle, provider: {subtitle_provider}")

    if sub_maker is None and subtitle_provider != "whisper":
        # 自定义音频不会经过 TTS，因此没有 Edge/Azure 等 TTS 返回的
        # sub_maker 时间轴。只有 Whisper 可以直接从音频文件转写字幕；
        # 其他字幕提供方继续保持原有行为，避免生成错误的空时间轴。
        logger.warning(
            "subtitle maker is missing, skip subtitle generation for provider: "
            f"{subtitle_provider}"
        )
        return ""

    subtitle_fallback = False
    if subtitle_provider == "edge":
        voice.create_subtitle(
            text=video_script, sub_maker=sub_maker, subtitle_file=subtitle_path
        )
        if not os.path.exists(subtitle_path):
            subtitle_fallback = True
            logger.warning("subtitle file not found, fallback to whisper")

    if subtitle_provider == "whisper" or subtitle_fallback:
        subtitle.create(audio_file=audio_file, subtitle_file=subtitle_path)
        logger.info("\n\n## correcting subtitle")
        subtitle.correct(subtitle_file=subtitle_path, video_script=video_script)

    subtitle_lines = subtitle.file_to_subtitles(subtitle_path)
    if not subtitle_lines:
        logger.warning(f"subtitle file is invalid: {subtitle_path}")
        return ""

    return subtitle_path


def _get_video_materials(task_id, params, video_terms, audio_duration):
    selected_sources = getattr(params, "video_sources", None) or [params.video_source]
    if "local" in selected_sources:
        logger.info("\n\n## preprocess local materials")
        materials = video.preprocess_video(
            materials=params.video_materials, clip_duration=params.video_clip_duration
        )
        if not materials:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error(
                "no valid materials found, please check the materials and try again."
            )
            return None
        return [material_info.url for material_info in materials]
    else:
        logger.info(f"\n\n## downloading videos from {', '.join(selected_sources)}")
        # 顺序匹配模式只在用户显式开启时生效。这里强制素材下载按关键词顺序
        # 轮询，避免某个早期关键词下载太多素材，把后续脚本主题挤出最终时间线。
        downloaded_videos = material.download_videos(
            task_id=task_id,
            search_terms=video_terms,
            source=params.video_source,
            sources=selected_sources,
            video_aspect=params.video_aspect,
            video_concat_mode=(
                VideoConcatMode.sequential
                if params.match_materials_to_script
                else params.video_concat_mode
            ),
            audio_duration=(
                audio_duration
                if params.match_materials_to_script
                else audio_duration * params.video_count
            ),
            max_clip_duration=params.video_clip_duration,
            match_script_order=params.match_materials_to_script,
        )
        if not downloaded_videos:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error(
                "failed to download videos, maybe the network is not available. if you are in China, please use a VPN."
            )
            return None
        return downloaded_videos


@performance.instrument_stage("materials")
def get_video_materials(task_id, params, video_terms, audio_duration):
    with performance.network_slot:
        return _get_video_materials(task_id, params, video_terms, audio_duration)


def _generate_final_videos(
    task_id, params, downloaded_videos, audio_file, subtitle_path
):
    final_video_paths = []
    combined_video_paths = []
    # 多视频生成默认会打散素材以增加差异；但“按文案顺序匹配素材”追求的是
    # 时间线稳定性和可解释性，所以开启后所有输出都使用顺序拼接。
    if params.match_materials_to_script:
        video_concat_mode = VideoConcatMode.sequential
    elif params.video_count == 1:
        video_concat_mode = params.video_concat_mode
    else:
        video_concat_mode = VideoConcatMode.random
    video_transition_mode = params.video_transition_mode

    _progress = 50
    for i in range(params.video_count):
        index = i + 1
        combined_video_path = path.join(
            utils.task_dir(task_id), f"combined-{index}.mp4"
        )
        logger.info(f"\n\n## combining video: {index} => {combined_video_path}")
        video.combine_videos(
            combined_video_path=combined_video_path,
            video_paths=downloaded_videos,
            audio_file=audio_file,
            video_aspect=params.video_aspect,
            video_fit_mode=params.video_fit_mode,
            video_concat_mode=video_concat_mode,
            video_transition_mode=video_transition_mode,
            max_clip_duration=params.video_clip_duration,
            threads=params.n_threads,
        )

        _progress += 50 / params.video_count / 2
        sm.state.update_task(task_id, progress=_progress)

        final_video_path = path.join(utils.task_dir(task_id), f"final-{index}.mp4")

        logger.info(f"\n\n## generating video: {index} => {final_video_path}")
        video.generate_video(
            video_path=combined_video_path,
            audio_path=audio_file,
            subtitle_path=subtitle_path,
            output_file=final_video_path,
            params=params,
        )

        _progress += 50 / params.video_count / 2
        sm.state.update_task(task_id, progress=_progress)

        final_video_paths.append(final_video_path)
        combined_video_paths.append(combined_video_path)

    return final_video_paths, combined_video_paths


@performance.instrument_stage("video_render")
def generate_final_videos(
    task_id, params, downloaded_videos, audio_file, subtitle_path
):
    with performance.render_slot:
        return _generate_final_videos(
            task_id, params, downloaded_videos, audio_file, subtitle_path
        )


@performance.instrument_task("video_generation")
def start(
    task_id,
    params: VideoParams,
    stop_at: str = "video",
    suppress_tiktok_upload: bool = False,
    suppress_youtube_upload: bool = False,
):
    profile = performance.get_runtime_profile()
    if str(config.app.get("performance_mode", "auto")).strip().lower() == "auto":
        params.n_threads = profile.ffmpeg_threads
    if profile.disk_critical:
        raise RuntimeError("Insufficient free disk space for safe video generation")
    logger.info(f"start task: {task_id}, stop_at: {stop_at}")
    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=5)

    # 1. Generate script
    video_script = generate_script(task_id, params)
    if not video_script or "Error: " in video_script:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        return

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=10)

    if stop_at == "script":
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100, script=video_script
        )
        return {"script": video_script}

    # 2. Generate terms
    video_terms = ""
    selected_sources = getattr(params, "video_sources", None) or [params.video_source]
    if "local" not in selected_sources:
        video_terms = generate_terms(task_id, params, video_script)
        if not video_terms:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            return

    save_script_data(task_id, video_script, video_terms, params)

    if stop_at == "terms":
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100, terms=video_terms
        )
        return {"script": video_script, "terms": video_terms}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=20)

    # 3. Generate audio
    audio_file, audio_duration, sub_maker = generate_audio(
        task_id, params, video_script
    )
    if not audio_file:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        return

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=30)

    if stop_at == "audio":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            audio_file=audio_file,
        )
        return {"audio_file": audio_file, "audio_duration": audio_duration}

    # 4. Generate subtitle
    subtitle_path = generate_subtitle(
        task_id, params, video_script, sub_maker, audio_file
    )

    if stop_at == "subtitle":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            subtitle_path=subtitle_path,
        )
        return {"subtitle_path": subtitle_path}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=40)

    # 5. Get video materials
    downloaded_videos = get_video_materials(
        task_id, params, video_terms, audio_duration
    )
    if not downloaded_videos:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        return

    if stop_at == "materials":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            materials=downloaded_videos,
        )
        return {"materials": downloaded_videos}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=50)

    # 仅完整视频生成流程才需要处理视频拼接模式；
    # 这样可以避免 /subtitle 和 /audio 这类请求访问不存在的字段。
    if type(params.video_concat_mode) is str:
        params.video_concat_mode = VideoConcatMode(params.video_concat_mode)

    # 6. Generate final videos
    final_video_paths, combined_video_paths = generate_final_videos(
        task_id, params, downloaded_videos, audio_file, subtitle_path
    )

    if not final_video_paths:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        return

    social_metadata = save_social_metadata(task_id, params, video_script)

    youtube_upload_results = []
    if not suppress_youtube_upload and youtube_uploader.is_configured() and youtube_uploader.auto_upload:
        try:
            metadata = social_metadata.get("youtube_shorts") or {}
            youtube_publish_plan = (
                youtube_uploader.create_publish_plan(len(final_video_paths))
                if youtube_uploader.schedule_enabled
                else []
            )
            youtube_schedule_cursor = 0
            for i, video_path in enumerate(final_video_paths):
                if youtube_uploader.remaining_api_slots() <= 0:
                    youtube_upload_results.append(
                        {"success": False, "skipped": True, "error": "daily_api_limit_reached"}
                    )
                    continue
                publish_slot = youtube_publish_plan[youtube_schedule_cursor] if youtube_publish_plan else {}
                publish_at = publish_slot.get("publish_at", "")
                result = youtube_uploader.upload_video(
                    video_path=video_path,
                    title=metadata.get("title", params.video_subject),
                    description=metadata.get("caption", ""),
                    tags=metadata.get("hashtags", []),
                    publish_at=publish_at,
                )
                status = "scheduled" if result.get("scheduled") else "completed"
                if not result.get("success"):
                    status = "failed"
                upload_tracker.add_entry(
                    task_id=task_id,
                    index=i + 1,
                    subject=params.video_subject,
                    video_path=video_path,
                    status=status,
                    youtube_id=result.get("video_id", ""),
                    youtube_url=result.get("url", ""),
                    publish_at=result.get("publish_at", ""),
                    publish_at_local=publish_slot.get("publish_at_local", ""),
                    schedule_mode=publish_slot.get("schedule_mode", ""),
                    error=result.get("error", ""),
                )
                youtube_upload_results.append(result)
                if result.get("success") and youtube_publish_plan:
                    youtube_schedule_cursor += 1
                if i < len(final_video_paths) - 1:
                    upload_delay = max(0, int(config.youtube.get("upload_interval_minutes", 0) or 0))
                    if upload_delay:
                        time.sleep(upload_delay * 60)
        except Exception as exc:
            logger.warning(f"failed to upload task {task_id} to YouTube: {exc}")

    logger.success(
        f"task {task_id} finished, generated {len(final_video_paths)} videos."
    )

    tiktok_upload_results = []
    if not suppress_tiktok_upload and tiktok_uploader.is_configured() and tiktok_uploader.auto_upload:
        try:
            metadata = social_metadata.get("tiktok") or {}
            caption_parts = [metadata.get("caption", ""), " ".join(metadata.get("hashtags", []))]
            caption = "\n\n".join(part for part in caption_parts if part).strip()[:2200]
            pending_jobs = sum(1 for job in tiktok_scheduler.load() if job.get("status") == "pending")
            for i, video_path in enumerate(final_video_paths):
                if bool(config.tiktok.get("schedule_enabled", False)):
                    scheduled_at = tiktok_scheduler.calculate_scheduled_at(pending_jobs + i)
                    result = tiktok_scheduler.add_job(
                        task_id=task_id,
                        index=i + 1,
                        subject=params.video_subject,
                        video_path=video_path,
                        caption=caption,
                        scheduled_at=scheduled_at,
                        provider=tiktok_uploader.provider,
                        privacy_level=tiktok_uploader.privacy_level,
                        allow_comment=tiktok_uploader.allow_comments,
                        allow_duet=tiktok_uploader.allow_duet,
                        allow_stitch=tiktok_uploader.allow_stitch,
                    )
                else:
                    if not tiktok_upload_tracker.claim(
                        task_id, i + 1, params.video_subject, video_path, tiktok_uploader.provider
                    ):
                        tiktok_upload_results.append({"success": True, "skipped": True, "reason": "already claimed"})
                        continue
                    result = tiktok_uploader.upload_video(
                        video_path=video_path,
                        caption=caption,
                        idempotency_key=f"{task_id}-{i + 1}",
                    )
                    status = (
                        result.get("status", "processing")
                        if result.get("success")
                        else "reconcile_required" if result.get("publish_id") else "failed"
                    )
                    tiktok_upload_tracker.add_entry(
                        task_id=task_id,
                        index=i + 1,
                        subject=params.video_subject,
                        video_path=video_path,
                        status=status,
                        provider=result.get("provider", tiktok_uploader.provider),
                        publish_id=result.get("publish_id", ""),
                        tiktok_url=result.get("tiktok_url", ""),
                        error=result.get("error", ""),
                    )
                tiktok_upload_results.append(result)
        except Exception as exc:
            logger.warning(f"failed to upload task {task_id} to TikTok: {exc}")

    # 7. Cross-post to social platforms (if enabled)
    cross_post_results = []
    if upload_post.upload_post_service.is_configured() and upload_post.upload_post_service.auto_upload:
        platforms = [
            platform for platform in upload_post.upload_post_service.platforms
            if not (platform == "tiktok" and bool(config.tiktok.get("enabled", False)))
            and not (
                platform.startswith("youtube")
                and bool(config.youtube.get("enabled", False))
                and bool(config.youtube.get("auto_upload", False))
            )
        ]
        if not platforms:
            platforms = []
        logger.info(f"\n\n## cross-posting videos to {', '.join(platforms)}")

        youtube_extra = None
        if any(p.startswith("youtube") for p in platforms):
            metadata = social_metadata.get("youtube_shorts") or {}
            youtube_extra = {
                "youtube_title": metadata.get("title", params.video_subject),
                "youtube_description": metadata.get("caption", ""),
                "tags": metadata.get("hashtags", []),
                "privacyStatus": upload_post.upload_post_service.youtube_privacy_status,
                "containsSyntheticMedia": True,
            }

        for video_path in final_video_paths if platforms else []:
            result = upload_post.cross_post_video(
                video_path=video_path,
                title=params.video_subject or "Check out this video! #shorts #viral",
                platforms=platforms,
                youtube_extra=youtube_extra,
            )
            cross_post_results.append(result)
            if result.get('success'):
                logger.info(f"✅ Cross-posted: {video_path}")
            else:
                logger.warning(f"⚠️ Failed to cross-post: {video_path} - {result.get('error', 'Unknown error')}")

    kwargs = {
        "videos": final_video_paths,
        "combined_videos": combined_video_paths,
        "script": video_script,
        "terms": video_terms,
        "audio_file": audio_file,
        "audio_duration": audio_duration,
        "subtitle_path": subtitle_path,
        "materials": downloaded_videos,
        "youtube_upload_results": youtube_upload_results if youtube_upload_results else None,
        "tiktok_upload_results": tiktok_upload_results if tiktok_upload_results else None,
        "cross_post_results": cross_post_results if cross_post_results else None,
    }
    sm.state.update_task(
        task_id, state=const.TASK_STATE_COMPLETE, progress=100, **kwargs
    )
    return kwargs


if __name__ == "__main__":
    task_id = "task_id"
    params = VideoParams(
        video_subject="金钱的作用",
        voice_name="zh-CN-XiaoyiNeural-Female",
        voice_rate=1.0,
    )
    start(task_id, params, stop_at="video")
