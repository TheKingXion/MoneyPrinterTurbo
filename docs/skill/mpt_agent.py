#!/usr/bin/env python3
"""Install MoneyPrinterTurbo and generate a final video for an AI agent."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ARCHIVE_URL = "https://github.com/harry0703/MoneyPrinterTurbo/archive/refs/heads/main.zip"
DEFAULT_ROOT = Path.home() / "MoneyPrinterTurbo"
DEFAULT_VOICE_NAME = "zh-CN-XiaoxiaoNeural-Female"
NEEDS_INPUT_EXIT_CODE = 10
PEXELS_API_KEY_URL = "https://www.pexels.com/api/"
KEYLESS_PROVIDERS = {"ollama", "litellm", "g4f"}


class SkillError(RuntimeError):
    pass


def log(message: str) -> None:
    print(f"[MoneyPrinterTurbo] {message}", flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", required=True, help="video topic")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("cli_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    args.subject = args.subject.strip()
    if not args.subject:
        parser.error("--subject cannot be empty")
    if args.cli_args and args.cli_args[0] == "--":
        args.cli_args = args.cli_args[1:]
    if any(item == "--stop-at" or item.startswith("--stop-at=") for item in args.cli_args):
        parser.error("--stop-at is managed by the Skill")
    return args


def _safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    destination = destination.resolve()
    for member in archive.infolist():
        target = (destination / member.filename).resolve()
        if target != destination and destination not in target.parents:
            raise SkillError(f"unsafe archive path: {member.filename}")
    archive.extractall(destination)


def ensure_project(root: Path) -> None:
    if (root / "cli.py").is_file() and (root / "config.example.toml").is_file():
        log(f"using existing project: {root}")
        return
    if root.exists() and any(root.iterdir()):
        raise SkillError(f"installation directory is not an MPT project: {root}")
    root.parent.mkdir(parents=True, exist_ok=True)
    log(f"downloading MoneyPrinterTurbo to {root}")
    with tempfile.TemporaryDirectory(prefix="mpt-install-") as value:
        temp_dir = Path(value)
        archive_path = temp_dir / "project.zip"
        request = urllib.request.Request(
            PROJECT_ARCHIVE_URL, headers={"User-Agent": "MoneyPrinterTurbo-Agent-Skill"}
        )
        with urllib.request.urlopen(request, timeout=120) as response, archive_path.open("wb") as output:
            shutil.copyfileobj(response, output)
        with zipfile.ZipFile(archive_path) as archive:
            _safe_extract(archive, temp_dir)
        candidates = [path for path in temp_dir.iterdir() if (path / "cli.py").is_file()]
        if len(candidates) != 1:
            raise SkillError("download did not contain one valid project")
        if root.exists():
            root.rmdir()
        shutil.move(str(candidates[0]), str(root))


def _value(text: str, key: str) -> str:
    match = re.search(rf"(?m)^{re.escape(key)}\s*=\s*(.*)$", text)
    if not match:
        return ""
    raw = match.group(1).split("#", 1)[0].strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip('"')
    if isinstance(parsed, list):
        return next((str(item).strip() for item in parsed if str(item).strip()), "")
    return str(parsed).strip()


def _replace(text: str, key: str, value: object) -> str:
    pattern = re.compile(rf"(?m)^({re.escape(key)}\s*=\s*).*$")
    if not pattern.search(text):
        raise SkillError(f"configuration field not found: {key}")
    return pattern.sub(lambda match: match.group(1) + json.dumps(value), text, count=1)


def ensure_config(root: Path) -> Path:
    path = root / "config.toml"
    if not path.exists():
        shutil.copy2(root / "config.example.toml", path)
        log(f"created configuration: {path}")
    text = path.read_text(encoding="utf-8")
    provider = os.environ.get("MPT_LLM_PROVIDER", "").strip().lower()
    provider = "oneapi" if provider == "openai_compatible" else provider
    if provider:
        text = _replace(text, "llm_provider", provider)
    provider = provider or _value(text, "llm_provider") or "moonshot"
    changes = {
        f"{provider}_api_key": os.environ.get("MPT_LLM_API_KEY", "").strip(),
        f"{provider}_base_url": os.environ.get("MPT_LLM_BASE_URL", "").strip(),
        f"{provider}_model_name": os.environ.get("MPT_LLM_MODEL_NAME", "").strip(),
    }
    for key, value in changes.items():
        if value:
            text = _replace(text, key, value)
    pexels_key = os.environ.get("MPT_PEXELS_API_KEY", "").strip()
    if pexels_key:
        text = _replace(text, "pexels_api_keys", [pexels_key])
    path.write_text(text, encoding="utf-8")
    return path


def selected_source(cli_args: list[str]) -> str:
    for index, item in enumerate(cli_args):
        if item == "--video-source" and index + 1 < len(cli_args):
            return cli_args[index + 1].strip().lower()
        if item.startswith("--video-source="):
            return item.split("=", 1)[1].strip().lower()
    return "pexels"


def missing_config(path: Path, cli_args: list[str]) -> tuple[str, list[str]]:
    text = path.read_text(encoding="utf-8")
    provider = _value(text, "llm_provider") or "moonshot"
    missing = []
    if provider not in KEYLESS_PROVIDERS and not _value(text, f"{provider}_api_key"):
        missing.append(f"{provider}_api_key")
    if provider == "oneapi":
        for suffix in ("base_url", "model_name"):
            if not _value(text, f"oneapi_{suffix}"):
                missing.append(f"oneapi_{suffix}")
    source = selected_source(cli_args)
    if source != "local" and not _value(text, f"{source}_api_keys"):
        missing.append(f"{source}_api_keys")
    return provider, missing


def manifest(root: Path, payload: dict[str, object]) -> Path:
    path = root / ".agent-logs" / "moneyprinterturbo-video" / "latest-result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": datetime.now(timezone.utc).isoformat(), **payload}
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)
    return path.resolve()


def generate(root: Path, subject: str, cli_args: list[str]) -> tuple[list[Path], Path, Path, Path]:
    uv = shutil.which("uv")
    if not uv:
        raise SkillError("uv was not found")
    log("installing or verifying dependencies")
    sync = subprocess.run([uv, "sync", "--frozen"], cwd=root, capture_output=True, text=True)
    if sync.returncode:
        print("\n".join((sync.stdout + sync.stderr).splitlines()[-30:]), file=sys.stderr)
        raise SkillError(f"dependency installation failed ({sync.returncode})")
    task_id = str(uuid.uuid4())
    task_dir = root / "storage" / "tasks" / task_id
    log_dir = root / ".agent-logs" / "moneyprinterturbo-video"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"run-{task_id}.log"
    command = [uv, "run", "python", "cli.py", *cli_args]
    if not any(item == "--voice-name" or item.startswith("--voice-name=") for item in cli_args):
        command += ["--voice-name", DEFAULT_VOICE_NAME]
    command += ["--video-subject", subject, "--task-id", task_id, "--stop-at", "video"]
    manifest(root, {"status": "running", "task_id": task_id, "log_file": str(log_path)})
    log(f"starting task {task_id}; log: {log_path}")
    with log_path.open("w", encoding="utf-8") as output:
        result = subprocess.run(command, cwd=root, stdout=output, stderr=subprocess.STDOUT, text=True)
    if result.returncode:
        tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
        print("\n".join(tail), file=sys.stderr)
        raise SkillError(f"generation failed ({result.returncode}); log: {log_path}")
    videos = sorted(path.resolve() for path in task_dir.glob("final-*.mp4") if path.stat().st_size)
    if not videos:
        raise SkillError(f"generation produced no valid MP4; log: {log_path}")
    result_path = manifest(root, {"status": "completed", "task_id": task_id, "video_files": [str(path) for path in videos], "log_file": str(log_path.resolve())})
    return videos, task_dir.resolve(), log_path.resolve(), result_path


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.expanduser().resolve()
    try:
        ensure_project(root)
        config_path = ensure_config(root)
        provider, missing = missing_config(config_path, args.cli_args)
        if missing:
            manifest(root, {"status": "needs_input", "missing": missing})
            print("MPT_NEEDS_INPUT")
            print(f"LLM_PROVIDER={provider}")
            for field in missing:
                print(f"MISSING={field}")
            if "pexels_api_keys" in missing:
                print(f"PEXELS_API_KEY_URL={PEXELS_API_KEY_URL}")
            return NEEDS_INPUT_EXIT_CODE
        videos, task_dir, log_path, result_path = generate(root, args.subject, args.cli_args)
    except (OSError, SkillError, urllib.error.URLError, zipfile.BadZipFile) as exc:
        print(f"MPT_ERROR={exc}", file=sys.stderr)
        return 1
    print("MPT_RESULT")
    for video in videos:
        print(f"VIDEO_FILE={video}")
    print(f"TASK_DIR={task_dir}")
    print(f"LOG_FILE={log_path}")
    print(f"RESULT_FILE={result_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
