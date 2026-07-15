import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from app.services import performance


def completed(stdout="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, "")


class PerformanceProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_detects_nvidia_and_system_resources(self):
        virtual_memory = Mock(total=16 * performance.GIB, available=10 * performance.GIB)
        fake_psutil = Mock()
        fake_psutil.cpu_count.return_value = 4
        fake_psutil.virtual_memory.return_value = virtual_memory
        nvidia = completed(
            "0, GPU-test, 00000000:01:00.0, RTX Test, 8192, 2048, 6144, 555.10, 52, 31\n"
        )
        with patch.object(performance, "_psutil", fake_psutil), patch.object(
            performance, "_windows_gpus", return_value=()
        ), patch.object(performance, "_cpu_temperature", return_value=(None, None)), patch.object(
            performance, "_run", return_value=nvidia
        ), patch.object(performance.os, "cpu_count", return_value=8), patch.object(
            performance.platform, "system", return_value="Windows"
        ):
            hardware = performance.detect_hardware(self.storage, force=True)

        self.assertEqual((hardware.cpu_physical, hardware.cpu_logical), (4, 8))
        self.assertEqual(hardware.ram_available, 10 * performance.GIB)
        self.assertEqual(hardware.gpus[0].vram_total, 8192 * 1024**2)
        self.assertEqual(hardware.gpus[0].temperature_c, 52)
        self.assertEqual(hardware.gpus[0].vendor, "nvidia")
        self.assertEqual(hardware.gpus[0].utilization_percent, 31)

    def test_windows_inventory_detects_amd_intel_and_unknown_adapters(self):
        inventory = completed(json.dumps([
            {
                "Name": "AMD Radeon RX 7800 XT",
                "PNPDeviceID": "PCI\\VEN_1002&DEV_747E",
                "DriverVersion": "32.1",
                "AdapterRAM": 4 * performance.GIB,
                "VideoProcessor": "AMD Radeon",
            },
            {
                "Name": "Intel(R) UHD Graphics",
                "PNPDeviceID": "PCI\\VEN_8086&DEV_4680",
                "DriverVersion": "31.0",
                "AdapterRAM": None,
                "VideoProcessor": "Intel UHD",
            },
            {
                "Name": "Virtual Display Adapter",
                "PNPDeviceID": "ROOT\\DISPLAY\\0000",
                "DriverVersion": "1.0",
                "AdapterRAM": 0,
                "VideoProcessor": "Virtual",
            },
        ]))

        with patch.object(performance, "_run", return_value=inventory):
            gpus = performance._windows_gpus()

        self.assertEqual([gpu.vendor for gpu in gpus], ["amd", "intel", "unknown"])
        self.assertIsNone(gpus[0].vram_total)
        self.assertIsNone(gpus[1].vram_total)

    def test_windows_inventory_merges_available_nvidia_telemetry(self):
        inventory = performance.GPUInfo(
            "NVIDIA GeForce RTX Test", None, None, "old", vendor="nvidia", device_id="PCI\\VEN_10DE"
        )
        telemetry = performance.GPUInfo(
            "NVIDIA GeForce RTX Test", 8 * performance.GIB, 6 * performance.GIB,
            "new", 55, "nvidia", "GPU-test", 20, 2 * performance.GIB, "nvidia-smi"
        )

        merged = performance._merge_gpus((inventory,), (telemetry,))

        self.assertEqual(merged[0].driver_version, "new")
        self.assertEqual(merged[0].temperature_c, 55)
        self.assertEqual(merged[0].metrics_source, "nvidia-smi")

    def test_linux_inventory_uses_device_name_and_merges_by_pci_bus(self):
        lspci = completed(
            '0000:01:00.0 "VGA compatible controller" "NVIDIA Corporation" '
            '"GA107M [GeForce RTX 3050 Mobile]" -r01 "ASUSTeK Computer Inc." "Device 1673"\n'
        )
        with patch.object(performance, "_run", return_value=lspci):
            inventory = performance._linux_gpus()
        telemetry = performance.GPUInfo(
            "NVIDIA GeForce RTX 3050 Laptop GPU", 4 * performance.GIB, None, "1",
            vendor="nvidia", pci_bus_id="00000000:01:00.0"
        )

        merged = performance._merge_gpus(inventory, (telemetry,))

        self.assertEqual(len(merged), 1)
        self.assertIn("GA107M", merged[0].name)
        self.assertEqual(merged[0].vram_total, 4 * performance.GIB)

    def test_macos_inventory_identifies_apple_without_fake_driver(self):
        profiler = completed(json.dumps({"SPDisplaysDataType": [{
            "_name": "Apple M3",
            "sppci_model": "Apple M3",
            "spdisplays_vendor": "sppci_vendor_Apple",
            "spdisplays_vendor-id": "0x106b",
        }]}))
        with patch.object(performance, "_run", return_value=profiler):
            gpus = performance._macos_gpus()

        self.assertEqual(gpus[0].vendor, "apple")
        self.assertEqual(gpus[0].driver_version, "")

    def test_cpu_temperature_prefers_package_sensor(self):
        sensor = lambda label, current: Mock(label=label, current=current)
        fake_psutil = Mock()
        fake_psutil.sensors_temperatures.return_value = {
            "coretemp": [sensor("Core 0", 48), sensor("Package id 0", 61)],
            "nvme": [sensor("Composite", 70)],
        }

        with patch.object(performance, "_psutil", fake_psutil):
            temperature, source = performance._cpu_temperature()

        self.assertEqual(temperature, 61)
        self.assertEqual(source, "psutil")

    def test_invalid_cpu_temperature_is_not_reported(self):
        fake_psutil = Mock()
        fake_psutil.sensors_temperatures.return_value = {
            "k10temp": [Mock(label="Tdie", current=999)]
        }
        with patch.object(performance, "_psutil", fake_psutil), patch.object(
            performance.platform, "system", return_value="Linux"
        ):
            self.assertEqual(performance._cpu_temperature(), (None, None))

    def test_inspects_ffmpeg_outputs(self):
        responses = [
            completed("ffmpeg version 7.1 test\n"),
            completed(
                " V..... h264_nvenc NVIDIA NVENC H.264 encoder\n"
                " V..... h264_amf AMD AMF H.264 encoder\n V..... libx264 x264\n"
            ),
            completed("Hardware acceleration methods:\ncuda\nqsv\n"),
        ]
        with patch.object(performance, "_run", side_effect=responses):
            capabilities = performance.inspect_ffmpeg("bundled-ffmpeg")

        self.assertEqual(capabilities.version, "ffmpeg version 7.1 test")
        self.assertEqual(capabilities.encoders, {"h264_nvenc", "h264_amf", "libx264"})
        self.assertEqual(capabilities.hwaccels, {"cuda", "qsv"})

    def test_probe_is_tiny_safe_and_inside_storage(self):
        ffmpeg = performance.FFmpegCapabilities(
            "ffmpeg", "v", frozenset({"h264_nvenc"}), frozenset()
        )
        seen = {}

        def fake_run(command, timeout=5.0):
            seen["command"] = command
            seen["timeout"] = timeout
            self.assertTrue(Path(command[-1]).is_relative_to(self.storage))
            return completed()

        with patch.object(performance, "_run", side_effect=fake_run):
            self.assertTrue(performance.probe_encoder(ffmpeg, "h264_nvenc", self.storage))

        self.assertIn("color=c=black:s=256x256:r=10:d=0.25", seen["command"])
        self.assertLessEqual(seen["timeout"], 8)
        self.assertNotIn("shell", seen["command"])

    def test_profile_prefers_successful_encoder_and_sets_disk_flags(self):
        hardware = performance.HardwareInfo(
            8, 16, 16 * performance.GIB, 9 * performance.GIB,
            100 * performance.GIB, 900 * 1024**2, "test",
        )
        ffmpeg = performance.FFmpegCapabilities("ffmpeg", "v")
        profile = performance.derive_profile(
            hardware,
            ffmpeg,
            {"h264_nvenc": False, "h264_qsv": True, "h264_mf": True, "libx264": True},
            "fingerprint",
        )
        self.assertEqual(profile.h264_codec, "h264_qsv")
        self.assertTrue(profile.disk_low)
        self.assertTrue(profile.disk_critical)
        self.assertGreaterEqual(profile.ffmpeg_threads, 1)

    def test_four_gb_nvidia_profile_limits_render_to_one_slot(self):
        hardware = performance.HardwareInfo(
            12,
            16,
            32 * performance.GIB,
            20 * performance.GIB,
            500 * performance.GIB,
            100 * performance.GIB,
            "Windows",
            (
                performance.GPUInfo(
                    "RTX 3050", 4 * performance.GIB, 3 * performance.GIB, "test", vendor="nvidia"
                ),
            ),
        )
        profile = performance.derive_profile(
            hardware,
            performance.FFmpegCapabilities("ffmpeg", "v"),
            {"h264_nvenc": True, "libx264": True},
        )

        self.assertEqual(profile.h264_codec, "h264_nvenc")
        self.assertEqual(profile.render_slots, 1)
        self.assertEqual(profile.ffmpeg_threads, 8)

    def test_profile_can_select_amd_amf_and_limit_slots_by_vram(self):
        hardware = performance.HardwareInfo(
            12, 24, 32 * performance.GIB, 20 * performance.GIB,
            500 * performance.GIB, 100 * performance.GIB, "Windows",
            (performance.GPUInfo(
                "Radeon RX", 6 * performance.GIB, None, "test", vendor="amd"
            ),),
        )

        profile = performance.derive_profile(
            hardware,
            performance.FFmpegCapabilities("ffmpeg", "v"),
            {"h264_nvenc": False, "h264_amf": True, "libx264": True},
        )

        self.assertEqual(profile.h264_codec, "h264_amf")
        self.assertEqual(profile.render_slots, 1)

    def test_probe_cache_reused_and_fingerprint_invalidates_it(self):
        hardware = performance.HardwareInfo(4, 8, 8 * performance.GIB, 6 * performance.GIB, 100 * performance.GIB, 50 * performance.GIB, "test")
        ffmpeg = performance.FFmpegCapabilities("missing-ffmpeg", "v1", frozenset({"libx264"}))
        with patch.object(performance, "detect_hardware", return_value=hardware), patch.object(
            performance, "inspect_ffmpeg", return_value=ffmpeg
        ), patch.object(performance, "probe_encoder", return_value=True) as probe:
            first = performance.get_performance_profile(self.storage)
            second = performance.get_performance_profile(self.storage)
            changed = replace(ffmpeg, version="v2")
            with patch.object(performance, "inspect_ffmpeg", return_value=changed):
                performance.get_performance_profile(self.storage)

        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(probe.call_count, 2)
        cache = json.loads((self.storage / "performance_profile.json").read_text())
        self.assertEqual(cache["version"], 3)

class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "performance.db"
        self.telemetry = performance.PerformanceTelemetry(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_task_stage_summary_and_error_redaction(self):
        with self.telemetry.task("video", request="safe"):
            with self.telemetry.stage("render", frames=2):
                pass
            with self.assertRaises(RuntimeError):
                with self.telemetry.stage("upload"):
                    raise RuntimeError("token=supersecret C:\\Users\\me\\private.txt")

        tasks = self.telemetry.recent_tasks()
        stages = self.telemetry.aggregate_stage_timings()
        self.assertEqual(tasks[0]["status"], "ok")
        self.assertEqual({row["name"] for row in stages}, {"render", "upload"})
        with closing(sqlite3.connect(self.db)) as connection:
            task_links = connection.execute("SELECT COUNT(DISTINCT task_id) FROM stage_runs").fetchone()[0]
            error = connection.execute("SELECT error FROM stage_runs WHERE status='failed'").fetchone()[0]
        self.assertEqual(task_links, 1)
        self.assertNotIn("supersecret", error)
        self.assertNotIn("Users", error)

    def test_task_decorator_records_failure(self):
        @self.telemetry.task_decorator("decorated")
        def fail():
            raise ValueError("bad")

        with self.assertRaises(ValueError):
            fail()
        self.assertEqual(self.telemetry.recent_tasks()[0]["status"], "failed")

    def test_task_can_mark_unsuccessful_result_as_failed(self):
        with self.telemetry.task("empty-result") as run:
            run.mark_failed("task returned no result")

        task = self.telemetry.recent_tasks()[0]
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["error"], "task returned no result")

    def test_sampler_is_bounded_and_resource_failures_are_safe(self):
        with patch.object(performance, "_psutil", None), patch.object(
            performance, "_run", side_effect=FileNotFoundError
        ):
            sampler = self.telemetry.sampler(interval=0.001, max_samples=2).start()
            sampler._thread.join(timeout=1)
            sampler.stop()
        with closing(sqlite3.connect(self.db)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM resource_samples").fetchone()[0]
        self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
