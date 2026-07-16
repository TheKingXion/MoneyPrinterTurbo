import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
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

        with patch.object(performance, "_windows_registry_gpus", return_value=()), patch.object(
            performance, "_run", return_value=inventory
        ):
            gpus = performance._windows_gpus()

        self.assertEqual([gpu.vendor for gpu in gpus], ["amd", "intel", "unknown"])
        self.assertIsNone(gpus[0].vram_total)
        self.assertIsNone(gpus[1].vram_total)

    def test_windows_registry_uses_64_bit_vram_without_wmi(self):
        values = {
            "DriverDesc": "AMD Radeon RX 580 2048SP",
            "MatchingDeviceId": "PCI\\VEN_1002&DEV_6FDF",
            "DriverVersion": "31.0",
            "ProviderName": "Advanced Micro Devices, Inc.",
            "HardwareInformation.qwMemorySize": 8 * performance.GIB,
        }

        class FakeKey:
            def __init__(self, name):
                self.name = name

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        fake_winreg = types.SimpleNamespace(
            HKEY_LOCAL_MACHINE=object(),
            OpenKey=lambda parent, name: FakeKey(name),
            QueryInfoKey=lambda key: (1, 0, 0),
            EnumKey=lambda key, index: "0000",
            QueryValueEx=lambda key, name: (values[name], 0),
        )
        with patch.object(performance.os, "name", "nt"), patch.dict(
            sys.modules, {"winreg": fake_winreg}
        ):
            gpus = performance._windows_registry_gpus()

        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0].vram_total, 8 * performance.GIB)
        self.assertEqual(gpus[0].vendor, "amd")
        self.assertEqual(gpus[0].metrics_source, "Windows registry")

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

    def test_single_vendor_telemetry_merges_and_calculates_free_vram(self):
        inventory = performance.GPUInfo(
            "AMD Radeon RX 580", 8 * performance.GIB, None, "31.0", vendor="amd"
        )
        telemetry = performance.GPUInfo(
            "AMD adapter DEADBEEF", None, None, "", 52, "amd",
            utilization_percent=73, vram_used=2 * performance.GIB,
            metrics_source="AMD ADL",
        )

        merged = performance._merge_gpus((inventory,), (telemetry,))

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].temperature_c, 52)
        self.assertEqual(merged[0].utilization_percent, 73)
        self.assertEqual(merged[0].vram_free, 6 * performance.GIB)
        self.assertEqual(merged[0].metrics_source, "AMD ADL")

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

    def test_probe_is_representative_safe_and_inside_storage(self):
        ffmpeg = performance.FFmpegCapabilities(
            "ffmpeg", "v", frozenset({"h264_nvenc"}), frozenset()
        )
        seen = {}

        def fake_run(command, timeout=5.0):
            seen["command"] = command
            seen["timeout"] = timeout
            self.assertTrue(Path(command[-1]).is_relative_to(self.storage))
            Path(command[-1]).write_bytes(b"encoded")
            return completed()

        with patch.object(performance, "_run", side_effect=fake_run):
            self.assertTrue(performance.probe_encoder(ffmpeg, "h264_nvenc", self.storage))

        self.assertIn("testsrc2=size=1080x1920:rate=30:duration=1", seen["command"])
        self.assertIn("sine=frequency=1000:sample_rate=44100:duration=1", seen["command"])
        self.assertEqual(seen["command"][seen["command"].index("-preset") + 1], "p4")
        self.assertLessEqual(seen["timeout"], 30)
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
        self.assertEqual(profile.ffmpeg_threads, 12)

    def test_measured_profile_prefers_vendor_hardware_with_small_cpu_advantage(self):
        hardware = performance.HardwareInfo(
            10, 16, 32 * performance.GIB, 20 * performance.GIB,
            500 * performance.GIB, 100 * performance.GIB, "Windows",
            (performance.GPUInfo(
                "RTX 3050 Laptop", 4 * performance.GIB, None, "test", vendor="nvidia"
            ),),
        )
        benchmarks = {
            "h264_nvenc": performance.EncoderBenchmark(True, 1.0, 30.0),
            "libx264": performance.EncoderBenchmark(True, 0.9, 33.0),
        }

        profile = performance.derive_profile(
            hardware,
            performance.FFmpegCapabilities("ffmpeg", "v"),
            {"h264_nvenc": True, "libx264": True},
            encoder_benchmarks=benchmarks,
        )

        self.assertEqual(profile.h264_codec, "h264_nvenc")
        self.assertEqual(profile.render_slots, 1)

    def test_laptop_on_battery_reduces_render_parallelism_and_threads(self):
        hardware = performance.HardwareInfo(
            10, 16, 32 * performance.GIB, 24 * performance.GIB,
            500 * performance.GIB, 100 * performance.GIB, "Windows", (),
            is_laptop=True, power_plugged=False,
        )

        profile = performance.derive_profile(
            hardware,
            performance.FFmpegCapabilities("ffmpeg", "v"),
            {"libx264": True},
        )

        self.assertEqual(profile.render_slots, 1)
        self.assertLessEqual(profile.ffmpeg_threads, 5)

    def test_task_slots_scale_with_memory_and_power(self):
        desktop_eight = performance.HardwareInfo(
            8, 16, 8 * performance.GIB, 4 * performance.GIB,
            100 * performance.GIB, 50 * performance.GIB, "Windows",
        )
        laptop_thirty_two = replace(
            desktop_eight,
            ram_total=32 * performance.GIB,
            ram_available=24 * performance.GIB,
            is_laptop=True,
            power_plugged=True,
        )
        laptop_battery = replace(laptop_thirty_two, power_plugged=False)

        self.assertEqual(performance.recommended_task_slots(desktop_eight, 5), 2)
        self.assertEqual(performance.recommended_task_slots(laptop_thirty_two, 5), 5)
        self.assertEqual(performance.recommended_task_slots(laptop_battery, 5), 2)

    def test_adaptive_render_gate_blocks_second_render_under_memory_pressure(self):
        gate = performance.AdaptiveRenderGate(2, 2 * performance.GIB, 3 * performance.GIB)
        with patch.object(
            performance, "_memory", return_value=(8 * performance.GIB, performance.GIB)
        ):
            self.assertTrue(gate.acquire(blocking=False))
            self.assertFalse(gate.acquire(blocking=False))
            gate.release()

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

    def test_profile_does_not_select_encoder_for_absent_gpu_vendor(self):
        hardware = performance.HardwareInfo(
            8, 16, 16 * performance.GIB, 10 * performance.GIB,
            100 * performance.GIB, 50 * performance.GIB, "Windows",
            (performance.GPUInfo(
                "Radeon", 8 * performance.GIB, None, "test", vendor="amd"
            ),),
        )

        profile = performance.derive_profile(
            hardware,
            performance.FFmpegCapabilities("ffmpeg", "v"),
            {"h264_nvenc": True, "h264_amf": True, "libx264": True},
        )

        self.assertEqual(profile.h264_codec, "h264_amf")

    def test_probe_cache_reused_and_fingerprint_invalidates_it(self):
        hardware = performance.HardwareInfo(4, 8, 8 * performance.GIB, 6 * performance.GIB, 100 * performance.GIB, 50 * performance.GIB, "test")
        ffmpeg = performance.FFmpegCapabilities("missing-ffmpeg", "v1", frozenset({"libx264"}))
        with patch.object(performance, "detect_hardware", return_value=hardware), patch.object(
            performance, "inspect_ffmpeg", return_value=ffmpeg
        ), patch.object(
            performance,
            "benchmark_encoder",
            return_value=performance.EncoderBenchmark(True, 1.0, 30.0),
        ) as probe:
            first = performance.get_performance_profile(self.storage)
            second = performance.get_performance_profile(self.storage)
            changed = replace(ffmpeg, version="v2")
            with patch.object(performance, "inspect_ffmpeg", return_value=changed):
                performance.get_performance_profile(self.storage)

        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(probe.call_count, 2)
        cache = json.loads((self.storage / "performance_profile.json").read_text())
        self.assertEqual(cache["version"], 5)

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

    def test_locked_database_does_not_change_instrumented_result(self):
        called = []

        @self.telemetry.task_decorator("locked")
        def work():
            called.append(True)
            return "result"

        with closing(sqlite3.connect(self.db, timeout=0)) as lock:
            lock.execute("PRAGMA journal_mode=DELETE")
            lock.execute("BEGIN EXCLUSIVE")
            self.assertEqual(work(), "result")

        self.assertEqual(called, [True])

    def test_start_failure_does_not_prevent_callable(self):
        called = []

        @self.telemetry.task_decorator("start-failure")
        def work():
            called.append(True)
            return 42

        with patch.object(
            self.telemetry, "_start_run", side_effect=sqlite3.OperationalError("locked")
        ):
            self.assertEqual(work(), 42)

        self.assertEqual(called, [True])

    def test_finish_failure_preserves_original_exception(self):
        original = ValueError("callable failed")

        @self.telemetry.task_decorator("finish-failure")
        def work():
            raise original

        with patch.object(
            self.telemetry, "_finish_run", side_effect=OSError("disk unavailable")
        ):
            with self.assertRaises(ValueError) as raised:
                work()

        self.assertIs(raised.exception, original)

    def test_initialization_and_sampling_database_errors_are_safe(self):
        with patch.object(
            performance.PerformanceTelemetry,
            "_initialize",
            side_effect=sqlite3.OperationalError("locked"),
        ):
            telemetry = performance.PerformanceTelemetry(self.db)

        with patch.object(
            telemetry, "_connect", side_effect=OSError("disk unavailable")
        ):
            telemetry.sample_resources()

        with patch.object(
            performance, "_storage_dir", side_effect=OSError("storage unavailable")
        ), patch.object(performance.PerformanceTelemetry, "_initialize"):
            fallback = performance.PerformanceTelemetry()
        self.assertEqual(fallback.db_path, Path(performance.os.devnull))

    def test_initialization_prunes_completed_telemetry_older_than_thirty_days(self):
        old = time.time() - 31 * 24 * 60 * 60
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "INSERT INTO task_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("old", "old", old, old, 1.0, "ok", "{}", None),
            )
            connection.execute(
                "INSERT INTO resource_samples (task_id, sampled_at) VALUES (?, ?)",
                ("old", old),
            )

        performance.PerformanceTelemetry(self.db)

        with closing(sqlite3.connect(self.db)) as connection:
            tasks = connection.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]
            samples = connection.execute(
                "SELECT COUNT(*) FROM resource_samples"
            ).fetchone()[0]
        self.assertEqual((tasks, samples), (0, 0))

    def test_initialization_closes_stale_running_rows(self):
        started = time.time() - 60
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "INSERT INTO task_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("stale", "video", started, None, None, "running", "{}", None),
            )
            connection.execute(
                "INSERT INTO stage_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "stale-stage", "stale", "render", started, None, None,
                    "running", "{}", None,
                ),
            )

        performance.PerformanceTelemetry(self.db)

        with closing(sqlite3.connect(self.db)) as connection:
            task = connection.execute(
                "SELECT status, duration, error FROM task_runs WHERE id='stale'"
            ).fetchone()
            stage = connection.execute(
                "SELECT status, duration FROM stage_runs WHERE id='stale-stage'"
            ).fetchone()
        self.assertEqual(task, ("interrupted", 0.0, "process interrupted"))
        self.assertEqual(stage, ("interrupted", 0.0))


if __name__ == "__main__":
    unittest.main()
