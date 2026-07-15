import math
import os
import random
import threading
import time
import weakref
from contextlib import contextmanager
from typing import List
from urllib.parse import urlencode

import requests
from loguru import logger
from moviepy.video.io.VideoFileClip import VideoFileClip

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect, VideoConcatMode
from app.services import clip_ranker
from app.utils import utils

# Thread-safe counter for API key rotation
_api_key_counter = 0
_api_key_lock = threading.Lock()
_download_locks_guard = threading.Lock()
_download_locks = weakref.WeakValueDictionary()


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
        response = r.json()
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
                    video_items.append(item)
                    break
        return video_items
    except Exception as e:
        logger.error(f"search videos failed: {str(e)}")

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
            raw = search_functions[source_name](
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
                raw = search_functions[source_name](
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

    concat_mode_value = getattr(video_concat_mode, "value", video_concat_mode)
    if concat_mode_value == VideoConcatMode.random.value:
        random.shuffle(valid_video_items)

    total_duration = 0.0
    for item in valid_video_items:
        try:
            logger.info(f"downloading video: {item.url}")
            saved_video_path = save_video(
                video_url=item.url, save_dir=material_directory
            )
            if saved_video_path:
                logger.info(f"video saved: {saved_video_path}")
                video_paths.append(saved_video_path)
                seconds = min(max_clip_duration, item.duration)
                total_duration += seconds
                if total_duration > audio_duration:
                    logger.info(
                        f"total duration of downloaded videos: {total_duration} seconds, skip downloading more"
                    )
                    break
        except Exception as e:
            logger.error(f"failed to download video: {utils.to_json(item)} => {str(e)}")
    logger.success(f"downloaded {len(video_paths)} videos")
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
                logger.info(
                    f"downloading ordered video for '{search_term}': {item.url}"
                )
                saved_video_path = save_video(
                    video_url=item.url, save_dir=material_directory
                )
                if saved_video_path:
                    logger.info(f"video saved: {saved_video_path}")
                    video_paths.append(saved_video_path)
                    clip_duration = min(max_clip_duration, item.duration)
                    term_duration += clip_duration
                    total_duration += clip_duration
                    downloaded_for_term += 1
            except Exception as e:
                logger.error(
                    f"failed to download ordered video: {utils.to_json(item)} => {str(e)}"
                )

        logger.info(
            f"ordered scene '{search_term}' downloaded {downloaded_for_term} clips "
            f"covering {term_duration:.1f} seconds"
        )

    logger.success(f"downloaded {len(video_paths)} ordered videos")
    return video_paths


if __name__ == "__main__":
    download_videos(
        "test123", ["Money Exchange Medium"], audio_duration=100, source="pixabay"
    )
