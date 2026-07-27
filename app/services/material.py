import math
import json
import inspect
import os
import random
import threading
import tempfile
import time
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import List
from urllib.parse import quote_plus, urlencode

import requests
from loguru import logger
from moviepy.video.io.VideoFileClip import VideoFileClip

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect, VideoConcatMode
from app.services import clip_ranker, material_cache
from app.utils import utils

# Thread-safe counter for API key rotation
_api_key_counter = 0
_api_key_lock = threading.Lock()
_download_locks_guard = threading.Lock()
_download_locks = weakref.WeakValueDictionary()


def _redact_secret(message: object, secret: object) -> str:
    safe_message = str(message)
    secret_value = str(secret or "")
    if not secret_value:
        return safe_message
    safe_message = safe_message.replace(secret_value, "***")
    encoded_secret = quote_plus(secret_value)
    if encoded_secret != secret_value:
        safe_message = safe_message.replace(encoded_secret, "***")
    return safe_message


def _redact_request_error(error: object, *secrets: object) -> str:
    message = str(error)
    for secret in secrets:
        message = _redact_secret(message, secret)
    for proxy_url in config.proxy.values():
        message = _redact_secret(message, proxy_url)
    return message


def _is_cloudflare_challenge(response: requests.Response) -> bool:
    headers = getattr(response, "headers", {}) or {}
    if str(headers.get("cf-mitigated", "")).lower() == "challenge":
        return True
    if "text/html" not in str(headers.get("content-type", "")).lower():
        return False
    body = str(getattr(response, "text", "")).lower()
    return "just a moment" in body or "/cdn-cgi/challenge-platform/" in body


def _source_info(
    provider: str,
    *,
    asset_id: object = None,
    source_page: object = None,
    creator_name: object = None,
    creator_page: object = None,
    width: object = None,
    height: object = None,
) -> dict:
    result: dict[str, object] = {"provider": provider}
    if asset_id not in (None, ""):
        result["asset_id"] = str(asset_id)
    safe_source_page = material_cache.safe_public_url(source_page)
    safe_creator_page = material_cache.safe_public_url(creator_page)
    if safe_source_page:
        result["source_page"] = safe_source_page
    if safe_creator_page:
        result["creator_page"] = safe_creator_page
    if creator_name not in (None, ""):
        result["creator_name"] = str(creator_name)
    if width not in (None, ""):
        result["width"] = width
    if height not in (None, ""):
        result["height"] = height
    return result


def _material_record(item: MaterialInfo, local_path: str) -> dict:
    source = item.source_info if isinstance(item.source_info, dict) else {}
    return {
        "scene_index": int(item.scene_index),
        "search_term": item.search_term,
        "provider": item.provider,
        "score": float(item.score),
        "local_file": Path(local_path).name,
        "asset_id": source.get("asset_id"),
        "source_page": material_cache.safe_public_url(source.get("source_page")),
        "creator_name": source.get("creator_name"),
        "creator_page": material_cache.safe_public_url(source.get("creator_page")),
        "width": source.get("width"),
        "height": source.get("height"),
    }


def _persist_material_records(task_id: str, records: list[dict]) -> str | None:
    if not records:
        return None
    target = Path(utils.task_dir(task_id)) / "materials.json"
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=".materials.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            json.dump(
                {"version": 1, "materials": records},
                temp_file,
                ensure_ascii=False,
                indent=2,
            )
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, target)
        return str(target)
    except Exception as exc:
        logger.warning(f"failed to persist material provenance: task_id={task_id}, error={exc}")
        return None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


@contextmanager
def _cache_key_lock(cache_key: str, lock_path: str):
    with _download_locks_guard:
        thread_lock = _download_locks.get(cache_key)
        if thread_lock is None:
            thread_lock = threading.Lock()
            _download_locks[cache_key] = thread_lock

    with thread_lock:
        lock_file = open(lock_path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if os.path.getsize(lock_path) == 0:
                    lock_file.write(b"\0")
                    lock_file.flush()
                lock_file.seek(0)
                while True:
                    try:
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.05)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

            yield
        finally:
            try:
                lock_file.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                lock_file.close()


def _get_tls_verify() -> bool:
    # 默认开启 TLS 证书校验，防止素材搜索和下载过程被中间人篡改。
    # 仅在企业代理、自签证书等明确需要的场景下，允许用户通过
    # `config.toml` 显式设置 `tls_verify = false` 临时关闭。
    tls_verify = config.app.get("tls_verify", True)
    if isinstance(tls_verify, str):
        tls_verify = tls_verify.strip().lower() not in ("0", "false", "no", "off")

    if not tls_verify:
        logger.warning(
            "TLS certificate verification is disabled by config.app.tls_verify=false. "
            "Only use this in trusted proxy environments."
        )

    return bool(tls_verify)


def get_api_key(cfg_key: str):
    api_keys = config.app.get(cfg_key)
    if not api_keys:
        raise ValueError(
            f"\n\n##### {cfg_key} is not set #####\n\nPlease set it in the config.toml file: {config.config_file}\n\n"
        )

    # if only one key is provided, return it
    if isinstance(api_keys, str):
        return api_keys

    global _api_key_counter
    with _api_key_lock:
        _api_key_counter += 1
        return api_keys[_api_key_counter % len(api_keys)]


def search_videos_pexels(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    aspect = VideoAspect(video_aspect)
    video_orientation = aspect.name
    video_width, video_height = aspect.to_resolution()
    api_key = get_api_key("pexels_api_keys")
    headers = {
        "Authorization": api_key,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    }
    # Build URL
    params = {"query": search_term, "per_page": 30, "orientation": video_orientation}
    query_url = f"https://api.pexels.com/videos/search?{urlencode(params)}"
    logger.info(f"searching Pexels videos: query={search_term}, with proxy={bool(config.proxy)}")

    try:
        r = requests.get(
            query_url,
            headers=headers,
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=(30, 60),
        )
        response = r.json()
        video_items = []
        if "videos" not in response:
            logger.error(f"search videos failed: {response}")
            return video_items
        videos = response["videos"]
        # loop through each video in the result
        for v in videos:
            duration = v["duration"]
            # check if video has desired minimum duration
            if duration < minimum_duration:
                continue
            video_files = v["video_files"]
            # loop through each url to determine the best quality
            for video in video_files:
                w = int(video["width"])
                h = int(video["height"])
                if w == video_width and h == video_height:
                    item = MaterialInfo()
                    item.provider = "pexels"
                    item.url = video["link"]
                    item.duration = duration
                    item.thumbnail_url = str(v.get("image") or "")
                    item.search_term = search_term
                    user = v.get("user") if isinstance(v.get("user"), dict) else {}
                    item.source_info = _source_info(
                        "pexels",
                        asset_id=v.get("id"),
                        source_page=v.get("url"),
                        creator_name=user.get("name"),
                        creator_page=user.get("url"),
                        width=w,
                        height=h,
                    )
                    video_items.append(item)
                    break
        return video_items
    except Exception as e:
        logger.error(f"search videos failed: {str(e)}")

    return []


def search_videos_pixabay(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    aspect = VideoAspect(video_aspect)

    video_width, video_height = aspect.to_resolution()

    api_key = get_api_key("pixabay_api_keys")
    # Build URL
    params = {
        "q": search_term,
        "video_type": "all",  # Accepted values: "all", "film", "animation"
        "per_page": 30,
        "key": api_key,
    }
    query_url = f"https://pixabay.com/api/videos/?{urlencode(params)}"
    logger.info(f"searching Pixabay videos: query={search_term}, with proxy={bool(config.proxy)}")

    try:
        r = requests.get(
            query_url, proxies=config.proxy, verify=_get_tls_verify(), timeout=(30, 60)
        )
        status_code = int(getattr(r, "status_code", 200))
        headers = getattr(r, "headers", {}) or {}
        content_type = str(headers.get("content-type", ""))
        if _is_cloudflare_challenge(r):
            logger.error(
                "pixabay search blocked by Cloudflare challenge: "
                f"status={status_code}, cf_ray={headers.get('cf-ray') or 'unknown'}"
            )
            return []
        if status_code == 429:
            logger.error(
                "pixabay API rate limit exceeded: "
                f"retry_after={headers.get('retry-after') or 'unknown'}"
            )
            return []
        if status_code >= 400:
            logger.error(
                f"pixabay search failed: status={status_code}, "
                f"content_type={content_type or 'unknown'}"
            )
            return []
        try:
            response = r.json()
        except ValueError:
            logger.error(
                "pixabay returned a non-JSON response: "
                f"status={status_code}, content_type={content_type or 'unknown'}"
            )
            return []
        video_items = []
        if "hits" not in response:
            logger.error(f"search videos failed: {response}")
            return video_items
        videos = response["hits"]
        # loop through each video in the result
        for v in videos:
            duration = v["duration"]
            # check if video has desired minimum duration
            if duration < minimum_duration:
                continue
            video_files = v["videos"]
            # loop through each url to determine the best quality
            for video_type in video_files:
                video = video_files[video_type]
                w = int(video["width"])
                # h = int(video["height"])
                if w >= video_width:
                    item = MaterialInfo()
                    item.provider = "pixabay"
                    item.url = video["url"]
                    item.duration = duration
                    item.thumbnail_url = str(video.get("thumbnail") or "")
                    item.search_term = search_term
                    item.source_info = _source_info(
                        "pixabay",
                        asset_id=v.get("id"),
                        source_page=v.get("pageURL"),
                        creator_name=v.get("user"),
                        creator_page=(
                            f"https://pixabay.com/users/{v.get('user')}-{v.get('user_id')}/"
                            if v.get("user") and v.get("user_id")
                            else None
                        ),
                        width=w,
                        height=video.get("height"),
                    )
                    video_items.append(item)
                    break
        return video_items
    except Exception as e:
        logger.error(
            "pixabay search request failed: "
            f"error={type(e).__name__}, detail={_redact_request_error(e, api_key)}"
        )

    return []


def search_videos_coverr(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    """
    Coverr (https://coverr.co) - free HD/4K stock videos,
    subject to Coverr license terms (https://coverr.co/license).

    Coverr API notes (based on official docs at api.coverr.co/docs/):
      - 鉴权: Authorization: Bearer <api_key>
      - 搜索端点: GET /videos?query=...,响应结构 {"hits": [...], ...}
      - 加 ?urls=true 在搜索响应里直接返回 mp4 直链
      - URL 是 signed JWT(绑定 API key,无过期时间)
      - Coverr 库以 16:9 横屏为主,9:16 portrait 占比极低(约 1%)
        因此本函数不做 aspect_ratio 过滤,由下游 video.py 的
        resize + letterbox 逻辑统一处理
      - duration 字段同时存在 number 和 string 两种形态,本函数都接受

    本函数使用 urls.mp4_download 字段作为下载地址 —— 按 Coverr 官方文档
    (https://api.coverr.co/docs/videos/#download-a-video) 的说法,
    GET 这个 URL 本身就被 Coverr 当作一次合法的 download 事件计入统计,
    无需再调用 PATCH /videos/:id/stats/downloads。
    """
    api_key = get_api_key("coverr_api_keys")
    headers = {"Authorization": f"Bearer {api_key}"}
    params = {
        "query": search_term,
        "page_size": 30,
        "urls": "true",
        "sort": "popular",
    }
    query_url = f"https://api.coverr.co/videos?{urlencode(params)}"
    logger.info(f"searching Coverr videos: query={search_term}, with proxy={bool(config.proxy)}")

    try:
        r = requests.get(
            query_url,
            headers=headers,
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=(30, 60),
        )
        response = r.json()
        video_items: List[MaterialInfo] = []

        if not isinstance(response, dict) or "hits" not in response:
            logger.error(f"search videos failed: {response}")
            return video_items

        for v in response["hits"]:
            # duration 在不同响应里可能是 number(11.625) 或 string("10.500000")
            try:
                duration = int(float(v.get("duration") or 0))
            except (TypeError, ValueError):
                continue
            if duration < minimum_duration:
                continue

            video_id = v.get("id")
            mp4_download_url = (v.get("urls") or {}).get("mp4_download")
            if not video_id or not mp4_download_url:
                continue

            item = MaterialInfo()
            item.provider = "coverr"
            item.url = mp4_download_url
            item.duration = duration
            item.thumbnail_url = str(
                v.get("thumbnail")
                or v.get("poster")
                or (v.get("urls") or {}).get("thumbnail")
                or ""
            )
            item.search_term = search_term
            item.source_info = _source_info(
                "coverr",
                asset_id=video_id,
                source_page=v.get("url"),
                creator_name=v.get("author_name"),
                creator_page=v.get("author_url"),
                width=v.get("width"),
                height=v.get("height"),
            )
            video_items.append(item)
        return video_items
    except Exception as e:
        logger.error(f"search videos failed: {str(e)}")

    return []


def save_video(video_url: str, save_dir: str = "") -> str:
    if not save_dir:
        save_dir = utils.storage_dir("cache_videos")

    os.makedirs(save_dir, exist_ok=True)

    url_without_query = video_url.split("?")[0]
    url_hash = utils.md5(url_without_query)
    video_id = f"vid-{url_hash}"
    video_path = f"{save_dir}/{video_id}.mp4"
    partial_path = f"{video_path}.partial"
    lock_path = f"{video_path}.lock"

    with _cache_key_lock(video_path, lock_path):
        # Recheck after locking so concurrent callers share the completed download.
        if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
            logger.info(f"video already exists: {video_path}")
            return video_path

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
        }
        response = None
        try:
            response = requests.get(
                video_url,
                headers=headers,
                proxies=config.proxy,
                verify=_get_tls_verify(),
                timeout=(60, 240),
                stream=True,
            )
            response.raise_for_status()
            with open(partial_path, "wb") as partial_file:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        partial_file.write(chunk)
                partial_file.flush()
                os.fsync(partial_file.fileno())
        except Exception:
            try:
                os.remove(partial_path)
            except FileNotFoundError:
                pass
            raise
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception as close_error:
                    logger.warning(
                        f"failed to close video response: {video_url}, error: {str(close_error)}"
                    )

        clip = None
        is_valid = False
        try:
            if not os.path.exists(partial_path) or os.path.getsize(partial_path) == 0:
                raise ValueError("downloaded video is empty")
            clip = VideoFileClip(partial_path)
            duration = clip.duration
            fps = clip.fps
            if duration > 0 and fps > 0:
                is_valid = True
        except Exception as e:
            logger.warning(f"invalid video file: {partial_path} => {str(e)}")
            try:
                os.remove(partial_path)
            except FileNotFoundError:
                pass
            except Exception as remove_error:
                logger.warning(
                    f"failed to remove invalid video file: {partial_path}, error: {str(remove_error)}"
                )
        finally:
            if clip is not None:
                try:
                    clip.close()
                except Exception as close_error:
                    logger.warning(
                        f"failed to close video clip: {partial_path}, error: {str(close_error)}"
                    )
        if not is_valid:
            try:
                os.remove(partial_path)
            except FileNotFoundError:
                pass
            return ""
        try:
            os.replace(partial_path, video_path)
        except Exception:
            try:
                os.remove(partial_path)
            except FileNotFoundError:
                pass
            raise
        return video_path
    return ""


def _search_with_cache(
    provider: str,
    search_function,
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect,
) -> List[MaterialInfo]:
    if not inspect.isfunction(search_function) or search_function.__module__ != __name__:
        return search_function(
            search_term=search_term,
            minimum_duration=minimum_duration,
            video_aspect=video_aspect,
        )
    cache_args = {
        "provider": provider,
        "search_term": search_term,
        "minimum_duration": minimum_duration,
        "video_aspect": video_aspect,
    }
    cached = material_cache.load_search_results(**cache_args)
    if cached is not None:
        return cached
    with material_cache.get_search_lock(**cache_args):
        cached = material_cache.load_search_results(**cache_args)
        if cached is not None:
            return cached
        items = search_function(
            search_term=search_term,
            minimum_duration=minimum_duration,
            video_aspect=video_aspect,
        )
        if items:
            material_cache.save_search_results(**cache_args, items=items)
        return items


def search_scene_candidates(
    scene: dict,
    sources: List[str],
    video_aspect: VideoAspect = VideoAspect.portrait,
    minimum_duration: int = 3,
    limit: int = 4,
) -> List[MaterialInfo]:
    search_functions = {
        "pexels": search_videos_pexels,
        "pixabay": search_videos_pixabay,
        "coverr": search_videos_coverr,
    }
    ordered_sources = [name for name in ("pexels", "pixabay", "coverr") if name in sources]
    accepted = []
    fallback = []
    for source_name in ordered_sources:
        if len(accepted) >= limit:
            break
        try:
            raw = _search_with_cache(
                provider=source_name,
                search_function=search_functions[source_name],
                search_term=scene["query"],
                minimum_duration=minimum_duration,
                video_aspect=video_aspect,
            )
            if raw:
                fallback.append(raw[0])
            ranked = clip_ranker.rank_materials(
                raw,
                query=scene["query"],
                required_objects=scene.get("required_objects"),
                excluded_elements=scene.get("excluded_elements"),
                limit=limit - len(accepted),
            )
            for item in ranked:
                item.scene_index = int(scene.get("index", -1))
            accepted.extend(ranked)
            logger.info(
                f"CLIP accepted {len(ranked)} {source_name} candidates for scene "
                f"{scene.get('index', -1)}"
            )
        except Exception as exc:
            logger.warning(f"candidate search failed for {source_name}: {exc}")
    if clip_ranker.requires_strict_verification(scene["query"]):
        if accepted:
            return accepted[:limit]
        relaxed_query = clip_ranker.relax_query_for_fallback(scene["query"])
        logger.warning(
            f"no strict candidates for '{scene['query']}', retrying as '{relaxed_query}'"
        )
        relaxed = []
        for source_name in ordered_sources:
            try:
                raw = _search_with_cache(
                    provider=source_name,
                    search_function=search_functions[source_name],
                    search_term=relaxed_query,
                    minimum_duration=minimum_duration,
                    video_aspect=video_aspect,
                )
                ranked = clip_ranker.rank_materials(
                    raw,
                    query=relaxed_query,
                    required_objects=scene.get("required_objects"),
                    excluded_elements=scene.get("excluded_elements"),
                    limit=limit - len(relaxed),
                    threshold=0.22,
                )
                relaxed.extend(ranked)
                if len(relaxed) >= limit:
                    break
            except Exception as exc:
                logger.warning(f"relaxed candidate search failed for {source_name}: {exc}")
        if relaxed:
            for item in relaxed:
                item.scene_index = int(scene.get("index", -1))
            return relaxed[:limit]
        return accepted[:limit]
    return (accepted or fallback[:1])[:limit]


def download_videos(
    task_id: str,
    search_terms: List[str],
    source: str = "pexels",
    sources: List[str] | None = None,
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_concat_mode: VideoConcatMode = VideoConcatMode.random,
    audio_duration: float = 0.0,
    max_clip_duration: int = 5,
    match_script_order: bool = False,
) -> List[str]:
    search_functions = {
        "pexels": search_videos_pexels,
        "pixabay": search_videos_pixabay,
        "coverr": search_videos_coverr,
    }
    source_names = list(dict.fromkeys(sources or [source]))
    source_names = [name for name in source_names if name in search_functions]
    if not source_names:
        raise ValueError("at least one valid online video source is required")

    def search_videos(search_term, minimum_duration, video_aspect):
        return search_scene_candidates(
            scene={
                "index": -1,
                "query": search_term,
                "required_objects": [],
                "excluded_elements": [],
            },
            sources=source_names,
            video_aspect=video_aspect,
            minimum_duration=minimum_duration,
            limit=8,
        )

    logger.info(f"using online video sources: {', '.join(source_names)}")

    material_directory = config.app.get("material_directory", "").strip()
    if material_directory == "task":
        material_directory = utils.task_dir(task_id)
    elif material_directory and not os.path.isdir(material_directory):
        material_directory = ""

    if match_script_order:
        return _download_videos_by_script_order(
            task_id=task_id,
            search_terms=search_terms,
            search_videos=search_videos,
            video_aspect=video_aspect,
            audio_duration=audio_duration,
            max_clip_duration=max_clip_duration,
            material_directory=material_directory,
        )

    valid_video_items = []
    valid_video_urls = []
    found_duration = 0.0
    for search_term in search_terms:
        video_items = search_videos(
            search_term=search_term,
            minimum_duration=max_clip_duration,
            video_aspect=video_aspect,
        )
        logger.info(f"found {len(video_items)} videos for '{search_term}'")

        for item in video_items:
            if item.url not in valid_video_urls:
                valid_video_items.append(item)
                valid_video_urls.append(item.url)
                found_duration += item.duration

    logger.info(
        f"found total videos: {len(valid_video_items)}, required duration: {audio_duration} seconds, found duration: {found_duration} seconds"
    )
    video_paths = []
    material_records: list[dict] = []

    concat_mode_value = getattr(video_concat_mode, "value", video_concat_mode)
    if concat_mode_value == VideoConcatMode.random.value:
        random.shuffle(valid_video_items)

    total_duration = 0.0
    for item in valid_video_items:
        try:
            source_info = item.source_info if isinstance(item.source_info, dict) else {}
            logger.info(
                f"downloading {item.provider} video: "
                f"asset_id={source_info.get('asset_id') or 'unknown'}"
            )
            saved_video_path = save_video(
                video_url=item.url, save_dir=material_directory
            )
            if saved_video_path:
                logger.info(f"video saved: {saved_video_path}")
                video_paths.append(saved_video_path)
                material_records.append(_material_record(item, saved_video_path))
                seconds = min(max_clip_duration, item.duration)
                total_duration += seconds
                if total_duration > audio_duration:
                    logger.info(
                        f"total duration of downloaded videos: {total_duration} seconds, skip downloading more"
                    )
                    break
        except Exception as e:
            logger.error(
                f"failed to download {item.provider} video: "
                f"{type(e).__name__}: {_redact_request_error(e, item.url)}"
            )
    logger.success(f"downloaded {len(video_paths)} videos")
    _persist_material_records(task_id, material_records)
    return video_paths


def download_storyboard_videos(
    task_id: str,
    scenes,
    sources: List[str],
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[str]:
    """Select and download one traceable stock clip for every timed scene."""
    video_paths: list[str] = []
    material_records: list[dict] = []
    used_urls: set[str] = set()
    previous_selection: tuple[str, MaterialInfo, float] | None = None
    material_directory = config.app.get("material_directory", "").strip()
    if material_directory == "task":
        material_directory = utils.task_dir(task_id)
    elif material_directory and not os.path.isdir(material_directory):
        material_directory = ""

    for scene in scenes:
        locked_selection = scene.selected_clip or {}
        locked_path = locked_selection.get("local_path", "")
        if getattr(scene, "locked", False) and os.path.isfile(locked_path):
            video_paths.append(locked_path)
            source_info = locked_selection.get("source_info") or {}
            material_records.append(
                {
                    "provider": locked_selection.get("provider", "cached"),
                    "resource_id": source_info.get("resource_id"),
                    "public_page": source_info.get("public_page"),
                    "author": source_info.get("author"),
                    "resolution": source_info.get("resolution"),
                    "query": locked_selection.get("search_term") or scene.query,
                    "local_path": locked_path,
                }
            )
            locked_url = locked_selection.get("url")
            if locked_url:
                used_urls.add(locked_url)
            continue

        queries = [scene.query, *getattr(scene, "fallback_queries", [])]
        candidates: list[MaterialInfo] = []
        selected_query = scene.query
        for query in dict.fromkeys(value for value in queries if value):
            candidates = search_scene_candidates(
                scene={
                    "index": scene.index,
                    "query": query,
                    "required_objects": scene.required_objects,
                    "excluded_elements": scene.excluded_elements,
                },
                sources=sources,
                video_aspect=video_aspect,
                minimum_duration=max(1, math.ceil(scene.duration)),
                limit=4,
            )
            if candidates:
                selected_query = query
                break

        candidates = sorted(
            candidates,
            key=lambda item: (item.url in used_urls, -float(item.score)),
        )
        candidate = candidates[0] if candidates else None
        start_time = 0.0
        if candidate is None:
            if previous_selection is None:
                scene.warnings.append("no_material_found")
                logger.error(f"no material found for storyboard scene {scene.scene_id}")
                continue
            saved_path, candidate, start_time = previous_selection
            scene.warnings.extend(["no_material_found", "reused_previous_clip"])
        else:
            analyzed = []
            candidate_limit = min(
                3,
                max(1, int(config.app.get("actual_frame_candidates", 2) or 2)),
            )
            for frame_candidate in candidates[:candidate_limit]:
                try:
                    frame_path = save_video(
                        frame_candidate.url, save_dir=material_directory
                    )
                    if not frame_path:
                        continue
                    frame_start, frame_score = clip_ranker.find_best_video_window(
                        video_path=frame_path,
                        query=selected_query,
                        window_duration=scene.duration,
                        required_objects=scene.required_objects,
                        excluded_elements=scene.excluded_elements,
                    )
                    analyzed.append(
                        (
                            float(frame_score or frame_candidate.score),
                            frame_start,
                            frame_path,
                            frame_candidate,
                        )
                    )
                except Exception as exc:
                    logger.warning(
                        f"storyboard frame analysis failed: scene={scene.scene_id}, "
                        f"provider={frame_candidate.provider}, "
                        f"error={_redact_request_error(exc, frame_candidate.url)}"
                    )
            if analyzed:
                actual_score, start_time, saved_path, candidate = max(
                    analyzed, key=lambda item: item[0]
                )
                candidate.score = actual_score
            else:
                if previous_selection is None:
                    scene.warnings.append("download_failed")
                    continue
                saved_path, candidate, start_time = previous_selection
                scene.warnings.append("reused_previous_clip")

        candidate.scene_index = scene.index
        candidate.search_term = selected_query
        if candidate.url in used_urls:
            scene.warnings.append("repeated_clip")
        used_urls.add(candidate.url)
        scene.fidelity_score = float(candidate.score)
        scene.selected_clip = {
            "local_path": saved_path,
            "url": candidate.url,
            "provider": candidate.provider,
            "search_term": selected_query,
            "score": float(candidate.score),
            "start_time": round(float(start_time), 3),
            "duration": round(float(scene.duration), 3),
            "source_info": candidate.source_info or {},
        }
        video_paths.append(saved_path)
        material_records.append(_material_record(candidate, saved_path))
        previous_selection = (saved_path, candidate, start_time)

    _persist_material_records(task_id, material_records)
    return video_paths


def _download_videos_by_script_order(
    task_id: str,
    search_terms: List[str],
    search_videos,
    video_aspect: VideoAspect,
    audio_duration: float,
    max_clip_duration: int,
    material_directory: str,
) -> List[str]:
    """
    按脚本文案顺序下载素材。

    默认下载逻辑会把所有关键词的候选素材合并成一个大列表；如果第一个
    关键词返回很多结果，最终下载时可能一直消耗这个关键词的素材，后续
    脚本主题就排不上时间线。这里按关键词分组后轮询下载：
    第 1 轮取每个关键词的第 1 个候选，第 2 轮取每个关键词的第 2 个候选。
    这样在不重写视频合成引擎的前提下，尽量保证素材顺序贴近文案顺序。
    """
    logger.info("downloading videos with script-order material matching")
    candidate_groups = []
    valid_video_urls = set()
    found_duration = 0.0

    for search_term in search_terms:
        video_items = search_videos(
            search_term=search_term,
            minimum_duration=max_clip_duration,
            video_aspect=video_aspect,
        )
        logger.info(f"found {len(video_items)} videos for '{search_term}'")

        term_items = []
        for item in video_items:
            if item.url in valid_video_urls:
                continue
            term_items.append(item)
            valid_video_urls.add(item.url)
            found_duration += item.duration

        if term_items:
            candidate_groups.append((search_term, term_items))

    logger.info(
        f"found total ordered video candidates: {sum(len(items) for _, items in candidate_groups)}, "
        f"required duration: {audio_duration} seconds, found duration: {found_duration} seconds"
    )

    video_paths = []
    material_records: list[dict] = []
    total_duration = 0.0
    group_count = max(len(candidate_groups), 1)
    required_clip_count = max(1, math.ceil(audio_duration / max(max_clip_duration, 1)))
    clips_per_group, extra_clip_groups = divmod(required_clip_count, group_count)

    # Keep each search term together for its share of the narration. The old
    # round-robin order restarted the story every few clips on longer videos.
    for group_index, (search_term, term_items) in enumerate(candidate_groups):
        target_clip_count = clips_per_group + (
            1 if group_index < extra_clip_groups else 0
        )
        if target_clip_count <= 0:
            continue
        term_duration = 0.0
        downloaded_for_term = 0
        for item in term_items:
            if downloaded_for_term >= target_clip_count:
                break
            try:
                source_info = (
                    item.source_info if isinstance(item.source_info, dict) else {}
                )
                logger.info(
                    f"downloading ordered {item.provider} video for {search_term!r}: "
                    f"asset_id={source_info.get('asset_id') or 'unknown'}"
                )
                saved_video_path = save_video(
                    video_url=item.url, save_dir=material_directory
                )
                if saved_video_path:
                    logger.info(f"video saved: {saved_video_path}")
                    video_paths.append(saved_video_path)
                    material_records.append(_material_record(item, saved_video_path))
                    clip_duration = min(max_clip_duration, item.duration)
                    term_duration += clip_duration
                    total_duration += clip_duration
                    downloaded_for_term += 1
            except Exception as e:
                logger.error(
                    f"failed to download ordered {item.provider} video: "
                    f"{type(e).__name__}: {_redact_request_error(e, item.url)}"
                )

        logger.info(
            f"ordered scene '{search_term}' downloaded {downloaded_for_term} clips "
            f"covering {term_duration:.1f} seconds"
        )

    logger.success(f"downloaded {len(video_paths)} ordered videos")
    _persist_material_records(task_id, material_records)
    return video_paths


if __name__ == "__main__":
    download_videos(
        "test123", ["Money Exchange Medium"], audio_duration=100, source="pixabay"
    )
