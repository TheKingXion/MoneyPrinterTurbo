import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.config import config
from app.services import material


class TestMaterialTlsVerification(unittest.TestCase):
    def setUp(self):
        self.original_app_config = dict(config.app)
        self.original_proxy_config = dict(config.proxy)

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)
        config.proxy.clear()
        config.proxy.update(self.original_proxy_config)

    def test_search_pexels_uses_tls_verification_by_default(self):
        """
        默认路径必须开启 TLS 校验，避免素材 API key 和返回的素材 URL
        在公共网络或不可信代理环境中被中间人攻击截获或篡改。
        """
        config.app["pexels_api_keys"] = ["pexels-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(
            json=lambda: {
                "videos": [
                    {
                        "duration": 8,
                        "video_files": [
                            {
                                "width": 1080,
                                "height": 1920,
                                "link": "https://example.com/video.mp4",
                            }
                        ],
                    }
                ]
            }
        )

        with patch("app.services.material.requests.get", return_value=fake_response) as get:
            results = material.search_videos_pexels("cat", minimum_duration=1)

        self.assertEqual(len(results), 1)
        self.assertTrue(get.call_args.kwargs["verify"])

    def test_search_pixabay_allows_explicit_tls_disable_for_proxy(self):
        """
        少数企业代理会使用自签证书。该场景必须显式配置关闭 TLS 校验，
        不能再由代码硬编码默认关闭。
        """
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        config.app["tls_verify"] = False
        config.proxy.clear()

        fake_response = SimpleNamespace(
            json=lambda: {
                "hits": [
                    {
                        "duration": 8,
                        "videos": {
                            "large": {
                                "width": 1920,
                                "url": "https://example.com/video.mp4",
                                "thumbnail": "https://example.com/video.jpg",
                            }
                        },
                    }
                ]
            }
        )

        with patch("app.services.material.requests.get", return_value=fake_response) as get:
            results = material.search_videos_pixabay("cat", minimum_duration=1)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].thumbnail_url, "https://example.com/video.jpg")
        self.assertFalse(get.call_args.kwargs["verify"])

    def test_save_video_uses_tls_verification_by_default(self):
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(
            raise_for_status=lambda: None,
            iter_content=lambda chunk_size: iter([b"fake-video"]),
            close=lambda: None,
        )

        class FakeVideoFileClip:
            duration = 1
            fps = 24

            def __init__(self, path):
                self.path = path

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "app.services.material.requests.get", return_value=fake_response
            ) as get, patch("app.services.material.VideoFileClip", FakeVideoFileClip):
                video_path = material.save_video(
                    "https://example.com/video.mp4?token=abc", save_dir=temp_dir
                )

            self.assertTrue(os.path.exists(video_path))
            self.assertTrue(get.call_args.kwargs["verify"])
            self.assertTrue(get.call_args.kwargs["stream"])


class TestMaterialVideoDownloads(unittest.TestCase):
    class FakeVideoFileClip:
        duration = 1
        fps = 24

        def __init__(self, path):
            self.path = path

        def close(self):
            return None

    @staticmethod
    def _response(chunks=(b"video-data",), error=None):
        def raise_for_status():
            if error:
                raise error

        return SimpleNamespace(
            raise_for_status=raise_for_status,
            iter_content=lambda chunk_size: iter(chunks),
            close=lambda: None,
        )

    def test_save_video_cache_hit_avoids_network_and_validation(self):
        url = "https://example.com/cached.mp4?token=first"
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(
                temp_dir, f"vid-{material.utils.md5(url.split('?')[0])}.mp4"
            )
            with open(path, "wb") as cached_file:
                cached_file.write(b"cached")

            with patch.object(material.requests, "get") as get, patch.object(
                material, "VideoFileClip"
            ) as video_clip:
                result = material.save_video(url, save_dir=temp_dir)

            self.assertEqual(result, f"{temp_dir}/vid-{material.utils.md5(url.split('?')[0])}.mp4")
            get.assert_not_called()
            video_clip.assert_not_called()

    def test_save_video_atomically_publishes_streamed_download(self):
        url = "https://example.com/atomic.mp4?token=abc"
        with tempfile.TemporaryDirectory() as temp_dir:
            expected = f"{temp_dir}/vid-{material.utils.md5(url.split('?')[0])}.mp4"
            partial = f"{expected}.partial"

            def chunks():
                self.assertFalse(os.path.exists(expected))
                yield b"video-"
                self.assertTrue(os.path.exists(partial))
                self.assertFalse(os.path.exists(expected))
                yield b"data"

            response = self._response(chunks=chunks())
            with patch.object(material.requests, "get", return_value=response) as get, patch.object(
                material, "VideoFileClip", self.FakeVideoFileClip
            ), patch.object(material.os, "fsync", wraps=os.fsync) as fsync, patch.object(
                material.os, "replace", wraps=os.replace
            ) as replace:
                result = material.save_video(url, save_dir=temp_dir)

            self.assertEqual(result, expected)
            with open(expected, "rb") as video_file:
                self.assertEqual(video_file.read(), b"video-data")
            self.assertFalse(os.path.exists(partial))
            self.assertTrue(get.call_args.kwargs["stream"])
            fsync.assert_called_once()
            replace.assert_called_once_with(partial, expected)

    def test_save_video_http_failure_removes_partial(self):
        url = "https://example.com/failure.mp4"
        with tempfile.TemporaryDirectory() as temp_dir:
            expected = f"{temp_dir}/vid-{material.utils.md5(url)}.mp4"
            partial = f"{expected}.partial"
            with open(partial, "wb") as stale_partial:
                stale_partial.write(b"stale")

            response = self._response(error=requests.HTTPError("503"))
            with patch.object(material.requests, "get", return_value=response):
                with self.assertRaises(requests.HTTPError):
                    material.save_video(url, save_dir=temp_dir)

            self.assertFalse(os.path.exists(expected))
            self.assertFalse(os.path.exists(partial))

    def test_save_video_coalesces_concurrent_same_url(self):
        url = "https://example.com/concurrent.mp4?token=abc"
        download_started = threading.Event()
        allow_download = threading.Event()

        def chunks():
            download_started.set()
            self.assertTrue(allow_download.wait(timeout=5))
            yield b"video-data"

        response = self._response(chunks=chunks())
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            material.requests, "get", return_value=response
        ) as get, patch.object(material, "VideoFileClip", self.FakeVideoFileClip):
            results = []
            errors = []

            def save():
                try:
                    results.append(material.save_video(url, save_dir=temp_dir))
                except Exception as exc:
                    errors.append(exc)

            first = threading.Thread(target=save)
            second = threading.Thread(target=save)
            first.start()
            self.assertTrue(download_started.wait(timeout=5))
            second.start()
            allow_download.set()
            first.join(timeout=5)
            second.join(timeout=5)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0], results[1])
            self.assertEqual(get.call_count, 1)

    def test_download_videos_accepts_plain_string_concat_mode(self):
        """
        download_videos 可能被服务层或测试直接传入字符串模式，而不是
        VideoConcatMode 枚举。这里用空搜索词避免真实网络请求，只验证
        字符串 "random" 不会再因为访问 `.value` 抛 AttributeError。
        """
        result = material.download_videos(
            task_id="string-concat-mode",
            search_terms=[],
            video_concat_mode="random",
        )

        self.assertEqual(result, [])

    def test_download_videos_can_round_robin_terms_in_script_order(self):
        """
        开启按文案顺序匹配素材后，不能让第一个关键词的多个候选先把
        音频时长填满。这里模拟两个关键词各有多个候选，验证下载顺序是
        term1-第1个、term2-第1个、term1-第2个，贴近脚本叙事顺序。
        """
        search_results = {
            "opening city": [
                material.MaterialInfo(provider="pexels", url="https://v.example/a1.mp4", duration=3),
                material.MaterialInfo(provider="pexels", url="https://v.example/a2.mp4", duration=3),
            ],
            "middle office": [
                material.MaterialInfo(provider="pexels", url="https://v.example/b1.mp4", duration=3),
                material.MaterialInfo(provider="pexels", url="https://v.example/b2.mp4", duration=3),
            ],
        }
        downloaded_urls = []

        def fake_search(search_term, minimum_duration, video_aspect):
            return search_results[search_term]

        def fake_save_video(video_url, save_dir=""):
            downloaded_urls.append(video_url)
            return f"/tmp/{video_url.rsplit('/', 1)[-1]}"

        with (
            patch.dict(config.app, {"material_directory": ""}),
            patch.object(material, "search_videos_pexels", side_effect=fake_search),
            patch.object(material, "save_video", side_effect=fake_save_video),
        ):
            result = material.download_videos(
                task_id="ordered-materials",
                search_terms=["opening city", "middle office"],
                source="pexels",
                audio_duration=7,
                max_clip_duration=3,
                match_script_order=True,
            )

        self.assertEqual(
            downloaded_urls,
                [
                    "https://v.example/a1.mp4",
                    "https://v.example/a2.mp4",
                    "https://v.example/b1.mp4",
                ],
        )
        self.assertEqual(result, ["/tmp/a1.mp4", "/tmp/a2.mp4", "/tmp/b1.mp4"])


class TestCoverrProvider(unittest.TestCase):
    """
    Coverr 视频素材源(spec: 2026-06-09-coverr-video-provider-design.md)。
    全部用 unittest.mock 替换 requests，确保 CI 不依赖真实网络和真实 API key。
    """

    def setUp(self):
        self.original_app_config = dict(config.app)
        self.original_proxy_config = dict(config.proxy)

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)
        config.proxy.clear()
        config.proxy.update(self.original_proxy_config)

    # ---------------- Tests for search_videos_coverr ----------------

    def test_search_coverr_uses_mp4_download_url(self):
        """
        search_videos_coverr 应把每个 hit 转成 MaterialInfo，并把 urls.mp4_download
        直接作为 MaterialInfo.url。
        按 Coverr 官方文档 (api.coverr.co/docs/videos/#download-a-video),
        GET mp4_download 本身就被 Coverr 计入下载统计,无需额外 PATCH ping。
        同时验证 Authorization header 使用 Bearer scheme。
        """
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(
            json=lambda: {
                "page": 0,
                "pages": 50,
                "page_size": 20,
                "total": 1,
                "hits": [
                    {
                        "id": "S1YbPl1NfI",
                        "duration": 11.625,
                        "aspect_ratio": "16:9",
                        "urls": {
                            "mp4": "https://storage.coverr.co/videos/abc?token=xyz",
                            "mp4_preview": "https://storage.coverr.co/videos/abc/preview?token=xyz",
                            "mp4_download": "https://storage.coverr.co/videos/abc/download?token=xyz",
                        },
                    }
                ],
            }
        )

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ) as get:
            results = material.search_videos_coverr("nature", minimum_duration=5)

        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertEqual(item.provider, "coverr")
        self.assertEqual(item.duration, 11)
        # url 字段就是 mp4_download URL,不再做 coverr://id|url 编码
        self.assertEqual(
            item.url, "https://storage.coverr.co/videos/abc/download?token=xyz"
        )
        # Bearer auth + TLS verify on by default
        self.assertEqual(
            get.call_args.kwargs["headers"]["Authorization"], "Bearer coverr-key"
        )
        self.assertTrue(get.call_args.kwargs["verify"])

    def test_search_coverr_uses_tls_verification_by_default(self):
        """与 pexels/pixabay 一致:未显式配置时 TLS 校验默认开启。"""
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(json=lambda: {"hits": []})

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ) as get:
            material.search_videos_coverr("nature", minimum_duration=1)

        self.assertTrue(get.call_args.kwargs["verify"])

    def test_search_coverr_allows_explicit_tls_disable_for_proxy(self):
        """企业自签证书代理场景必须能显式关闭 TLS 校验。"""
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app["tls_verify"] = False
        config.proxy.clear()

        fake_response = SimpleNamespace(json=lambda: {"hits": []})

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ) as get:
            material.search_videos_coverr("nature", minimum_duration=1)

        self.assertFalse(get.call_args.kwargs["verify"])

    def test_search_coverr_filters_by_min_duration_and_accepts_string(self):
        """
        Coverr duration 字段在不同响应里可能是 number 或 string,
        两种格式都要接受;低于 minimum_duration 的应被过滤。
        """
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(
            json=lambda: {
                "hits": [
                    {
                        "id": "shortvid",
                        "duration": 3,  # below minimum
                        "urls": {"mp4_download": "https://example.com/a.mp4"},
                    },
                    {
                        "id": "stringdur",
                        "duration": "10.500000",  # string accepted
                        "urls": {"mp4_download": "https://example.com/b.mp4"},
                    },
                ]
            }
        )

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ):
            results = material.search_videos_coverr("x", minimum_duration=5)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].duration, 10)
        self.assertEqual(results[0].url, "https://example.com/b.mp4")

    def test_search_coverr_skips_invalid_items(self):
        """缺 id 或缺 urls.mp4_download 的条目应被跳过,不应抛异常。"""
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(
            json=lambda: {
                "hits": [
                    {  # missing urls.mp4_download
                        "id": "no-download",
                        "duration": 10,
                        "urls": {"mp4_preview": "https://example.com/preview.mp4"},
                    },
                    {  # missing id
                        "duration": 10,
                        "urls": {"mp4_download": "https://example.com/x.mp4"},
                    },
                    {  # valid baseline
                        "id": "good",
                        "duration": 10,
                        "urls": {"mp4_download": "https://example.com/good.mp4"},
                    },
                ]
            }
        )

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ):
            results = material.search_videos_coverr("x", minimum_duration=1)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/good.mp4")

    def test_search_coverr_returns_empty_on_failure(self):
        """
        响应结构异常 / 网络异常时,函数必须返回 [] 而不是抛异常,
        与 pexels/pixabay 行为保持一致。
        """
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        # Subtest A: malformed response (no "hits" key)
        with self.subTest("malformed response"):
            fake_response = SimpleNamespace(
                json=lambda: {"error": "rate limited"}
            )
            with patch(
                "app.services.material.requests.get", return_value=fake_response
            ):
                results = material.search_videos_coverr("x", minimum_duration=1)
            self.assertEqual(results, [])

        # Subtest B: network exception bubbles up from requests.get
        with self.subTest("network exception"):
            with patch(
                "app.services.material.requests.get",
                side_effect=requests.ConnectionError("boom"),
            ):
                results = material.search_videos_coverr("x", minimum_duration=1)
            self.assertEqual(results, [])

    # ---------------- Tests for download_videos coverr branch ----------------

    def test_download_videos_passes_mp4_download_url_to_save_video(self):
        """
        在 source="coverr" 时:
          1. dispatch 到 search_videos_coverr
          2. coverr item 走通用下载路径:save_video 收到的就是 mp4_download URL
             (不再有 coverr://id|url 编码,也不再调用 PATCH ping)
          3. 返回保存路径
        """
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.app.pop("material_directory", None)
        config.proxy.clear()

        fake_item = material.MaterialInfo()
        fake_item.provider = "coverr"
        fake_item.url = "https://storage.coverr.co/videos/abc/download?token=xyz"
        fake_item.duration = 10

        with patch(
            "app.services.material.search_videos_coverr",
            return_value=[fake_item],
        ) as search, patch(
            "app.services.material.save_video",
            return_value="/tmp/coverr-saved.mp4",
        ) as save:
            result = material.download_videos(
                task_id="t-coverr",
                search_terms=["nature"],
                source="coverr",
                audio_duration=5,
                max_clip_duration=5,
            )

        # 1. dispatch
        self.assertEqual(search.call_count, 1)

        # 2. save_video 收到的就是 mp4_download URL,原样传入
        save_url = save.call_args.kwargs.get("video_url") or save.call_args.args[0]
        self.assertEqual(
            save_url, "https://storage.coverr.co/videos/abc/download?token=xyz"
        )

        # 3. 返回值正确
        self.assertEqual(result, ["/tmp/coverr-saved.mp4"])


class TestReviewedMaterialWorkflow(unittest.TestCase):
    def _item(self, provider, index):
        return material.MaterialInfo(
            provider=provider,
            url=f"https://example.com/{provider}-{index}.mp4",
            duration=9,
            thumbnail_url=f"https://example.com/{provider}-{index}.jpg",
        )

    def test_pexels_is_primary_when_it_has_enough_relevant_candidates(self):
        pexels = [self._item("pexels", index) for index in range(4)]
        with patch.object(material, "search_videos_pexels", return_value=pexels):
            with patch.object(material, "search_videos_pixabay") as pixabay:
                with patch.object(
                    material.clip_ranker,
                    "rank_materials",
                    side_effect=lambda items, **kwargs: list(items)[: kwargs["limit"]],
                ):
                    result = material.search_scene_candidates(
                        {"index": 0, "query": "teenage boy smartphone"},
                        ["pexels", "pixabay"],
                        limit=4,
                    )
        self.assertEqual([item.provider for item in result], ["pexels"] * 4)
        pixabay.assert_not_called()

    def test_pixabay_is_used_only_as_fallback(self):
        pexels = [self._item("pexels", 0)]
        pixabay_items = [self._item("pixabay", index) for index in range(3)]
        with patch.object(material, "search_videos_pexels", return_value=pexels):
            with patch.object(
                material, "search_videos_pixabay", return_value=pixabay_items
            ) as pixabay:
                with patch.object(
                    material.clip_ranker,
                    "rank_materials",
                    side_effect=lambda items, **kwargs: list(items)[: kwargs["limit"]],
                ):
                    result = material.search_scene_candidates(
                        {"index": 0, "query": "teenage boy smartphone"},
                        ["pexels", "pixabay"],
                        limit=4,
                    )
        self.assertEqual([item.provider for item in result], ["pexels"] + ["pixabay"] * 3)
        pixabay.assert_called_once()

    def test_strict_search_retries_with_relaxed_query_when_empty(self):
        fallback_item = self._item("pexels", 1)
        with patch.object(
            material,
            "search_videos_pexels",
            side_effect=[[], [fallback_item]],
        ) as search:
            with patch.object(
                material.clip_ranker,
                "rank_materials",
                side_effect=lambda items, **kwargs: list(items)[: kwargs["limit"]],
            ):
                result = material.search_scene_candidates(
                    {
                        "index": 0,
                        "query": "young adult woman securing data smartphone warehouse",
                    },
                    ["pexels"],
                    limit=2,
                )

        self.assertEqual(result, [fallback_item])
        self.assertEqual(
            search.call_args_list[1].kwargs["search_term"],
            "person securing data smartphone",
        )

if __name__ == "__main__":
    unittest.main()
