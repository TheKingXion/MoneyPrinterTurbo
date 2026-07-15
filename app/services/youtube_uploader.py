import glob
import json
import os
import re
import secrets
import shutil
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from loguru import logger

from app.config import config
from app.utils import utils


YOUTUBE_UPLOAD_SCOPE = ["https://www.googleapis.com/auth/youtube.upload"]
SUCCESS_STATUSES = {"completed", "scheduled"}
CLAIMED_STATUSES = {"uploading", "reconcile_required"}
SCHEDULE_MODES = {"interval", "daily_block"}


def _local_timezone(timezone_name: str = "local"):
    if timezone_name and timezone_name != "local":
        try:
            return ZoneInfo(timezone_name)
        except Exception as exc:
            raise ValueError(f"invalid YouTube schedule timezone: {timezone_name}") from exc
    try:
        from tzlocal import get_localzone

        return get_localzone()
    except ImportError:
        return datetime.now().astimezone().tzinfo


def _parse_schedule_time(value: str) -> tuple[int, int]:
    try:
        parsed = datetime.strptime(str(value), "%H:%M")
    except (TypeError, ValueError) as exc:
        raise ValueError("YouTube schedule time must use HH:MM (00:00-23:59)") from exc
    return parsed.hour, parsed.minute


def build_publish_plan(
    total_videos: int,
    schedule_mode: str = "interval",
    start_date: date | str | None = None,
    schedule_at: str = "21:00",
    videos_per_day: int = 4,
    interval_minutes: int = 15,
    timezone_name: str = "local",
    now: datetime | None = None,
    occupied_publish_at: Optional[set[str]] = None,
    occupied_counts_toward_daily_capacity: bool = True,
    collision_policy: str = "skip",
    allow_shared_publish_time: bool = False,
) -> list[dict[str, Any]]:
    total_videos = int(total_videos)
    videos_per_day = int(videos_per_day)
    interval_minutes = int(interval_minutes)
    if total_videos < 0:
        raise ValueError("total_videos cannot be negative")
    if total_videos == 0:
        return []
    if schedule_mode not in SCHEDULE_MODES:
        raise ValueError(f"invalid YouTube schedule mode: {schedule_mode}")
    if videos_per_day < 1:
        raise ValueError("videos_per_day must be at least 1")
    if schedule_mode == "daily_block" and videos_per_day > 60:
        raise ValueError("videos_per_day cannot exceed 60 in daily block mode")
    if interval_minutes < 0 or (interval_minutes == 0 and not allow_shared_publish_time):
        raise ValueError("interval_minutes must be at least 1 unless shared publish time is enabled")
    if collision_policy not in {"skip", "error"}:
        raise ValueError("collision_policy must be skip or error")

    hour, minute = _parse_schedule_time(schedule_at)
    local_tz = _local_timezone(timezone_name)
    current = now or datetime.now(local_tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=local_tz)
    else:
        current = current.astimezone(local_tz)

    explicit_start = start_date not in (None, "")
    if isinstance(start_date, str):
        try:
            first_date = date.fromisoformat(start_date)
        except ValueError as exc:
            raise ValueError("YouTube start date must use YYYY-MM-DD") from exc
    elif isinstance(start_date, date):
        first_date = start_date
    else:
        first_date = current.date()

    base = datetime.combine(first_date, datetime.min.time(), tzinfo=local_tz).replace(hour=hour, minute=minute)
    if explicit_start and base <= current:
        raise ValueError("YouTube schedule start date and time must be in the future")
    if not explicit_start and base <= current:
        base += timedelta(days=1)

    occupied = set()
    occupied_by_local_date: dict[str, int] = {}
    for value in occupied_publish_at or set():
        try:
            occupied_dt = datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            continue
        occupied.add(occupied_dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
        local_date = occupied_dt.astimezone(local_tz).date().isoformat()
        occupied_by_local_date[local_date] = occupied_by_local_date.get(local_date, 0) + 1
    plan: list[dict[str, Any]] = []
    planned_by_local_date: dict[str, int] = {}
    candidate_index = 0
    daily_day_offset = 0
    daily_position = 0
    max_candidates = total_videos + len(occupied) + 10000
    while len(plan) < total_videos and candidate_index < max_candidates:
        if schedule_mode == "daily_block":
            day_offset = daily_day_offset
            position = daily_position
            minute_offset = position * interval_minutes
            local_datetime = base + timedelta(days=day_offset, minutes=minute_offset)
            daily_position += 1
        else:
            day_offset = 0
            position = candidate_index
            local_datetime = base + timedelta(minutes=candidate_index * interval_minutes)
        # Normalize nonexistent local wall times across daylight-saving transitions.
        round_trip = local_datetime.astimezone(timezone.utc).astimezone(local_tz)
        if round_trip.replace(tzinfo=None) != local_datetime.replace(tzinfo=None):
            local_datetime = round_trip
        local_date = local_datetime.date().isoformat()
        if schedule_mode == "daily_block" and (
            (occupied_by_local_date.get(local_date, 0) if occupied_counts_toward_daily_capacity else 0)
            + planned_by_local_date.get(local_date, 0)
            >= videos_per_day
        ):
            daily_day_offset += 1
            daily_position = 0
            candidate_index += 1
            continue
        publish_at = local_datetime.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        candidate_index += 1
        if publish_at in occupied and not allow_shared_publish_time:
            if collision_policy == "error":
                raise ValueError(f"YouTube schedule slot is already occupied: {local_datetime.isoformat()}")
            continue
        if not allow_shared_publish_time and any(item["publish_at"] == publish_at for item in plan):
            continue
        plan.append(
            {
                "index": len(plan),
                "schedule_mode": schedule_mode,
                "publish_at": publish_at,
                "publish_at_local": local_datetime.isoformat(),
                "local_date": local_date,
                "local_time": local_datetime.strftime("%H:%M"),
                "day_offset": day_offset,
                "position_in_day": position,
                "timezone": str(getattr(local_tz, "key", local_tz)),
            }
        )
        planned_by_local_date[local_date] = planned_by_local_date.get(local_date, 0) + 1
        if (
            schedule_mode == "daily_block"
            and (occupied_by_local_date.get(local_date, 0) if occupied_counts_toward_daily_capacity else 0)
            + planned_by_local_date[local_date]
            >= videos_per_day
        ):
            daily_day_offset += 1
            daily_position = 0
    if len(plan) != total_videos:
        raise RuntimeError("Unable to allocate all YouTube schedule slots")

    daily_counts: dict[str, int] = {}
    for item in plan:
        daily_counts[item["local_date"]] = daily_counts.get(item["local_date"], 0) + 1
    for item in plan:
        item["videos_on_date"] = daily_counts[item["local_date"]]
    return plan


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _storage_file(filename: str) -> str:
    return os.path.join(utils.storage_dir(create=True), filename)


def _safe_read_text(file_path: str) -> str:
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _normalize_hashtag(tag: str) -> str:
    tag = (tag or "").strip()
    if not tag:
        return ""
    return tag if tag.startswith("#") else f"#{tag}"


def parse_metadata_file(metadata_path: str) -> dict[str, Any]:
    """Parse METADATOS.md into YouTube-friendly metadata."""
    content = _safe_read_text(metadata_path)
    if not content:
        return {"title": "", "description": "", "tags": []}

    youtube_section = content
    youtube_match = re.search(
        r"##\s*YouTube Shorts(?P<section>.*?)(?:\n##\s+|\Z)",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if youtube_match:
        youtube_section = youtube_match.group("section")

    title_match = re.search(r"^Título:\s*(.+)$", youtube_section, re.MULTILINE)
    description_match = re.search(
        r"^Descripción:\s*(.+?)(?:\n\s*\n|\Z)",
        youtube_section,
        flags=re.MULTILINE | re.DOTALL,
    )
    hashtags_match = re.search(r"^Hashtags:\s*(.+)$", youtube_section, re.MULTILINE)

    if not title_match:
        title_match = re.search(r"^Título:\s*(.+)$", content, re.MULTILINE)

    tags = []
    if hashtags_match:
        tags = [
            tag
            for tag in (_normalize_hashtag(item) for item in hashtags_match.group(1).split())
            if tag
        ]

    return {
        "title": (title_match.group(1).strip() if title_match else "")[:100],
        "description": (description_match.group(1).strip() if description_match else "")[:5000],
        "tags": tags[:15],
    }


def read_task_subject(task_dir: str, metadata_path: str = "") -> str:
    script_path = os.path.join(task_dir, "script.json")
    try:
        with open(script_path, "r", encoding="utf-8") as f:
            script_data = json.load(f)
        params = script_data.get("params") or {}
        if isinstance(params, dict) and params.get("video_subject"):
            return str(params["video_subject"]).strip()
    except (OSError, json.JSONDecodeError, TypeError):
        pass

    if metadata_path:
        metadata = parse_metadata_file(metadata_path)
        if metadata.get("title"):
            return metadata["title"]
    return os.path.basename(task_dir)


def classify_youtube_upload_error(exc: Exception) -> dict[str, Any]:
    """Classify failures without including response bodies or credential data."""
    response = getattr(exc, "resp", None) or getattr(exc, "response", None)
    status_code = int(getattr(response, "status", 0) or getattr(response, "status_code", 0) or 0)
    reason = ""
    error_details = getattr(exc, "error_details", None)
    if isinstance(error_details, list) and error_details and isinstance(error_details[0], dict):
        reason = str(error_details[0].get("reason", ""))
    transient = status_code in {408, 429} or status_code >= 500
    if not status_code:
        name = type(exc).__name__.lower()
        text = str(exc).lower()
        transient = any(value in name or value in text for value in ("timeout", "connection", "transport"))
    return {
        "status_code": status_code,
        "reason": reason,
        "retryable": transient,
        "outcome_unknown": transient,
    }


def _managed_upload_identity(video_path: str) -> tuple[str, int] | None:
    tasks_root = os.path.realpath(utils.task_dir())
    resolved = os.path.realpath(video_path)
    try:
        relative = os.path.relpath(resolved, tasks_root)
    except ValueError:
        return None
    parts = relative.split(os.sep)
    if len(parts) != 2 or parts[0] in {"", ".", ".."}:
        return None
    filename = parts[1]
    match = re.fullmatch(r"final-(\d+)\.mp4", filename, re.IGNORECASE)
    if match:
        return parts[0], int(match.group(1))
    if filename.lower() == "final.mp4":
        return parts[0], 1
    return None


class UploadTracker:
    def __init__(self, log_path: str | None = None):
        self.log_path = log_path or _storage_file("youtube_upload_log.json")
        self.lock_path = f"{self.log_path}.lock"
        self._lock = threading.RLock()

    def _load_unlocked(self) -> list[dict[str, Any]]:
        if not os.path.isfile(self.log_path):
            return []
        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("uploads"), list):
                raise RuntimeError(f"YouTube upload log has an invalid format: {self.log_path}")
            return data["uploads"]
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"YouTube upload log is corrupt: {self.log_path}") from exc
        except OSError as exc:
            raise RuntimeError(f"YouTube upload log cannot be read: {self.log_path}") from exc

    def _save_unlocked(self, uploads: list[dict[str, Any]]) -> None:
        os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
        temporary = f"{self.log_path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        with open(temporary, "w", encoding="utf-8") as f:
            json.dump({"uploads": uploads}, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, self.log_path)

    @contextmanager
    def _transaction(self):
        with self._lock:
            os.makedirs(os.path.dirname(self.lock_path) or ".", exist_ok=True)
            deadline = time.monotonic() + 10
            owner_token = secrets.token_hex(16)
            while True:
                try:
                    descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    with os.fdopen(descriptor, "w", encoding="ascii") as f:
                        f.write(f"{owner_token} {time.time()}")
                    break
                except FileExistsError:
                    try:
                        stale = time.time() - os.path.getmtime(self.lock_path) > 30
                    except OSError:
                        stale = False
                    if stale:
                        try:
                            os.remove(self.lock_path)
                        except FileNotFoundError:
                            pass
                        continue
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Timed out waiting for YouTube upload log lock")
                    time.sleep(0.05)
            try:
                yield
            finally:
                try:
                    with open(self.lock_path, "r", encoding="ascii") as f:
                        current_token = f.read().split()[0]
                    if secrets.compare_digest(current_token, owner_token):
                        os.remove(self.lock_path)
                except (FileNotFoundError, OSError, IndexError):
                    pass

    def load(self) -> list[dict[str, Any]]:
        with self._transaction():
            return self._load_unlocked()

    def save(self, uploads: list[dict[str, Any]]) -> None:
        with self._transaction():
            self._save_unlocked(uploads)

    def get_by_task_id(self, task_id: str, index: int = 1) -> Optional[dict[str, Any]]:
        for entry in self.load():
            if entry.get("task_id") == task_id and int(entry.get("index", 1)) == index:
                return entry
        return None

    def get_by_subject(self, subject: str) -> Optional[dict[str, Any]]:
        normalized = (subject or "").strip().lower()
        if not normalized:
            return None
        for entry in self.load():
            if str(entry.get("video_subject", "")).strip().lower() == normalized:
                return entry
        return None

    def is_uploaded(self, task_id: str, index: int = 1) -> bool:
        entry = self.get_by_task_id(task_id, index)
        return bool(entry and entry.get("status") in SUCCESS_STATUSES)

    def add_entry(
        self,
        task_id: str,
        index: int,
        subject: str,
        video_path: str,
        status: str,
        youtube_id: str = "",
        youtube_url: str = "",
        publish_at: str = "",
        publish_at_local: str = "",
        schedule_mode: str = "",
        error: str = "",
    ) -> dict[str, Any]:
        with self._transaction():
            uploads = self._load_unlocked()
            existing = None
            for entry in uploads:
                if entry.get("task_id") == task_id and int(entry.get("index", 1)) == index:
                    existing = entry
                    break

            if existing is None:
                existing = {
                    "task_id": task_id,
                    "index": index,
                    "attempts": 1,
                    "created_at": _utc_now_iso(),
                }
                uploads.append(existing)
            elif existing.get("status") in CLAIMED_STATUSES:
                return existing
            elif existing.get("status") in SUCCESS_STATUSES and status != existing.get("status"):
                return existing

            existing.update(
                {
                    "video_subject": subject,
                    "video_path": video_path,
                    "status": status,
                    "youtube_id": youtube_id or existing.get("youtube_id", ""),
                    "youtube_url": youtube_url or existing.get("youtube_url", ""),
                    "publish_at": publish_at or existing.get("publish_at", ""),
                    "publish_at_local": publish_at_local or existing.get("publish_at_local", ""),
                    "schedule_mode": schedule_mode or existing.get("schedule_mode", ""),
                    "uploaded_at": (
                        existing.get("uploaded_at") or _utc_now_iso()
                        if status in SUCCESS_STATUSES
                        else existing.get("uploaded_at", "")
                    ),
                    "updated_at": _utc_now_iso(),
                    "error": error,
                }
            )
            self._save_unlocked(uploads)
            return existing

    def claim(self, task_id: str, index: int, subject: str, video_path: str) -> str | None:
        """Atomically reserve one generated output and return its ownership token."""
        with self._transaction():
            uploads = self._load_unlocked()
            entry = next(
                (item for item in uploads if item.get("task_id") == task_id and int(item.get("index", 1)) == index),
                None,
            )
            if entry and entry.get("status") in SUCCESS_STATUSES | CLAIMED_STATUSES:
                return None
            if entry is None:
                entry = {"task_id": task_id, "index": index, "created_at": _utc_now_iso(), "attempts": 0}
                uploads.append(entry)
            token = secrets.token_hex(16)
            entry.update(
                video_subject=subject,
                video_path=video_path,
                status="uploading",
                claim_token=token,
                attempts=int(entry.get("attempts", 0)) + 1,
                updated_at=_utc_now_iso(),
                error="",
            )
            self._save_unlocked(uploads)
            return token

    def finalize(self, task_id: str, index: int, claim_token: str, status: str, **values) -> Optional[dict[str, Any]]:
        if status not in SUCCESS_STATUSES | {"reconcile_required"}:
            raise ValueError(f"invalid YouTube final status: {status}")
        with self._transaction():
            uploads = self._load_unlocked()
            entry = next(
                (item for item in uploads if item.get("task_id") == task_id and int(item.get("index", 1)) == index),
                None,
            )
            if not entry or not secrets.compare_digest(str(entry.get("claim_token", "")), claim_token):
                return None
            entry.update(status=status, updated_at=_utc_now_iso(), **values)
            entry.pop("claim_token", None)
            if status in SUCCESS_STATUSES:
                entry["uploaded_at"] = _utc_now_iso()
            self._save_unlocked(uploads)
            return entry

    def release(self, task_id: str, index: int, claim_token: str, error: str = "") -> bool:
        with self._transaction():
            uploads = self._load_unlocked()
            entry = next(
                (item for item in uploads if item.get("task_id") == task_id and int(item.get("index", 1)) == index),
                None,
            )
            if not entry or not secrets.compare_digest(str(entry.get("claim_token", "")), claim_token):
                return False
            entry.update(status="failed", error=error, updated_at=_utc_now_iso())
            entry.pop("claim_token", None)
            self._save_unlocked(uploads)
            return True

    def update_status(self, task_id: str, status: str, **kwargs) -> Optional[dict[str, Any]]:
        with self._transaction():
            uploads = self._load_unlocked()
            for entry in uploads:
                if entry.get("task_id") == task_id:
                    entry.update({"status": status, "updated_at": _utc_now_iso(), **kwargs})
                    self._save_unlocked(uploads)
                    return entry
            return None

    def count_successes(self) -> int:
        return sum(1 for entry in self.load() if entry.get("status") in SUCCESS_STATUSES)

    def future_publish_times(self) -> set[str]:
        now = datetime.now(timezone.utc)
        result = set()
        for entry in self.load():
            if entry.get("status") != "scheduled" or not entry.get("publish_at"):
                continue
            try:
                publish_at = datetime.fromisoformat(str(entry["publish_at"]).replace("Z", "+00:00"))
            except ValueError:
                continue
            if publish_at > now:
                result.add(str(entry["publish_at"]))
        return result

    def last_upload_info(self) -> dict[str, Any]:
        uploads = [entry for entry in self.load() if entry.get("uploaded_at")]
        if not uploads:
            return {}
        uploads.sort(key=lambda entry: str(entry.get("uploaded_at", "")), reverse=True)
        return uploads[0]

    def count_today_api_uploads(self) -> int:
        pacific = ZoneInfo("America/Los_Angeles")
        today = datetime.now(pacific).date()
        count = 0
        for entry in self.load():
            if entry.get("status") not in SUCCESS_STATUSES:
                continue
            uploaded_at = str(entry.get("uploaded_at", ""))
            if not uploaded_at:
                continue
            try:
                uploaded_date = datetime.fromisoformat(
                    uploaded_at.replace("Z", "+00:00")
                ).astimezone(pacific).date()
            except ValueError:
                continue
            if uploaded_date == today:
                count += 1
        return count


class YouTubeUploader:
    def __init__(self):
        self.token_path = _storage_file("youtube_token.json")
        self.sync_from_config()

    def sync_from_config(self) -> None:
        youtube_config = getattr(config, "youtube", {})
        self.enabled = bool(youtube_config.get("enabled", False))
        self.auto_upload = bool(youtube_config.get("auto_upload", False))
        self.privacy_status = youtube_config.get("privacy_status", "public") or "public"
        self.schedule_enabled = bool(youtube_config.get("schedule_enabled", False))
        self.schedule_at = youtube_config.get("schedule_at", "21:00") or "21:00"
        self.schedule_interval_minutes = int(
            youtube_config.get("schedule_interval_minutes", 15) or 15
        )
        self.schedule_mode = str(youtube_config.get("schedule_mode", "interval") or "interval")
        self.schedule_videos_per_day = int(youtube_config.get("schedule_videos_per_day", 4) or 4)
        self.schedule_timezone = str(youtube_config.get("schedule_timezone", "local") or "local")
        self.daily_api_limit = int(youtube_config.get("daily_api_limit", 7) or 7)
        self.client_id = youtube_config.get("client_id", "") or ""
        self.client_secret = youtube_config.get("client_secret", "") or ""

    def sync_from_disk(self) -> None:
        persisted = config.load_config().get("youtube", {})
        if isinstance(persisted, dict):
            config.youtube.update(persisted)
        self.sync_from_config()

    def is_configured(self) -> bool:
        return bool(self.enabled and self.client_id and self.client_secret)

    def is_authorized(self) -> bool:
        return os.path.isfile(self.token_path)

    def remaining_api_slots(self) -> int:
        return max(0, self.daily_api_limit - upload_tracker.count_today_api_uploads())

    def _client_config(self) -> dict[str, Any]:
        return {
            "installed": {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }

    def _get_service(self):
        if not self.is_configured():
            raise RuntimeError("YouTube is not configured. Fill [youtube] in config.toml.")

        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise RuntimeError(
                "Missing Google dependencies. Run: pip install google-api-python-client google-auth-oauthlib google-auth-httplib2"
            ) from exc

        credentials = None
        if os.path.isfile(self.token_path):
            credentials = Credentials.from_authorized_user_file(
                self.token_path, YOUTUBE_UPLOAD_SCOPE
            )

        if not credentials or not credentials.valid:
            if credentials and credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_config(
                    self._client_config(), YOUTUBE_UPLOAD_SCOPE
                )
                credentials = flow.run_local_server(port=0)
            os.makedirs(os.path.dirname(self.token_path), exist_ok=True)
            with open(self.token_path, "w", encoding="utf-8") as token_file:
                token_file.write(credentials.to_json())

        return build("youtube", "v3", credentials=credentials)

    def authorize(self) -> dict[str, Any]:
        self._get_service()
        return {"success": True, "authorized": True, "token_path": self.token_path}

    def create_publish_plan(
        self,
        total_videos: int,
        start_date: date | str | None = None,
        schedule_mode: str = "",
        schedule_at: str = "",
        videos_per_day: int | None = None,
        interval_minutes: int | None = None,
        timezone_name: str = "",
        avoid_existing: bool = True,
        occupied_counts_toward_daily_capacity: bool = True,
        collision_policy: str = "skip",
        occupied_publish_at: Optional[set[str]] = None,
        allow_shared_publish_time: bool = False,
    ) -> list[dict[str, Any]]:
        occupied = upload_tracker.future_publish_times() if avoid_existing else set()
        occupied.update(occupied_publish_at or set())
        return build_publish_plan(
            total_videos=total_videos,
            schedule_mode=schedule_mode or self.schedule_mode,
            start_date=start_date,
            schedule_at=schedule_at or self.schedule_at,
            videos_per_day=self.schedule_videos_per_day if videos_per_day is None else videos_per_day,
            interval_minutes=self.schedule_interval_minutes if interval_minutes is None else interval_minutes,
            timezone_name=timezone_name or self.schedule_timezone,
            occupied_publish_at=occupied,
            occupied_counts_toward_daily_capacity=occupied_counts_toward_daily_capacity,
            collision_policy=collision_policy,
            allow_shared_publish_time=allow_shared_publish_time,
        )

    def calculate_publish_at(self, slot_index: int = 0) -> str:
        slot_index = max(0, int(slot_index))
        return self.create_publish_plan(slot_index + 1, avoid_existing=False)[slot_index]["publish_at"]

    def next_publish_slot(self) -> dict[str, Any]:
        return self.create_publish_plan(1, avoid_existing=True)[0]

    def upload_video(
        self,
        video_path: str,
        title: str,
        description: str = "",
        tags: Optional[list[str]] = None,
        publish_at: str = "",
        privacy_status: str = "",
        task_id: str = "",
        index: int | None = None,
    ) -> dict[str, Any]:
        if not os.path.isfile(video_path):
            return {"success": False, "error": f"video file not found: {video_path}"}

        try:
            from googleapiclient.http import MediaFileUpload
        except ImportError as exc:
            return {"success": False, "error": str(exc)}

        identity = _managed_upload_identity(video_path)
        if identity and task_id and identity[0] != task_id:
            return {"success": False, "error": "task_id does not match the generated video path"}
        claim_task_id = task_id or (identity[0] if identity else "")
        claim_index = int(index if index is not None else (identity[1] if identity else 1))
        claim_token = ""
        if claim_task_id:
            claim_token = upload_tracker.claim(claim_task_id, claim_index, title, video_path) or ""
            if not claim_token:
                return {
                    "success": False,
                    "skipped": True,
                    "error": "already_claimed",
                    "task_id": claim_task_id,
                    "index": claim_index,
                }

        request_started = False
        try:
            youtube = self._get_service()
            status = {
                "privacyStatus": "private" if publish_at else (privacy_status or self.privacy_status),
                "selfDeclaredMadeForKids": False,
                "containsSyntheticMedia": True,
            }
            if publish_at:
                status["publishAt"] = publish_at

            body = {
                "snippet": {
                    "title": (title or os.path.basename(video_path))[:100],
                    "description": (description or "")[:5000],
                    "tags": [str(tag).lstrip("#") for tag in (tags or [])][:15],
                    "categoryId": "22",
                },
                "status": status,
            }
            media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
            request_started = True
            response = (
                youtube.videos()
                .insert(part="snippet,status", body=body, media_body=media)
                .execute()
            )
            video_id = response.get("id", "")
            result = {
                "success": True,
                "video_id": video_id,
                "url": f"https://youtu.be/{video_id}" if video_id else "",
                "scheduled": bool(publish_at),
                "publish_at": publish_at,
            }
            if claim_token:
                upload_tracker.finalize(
                    claim_task_id,
                    claim_index,
                    claim_token,
                    "scheduled" if publish_at else "completed",
                    youtube_id=result["video_id"],
                    youtube_url=result["url"],
                    publish_at=publish_at,
                    error="",
                )
            return result
        except Exception as exc:
            logger.exception(f"YouTube upload failed: {exc}")
            classification = classify_youtube_upload_error(exc)
            classification["outcome_unknown"] = bool(request_started and classification["outcome_unknown"])
            if claim_token:
                if classification["outcome_unknown"]:
                    upload_tracker.finalize(
                        claim_task_id,
                        claim_index,
                        claim_token,
                        "reconcile_required",
                        error=str(exc),
                    )
                else:
                    upload_tracker.release(claim_task_id, claim_index, claim_token, str(exc))
            return {"success": False, "error": str(exc), **classification}


def scan_pending_videos(status_filter: str = "") -> dict[str, Any]:
    tasks_root = utils.task_dir()
    uploads_by_task = {
        (entry.get("task_id"), int(entry.get("index", 1))): entry
        for entry in upload_tracker.load()
    }
    videos: list[dict[str, Any]] = []

    for task_dir in glob.glob(os.path.join(tasks_root, "*")):
        if not os.path.isdir(task_dir):
            continue
        task_id = os.path.basename(task_dir)
        video_files = glob.glob(os.path.join(task_dir, "final-*.mp4"))
        video_files.sort(
            key=lambda file_path: int(match.group(1))
            if (match := re.search(r"final-(\d+)\.mp4$", os.path.basename(file_path), re.IGNORECASE))
            else 0
        )
        if not video_files:
            final_one = os.path.join(task_dir, "final.mp4")
            video_files = [final_one] if os.path.isfile(final_one) else []
        if not video_files:
            continue

        metadata_path = os.path.join(task_dir, "METADATOS.md")
        has_metadata = os.path.isfile(metadata_path)
        subject = read_task_subject(task_dir, metadata_path if has_metadata else "")
        for fallback_index, video_path in enumerate(video_files, start=1):
            filename_match = re.search(r"final-(\d+)\.mp4$", os.path.basename(video_path), re.IGNORECASE)
            index = int(filename_match.group(1)) if filename_match else fallback_index
            try:
                size_bytes = os.path.getsize(video_path)
            except OSError:
                continue
            if size_bytes < 1024 * 1024:
                continue
            entry = uploads_by_task.get((task_id, index)) or {}
            youtube_status = entry.get("status") or "pending"
            if status_filter and youtube_status != status_filter:
                continue

            videos.append(
                {
                    "task_id": task_id,
                    "index": index,
                    "subject": subject,
                    "video_path": video_path,
                    "video_size_mb": round(size_bytes / 1024 / 1024, 2),
                    "has_metadata": has_metadata,
                    "metadata_path": metadata_path if has_metadata else "",
                    "generated_at": datetime.fromtimestamp(
                        os.path.getmtime(video_path)
                    ).isoformat(timespec="seconds"),
                    "youtube_status": youtube_status,
                    "youtube_url": entry.get("youtube_url", ""),
                    "youtube_id": entry.get("youtube_id", ""),
                    "publish_at": entry.get("publish_at", ""),
                    "publish_at_local": entry.get("publish_at_local", ""),
                    "error": entry.get("error", ""),
                }
            )

    order = {"pending": 0, "failed": 1, "scheduled": 2, "completed": 3}
    videos.sort(key=lambda item: (order.get(item["youtube_status"], 9), item["generated_at"]))
    return {
        "total": len(videos),
        "pending": sum(1 for item in videos if item["youtube_status"] == "pending"),
        "scheduled": sum(1 for item in videos if item["youtube_status"] == "scheduled"),
        "completed": sum(1 for item in videos if item["youtube_status"] == "completed"),
        "failed": sum(1 for item in videos if item["youtube_status"] == "failed"),
        "videos": videos,
    }


def move_uploaded_task(task_id: str) -> str:
    task_id = str(task_id)
    if not task_id or task_id in {".", ".."} or os.path.basename(task_id) != task_id:
        raise ValueError("invalid task_id")
    tasks_root = os.path.realpath(utils.task_dir())
    source = os.path.join(tasks_root, task_id)
    if os.path.dirname(os.path.realpath(source)) != tasks_root or os.path.islink(source):
        raise ValueError("invalid task path")
    if not os.path.isdir(source):
        return ""
    uploaded_root = utils.storage_dir("uploaded", create=True)
    destination = os.path.join(uploaded_root, task_id)
    if os.path.dirname(os.path.realpath(destination)) != os.path.realpath(uploaded_root):
        raise ValueError("invalid uploaded task path")
    if os.path.islink(destination):
        raise ValueError("uploaded task destination cannot be a symbolic link")
    if os.path.exists(destination):
        shutil.rmtree(destination)
    shutil.move(source, destination)
    return destination


youtube_uploader = YouTubeUploader()
upload_tracker = UploadTracker()
