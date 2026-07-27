"""Persistent, credential-safe cache for stock-material search results."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

from loguru import logger

from app.models.schema import MaterialInfo, VideoAspect
from app.utils import utils


MATERIAL_SEARCH_CACHE_TTL_SECONDS = 24 * 60 * 60
_CACHE_FORMAT_VERSION = 1
_CACHE_CLEANUP_INTERVAL_SECONDS = 60 * 60
_CACHE_FILE_PATTERN = re.compile(r"^[0-9a-f]{64}\.json$")
_CACHE_LOCKS = tuple(threading.Lock() for _ in range(256))
_cleanup_lock = threading.Lock()
_last_cleanup = 0.0


def safe_public_url(value: object) -> str | None:
    """Return a public HTTP(S) URL without credentials or query parameters."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _cache_dir() -> Path:
    return Path(utils.storage_dir("cache_material_search", create=True))


def _cache_key(
    provider: str,
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect | str,
) -> str:
    aspect = getattr(video_aspect, "value", video_aspect)
    payload = json.dumps(
        {
            "provider": str(provider).strip().lower(),
            "search_term": str(search_term).strip(),
            "minimum_duration": int(minimum_duration),
            "video_aspect": str(aspect),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_path(**kwargs) -> Path:
    return _cache_dir() / f"{_cache_key(**kwargs)}.json"


def get_search_lock(**kwargs) -> threading.Lock:
    digest = _cache_key(**kwargs)
    return _CACHE_LOCKS[int(digest[:8], 16) % len(_CACHE_LOCKS)]


def _sanitize_source_info(item: MaterialInfo) -> dict:
    raw = item.source_info if isinstance(item.source_info, dict) else {}
    result: dict[str, object] = {"provider": item.provider}
    for field in ("asset_id", "width", "height"):
        value = raw.get(field)
        if value not in (None, ""):
            result[field] = str(value) if field == "asset_id" else value
    source_page = safe_public_url(raw.get("source_page"))
    creator_page = safe_public_url(raw.get("creator_page"))
    if source_page:
        result["source_page"] = source_page
    if creator_page:
        result["creator_page"] = creator_page
    creator_name = raw.get("creator_name")
    if creator_name not in (None, ""):
        result["creator_name"] = str(creator_name)
    return result


def load_search_results(
    provider: str,
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect | str,
) -> list[MaterialInfo] | None:
    """Return a fresh cache entry, or None when remote search is required."""
    if str(provider).strip().lower() == "coverr":
        return None
    kwargs = {
        "provider": provider,
        "search_term": search_term,
        "minimum_duration": minimum_duration,
        "video_aspect": video_aspect,
    }
    try:
        cache_path = _cache_path(**kwargs)
        age = time.time() - cache_path.stat().st_mtime
        if age < 0 or age >= MATERIAL_SEARCH_CACHE_TTL_SECONDS:
            cache_path.unlink(missing_ok=True)
            return None
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        if payload.get("version") != _CACHE_FORMAT_VERSION:
            raise ValueError("unsupported material cache version")
        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise ValueError("empty material cache")
        items = []
        for raw in raw_items:
            if not isinstance(raw, dict) or not raw.get("url"):
                raise ValueError("invalid cached material")
            items.append(
                MaterialInfo(
                    provider=str(raw.get("provider") or provider),
                    url=str(raw["url"]),
                    duration=int(raw.get("duration") or 0),
                    thumbnail_url=str(raw.get("thumbnail_url") or ""),
                    search_term=search_term,
                    score=0.0,
                    scene_index=-1,
                    source_info=raw.get("source_info")
                    if isinstance(raw.get("source_info"), dict)
                    else None,
                )
            )
        logger.info(
            f"material search cache hit: provider={provider}, "
            f"term={search_term!r}, items={len(items)}"
        )
        return items
    except FileNotFoundError:
        return None
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning(
            f"invalid material search cache ignored: provider={provider}, error={exc}"
        )
        try:
            _cache_path(**kwargs).unlink(missing_ok=True)
        except OSError:
            pass
        return None


def save_search_results(
    provider: str,
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect | str,
    items: Iterable[MaterialInfo],
) -> bool:
    """Atomically save successful results while preserving CLIP thumbnails."""
    if str(provider).strip().lower() == "coverr":
        return False
    serialized = [
        {
            "provider": item.provider,
            "url": item.url,
            "duration": int(item.duration),
            "thumbnail_url": item.thumbnail_url,
            "source_info": _sanitize_source_info(item),
        }
        for item in items
        if item.url and item.duration > 0
    ]
    if not serialized:
        return False
    cache_path = _cache_path(
        provider=provider,
        search_term=search_term,
        minimum_duration=minimum_duration,
        video_aspect=video_aspect,
    )
    temp_path: Path | None = None
    try:
        cleanup_expired()
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=cache_path.parent,
            prefix=f".{cache_path.stem}-",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            json.dump(
                {"version": _CACHE_FORMAT_VERSION, "items": serialized},
                temp_file,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, cache_path)
        return True
    except Exception as exc:
        logger.warning(f"material search cache write failed: {exc}")
        return False
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def cleanup_expired(*, force: bool = False) -> int:
    global _last_cleanup
    now_monotonic = time.monotonic()
    with _cleanup_lock:
        if not force and now_monotonic - _last_cleanup < _CACHE_CLEANUP_INTERVAL_SECONDS:
            return 0
        _last_cleanup = now_monotonic
    deleted = 0
    now = time.time()
    try:
        entries = list(_cache_dir().iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.is_file() or not _CACHE_FILE_PATTERN.fullmatch(entry.name):
            continue
        try:
            age = now - entry.stat().st_mtime
            if age < 0 or age >= MATERIAL_SEARCH_CACHE_TTL_SECONDS:
                entry.unlink()
                deleted += 1
        except OSError:
            continue
    return deleted
