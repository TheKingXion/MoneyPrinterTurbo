import hashlib
import json
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

import requests
from loguru import logger

from app.config import config
from app.services.social_video_scanner import scan_generated_videos
from app.services.upload_post import UploadPostService
from app.utils import utils


TIKTOK_AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
TIKTOK_API_BASE = "https://open.tiktokapis.com/v2"
SUCCESS_STATUSES = {"published", "processing"}


def _storage_file(filename: str) -> str:
    return os.path.join(utils.storage_dir(create=True), filename)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as file:
            return file.read()
    except OSError:
        return ""


def parse_tiktok_metadata_file(metadata_path: str) -> dict[str, Any]:
    content = _read_text(metadata_path)
    if not content:
        return {"title": "", "caption": "", "hashtags": []}
    section = content
    match = re.search(r"##\s*TikTok(?P<section>.*?)(?:\n##\s+|\Z)", content, re.IGNORECASE | re.DOTALL)
    if match:
        section = match.group("section")
    title = re.search(r"^Título:\s*(.+)$", section, re.MULTILINE)
    description = re.search(r"^Descripción:\s*(.+?)(?:\n\s*\n|\Z)", section, re.MULTILINE | re.DOTALL)
    hashtag_line = re.search(r"^Hashtags:\s*(.+)$", section, re.MULTILINE)
    hashtags: list[str] = []
    if hashtag_line:
        for item in hashtag_line.group(1).split():
            tag = item.strip()
            if tag:
                normalized = tag if tag.startswith("#") else f"#{tag}"
                if normalized.lower() not in {existing.lower() for existing in hashtags}:
                    hashtags.append(normalized)
    caption_parts = [(description.group(1).strip() if description else ""), " ".join(hashtags)]
    caption = "\n\n".join(part for part in caption_parts if part).strip()[:2200]
    return {
        "title": (title.group(1).strip() if title else "")[:100],
        "caption": caption,
        "hashtags": hashtags,
    }


class TikTokUploadTracker:
    def __init__(self, log_path: str | None = None):
        self.log_path = log_path or _storage_file("tiktok_upload_log.json")
        self.lock_path = f"{self.log_path}.lock"
        self._lock = threading.RLock()

    def _load_unlocked(self) -> list[dict[str, Any]]:
        if not os.path.isfile(self.log_path):
            return []
        try:
            with open(self.log_path, "r", encoding="utf-8") as file:
                data = json.load(file)
            uploads = data.get("uploads", []) if isinstance(data, dict) else []
            return uploads if isinstance(uploads, list) else []
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"TikTok upload log is corrupt: {self.log_path}") from exc
        except OSError as exc:
            raise RuntimeError(f"TikTok upload log cannot be read: {self.log_path}") from exc

    def _save_unlocked(self, uploads: list[dict[str, Any]]) -> None:
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        temporary = f"{self.log_path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump({"uploads": uploads}, file, ensure_ascii=False, indent=2)
        os.replace(temporary, self.log_path)

    @contextmanager
    def _transaction(self):
        with self._lock:
            deadline = time.monotonic() + 10
            owner_token = secrets.token_hex(16)
            while True:
                try:
                    descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    with os.fdopen(descriptor, "w", encoding="ascii") as file:
                        file.write(f"{owner_token} {time.time()}")
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
                        raise RuntimeError("Timed out waiting for TikTok upload log lock")
                    time.sleep(0.05)
            try:
                yield
            finally:
                try:
                    with open(self.lock_path, "r", encoding="ascii") as file:
                        current_token = file.read().split()[0]
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
        return next(
            (entry for entry in self.load() if entry.get("task_id") == task_id and int(entry.get("index", 1)) == index),
            None,
        )

    def add_entry(self, task_id: str, index: int, subject: str, video_path: str, status: str, **values) -> dict[str, Any]:
        with self._transaction():
            uploads = self._load_unlocked()
            entry = next(
                (item for item in uploads if item.get("task_id") == task_id and int(item.get("index", 1)) == index),
                None,
            )
            if entry is None:
                entry = {"task_id": task_id, "index": index, "created_at": _utc_now_iso(), "attempts": 0}
                uploads.append(entry)
            previous_status = entry.get("status")
            entry.update(
                {
                    "video_subject": subject,
                    "video_path": video_path,
                    "status": status,
                    "attempts": int(entry.get("attempts", 0)) + (1 if status == "uploading" and previous_status != "uploading" else 0),
                    "updated_at": _utc_now_iso(),
                    **values,
                }
            )
            if status in SUCCESS_STATUSES and not entry.get("uploaded_at"):
                entry["uploaded_at"] = _utc_now_iso()
            self._save_unlocked(uploads)
            return entry

    def claim(self, task_id: str, index: int, subject: str, video_path: str, provider: str) -> bool:
        with self._transaction():
            uploads = self._load_unlocked()
            entry = next(
                (item for item in uploads if item.get("task_id") == task_id and int(item.get("index", 1)) == index),
                None,
            )
            if entry and entry.get("status") in {
                "scheduled", "scheduled_retry", "uploading", "processing", "reconcile_required", "published"
            }:
                return False
            today = datetime.now().astimezone().date()
            active_today = 0
            for item in uploads:
                if item.get("status") not in {"uploading", "processing", "published"}:
                    continue
                stamp = item.get("uploaded_at") or item.get("updated_at") or item.get("created_at")
                try:
                    if datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone().date() == today:
                        active_today += 1
                except ValueError:
                    pass
            daily_limit = max(1, int(config.tiktok.get("daily_upload_limit", 10) or 10))
            if active_today >= daily_limit:
                return False
            if entry is None:
                entry = {"task_id": task_id, "index": index, "created_at": _utc_now_iso(), "attempts": 0}
                uploads.append(entry)
            entry.update(
                video_subject=subject,
                video_path=video_path,
                status="uploading",
                provider=provider,
                error="",
                attempts=int(entry.get("attempts", 0)) + 1,
                updated_at=_utc_now_iso(),
            )
            self._save_unlocked(uploads)
            return True

    def reserve_schedule(
        self,
        task_id: str,
        index: int,
        subject: str,
        video_path: str,
        provider: str,
        scheduled_at: str,
    ) -> bool:
        with self._transaction():
            uploads = self._load_unlocked()
            entry = next(
                (item for item in uploads if item.get("task_id") == task_id and int(item.get("index", 1)) == index),
                None,
            )
            if entry and entry.get("status") in {
                "scheduled", "scheduled_retry", "uploading", "processing", "reconcile_required", "published"
            }:
                return False
            if entry is None:
                entry = {"task_id": task_id, "index": index, "created_at": _utc_now_iso(), "attempts": 0}
                uploads.append(entry)
            entry.update(
                video_subject=subject,
                video_path=video_path,
                status="scheduled",
                provider=provider,
                scheduled_at=scheduled_at,
                error="",
                updated_at=_utc_now_iso(),
            )
            self._save_unlocked(uploads)
            return True

    def update_status(self, task_id: str, status: str, index: int = 1, **values) -> Optional[dict[str, Any]]:
        with self._transaction():
            uploads = self._load_unlocked()
            entry = next(
                (item for item in uploads if item.get("task_id") == task_id and int(item.get("index", 1)) == index),
                None,
            )
            if not entry:
                return None
            previous_status = entry.get("status")
            entry.update(status=status, updated_at=_utc_now_iso(), **values)
            if status == "uploading" and previous_status != "uploading":
                entry["attempts"] = int(entry.get("attempts", 0)) + 1
            if status in SUCCESS_STATUSES and not entry.get("uploaded_at"):
                entry["uploaded_at"] = _utc_now_iso()
            self._save_unlocked(uploads)
            return entry

    def count_today_uploads(self) -> int:
        today = datetime.now().astimezone().date()
        count = 0
        for entry in self.load():
            if entry.get("status") not in SUCCESS_STATUSES:
                continue
            try:
                uploaded = datetime.fromisoformat(str(entry.get("uploaded_at", "")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if uploaded.astimezone().date() == today:
                count += 1
        return count

    def last_upload_info(self) -> dict[str, Any]:
        uploads = [item for item in self.load() if item.get("uploaded_at")]
        return max(uploads, key=lambda item: str(item.get("uploaded_at", ""))) if uploads else {}


class TikTokUploader:
    def __init__(self):
        self.token_path = _storage_file("tiktok_token.json")
        self.token_lock_path = f"{self.token_path}.lock"
        self.state_path = _storage_file("tiktok_oauth_state.json")
        self.state_dir = _storage_file("tiktok_oauth_states")
        self._token_lock = threading.RLock()
        self.sync_from_config()

    def sync_from_config(self, settings: Optional[dict[str, Any]] = None) -> None:
        settings = settings if settings is not None else getattr(config, "tiktok", {})
        self.enabled = bool(settings.get("enabled", False))
        self.provider = str(settings.get("provider", "official") or "official")
        self.auto_upload = bool(settings.get("auto_upload", False))
        self.client_key = str(settings.get("client_key", "") or "")
        self.client_secret = str(settings.get("client_secret", "") or "")
        self.redirect_uri = str(settings.get("redirect_uri", "") or "")
        self.privacy_level = str(settings.get("privacy_level", "SELF_ONLY") or "SELF_ONLY")
        self.allow_comments = bool(settings.get("allow_comments", True))
        self.allow_duet = bool(settings.get("allow_duet", False))
        self.allow_stitch = bool(settings.get("allow_stitch", False))
        self.daily_upload_limit = int(settings.get("daily_upload_limit", 10) or 10)

    def sync_from_disk(self) -> None:
        persisted = config.load_config().get("tiktok", {})
        if isinstance(persisted, dict):
            config.tiktok.update(persisted)
        if os.getenv("TIKTOK_CLIENT_KEY"):
            config.tiktok["client_key"] = os.environ["TIKTOK_CLIENT_KEY"]
        if os.getenv("TIKTOK_CLIENT_SECRET"):
            config.tiktok["client_secret"] = os.environ["TIKTOK_CLIENT_SECRET"]
        self.sync_from_config()

    def _client_key_fingerprint(self) -> str:
        return hashlib.sha256(self.client_key.encode("utf-8")).hexdigest()

    def _validate_oauth_config(self) -> None:
        if self.provider != "official":
            raise RuntimeError("TikTok OAuth requires the official provider")
        if not self.client_key or not self.client_secret or not self.redirect_uri:
            raise RuntimeError("TikTok Client Key, Client Secret, and redirect URI are required")
        parsed = urlparse(self.redirect_uri)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1"}:
            raise RuntimeError("TikTok Desktop redirect URI must use localhost or 127.0.0.1")
        try:
            port = parsed.port
        except ValueError as exc:
            raise RuntimeError("TikTok Desktop redirect URI has an invalid port") from exc
        if port is None:
            raise RuntimeError("TikTok Desktop redirect URI must include a port")
        if parsed.query or parsed.fragment:
            raise RuntimeError("TikTok redirect URI cannot contain query parameters or fragments")

    def _state_file(self, state: str) -> str:
        state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
        return os.path.join(self.state_dir, f"{state_hash}.json")

    def _cleanup_oauth_states(self) -> None:
        if not os.path.isdir(self.state_dir):
            return
        now = datetime.now(timezone.utc).timestamp()
        for item in os.scandir(self.state_dir):
            try:
                if item.is_file() and now - item.stat().st_mtime > 900:
                    os.remove(item.path)
            except OSError:
                pass

    def is_configured(self) -> bool:
        self.sync_from_config()
        if not self.enabled:
            return False
        if self.provider == "upload_post":
            return UploadPostService(config.tiktok).is_configured()
        return bool(self.client_key and self.client_secret and self.redirect_uri)

    def is_authorized(self) -> bool:
        if self.provider == "upload_post":
            return UploadPostService(config.tiktok).is_configured()
        token = self._load_token()
        if not token.get("access_token"):
            return False
        expected_fingerprint = token.get("client_key_fingerprint")
        if expected_fingerprint and not secrets.compare_digest(str(expected_fingerprint), self._client_key_fingerprint()):
            return False
        saved_at = int(token.get("saved_at", 0) or 0)
        expires_in = int(token.get("expires_in", 0) or 0)
        expired = bool(expires_in and int(datetime.now(timezone.utc).timestamp()) >= saved_at + expires_in)
        return not expired or bool(token.get("refresh_token"))

    def remaining_upload_slots(self) -> int:
        return max(0, self.daily_upload_limit - tiktok_upload_tracker.count_today_uploads())

    def _load_token(self) -> dict[str, Any]:
        try:
            with open(self.token_path, "r", encoding="utf-8") as file:
                return json.load(file)
        except (OSError, json.JSONDecodeError):
            return {}

    @contextmanager
    def _token_transaction(self):
        with self._token_lock:
            deadline = time.monotonic() + 40
            owner_token = secrets.token_hex(16)
            while True:
                try:
                    descriptor = os.open(self.token_lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    with os.fdopen(descriptor, "w", encoding="ascii") as file:
                        file.write(f"{owner_token} {time.time()}")
                    break
                except FileExistsError:
                    try:
                        stale = time.time() - os.path.getmtime(self.token_lock_path) > 120
                    except OSError:
                        stale = False
                    if stale:
                        try:
                            os.remove(self.token_lock_path)
                        except FileNotFoundError:
                            pass
                        continue
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Timed out waiting for TikTok token lock")
                    time.sleep(0.05)
            try:
                yield
            finally:
                try:
                    with open(self.token_lock_path, "r", encoding="ascii") as file:
                        current_token = file.read().split()[0]
                    if secrets.compare_digest(current_token, owner_token):
                        os.remove(self.token_lock_path)
                except (FileNotFoundError, OSError, IndexError):
                    pass

    def _save_token_unlocked(self, token: dict[str, Any]) -> None:
        token["saved_at"] = int(datetime.now(timezone.utc).timestamp())
        token["client_key_fingerprint"] = self._client_key_fingerprint()
        token["redirect_uri"] = self.redirect_uri
        os.makedirs(os.path.dirname(self.token_path), exist_ok=True)
        temporary = f"{self.token_path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(token, file, ensure_ascii=False, indent=2)
        os.replace(temporary, self.token_path)

    def _save_token(self, token: dict[str, Any]) -> None:
        with self._token_transaction():
            self._save_token_unlocked(token)

    @staticmethod
    def _response_payload(response: requests.Response) -> dict[str, Any]:
        try:
            payload = response.json()
            return payload if isinstance(payload, dict) else {}
        except (ValueError, requests.RequestException):
            return {}

    @classmethod
    def _raise_oauth_error(cls, response: requests.Response, fallback: str) -> dict[str, Any]:
        payload = cls._response_payload(response)
        if response.ok:
            return payload
        message = payload.get("error_description") or payload.get("message") or payload.get("error") or fallback
        log_id = payload.get("log_id")
        if log_id:
            message = f"{message} (TikTok log_id: {log_id})"
        raise RuntimeError(f"{message} [HTTP {response.status_code}]")

    def authorization_url(self) -> dict[str, Any]:
        self.sync_from_config()
        self._validate_oauth_config()
        self._cleanup_oauth_states()
        state = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = hashlib.sha256(code_verifier.encode("ascii")).hexdigest()
        os.makedirs(self.state_dir, exist_ok=True)
        state_file = self._state_file(state)
        temporary = f"{state_file}.{os.getpid()}.tmp"
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(
                {
                    "state": state,
                    "code_verifier": code_verifier,
                    "created_at": _utc_now_iso(),
                    "redirect_uri": self.redirect_uri,
                    "client_key_fingerprint": self._client_key_fingerprint(),
                },
                file,
            )
        os.replace(temporary, state_file)
        query = urlencode(
            {
                "client_key": self.client_key,
                "response_type": "code",
                "scope": "user.info.basic,video.upload,video.publish",
                "redirect_uri": self.redirect_uri,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return {"success": True, "authorization_url": f"{TIKTOK_AUTH_URL}?{query}", "state": state}

    def exchange_code(self, code: str, state: str) -> dict[str, Any]:
        if not code:
            raise RuntimeError("TikTok authorization code is missing")
        if not state:
            raise RuntimeError("TikTok OAuth state is missing")
        state_file = self._state_file(state)
        claimed_file = f"{state_file}.{os.getpid()}.{secrets.token_hex(4)}.claimed"
        try:
            os.replace(state_file, claimed_file)
        except FileNotFoundError as exc:
            raise RuntimeError("Invalid, expired, or already used TikTok OAuth state") from exc
        try:
            with open(claimed_file, "r", encoding="utf-8") as file:
                expected = json.load(file)
            if not secrets.compare_digest(state, str(expected.get("state", ""))):
                raise RuntimeError("Invalid TikTok OAuth state")
            try:
                created_at = datetime.fromisoformat(str(expected.get("created_at", "")).replace("Z", "+00:00"))
            except ValueError as exc:
                raise RuntimeError("Invalid TikTok OAuth state timestamp") from exc
            age = (datetime.now(timezone.utc) - created_at).total_seconds()
            if age < -60 or age > 600:
                raise RuntimeError("TikTok OAuth state expired")
            code_verifier = str(expected.get("code_verifier", ""))
            if not 43 <= len(code_verifier) <= 128 or re.fullmatch(r"[A-Za-z0-9._~-]+", code_verifier) is None:
                raise RuntimeError("TikTok OAuth PKCE verifier is missing or invalid; start authorization again")

            self.sync_from_disk()
            self._validate_oauth_config()
            expected_fingerprint = str(expected.get("client_key_fingerprint", ""))
            if not secrets.compare_digest(expected_fingerprint, self._client_key_fingerprint()):
                raise RuntimeError("TikTok Client Key changed during authorization; start again")
            redirect_uri = str(expected.get("redirect_uri", ""))
            if not secrets.compare_digest(redirect_uri, self.redirect_uri):
                raise RuntimeError("TikTok redirect URI changed during authorization; start again")

            response = requests.post(
                TIKTOK_TOKEN_URL,
                data={
                    "client_key": self.client_key,
                    "client_secret": self.client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                },
                timeout=30,
            )
            token = self._raise_oauth_error(response, "TikTok token exchange failed")
            if not token.get("access_token"):
                raise RuntimeError(token.get("error_description") or token.get("error") or "TikTok token exchange failed")
            self._save_token(token)
            return {"success": True, "authorized": True, "open_id": token.get("open_id", "")}
        finally:
            try:
                os.remove(claimed_file)
            except FileNotFoundError:
                pass

    def _access_token(self) -> str:
        token = self._load_token()
        if not token.get("access_token"):
            raise RuntimeError("TikTok is not authorized")
        saved_at = int(token.get("saved_at", 0) or 0)
        expires_in = int(token.get("expires_in", 0) or 0)
        now = int(datetime.now(timezone.utc).timestamp())
        if expires_in and now >= saved_at + expires_in - 300:
            token = self.refresh_token(token)
        return str(token["access_token"])

    def refresh_token(self, token: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        with self._token_transaction():
            current = self._load_token()
            if token and int(current.get("saved_at", 0) or 0) > int(token.get("saved_at", 0) or 0):
                now = int(datetime.now(timezone.utc).timestamp())
                if now < int(current.get("saved_at", 0) or 0) + int(current.get("expires_in", 0) or 0) - 300:
                    return current
            token = current or token or {}
            if not token.get("refresh_token"):
                raise RuntimeError("TikTok refresh token is missing")
            expected_fingerprint = token.get("client_key_fingerprint")
            if expected_fingerprint and not secrets.compare_digest(str(expected_fingerprint), self._client_key_fingerprint()):
                raise RuntimeError("TikTok token belongs to a different Client Key; authorize again")
            response = requests.post(
                TIKTOK_TOKEN_URL,
                data={
                    "client_key": self.client_key,
                    "client_secret": self.client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": token["refresh_token"],
                },
                timeout=30,
            )
            refreshed = self._raise_oauth_error(response, "TikTok token refresh failed")
            if not refreshed.get("access_token"):
                raise RuntimeError(refreshed.get("error_description") or "TikTok token refresh failed")
            merged = {**token, **refreshed}
            self._save_token_unlocked(merged)
            return merged

    def creator_info(self, provider: str = "") -> dict[str, Any]:
        selected_provider = provider or self.provider
        if selected_provider == "upload_post":
            return {"provider": "upload_post", "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"]}
        response = requests.post(
            f"{TIKTOK_API_BASE}/post/publish/creator_info/query/",
            headers={"Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json; charset=UTF-8"},
            json={},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        error = payload.get("error") or {}
        if error.get("code") not in (None, "", "ok"):
            raise RuntimeError(error.get("message") or error.get("code"))
        return payload.get("data") or {}

    def upload_video(
        self,
        video_path: str,
        caption: str,
        privacy_level: str = "",
        allow_comment: Optional[bool] = None,
        allow_duet: Optional[bool] = None,
        allow_stitch: Optional[bool] = None,
        provider: str = "",
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        self.sync_from_config()
        selected_provider = provider or self.provider
        if not self.enabled:
            return {"success": False, "error": "TikTok upload is disabled"}
        if not os.path.isfile(video_path):
            return {"success": False, "error": f"video file not found: {video_path}"}
        if os.path.getsize(video_path) <= 0:
            return {"success": False, "error": f"video file is empty: {video_path}"}
        if self.remaining_upload_slots() <= 0:
            return {"success": False, "error": "daily_upload_limit_reached"}
        if selected_provider == "upload_post":
            result = UploadPostService(config.tiktok).upload_video(
                video_path=video_path,
                title=(caption or os.path.basename(video_path))[:2200],
                platforms=["tiktok"],
                privacy_level=privacy_level or self.privacy_level,
                idempotency_key=idempotency_key,
            )
            platform_result = (result.get("results") or {}).get("tiktok") if isinstance(result.get("results"), dict) else None
            if isinstance(platform_result, dict) and platform_result.get("success") is False:
                return {
                    "success": False,
                    "provider": selected_provider,
                    "status": "failed",
                    "error": platform_result.get("error") or platform_result.get("message") or "TikTok upload failed",
                    "raw": result,
                }
            request_id = result.get("request_id", "")
            synchronous_url = (platform_result or {}).get("url", "") if isinstance(platform_result, dict) else ""
            return {
                "success": bool(result.get("success")),
                "provider": selected_provider,
                "publish_id": request_id,
                "tiktok_url": synchronous_url,
                "status": "published" if synchronous_url and not request_id else "processing" if result.get("success") else "failed",
                "error": result.get("error") or result.get("message", ""),
                "raw": result,
            }

        publish_id = ""
        try:
            creator = self.creator_info(selected_provider)
            privacy = privacy_level or self.privacy_level
            options = creator.get("privacy_level_options") or []
            if options and privacy not in options:
                raise RuntimeError(f"Privacy level not allowed for this creator: {privacy}")
            size = os.path.getsize(video_path)
            chunk_size = min(10 * 1024 * 1024, size)
            total_chunks = max(1, size // max(chunk_size, 1))
            post_info = {
                "title": (caption or os.path.basename(video_path))[:2200],
                "privacy_level": privacy,
                "disable_comment": bool(creator.get("comment_disabled")) or not (self.allow_comments if allow_comment is None else allow_comment),
                "disable_duet": bool(creator.get("duet_disabled")) or not (self.allow_duet if allow_duet is None else allow_duet),
                "disable_stitch": bool(creator.get("stitch_disabled")) or not (self.allow_stitch if allow_stitch is None else allow_stitch),
                "video_cover_timestamp_ms": 1000,
                "is_aigc": True,
            }
            response = requests.post(
                f"{TIKTOK_API_BASE}/post/publish/video/init/",
                headers={"Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json; charset=UTF-8"},
                json={
                    "post_info": post_info,
                    "source_info": {
                        "source": "FILE_UPLOAD",
                        "video_size": size,
                        "chunk_size": chunk_size,
                        "total_chunk_count": total_chunks,
                    },
                },
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            error = payload.get("error") or {}
            if error.get("code") not in (None, "", "ok"):
                raise RuntimeError(error.get("message") or error.get("code"))
            data = payload.get("data") or {}
            upload_url = data.get("upload_url")
            publish_id = data.get("publish_id", "")
            if not upload_url or not publish_id:
                raise RuntimeError("TikTok did not return upload_url and publish_id")
            with open(video_path, "rb") as video:
                start = 0
                for chunk_index in range(total_chunks):
                    read_size = chunk_size if chunk_index < total_chunks - 1 else size - start
                    chunk = video.read(read_size)
                    end = start + len(chunk) - 1
                    upload = requests.put(
                        upload_url,
                        headers={
                            "Content-Type": "video/mp4",
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {start}-{end}/{size}",
                        },
                        data=chunk,
                        timeout=300,
                    )
                    upload.raise_for_status()
                    start = end + 1
            return {"success": True, "provider": "official", "publish_id": publish_id, "status": "processing"}
        except Exception as exc:
            logger.exception(f"TikTok upload failed: {exc}")
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", 0) or 0
            transient = isinstance(exc, (requests.Timeout, requests.ConnectionError)) or status_code in {408, 429} or status_code >= 500
            return {
                "success": False,
                "provider": "official",
                "status": "failed",
                "publish_id": publish_id,
                "retryable": not bool(publish_id) and transient,
                "error": str(exc),
            }

    @staticmethod
    def _normalize_upload_post_status(payload: dict[str, Any]) -> dict[str, Any]:
        aggregate = str(payload.get("status", "processing") or "processing").lower()
        results = payload.get("results") or payload.get("posts") or []
        platform_result: dict[str, Any] = {}
        if isinstance(results, dict):
            candidate = results.get("tiktok")
            platform_result = candidate if isinstance(candidate, dict) else {}
        elif isinstance(results, list):
            platform_result = next(
                (
                    item for item in results
                    if isinstance(item, dict) and str(item.get("platform", "")).lower() == "tiktok"
                ),
                {},
            )
        platform_status = str(platform_result.get("status", "") or "").lower()
        error = platform_result.get("error") or platform_result.get("message") or payload.get("error") or ""
        failed = (
            aggregate in {"failed", "error", "rejected"}
            or platform_result.get("success") is False
            or platform_status in {"failed", "error", "rejected"}
        )
        completed = aggregate in {"completed", "complete", "published", "success"}
        published = platform_result.get("success") is True or platform_status in {"completed", "published", "success"}
        if failed:
            status = "failed"
        elif published or (completed and not platform_result):
            status = "published"
        else:
            status = "processing"
        return {
            "success": True,
            "status": status,
            "error": str(error),
            "tiktok_url": platform_result.get("url") or platform_result.get("post_url") or "",
            "post_id": platform_result.get("post_id") or platform_result.get("id") or "",
            "data": platform_result,
            "raw": payload,
        }

    def fetch_status(self, publish_id: str, provider: str = "") -> dict[str, Any]:
        provider = provider or self.provider
        if provider == "upload_post":
            result = UploadPostService(config.tiktok).check_status(publish_id)
            if result.get("success") is False:
                return {"success": False, "status": "failed", "error": result.get("error") or result.get("message", ""), "raw": result}
            return self._normalize_upload_post_status(result)
        response = requests.post(
            f"{TIKTOK_API_BASE}/post/publish/status/fetch/",
            headers={"Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json; charset=UTF-8"},
            json={"publish_id": publish_id},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        error = payload.get("error") or {}
        if error.get("code") not in (None, "", "ok"):
            raise RuntimeError(error.get("message") or error.get("code"))
        data = payload.get("data") or {}
        if not data:
            raise RuntimeError("TikTok returned an empty publish status")
        return {"success": True, "status": data.get("status", "processing"), "data": data}

    def disconnect(self) -> None:
        for path in (self.token_path, self.state_path):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        if os.path.isdir(self.state_dir):
            for item in os.scandir(self.state_dir):
                try:
                    if item.is_file():
                        os.remove(item.path)
                except OSError:
                    pass


def scan_tiktok_videos(status_filter: str = "") -> dict[str, Any]:
    return scan_generated_videos(
        tiktok_upload_tracker.load(),
        parse_tiktok_metadata_file,
        "tiktok",
        status_filter,
    )


tiktok_upload_tracker = TikTokUploadTracker()
tiktok_uploader = TikTokUploader()
