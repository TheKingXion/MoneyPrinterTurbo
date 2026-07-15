import argparse
import json
import math
import os
import re
import shutil
from typing import Sequence
from uuid import UUID, uuid4

from loguru import logger

from app.models.schema import MaterialInfo, VideoParams
from app.services import task as tm
from app.utils import utils

DEFAULT_VOICE_NAME = "zh-CN-XiaoxiaoNeural-Female"
_PIPELINE_STAGES = ("script", "terms", "audio", "subtitle", "materials", "video")
_CUSTOM_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}


class _CliHelpFormatter(
    argparse.ArgumentDefaultsHelpFormatter,
    argparse.RawDescriptionHelpFormatter,
):
    def _get_help_string(self, action):
        help_text = action.help or ""
        if (
            "%(default)" not in help_text
            and action.default not in (None, "", argparse.SUPPRESS)
            and action.option_strings
            and "default:" not in help_text.lower()
        ):
            help_text += " (default: %(default)s)"
        return help_text


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"value must be >= 1, got {parsed}")
    return parsed


def _paragraph_count(value: str) -> int:
    parsed = int(value)
    if parsed < 1 or parsed > 10:
        raise argparse.ArgumentTypeError(
            f"paragraph-number must be between 1 and 10, got {parsed}"
        )
    return parsed


def _non_negative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError(
            f"value must be a finite number >= 0, got {value!r}"
        )
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError(
            f"value must be a finite number > 0, got {value!r}"
        )
    return parsed


def _percent_position(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0 or parsed > 100:
        raise argparse.ArgumentTypeError(
            f"custom-position must be a finite number between 0 and 100, got {value!r}"
        )
    return parsed


def _hex_color(value: str) -> str:
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise argparse.ArgumentTypeError(
            f"color must use #RRGGBB format, got {value!r}"
        )
    return value


def _task_id(value: str) -> str:
    try:
        return str(UUID(value.strip()))
    except (AttributeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            f"task-id must be a valid UUID, got {value!r}"
        ) from exc


def _video_sources(value: str) -> list[str]:
    sources = [item.strip().lower() for item in re.split(r"[,，]", value) if item.strip()]
    allowed = {"pexels", "pixabay", "coverr", "local"}
    invalid = [source for source in sources if source not in allowed]
    if not sources or invalid:
        raise argparse.ArgumentTypeError(
            "video-sources must be a comma-separated list of: coverr, local, pexels, pixabay"
        )
    return list(dict.fromkeys(sources))


_TRANSITION_MODE_VALUES = {
    "none": None,
    "shuffle": "Shuffle",
    "fade-in": "FadeIn",
    "fade-out": "FadeOut",
    "slide-in": "SlideIn",
    "slide-out": "SlideOut",
}


def _transition_mode(value: str) -> str | None:
    normalized = value.strip().lower()
    if normalized not in _TRANSITION_MODE_VALUES:
        allowed = ", ".join(_TRANSITION_MODE_VALUES)
        raise argparse.ArgumentTypeError(
            f"video-transition-mode must be one of: {allowed}"
        )
    return _TRANSITION_MODE_VALUES[normalized]


def _bgm_type(value: str) -> str:
    normalized = value.strip().lower()
    if normalized == "none":
        return ""
    if normalized in {"", "random", "custom"}:
        return normalized
    raise argparse.ArgumentTypeError("bgm-type must be one of: none, random, custom")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate MoneyPrinterTurbo videos without the WebUI.\n\n"
            "Provider settings and credentials are read from config.toml.\n"
            "The default Edge TTS voice requires no API key."
        ),
        epilog="""
Examples:
  uv run python cli.py --video-subject "How AI is changing everyday life"
  uv run python cli.py --video-subject "Local demo" --video-source local \\
    --video-materials "./1.mp4,./2.mp4"
  uv run python cli.py --video-script "Your complete script" --voice-name no-voice

Pipeline stages:
  script, terms, audio, subtitle, materials, video

A successful command prints one JSON object and exits with 0. Task failures exit
with 1; invalid file input or argument errors exit with 2.
""",
        formatter_class=_CliHelpFormatter,
    )
    parser.add_argument(
        "--video-subject",
        default="",
        help="video topic; required unless --video-script is provided",
    )
    parser.add_argument("--video-script", default="", help="custom script")
    parser.add_argument("--video-terms", default=None, help="comma-separated terms")
    parser.add_argument(
        "--video-language",
        default=None,
        help="script generation language code (default: auto detect)",
    )
    parser.add_argument(
        "--paragraph-number",
        type=_paragraph_count,
        default=None,
        help="script paragraph count, 1-10",
    )
    parser.add_argument(
        "--video-script-prompt",
        default=None,
        help="custom script requirements prompt",
    )
    parser.add_argument(
        "--custom-system-prompt",
        default=None,
        help="custom system prompt for script generation",
    )
    parser.add_argument(
        "--video-source",
        default="pexels",
        choices=["pexels", "pixabay", "coverr", "local"],
        help="video material source",
    )
    parser.add_argument(
        "--video-sources",
        type=_video_sources,
        default=None,
        metavar="SOURCE[,SOURCE...]",
        help="combine online material providers; preserves configured order",
    )
    parser.add_argument(
        "--video-fit-mode",
        choices=["cover", "blur", "contain"],
        default=None,
        help="fit source footage to the output frame",
    )
    parser.add_argument(
        "--video-materials",
        default="",
        help="comma-separated local material paths",
    )
    parser.add_argument(
        "--stop-at",
        default="video",
        choices=_PIPELINE_STAGES,
        help="pipeline stop stage",
    )
    parser.add_argument(
        "--video-count", type=_positive_int, default=1, help="output video count (>=1)"
    )
    parser.add_argument(
        "--video-aspect",
        choices=["9:16", "16:9", "1:1"],
        default="9:16",
        help="video aspect ratio",
    )
    parser.add_argument(
        "--video-concat-mode",
        choices=["random", "sequential"],
        default=None,
        help="video concatenation mode",
    )
    parser.add_argument(
        "--video-transition-mode",
        type=_transition_mode,
        default=None,
        metavar="{none,shuffle,fade-in,fade-out,slide-in,slide-out}",
        help="video transition mode",
    )
    parser.add_argument(
        "--video-clip-duration",
        type=_positive_int,
        default=None,
        help="maximum duration of each source clip in seconds",
    )
    parser.add_argument(
        "--match-materials-to-script",
        default=None,
        action=argparse.BooleanOptionalAction,
        help="match generated/search materials to script order",
    )
    parser.add_argument(
        "--n-threads",
        type=_positive_int,
        default=None,
        help="FFmpeg worker thread count",
    )
    parser.add_argument(
        "--voice-name", default=DEFAULT_VOICE_NAME, help="tts voice name"
    )
    parser.add_argument(
        "--voice-volume",
        type=_non_negative_float,
        default=None,
        help="speech volume multiplier",
    )
    parser.add_argument(
        "--voice-rate",
        type=_positive_float,
        default=None,
        help="speech rate multiplier",
    )
    parser.add_argument(
        "--custom-audio-file",
        default=None,
        metavar="PATH",
        help="existing voiceover file; skips TTS",
    )
    parser.add_argument(
        "--bgm-type",
        type=_bgm_type,
        default=None,
        metavar="{none,random,custom}",
        help="background music mode",
    )
    parser.add_argument("--bgm-file", default=None, help="custom background music file")
    parser.add_argument(
        "--bgm-volume",
        type=_non_negative_float,
        default=None,
        help="background music volume multiplier",
    )
    parser.add_argument(
        "--subtitle-enabled",
        default=True,
        action=argparse.BooleanOptionalAction,
        help="enable subtitles (default: enabled, use --no-subtitle-enabled to disable)",
    )
    parser.add_argument("--font-name", default=None, help="subtitle font file name")
    parser.add_argument(
        "--subtitle-position",
        choices=["top", "center", "bottom", "custom"],
        default=None,
        help="subtitle position",
    )
    parser.add_argument(
        "--custom-position",
        type=_percent_position,
        default=None,
        help="custom subtitle position as percent from top, 0-100",
    )
    parser.add_argument(
        "--text-fore-color",
        type=_hex_color,
        default=None,
        help="subtitle text color in #RRGGBB format",
    )
    parser.add_argument(
        "--font-size", type=_positive_int, default=None, help="subtitle font size"
    )
    parser.add_argument(
        "--stroke-color",
        type=_hex_color,
        default=None,
        help="subtitle outline color in #RRGGBB format",
    )
    parser.add_argument(
        "--stroke-width",
        type=_non_negative_float,
        default=None,
        help="subtitle outline width",
    )
    parser.add_argument(
        "--subtitle-background-enabled",
        default=None,
        action=argparse.BooleanOptionalAction,
        help="enable subtitle background",
    )
    parser.add_argument(
        "--subtitle-background-color",
        type=_hex_color,
        default=None,
        help="subtitle background color in #RRGGBB format",
    )
    parser.add_argument(
        "--rounded-subtitle-background",
        default=None,
        action=argparse.BooleanOptionalAction,
        help="enable rounded translucent subtitle background",
    )
    parser.add_argument(
        "--task-id", type=_task_id, default=None, help="custom UUID task id"
    )
    args = parser.parse_args(argv)

    if not args.video_subject.strip() and not args.video_script.strip():
        parser.error("one of --video-subject or --video-script is required")

    effective_sources = args.video_sources or [args.video_source]
    has_local_source = "local" in effective_sources
    if has_local_source and len(effective_sources) > 1:
        parser.error("local materials cannot be combined with online --video-sources")

    if has_local_source and args.stop_at == "terms":
        parser.error(
            "--stop-at terms has no effect with --video-source local "
            "(search terms are not generated for local sources)"
        )

    has_video_materials = bool((args.video_materials or "").strip())
    if has_local_source and args.stop_at in {"materials", "video"} and not has_video_materials:
        parser.error(
            "--video-materials is required with a local source when --stop-at is materials or video"
        )
    if not has_local_source and has_video_materials:
        parser.error("--video-materials can only be used with a local source")

    if args.bgm_file:
        if args.bgm_type in (None, "custom"):
            args.bgm_type = "custom"
        else:
            parser.error("--bgm-file cannot be combined with --bgm-type none or random")
    elif args.bgm_type == "custom":
        parser.error("--bgm-file is required when --bgm-type is custom")

    if args.custom_position is not None and args.subtitle_position != "custom":
        parser.error("--custom-position requires --subtitle-position custom")
    if args.stop_at == "subtitle" and not args.subtitle_enabled:
        parser.error("--stop-at subtitle cannot be combined with --no-subtitle-enabled")
    return args


def build_video_params(args: argparse.Namespace) -> VideoParams:
    video_terms = args.video_terms
    if video_terms:
        video_terms = [
            term.strip() for term in re.split(r"[,，]", video_terms) if term.strip()
        ]

    video_materials = None
    materials_arg = args.video_materials or ""
    if materials_arg.strip():
        video_materials = [
            # Actual duration will be detected during video processing; use 0 as placeholder.
            MaterialInfo(provider="local", url=item.strip(), duration=0)
            for item in materials_arg.split(",")
            if item.strip()
        ]

    params_kwargs = {
        "video_subject": args.video_subject.strip(),
        "video_script": args.video_script,
        "video_terms": video_terms,
        "video_source": (args.video_sources or [args.video_source])[0],
        "video_sources": args.video_sources,
        "video_materials": video_materials,
        "video_count": args.video_count,
        "video_aspect": args.video_aspect,
        "voice_name": args.voice_name,
        "subtitle_enabled": args.subtitle_enabled,
    }

    optional_arg_names = [
        "video_language",
        "paragraph_number",
        "video_script_prompt",
        "custom_system_prompt",
        "video_fit_mode",
        "video_concat_mode",
        "video_transition_mode",
        "video_clip_duration",
        "match_materials_to_script",
        "n_threads",
        "voice_volume",
        "voice_rate",
        "custom_audio_file",
        "bgm_type",
        "bgm_file",
        "bgm_volume",
        "font_name",
        "subtitle_position",
        "custom_position",
        "text_fore_color",
        "font_size",
        "stroke_color",
        "stroke_width",
        "rounded_subtitle_background",
    ]
    for name in optional_arg_names:
        value = getattr(args, name)
        if value is not None:
            params_kwargs[name] = value

    if args.subtitle_background_enabled is False:
        params_kwargs["text_background_color"] = False
        params_kwargs["rounded_subtitle_background"] = False
    elif args.subtitle_background_color is not None:
        params_kwargs["text_background_color"] = args.subtitle_background_color
    elif args.subtitle_background_enabled is True:
        params_kwargs["text_background_color"] = True

    return VideoParams(**params_kwargs)


def _resolve_cli_file(
    raw_path: str,
    *,
    description: str,
    fallback_dir: str | None = None,
) -> str:
    expanded_path = os.path.expanduser(raw_path.strip())
    if not expanded_path:
        raise ValueError(f"{description} path cannot be empty")
    candidate = expanded_path if os.path.isabs(expanded_path) else os.path.join(os.getcwd(), expanded_path)
    resolved_path = os.path.realpath(candidate)
    if not os.path.isfile(resolved_path) and fallback_dir and not os.path.isabs(expanded_path):
        resolved_path = os.path.realpath(os.path.join(fallback_dir, expanded_path))
    if not os.path.isfile(resolved_path):
        raise ValueError(f"{description} file does not exist: {raw_path}")
    return resolved_path


def _path_is_within_directory(file_path: str, directory: str) -> bool:
    try:
        return os.path.commonpath(
            [os.path.realpath(directory), os.path.realpath(file_path)]
        ) == os.path.realpath(directory)
    except ValueError:
        return False


def _resolve_managed_resource_file(
    raw_path: str,
    *,
    resource_dir: str,
    description: str,
) -> str:
    expanded_path = os.path.expanduser(raw_path.strip())
    candidates = (
        [expanded_path]
        if os.path.isabs(expanded_path)
        else [os.path.join(resource_dir, expanded_path), os.path.join(utils.root_dir(), expanded_path)]
    )
    for candidate in candidates:
        resolved_path = os.path.realpath(candidate)
        if os.path.isfile(resolved_path) and _path_is_within_directory(resolved_path, resource_dir):
            return resolved_path
    raise ValueError(f"{description} file must exist inside {resource_dir}: {raw_path}")


def prepare_cli_files(params: VideoParams, stop_at: str) -> None:
    from app.models import const

    local_material_extensions = {
        *(f".{extension}" for extension in const.FILE_TYPE_VIDEOS),
        *(f".{extension}" for extension in const.FILE_TYPE_IMAGES),
        ".avi",
        ".flv",
    }

    if params.custom_audio_file:
        params.custom_audio_file = _resolve_cli_file(
            params.custom_audio_file, description="custom audio"
        )
        extension = os.path.splitext(params.custom_audio_file)[1].lower()
        if extension not in _CUSTOM_AUDIO_EXTENSIONS:
            allowed = ", ".join(sorted(_CUSTOM_AUDIO_EXTENSIONS))
            raise ValueError(f"unsupported custom audio type {extension or '<none>'}; allowed: {allowed}")

    if params.bgm_type == "custom" and params.bgm_file:
        params.bgm_file = _resolve_managed_resource_file(
            params.bgm_file,
            resource_dir=utils.song_dir(),
            description="background music",
        )
        if not params.bgm_file.lower().endswith(".mp3"):
            raise ValueError("background music must use the .mp3 extension")

    if params.subtitle_enabled and params.font_name and stop_at == "video":
        font_path = _resolve_managed_resource_file(
            params.font_name,
            resource_dir=utils.font_dir(),
            description="subtitle font",
        )
        if not font_path.lower().endswith((".ttf", ".ttc")):
            raise ValueError("subtitle font must use the .ttf or .ttc extension")
        params.font_name = os.path.basename(font_path)

    if params.video_source != "local" or stop_at not in {"materials", "video"}:
        return

    local_videos_dir = utils.storage_dir("local_videos", create=True)
    resolved_materials = []
    for material in params.video_materials or []:
        source_path = _resolve_cli_file(
            material.url,
            description="local material",
            fallback_dir=local_videos_dir,
        )
        extension = os.path.splitext(source_path)[1].lower()
        if extension not in local_material_extensions:
            allowed = ", ".join(sorted(local_material_extensions))
            raise ValueError(f"unsupported local material type {extension or '<none>'}; allowed: {allowed}")
        resolved_materials.append((material, source_path, extension))

    prepared_paths = {}
    for material, source_path, extension in resolved_materials:
        prepared_path = prepared_paths.get(source_path)
        if prepared_path is None:
            if _path_is_within_directory(source_path, local_videos_dir):
                prepared_path = source_path
            else:
                prepared_path = os.path.join(
                    local_videos_dir, f"cli-material-{uuid4().hex}{extension}"
                )
                shutil.copy2(source_path, prepared_path)
                logger.info(
                    f"copied CLI local material into managed storage: source={source_path}, target={prepared_path}"
                )
            prepared_paths[source_path] = prepared_path
        material.url = prepared_path


def run_cli(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        params = build_video_params(args)
        prepare_cli_files(params, stop_at=args.stop_at)
    except (ValueError, OSError) as exc:
        logger.error(f"invalid CLI input: {exc}")
        return 2
    task_id = args.task_id or utils.get_uuid()
    logger.info(f"start CLI task: task_id={task_id}, stop_at={args.stop_at}")
    try:
        result = tm.start(task_id=task_id, params=params, stop_at=args.stop_at)
    except Exception as exc:
        logger.exception(f"CLI task failed: task_id={task_id}, error={exc}")
        return 1
    if not result:
        logger.error(f"CLI task failed: task_id={task_id}, stop_at={args.stop_at}")
        return 1

    print(json.dumps({"task_id": task_id, "result": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
