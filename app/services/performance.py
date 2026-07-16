"""Hardware profiling, safe FFmpeg capability probes, and local telemetry.

The module has no required third-party dependencies.  ``psutil`` is used when
installed, while all subprocesses use fixed argument lists, short timeouts,
and no shell.  Call :func:`get_performance_profile` during application startup
to configure the exported ``render_slot`` and ``network_slot`` semaphores.
"""

from __future__ import annotations

import contextvars
import csv
import dataclasses
import functools
import hashlib
import io
import json
import logging
import math
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import ContextDecorator, closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, ParamSpec, TypeVar

try:  # Optional by design.
    import psutil as _psutil
except ImportError:  # pragma: no cover - depends on the runtime environment
    _psutil = None


P = ParamSpec("P")
R = TypeVar("R")
GIB = 1024**3
_PROFILE_CACHE_VERSION = 5
_ENCODER_PREFERENCE = (
    "h264_nvenc", "h264_amf", "h264_qsv", "h264_mf", "h264_videotoolbox", "libx264"
)
_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|authorization|password|secret|token)\s*[=:]\s*[^\s,;]+"
)
_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|/)[^\s,;]+")
_TELEMETRY_RETENTION_SECONDS = 30 * 24 * 60 * 60
_TELEMETRY_WARNING_INTERVAL = 60.0
_telemetry_logger = logging.getLogger(__name__)
_telemetry_warning_lock = threading.Lock()
_telemetry_warning_times: dict[str, float] = {}


def _log_telemetry_error(operation: str, error: BaseException) -> None:
    """Report telemetry failures without making logging part of the failure path."""

    now = time.monotonic()
    with _telemetry_warning_lock:
        previous = _telemetry_warning_times.get(operation, 0.0)
        if now - previous < _TELEMETRY_WARNING_INTERVAL:
            return
        _telemetry_warning_times[operation] = now
    try:
        _telemetry_logger.warning(
            "Performance telemetry %s failed: %s",
            operation,
            redact_error(error),
        )
    except Exception:
        pass


def _storage_dir() -> str:
    # Lazy imports keep this service usable without the application's optional
    # logging/runtime dependency graph.
    from app.utils.utils import storage_dir

    return storage_dir()


def _ffmpeg_binary() -> str:
    from app.utils.utils import get_ffmpeg_binary

    return get_ffmpeg_binary()


@dataclass(frozen=True)
class GPUInfo:
    """A graphics adapter and any optional vendor telemetry available for it."""

    name: str
    vram_total: int | None
    vram_free: int | None
    driver_version: str
    temperature_c: float | None = None
    vendor: str = "unknown"
    device_id: str = ""
    utilization_percent: float | None = None
    vram_used: int | None = None
    metrics_source: str | None = None
    pci_bus_id: str = ""


@dataclass(frozen=True)
class HardwareInfo:
    """Host resources relevant to video generation."""

    cpu_physical: int
    cpu_logical: int
    ram_total: int
    ram_available: int
    disk_total: int
    disk_free: int
    platform: str
    gpus: tuple[GPUInfo, ...] = ()
    cpu_temperature_c: float | None = None
    cpu_temperature_source: str | None = None
    cpu_name: str = ""
    is_laptop: bool = False
    power_plugged: bool | None = None


@dataclass(frozen=True)
class FFmpegCapabilities:
    """Capabilities advertised by the resolved FFmpeg binary."""

    binary: str
    version: str
    encoders: frozenset[str] = field(default_factory=frozenset)
    hwaccels: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class EncoderBenchmark:
    """Result of a representative vertical-video encoder benchmark."""

    supported: bool
    elapsed_seconds: float | None = None
    encoded_fps: float | None = None
    error: str | None = None


@dataclass(frozen=True)
class PerformanceProfile:
    """Conservative concurrency and encoder settings for the current host."""

    fingerprint: str
    h264_codec: str
    ffmpeg_threads: int
    render_slots: int
    network_slots: int
    task_slots: int
    disk_low: bool
    disk_critical: bool
    encoder_probes: Mapping[str, bool] = field(default_factory=dict)
    encoder_benchmarks: Mapping[str, EncoderBenchmark] = field(default_factory=dict)
    ram_reserve_bytes: int = 0
    estimated_ram_per_render: int = 0
    selection_reason: str = ""


def _run(command: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        shell=False,
        stdin=subprocess.DEVNULL,
    )


def _memory() -> tuple[int, int]:
    if _psutil is not None:
        memory = _psutil.virtual_memory()
        return int(memory.total), int(memory.available)
    if hasattr(os, "sysconf"):
        try:
            page = int(os.sysconf("SC_PAGE_SIZE"))
            total = page * int(os.sysconf("SC_PHYS_PAGES"))
            available = page * int(os.sysconf("SC_AVPHYS_PAGES"))
            return total, available
        except (OSError, ValueError, TypeError):
            pass
    if os.name == "nt":
        try:
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("memory_load", ctypes.c_ulong),
                    ("total_physical", ctypes.c_ulonglong),
                    ("available_physical", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong),
                    ("available_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended_virtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.total_physical), int(status.available_physical)
        except (AttributeError, OSError):
            pass
    return 0, 0


def _battery_state() -> tuple[bool, bool | None]:
    """Return whether the host has a battery and whether external power is connected."""

    if _psutil is None:
        return False, None
    try:
        battery = _psutil.sensors_battery()
    except Exception:
        return False, None
    if battery is None:
        return False, None
    plugged = getattr(battery, "power_plugged", None)
    return True, bool(plugged) if plugged is not None else None


def _cpu_counts() -> tuple[int, int]:
    logical = max(1, os.cpu_count() or 1)
    physical = None
    if _psutil is not None:
        try:
            physical = _psutil.cpu_count(logical=False)
        except Exception:
            physical = None
    if not physical and platform.system() == "Linux":
        try:
            packages = set()
            physical_id = core_id = None
            with open("/proc/cpuinfo", encoding="utf-8") as cpuinfo:
                for line in cpuinfo:
                    if not line.strip():
                        if physical_id is not None and core_id is not None:
                            packages.add((physical_id, core_id))
                        physical_id = core_id = None
                    elif line.startswith("physical id"):
                        physical_id = line.partition(":")[2].strip()
                    elif line.startswith("core id"):
                        core_id = line.partition(":")[2].strip()
            physical = len(packages) or None
        except OSError:
            pass
    if not physical and platform.system() in {"Darwin", "Windows"}:
        command = (
            ["sysctl", "-n", "hw.physicalcpu"]
            if platform.system() == "Darwin"
            else [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "(Get-CimInstance Win32_Processor | Measure-Object NumberOfCores -Sum).Sum",
            ]
        )
        try:
            result = _run(command, timeout=3.0)
            if result.returncode == 0:
                physical = int(result.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return max(1, int(physical or logical)), logical


def _cpu_name() -> str:
    """Return the processor model without requiring WMI privileges."""

    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "ProcessorNameString")
                if str(value).strip():
                    return " ".join(str(value).split())
        except (ImportError, OSError):
            pass
    if platform.system() == "Linux":
        try:
            with open("/proc/cpuinfo", encoding="utf-8") as cpuinfo:
                for line in cpuinfo:
                    if line.casefold().startswith(("model name", "hardware")):
                        value = line.partition(":")[2].strip()
                        if value:
                            return " ".join(value.split())
        except OSError:
            pass
    return " ".join((platform.processor() or platform.machine() or "Unknown CPU").split())


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", "."))
    if not match:
        return None
    try:
        number = float(match.group())
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _valid_temperature(value: Any) -> float | None:
    temperature = _optional_float(value)
    return temperature if temperature is not None and -20 <= temperature <= 150 else None


def _json_output(result: subprocess.CompletedProcess[str]) -> Any:
    if result.returncode or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except (TypeError, ValueError):
        return None


def _gpu_vendor(*values: Any) -> str:
    text = " ".join(str(value or "") for value in values).casefold()
    if "ven_10de" in text or "nvidia" in text:
        return "nvidia"
    if "ven_1002" in text or "amd" in text or "radeon" in text or "advanced micro devices" in text:
        return "amd"
    if "ven_8086" in text or "intel" in text:
        return "intel"
    if "apple" in text:
        return "apple"
    return "unknown"


def _nvidia_gpus() -> tuple[GPUInfo, ...]:
    fields = (
        "index,uuid,pci.bus_id,name,memory.total,memory.used,memory.free,"
        "driver_version,temperature.gpu,utilization.gpu"
    )
    try:
        result = _run(
            [
                "nvidia-smi",
                f"--query-gpu={fields}",
                "--format=csv,noheader,nounits",
            ],
            timeout=3.0,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    if result.returncode:
        return ()
    gpus = []
    for parts in csv.reader(io.StringIO(result.stdout)):
        parts = [part.strip() for part in parts]
        if len(parts) != 10:
            continue
        total = _optional_float(parts[4])
        used = _optional_float(parts[5])
        free = _optional_float(parts[6])
        gpus.append(
            GPUInfo(
                name=parts[3],
                vram_total=int(total * 1024**2) if total is not None else None,
                vram_free=int(free * 1024**2) if free is not None else None,
                driver_version=parts[7],
                temperature_c=_valid_temperature(parts[8]),
                vendor="nvidia",
                device_id=parts[1] or parts[2] or parts[0],
                utilization_percent=_optional_float(parts[9]),
                vram_used=int(used * 1024**2) if used is not None else None,
                metrics_source="nvidia-smi",
                pci_bus_id=parts[2],
            )
        )
    return tuple(gpus)


def _windows_registry_gpus() -> tuple[GPUInfo, ...]:
    """Read display inventory and 64-bit VRAM values directly from the registry."""

    if os.name != "nt":
        return ()
    try:
        import winreg
    except ImportError:  # pragma: no cover - Windows standard library only
        return ()

    class_path = (
        "SYSTEM\\CurrentControlSet\\Control\\Class\\"
        "{4d36e968-e325-11ce-bfc1-08002be10318}"
    )
    adapters: list[GPUInfo] = []
    seen: set[tuple[str, str]] = set()
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, class_path)
    except OSError:
        return ()
    with root:
        subkey_count = winreg.QueryInfoKey(root)[0]
        for index in range(subkey_count):
            try:
                subkey_name = winreg.EnumKey(root, index)
                subkey = winreg.OpenKey(root, subkey_name)
            except OSError:
                continue
            with subkey:
                def value(name: str, default: Any = "") -> Any:
                    try:
                        return winreg.QueryValueEx(subkey, name)[0]
                    except OSError:
                        return default

                name = str(value("DriverDesc") or "").strip()
                if not name:
                    continue
                device_id = str(value("MatchingDeviceId") or "").strip()
                driver = str(value("DriverVersion") or "").strip()
                provider = str(value("ProviderName") or "").strip()
                vendor = _gpu_vendor(device_id, name, provider)
                total = value("HardwareInformation.qwMemorySize", None)
                if total is None:
                    total = value("HardwareInformation.MemorySize", None)
                try:
                    vram = int(total) if total is not None else None
                except (TypeError, ValueError, OverflowError):
                    vram = None
                if vram is not None and vram <= 0:
                    vram = None
                if vendor == "intel":
                    vram = None
                identity = (device_id.casefold(), name.casefold())
                if identity in seen:
                    continue
                seen.add(identity)
                adapters.append(
                    GPUInfo(
                        name=name,
                        vram_total=vram,
                        vram_free=None,
                        driver_version=driver,
                        vendor=vendor,
                        device_id=device_id or name,
                        metrics_source="Windows registry",
                    )
                )
    return tuple(adapters)


def _windows_wmi_gpus() -> tuple[GPUInfo, ...]:
    script = (
        "$g=@(Get-CimInstance Win32_VideoController | Select-Object Name,PNPDeviceID,"
        "DriverVersion,AdapterRAM,VideoProcessor); ConvertTo-Json -InputObject $g -Compress"
    )
    try:
        data = _json_output(_run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], timeout=5.0
        ))
    except (OSError, subprocess.SubprocessError):
        return ()
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return ()
    gpus = []
    for item in data:
        if not isinstance(item, dict) or not item.get("Name"):
            continue
        vram = _optional_float(item.get("AdapterRAM"))
        # Win32_VideoController exposes a 32-bit field and commonly truncates
        # dedicated memory at roughly 4 GiB. Do not present that as authoritative.
        if vram is not None and (vram <= 0 or vram >= 4 * GIB - 16 * 1024**2):
            vram = None
        vendor = _gpu_vendor(item.get("PNPDeviceID"), item.get("Name"), item.get("VideoProcessor"))
        if vendor == "intel":
            vram = None  # Integrated Intel GPUs use shared system memory.
        gpus.append(GPUInfo(
            name=str(item["Name"]),
            vram_total=max(0, int(vram)) if vram is not None else None,
            vram_free=None,
            driver_version=str(item.get("DriverVersion") or ""),
            vendor=vendor,
            device_id=str(item.get("PNPDeviceID") or item.get("Name")),
        ))
    return tuple(gpus)


def _windows_gpus() -> tuple[GPUInfo, ...]:
    """Combine privilege-free registry inventory with optional WMI details."""

    registry = _windows_registry_gpus()
    # The display class already contains model, driver, device id, and 64-bit
    # dedicated VRAM. Avoid a slow/permission-sensitive WMI process when those
    # authoritative values are available.
    return registry or _windows_wmi_gpus()


def _amd_adl_gpus() -> tuple[GPUInfo, ...]:
    """Read AMD temperature, load and VRAM usage through the installed ADL driver."""

    if os.name != "nt":
        return ()
    try:
        import ctypes
        from ctypes import POINTER, byref, c_int, c_void_p

        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        library = ctypes.WinDLL(str(Path(system_root) / "System32" / "atiadlxx.dll"))
    except (AttributeError, ImportError, OSError):
        return ()

    class ADLPMActivity(ctypes.Structure):
        _fields_ = [(name, c_int) for name in (
            "iSize", "iEngineClock", "iMemoryClock", "iVddc",
            "iActivityPercent", "iCurrentPerformanceLevel", "iCurrentBusSpeed",
            "iCurrentBusLanes", "iMaximumBusLanes", "iReserved",
        )]

    allocations: list[Any] = []

    @ctypes.CFUNCTYPE(c_void_p, c_int)
    def allocate(size: int) -> int:
        buffer = ctypes.create_string_buffer(size)
        allocations.append(buffer)
        return ctypes.addressof(buffer)

    context = c_void_p()
    destroy = None
    try:
        create = library.ADL2_Main_Control_Create
        create.argtypes = [type(allocate), c_int, POINTER(c_void_p)]
        create.restype = c_int
        if create(allocate, 1, byref(context)) != 0 or not context:
            return ()
        destroy = library.ADL2_Main_Control_Destroy
        destroy.argtypes = [c_void_p]

        count = c_int()
        number = library.ADL2_Adapter_NumberOfAdapters_Get
        number.argtypes = [c_void_p, POINTER(c_int)]
        if number(context, byref(count)) != 0:
            return ()

        adapter_id_get = library.ADL2_Adapter_ID_Get
        adapter_id_get.argtypes = [c_void_p, c_int, POINTER(c_int)]
        temperature_get = getattr(library, "ADL2_OverdriveN_Temperature_Get", None)
        if temperature_get is not None:
            temperature_get.argtypes = [c_void_p, c_int, c_int, POINTER(c_int)]
        activity_get = getattr(library, "ADL2_Overdrive5_CurrentActivity_Get", None)
        if activity_get is not None:
            activity_get.argtypes = [c_void_p, c_int, POINTER(ADLPMActivity)]
        memory_get = getattr(library, "ADL2_Adapter_DedicatedVRAMUsage_Get", None)
        if memory_get is not None:
            memory_get.argtypes = [c_void_p, c_int, POINTER(c_int)]

        adapters = []
        seen: set[int] = set()
        for index in range(max(0, count.value)):
            raw_id = c_int()
            if adapter_id_get(context, index, byref(raw_id)) != 0:
                continue
            adapter_id = raw_id.value & 0xFFFFFFFF
            if not adapter_id or adapter_id in seen:
                continue
            seen.add(adapter_id)

            temperature = None
            if temperature_get is not None:
                raw_temperature = c_int()
                for temperature_type in (1, 0, 2):
                    if temperature_get(
                        context, index, temperature_type, byref(raw_temperature)
                    ) == 0:
                        temperature = _valid_temperature(raw_temperature.value / 1000)
                        if temperature is not None:
                            break

            utilization = None
            if activity_get is not None:
                activity = ADLPMActivity()
                activity.iSize = ctypes.sizeof(ADLPMActivity)
                if activity_get(context, index, byref(activity)) == 0:
                    if 0 <= activity.iActivityPercent <= 100:
                        utilization = float(activity.iActivityPercent)

            used = None
            if memory_get is not None:
                used_mb = c_int()
                if memory_get(context, index, byref(used_mb)) == 0 and used_mb.value >= 0:
                    used = used_mb.value * 1024**2

            adapters.append(
                GPUInfo(
                    name=f"AMD adapter {adapter_id:08X}",
                    vram_total=None,
                    vram_free=None,
                    driver_version="",
                    temperature_c=temperature,
                    vendor="amd",
                    device_id=f"ADL-{adapter_id:08X}",
                    utilization_percent=utilization,
                    vram_used=used,
                    metrics_source="AMD ADL",
                )
            )
        return tuple(adapters)
    except Exception:
        return ()
    finally:
        if destroy is not None and context:
            try:
                destroy(context)
            except Exception:
                pass


def _linux_gpus() -> tuple[GPUInfo, ...]:
    try:
        result = _run(["lspci", "-D", "-mm"], timeout=3.0)
    except (OSError, subprocess.SubprocessError):
        return ()
    if result.returncode:
        return ()
    gpus = []
    for line in result.stdout.splitlines():
        try:
            fields = next(csv.reader([line], delimiter=" ", skipinitialspace=True))
        except (csv.Error, StopIteration):
            continue
        if len(fields) < 3 or not any(kind in fields[1].casefold() for kind in ("vga", "3d", "display")):
            continue
        name = " ".join(fields[2:4]).strip()
        gpus.append(GPUInfo(
            name, None, None, "", vendor=_gpu_vendor(name), device_id=fields[0], pci_bus_id=fields[0]
        ))
    return tuple(gpus)


def _macos_gpus() -> tuple[GPUInfo, ...]:
    try:
        data = _json_output(_run(["system_profiler", "SPDisplaysDataType", "-json"], timeout=8.0))
    except (OSError, subprocess.SubprocessError):
        return ()
    adapters = data.get("SPDisplaysDataType", []) if isinstance(data, dict) else []
    return tuple(
        GPUInfo(
            str(item.get("sppci_model") or item.get("_name")), None, None,
            "",
            vendor=_gpu_vendor(item.get("spdisplays_vendor"), item.get("sppci_model")),
            device_id=str(item.get("spdisplays_device-id") or item.get("_name") or ""),
        )
        for item in adapters
        if isinstance(item, dict) and (item.get("sppci_model") or item.get("_name"))
    )


def _merge_gpus(inventory: tuple[GPUInfo, ...], telemetry: tuple[GPUInfo, ...]) -> tuple[GPUInfo, ...]:
    if not inventory:
        return telemetry
    remaining = list(telemetry)
    merged = []
    for inventory_index, gpu in enumerate(inventory):
        gpu_bus = gpu.pci_bus_id.casefold().lstrip("0:")
        match = next((
            candidate for candidate in remaining
            if (
                gpu_bus and candidate.pci_bus_id.casefold().lstrip("0:") == gpu_bus
            ) or candidate.name.casefold() == gpu.name.casefold()
        ), None)
        if match is None:
            same_vendor = [candidate for candidate in remaining if candidate.vendor == gpu.vendor]
            unmatched_inventory = [
                candidate for candidate in inventory[inventory_index:]
                if candidate.vendor == gpu.vendor
            ]
            if same_vendor and len(same_vendor) == len(unmatched_inventory):
                match = same_vendor[0]
        if match is None:
            merged.append(gpu)
            continue
        remaining.remove(match)
        total = match.vram_total or gpu.vram_total
        used = match.vram_used
        free = match.vram_free
        if free is None and total is not None and used is not None:
            free = max(0, total - used)
        merged.append(dataclasses.replace(
            gpu,
            vram_total=total,
            vram_free=free,
            driver_version=match.driver_version or gpu.driver_version,
            temperature_c=match.temperature_c,
            utilization_percent=match.utilization_percent,
            vram_used=used,
            metrics_source=match.metrics_source or gpu.metrics_source,
            pci_bus_id=match.pci_bus_id or gpu.pci_bus_id,
        ))
    return tuple(merged + remaining)


_hardware_probe_lock = threading.Lock()
_hardware_probe_cache: dict[str, tuple[float, tuple[GPUInfo, ...], float | None, str | None]] = {}


def _probe_hardware_devices(system: str, *, force: bool = False) -> tuple[tuple[GPUInfo, ...], float | None, str | None]:
    with _hardware_probe_lock:
        cached = _hardware_probe_cache.get(system)
        if cached and not force and time.monotonic() - cached[0] < 10.0:
            return cached[1], cached[2], cached[3]
        inventory = (
            _windows_gpus() if system == "Windows" else
            _linux_gpus() if system == "Linux" else
            _macos_gpus() if system == "Darwin" else ()
        )
        telemetry = _nvidia_gpus()
        if system == "Windows":
            telemetry += _amd_adl_gpus()
        gpus = _merge_gpus(inventory, telemetry)
        cpu_temperature, temperature_source = _cpu_temperature()
        _hardware_probe_cache[system] = (
            time.monotonic(), gpus, cpu_temperature, temperature_source
        )
        return gpus, cpu_temperature, temperature_source


def _cpu_temperature() -> tuple[float | None, str | None]:
    if _psutil is not None and hasattr(_psutil, "sensors_temperatures"):
        try:
            sensors = _psutil.sensors_temperatures(fahrenheit=False) or {}
            preferred = []
            fallback = []
            for chip, entries in sensors.items():
                if chip.casefold() not in {"coretemp", "k10temp", "zenpower", "cpu_thermal"}:
                    continue
                for entry in entries:
                    temperature = _valid_temperature(getattr(entry, "current", None))
                    if temperature is None:
                        continue
                    label = str(getattr(entry, "label", "") or "").casefold()
                    target = preferred if any(name in label for name in ("package", "physical", "tdie", "tctl")) else fallback
                    target.append(temperature)
            values = preferred or fallback
            if values:
                return max(values), "psutil"
        except (AttributeError, OSError, RuntimeError, TypeError):
            pass
    if platform.system() != "Windows":
        return None, None
    if _psutil is not None:
        try:
            monitor_names = {"librehardwaremonitor.exe", "openhardwaremonitor.exe"}
            running = any(
                str(process.info.get("name") or "").casefold() in monitor_names
                for process in _psutil.process_iter(["name"])
            )
            if not running:
                return None, None
        except (AttributeError, OSError, RuntimeError, TypeError):
            pass
    script = (
        "$n=@('root/LibreHardwareMonitor','root/OpenHardwareMonitor');$r=@();"
        "foreach($x in $n){try{$r+=@(Get-CimInstance -Namespace $x -ClassName Sensor -ErrorAction Stop|"
        "Where-Object {$_.SensorType -eq 'Temperature' -and ($_.Identifier -match 'cpu' -or $_.Parent -match 'cpu')}|"
        "Select-Object Name,Value,@{n='Source';e={$x}})}catch{}};ConvertTo-Json -InputObject $r -Compress"
    )
    try:
        data = _json_output(_run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], timeout=5.0
        ))
    except (OSError, subprocess.SubprocessError):
        return None, None
    if isinstance(data, dict):
        data = [data]
    readings = []
    for item in data if isinstance(data, list) else []:
        temperature = _valid_temperature(item.get("Value")) if isinstance(item, dict) else None
        if temperature is not None:
            name = str(item.get("Name") or "").casefold()
            priority = 0 if any(key in name for key in ("package", "tdie", "tctl")) else 1
            readings.append((priority, temperature, str(item.get("Source") or "hardware monitor")))
    if not readings:
        return None, None
    best_priority = min(item[0] for item in readings)
    candidates = [item for item in readings if item[0] == best_priority]
    _, temperature, source = max(candidates, key=lambda item: item[1])
    return temperature, source


def detect_hardware(
    storage_path: str | os.PathLike[str] | None = None, *, force: bool = False
) -> HardwareInfo:
    """Detect host resources without requiring vendor-specific GPU utilities."""

    base = Path(storage_path or _storage_dir()).resolve()
    try:
        disk = shutil.disk_usage(base)
    except OSError:
        disk = shutil.disk_usage(base.anchor or os.curdir)
    physical, logical = _cpu_counts()
    ram_total, ram_available = _memory()
    is_laptop, power_plugged = _battery_state()
    system = platform.system()
    gpus, cpu_temperature, temperature_source = _probe_hardware_devices(system, force=force)
    return HardwareInfo(
        cpu_physical=physical,
        cpu_logical=logical,
        ram_total=ram_total,
        ram_available=ram_available,
        disk_total=int(disk.total),
        disk_free=int(disk.free),
        platform=f"{system}-{platform.machine()}",
        gpus=gpus,
        cpu_temperature_c=cpu_temperature,
        cpu_temperature_source=temperature_source,
        cpu_name=_cpu_name(),
        is_laptop=is_laptop,
        power_plugged=power_plugged,
    )


def inspect_ffmpeg(binary: str | None = None) -> FFmpegCapabilities:
    """Resolve FFmpeg and inspect its version, H.264 encoders, and hwaccels."""

    binary = binary or _ffmpeg_binary()
    version = "unavailable"
    encoders: set[str] = set()
    hwaccels: set[str] = set()
    try:
        result = _run([binary, "-hide_banner", "-version"])
        if result.returncode == 0 and result.stdout:
            version = result.stdout.splitlines()[0].strip()
        result = _run([binary, "-hide_banner", "-encoders"])
        if result.returncode == 0:
            for name in _ENCODER_PREFERENCE:
                if re.search(rf"(?m)^\s*[A-Z.]+\s+{re.escape(name)}(?:\s|$)", result.stdout):
                    encoders.add(name)
        result = _run([binary, "-hide_banner", "-hwaccels"])
        if result.returncode == 0:
            known = {"cuda", "qsv", "d3d11va", "dxva2", "vulkan", "vaapi", "videotoolbox"}
            hwaccels.update(line.strip() for line in result.stdout.splitlines() if line.strip() in known)
    except (OSError, subprocess.SubprocessError):
        pass
    return FFmpegCapabilities(binary, version, frozenset(encoders), frozenset(hwaccels))


def hardware_fingerprint(hardware: HardwareInfo, ffmpeg: FFmpegCapabilities) -> str:
    """Return a stable digest that changes with hardware, driver, or FFmpeg."""

    binary_identity: dict[str, Any] = {"path": os.path.realpath(ffmpeg.binary), "version": ffmpeg.version}
    try:
        stat = os.stat(ffmpeg.binary)
        binary_identity.update(size=stat.st_size, mtime_ns=stat.st_mtime_ns)
    except OSError:
        pass
    stable = {
        "cpu": [hardware.cpu_name, hardware.cpu_physical, hardware.cpu_logical],
        "ram_total": hardware.ram_total,
        "platform": hardware.platform,
        "gpus": [
            [gpu.name, gpu.vram_total, gpu.driver_version] for gpu in hardware.gpus
        ],
        "ffmpeg": binary_identity,
        "encoders": sorted(ffmpeg.encoders),
        "hwaccels": sorted(ffmpeg.hwaccels),
    }
    raw = json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def encoder_preset(codec: str) -> str | None:
    """Return a fast, broadly supported MoviePy/FFmpeg preset for a codec."""

    return {
        "h264_amf": "speed",
        "h264_nvenc": "p4",
        "h264_qsv": "veryfast",
        "libx264": "veryfast",
    }.get(codec)


def _encoder_probe_command(
    ffmpeg: FFmpegCapabilities,
    codec: str,
    output: str,
    *,
    duration: float = 1.0,
) -> list[str]:
    """Build a vertical-video probe matching the application's output path."""

    command = [
        ffmpeg.binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=1080x1920:rate=30:duration={duration:g}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=1000:sample_rate=44100:duration={duration:g}",
        "-shortest",
        "-c:v",
        codec,
    ]
    preset = encoder_preset(codec)
    if preset:
        command.extend(["-preset", preset])
    command.extend(["-pix_fmt", "yuv420p", "-c:a", "aac", "-y", output])
    return command


def benchmark_encoder(
    ffmpeg: FFmpegCapabilities,
    codec: str,
    storage_path: str | os.PathLike[str],
    *,
    duration: float = 1.0,
) -> EncoderBenchmark:
    """Measure a representative 1080x1920 H.264 encode with audio."""

    if codec not in ffmpeg.encoders or codec not in _ENCODER_PREFERENCE:
        return EncoderBenchmark(False, error="encoder is not advertised by FFmpeg")
    storage = Path(storage_path).resolve()
    storage.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory(prefix="ffmpeg-probe-", dir=storage) as temp_dir:
            output = str(Path(temp_dir) / "probe.mp4")
            result = _run(
                _encoder_probe_command(
                    ffmpeg, codec, output, duration=max(0.5, float(duration))
                ),
                timeout=30.0,
            )
            elapsed = max(0.001, time.perf_counter() - started)
            valid_output = result.returncode == 0 and Path(output).stat().st_size > 0
            if not valid_output:
                error = (result.stderr or result.stdout or "encoder probe failed").strip()
                return EncoderBenchmark(False, elapsed, None, redact_error(error))
            return EncoderBenchmark(
                True,
                elapsed_seconds=elapsed,
                encoded_fps=(max(0.5, float(duration)) * 30.0) / elapsed,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        return EncoderBenchmark(False, error=redact_error(exc))


def probe_encoder(
    ffmpeg: FFmpegCapabilities,
    codec: str,
    storage_path: str | os.PathLike[str],
) -> bool:
    """Return whether a representative vertical-video benchmark succeeds."""

    return benchmark_encoder(ffmpeg, codec, storage_path).supported


def derive_profile(
    hardware: HardwareInfo,
    ffmpeg: FFmpegCapabilities,
    encoder_probes: Mapping[str, bool],
    fingerprint: str | None = None,
    encoder_benchmarks: Mapping[str, EncoderBenchmark] | None = None,
) -> PerformanceProfile:
    """Derive measured encoder, CPU and concurrency settings for this host."""

    codec_vendors = {
        "h264_nvenc": "nvidia",
        "h264_amf": "amd",
        "h264_qsv": "intel",
        "h264_videotoolbox": "apple",
    }
    detected_vendors = {gpu.vendor for gpu in hardware.gpus}
    benchmarks = dict(encoder_benchmarks or {})
    candidates = [
        name
        for name in _ENCODER_PREFERENCE
        if encoder_probes.get(name)
        and (
            name not in codec_vendors
            or not detected_vendors
            or codec_vendors[name] in detected_vendors
        )
    ]
    measured = [
        name
        for name in candidates
        if benchmarks.get(name) and benchmarks[name].supported
        and benchmarks[name].encoded_fps is not None
    ]
    native_measured = [
        name
        for name in measured
        if name in codec_vendors and codec_vendors[name] in detected_vendors
    ]
    if native_measured:
        codec = max(
            native_measured,
            key=lambda name: float(benchmarks[name].encoded_fps or 0),
        )
        selection_reason = "fastest stable native GPU encoder"
    elif measured:
        codec = max(
            measured,
            key=lambda name: float(benchmarks[name].encoded_fps or 0),
        )
        selection_reason = "fastest representative benchmark"
    else:
        codec = candidates[0] if candidates else "libx264"
        selection_reason = "first stable vendor-compatible encoder"
    available_gib = hardware.ram_available / GIB if hardware.ram_available else 2.0
    total_gib = hardware.ram_total / GIB if hardware.ram_total else available_gib
    ram_reserve = int(max(1.5, total_gib * 0.15) * GIB)
    estimated_ram_per_render = int((2.5 if total_gib <= 16 else 3.0) * GIB)
    usable_memory = max(0, hardware.ram_available - ram_reserve)
    cpu_limit = max(1, hardware.cpu_physical // 2)
    memory_limit = max(1, usable_memory // max(1, estimated_ram_per_render))
    render_slots = min(4, cpu_limit, memory_limit)
    codec_vendor = codec_vendors.get(codec)
    matching_vram = [
        gpu.vram_total for gpu in hardware.gpus
        if gpu.vendor == codec_vendor and gpu.vram_total
    ]
    if matching_vram:
        total_vram = sum(matching_vram)
        gpu_limit = 1 if total_vram <= 6 * GIB else 2 if total_vram <= 12 * GIB else 3
        render_slots = min(render_slots, gpu_limit)
    if hardware.is_laptop and hardware.power_plugged is False:
        render_slots = 1
    disk_low_threshold = max(5 * GIB, int(hardware.disk_total * 0.05))
    disk_critical_threshold = max(GIB, int(hardware.disk_total * 0.02))
    threads_per_render = max(1, hardware.cpu_logical // max(1, render_slots))
    thread_cap = 16 if codec == "libx264" else 12
    ffmpeg_threads = max(1, min(thread_cap, hardware.cpu_physical, threads_per_render))
    if hardware.is_laptop and hardware.power_plugged is False:
        ffmpeg_threads = max(2, min(ffmpeg_threads, max(2, hardware.cpu_physical // 2)))
    return PerformanceProfile(
        fingerprint=fingerprint or hardware_fingerprint(hardware, ffmpeg),
        h264_codec=codec,
        ffmpeg_threads=ffmpeg_threads,
        render_slots=max(1, render_slots),
        network_slots=max(2, min(4, hardware.cpu_logical)),
        task_slots=recommended_task_slots(hardware, 5),
        disk_low=hardware.disk_free < disk_low_threshold,
        disk_critical=hardware.disk_free < disk_critical_threshold,
        encoder_probes=dict(encoder_probes),
        encoder_benchmarks=benchmarks,
        ram_reserve_bytes=ram_reserve,
        estimated_ram_per_render=estimated_ram_per_render,
        selection_reason=selection_reason,
    )


def recommended_task_slots(hardware: HardwareInfo, configured_limit: int) -> int:
    """Limit whole pipelines by RAM while allowing network/render overlap."""

    configured = max(1, int(configured_limit or 1))
    total_gib = hardware.ram_total / GIB if hardware.ram_total else 4.0
    memory_slots = 1 if total_gib < 6 else 2 if total_gib < 12 else 3 if total_gib < 24 else 5
    if hardware.is_laptop and hardware.power_plugged is False:
        memory_slots = min(memory_slots, 2)
    return max(1, min(configured, memory_slots))


class AdaptiveRenderGate:
    """Semaphore that also prevents new renders during live resource pressure."""

    def __init__(
        self,
        capacity: int,
        ram_reserve_bytes: int,
        estimated_ram_per_render: int,
    ) -> None:
        self.capacity = max(1, int(capacity))
        self.ram_reserve_bytes = max(512 * 1024**2, int(ram_reserve_bytes))
        self.estimated_ram_per_render = max(GIB, int(estimated_ram_per_render))
        self._semaphore = threading.BoundedSemaphore(self.capacity)
        self._lock = threading.Lock()
        self._active = 0

    def _resources_allow_start(self) -> bool:
        _, available = _memory()
        with self._lock:
            active = self._active
        if available and available < 512 * 1024**2:
            return False
        if active == 0:
            return True
        required = self.ram_reserve_bytes + self.estimated_ram_per_render
        if available and available < required:
            return False
        telemetry = _nvidia_gpus()
        if os.name == "nt":
            telemetry += _amd_adl_gpus()
        temperatures = [
            gpu.temperature_c for gpu in telemetry if gpu.temperature_c is not None
        ]
        return not temperatures or max(temperatures) < 88

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        started = time.monotonic()
        while True:
            remaining = None
            if timeout is not None:
                remaining = max(0.0, timeout - (time.monotonic() - started))
                if remaining <= 0:
                    return False
            wait = min(1.0, remaining) if remaining is not None else 1.0
            acquired = (
                self._semaphore.acquire(timeout=wait)
                if blocking
                else self._semaphore.acquire(blocking=False)
            )
            if not acquired:
                if not blocking:
                    return False
                continue
            if self._resources_allow_start():
                with self._lock:
                    self._active += 1
                return True
            self._semaphore.release()
            if not blocking:
                return False
            time.sleep(0.25)

    def release(self) -> None:
        with self._lock:
            if self._active <= 0:
                raise ValueError("render gate released too many times")
            self._active -= 1
        self._semaphore.release()

    def __enter__(self) -> "AdaptiveRenderGate":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        self.release()
        return False


render_slot = AdaptiveRenderGate(1, int(1.5 * GIB), int(2.5 * GIB))
network_slot = threading.BoundedSemaphore(2)
_profile_lock = threading.Lock()
_runtime_profile: PerformanceProfile | None = None
_runtime_power_state: tuple[bool, bool | None] | None = None


def configure_slots(profile: PerformanceProfile) -> None:
    """Replace the exported semaphores with limits from ``profile``."""

    global render_slot, network_slot
    render_slot = AdaptiveRenderGate(
        profile.render_slots,
        profile.ram_reserve_bytes,
        profile.estimated_ram_per_render,
    )
    network_slot = threading.BoundedSemaphore(profile.network_slots)


def get_performance_profile(
    storage_path: str | os.PathLike[str] | None = None,
    *,
    force: bool = False,
) -> PerformanceProfile:
    """Load or safely probe a fingerprinted adaptive profile.

    The cache contains only probe outcomes and derived settings. Dynamic values
    such as free disk and available memory are always re-applied.
    """

    storage = Path(storage_path or _storage_dir()).resolve()
    storage.mkdir(parents=True, exist_ok=True)
    hardware = detect_hardware(storage, force=force)
    ffmpeg = inspect_ffmpeg()
    fingerprint = hardware_fingerprint(hardware, ffmpeg)
    cache_path = storage / "performance_profile.json"
    probes: dict[str, bool] | None = None
    benchmarks: dict[str, EncoderBenchmark] | None = None
    if not force:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("version") == _PROFILE_CACHE_VERSION and cached.get("fingerprint") == fingerprint:
                probes = {str(k): bool(v) for k, v in cached["encoder_probes"].items()}
                benchmarks = {
                    str(codec): EncoderBenchmark(**result)
                    for codec, result in cached["encoder_benchmarks"].items()
                    if isinstance(result, dict)
                }
        except (OSError, ValueError, KeyError, TypeError):
            pass
    if probes is None or benchmarks is None:
        benchmarks = {
            codec: benchmark_encoder(ffmpeg, codec, storage)
            for codec in _ENCODER_PREFERENCE
            if codec in ffmpeg.encoders
        }
        probes = {codec: result.supported for codec, result in benchmarks.items()}
        payload = {
            "version": _PROFILE_CACHE_VERSION,
            "fingerprint": fingerprint,
            "encoder_probes": probes,
            "encoder_benchmarks": {
                codec: dataclasses.asdict(result)
                for codec, result in benchmarks.items()
            },
        }
        temp_path = cache_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            os.replace(temp_path, cache_path)
        except OSError:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
    profile = derive_profile(hardware, ffmpeg, probes, fingerprint, benchmarks)
    configure_slots(profile)
    return profile


def get_runtime_profile(force: bool = False) -> PerformanceProfile:
    """Return the process-wide adaptive profile, probing only when necessary."""

    global _runtime_profile, _runtime_power_state
    with _profile_lock:
        power_state = _battery_state()
        if _runtime_profile is None or force or power_state != _runtime_power_state:
            _runtime_profile = get_performance_profile(force=force)
            _runtime_power_state = power_state
        return _runtime_profile


def redact_error(error: BaseException | str | None) -> str | None:
    """Remove likely credentials and local paths from an error message."""

    if error is None:
        return None
    text = _SECRET_RE.sub(r"\1=<redacted>", str(error))
    text = _PATH_RE.sub("<path>", text)
    return text[:1000]


_task_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("performance_task_id", default=None)


class _RunContext(ContextDecorator):
    def __init__(
        self,
        telemetry: "PerformanceTelemetry",
        kind: str,
        name: str,
        context: Mapping[str, Any] | None,
    ) -> None:
        self.telemetry = telemetry
        self.kind = kind
        self.name = name
        self.context = dict(context or {})
        self.run_id = uuid.uuid4().hex
        self.started = 0.0
        self.token: contextvars.Token[str | None] | None = None
        self.failure: BaseException | str | None = None

    def __enter__(self) -> "_RunContext":
        self.started = time.time()
        task_id = _task_id.get()
        if self.kind == "task":
            task_id = self.run_id
            self.token = _task_id.set(task_id)
        try:
            self.telemetry._start_run(
                self.kind, self.run_id, task_id, self.name, self.started, self.context
            )
        except (sqlite3.Error, OSError) as error:
            _log_telemetry_error("run start", error)
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, traceback: Any) -> bool:
        try:
            try:
                self.telemetry._finish_run(
                    self.kind, self.run_id, time.time(), exc or self.failure
                )
            except (sqlite3.Error, OSError) as error:
                _log_telemetry_error("run finish", error)
        finally:
            if self.token is not None:
                _task_id.reset(self.token)
        return False

    def mark_failed(self, error: BaseException | str) -> None:
        self.failure = error

    def __call__(self, function: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(function)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            with _RunContext(self.telemetry, self.kind, self.name, self.context):
                return function(*args, **kwargs)

        return wrapped


class ResourceSampler:
    """Bounded background sampler returned by :meth:`PerformanceTelemetry.sampler`."""

    def __init__(self, telemetry: "PerformanceTelemetry", interval: float, max_samples: int) -> None:
        if interval <= 0 or max_samples <= 0:
            raise ValueError("interval and max_samples must be positive")
        self.telemetry = telemetry
        self.interval = interval
        self.max_samples = max_samples
        self.task_id = _task_id.get()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "ResourceSampler":
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="resource-sampler")
            try:
                self._thread.start()
            except OSError as error:
                self._thread = None
                _log_telemetry_error("sampler start", error)
        return self

    def _run(self) -> None:
        for _ in range(self.max_samples):
            if self._stop.is_set():
                break
            try:
                self.telemetry.sample_resources(task_id=self.task_id)
            except (sqlite3.Error, OSError) as error:
                _log_telemetry_error("resource sampling", error)
            if self._stop.wait(self.interval):
                break

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=max(1.0, self.interval * 2))

    def __enter__(self) -> "ResourceSampler":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        self.stop()
        return False


class PerformanceTelemetry:
    """SQLite-backed task, stage, and bounded process-resource telemetry."""

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        try:
            path = db_path or Path(_storage_dir()) / "performance.db"
            self.db_path = Path(path).resolve()
        except OSError as error:
            self.db_path = Path(os.devnull)
            _log_telemetry_error("path initialization", error)
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            _log_telemetry_error("directory initialization", error)
        self._process = None
        if _psutil is not None:
            try:
                self._process = _psutil.Process()
                self._process.cpu_percent(interval=None)
                _psutil.cpu_percent(interval=None)
            except Exception:
                self._process = None
        try:
            self._initialize()
        except (sqlite3.Error, OSError) as error:
            _log_telemetry_error("database initialization", error)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=0.25)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS task_runs (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, started_at REAL NOT NULL,
                    finished_at REAL, duration REAL, status TEXT NOT NULL,
                    context_json TEXT NOT NULL, error TEXT
                );
                CREATE TABLE IF NOT EXISTS stage_runs (
                    id TEXT PRIMARY KEY, task_id TEXT, name TEXT NOT NULL,
                    started_at REAL NOT NULL, finished_at REAL, duration REAL,
                    status TEXT NOT NULL, context_json TEXT NOT NULL, error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_stage_task ON stage_runs(task_id);
                CREATE TABLE IF NOT EXISTS resource_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, sampled_at REAL NOT NULL,
                    cpu_percent REAL, rss_bytes INTEGER, ram_available INTEGER,
                    gpu_percent REAL, gpu_memory_used INTEGER,
                    system_cpu_percent REAL, cpu_frequency_mhz REAL,
                    gpu_temperature_c REAL
                );
                """
            )
            existing = {
                row[1] for row in connection.execute("PRAGMA table_info(resource_samples)")
            }
            for name, sql_type in (
                ("system_cpu_percent", "REAL"),
                ("cpu_frequency_mhz", "REAL"),
                ("gpu_temperature_c", "REAL"),
            ):
                if name not in existing:
                    connection.execute(
                        f"ALTER TABLE resource_samples ADD COLUMN {name} {sql_type}"
                    )
            # Rows still marked running belong to a previous process. Closing
            # them at startup keeps audits and throughput estimates honest.
            for table in ("task_runs", "stage_runs"):
                connection.execute(
                    f"UPDATE {table} SET finished_at=started_at, duration=0, "
                    "status='interrupted', error=COALESCE(error, 'process interrupted') "
                    "WHERE status='running'"
                )
            cutoff = time.time() - _TELEMETRY_RETENTION_SECONDS
            connection.execute(
                "DELETE FROM resource_samples WHERE sampled_at < ?", (cutoff,)
            )
            connection.execute(
                "DELETE FROM stage_runs WHERE finished_at IS NOT NULL AND finished_at < ?",
                (cutoff,),
            )
            connection.execute(
                "DELETE FROM task_runs WHERE finished_at IS NOT NULL AND finished_at < ?",
                (cutoff,),
            )

    @staticmethod
    def _context_json(context: Mapping[str, Any]) -> str:
        try:
            return json.dumps(context, default=str, sort_keys=True)[:10000]
        except (TypeError, ValueError):
            return "{}"

    def _start_run(self, kind: str, run_id: str, task_id: str | None, name: str, started: float, context: Mapping[str, Any]) -> None:
        table = "task_runs" if kind == "task" else "stage_runs"
        columns = "id, name, started_at, status, context_json"
        values: tuple[Any, ...] = (run_id, name, started, "running", self._context_json(context))
        if kind == "stage":
            columns = "id, task_id, name, started_at, status, context_json"
            values = (run_id, task_id, name, started, "running", self._context_json(context))
        placeholders = ",".join("?" for _ in values)
        with closing(self._connect()) as connection, connection:
            connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", values)

    def _finish_run(
        self,
        kind: str,
        run_id: str,
        finished: float,
        error: BaseException | str | None,
    ) -> None:
        table = "task_runs" if kind == "task" else "stage_runs"
        with closing(self._connect()) as connection, connection:
            row = connection.execute(f"SELECT started_at FROM {table} WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return
            connection.execute(
                f"UPDATE {table} SET finished_at=?, duration=?, status=?, error=? WHERE id=?",
                (finished, max(0.0, finished - row[0]), "failed" if error else "ok", redact_error(error), run_id),
            )

    def task(self, name: str, **context: Any) -> _RunContext:
        """Return a task context manager (also usable as a decorator)."""

        return _RunContext(self, "task", name, context)

    def stage(self, name: str, **context: Any) -> _RunContext:
        """Return a stage context manager linked to the current task."""

        return _RunContext(self, "stage", name, context)

    def task_decorator(self, name: str | None = None, **context: Any) -> Callable[[Callable[P, R]], Callable[P, R]]:
        """Decorate a function as a telemetry task."""

        def decorate(function: Callable[P, R]) -> Callable[P, R]:
            @functools.wraps(function)
            def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
                with self.task(name or function.__name__, **context):
                    return function(*args, **kwargs)
            return wrapped
        return decorate

    def sample_resources(self, task_id: str | None = None) -> None:
        """Store one process/memory/GPU sample; unavailable metrics stay NULL."""

        cpu = rss = available = gpu_percent = gpu_memory = None
        system_cpu = cpu_frequency = gpu_temperature = None
        if _psutil is not None:
            try:
                process = self._process or _psutil.Process()
                cpu = float(process.cpu_percent(interval=None))
                rss = int(process.memory_info().rss)
                available = int(_psutil.virtual_memory().available)
                system_cpu = float(_psutil.cpu_percent(interval=None))
                frequency = _psutil.cpu_freq()
                if frequency is not None:
                    cpu_frequency = float(frequency.current)
            except Exception:
                pass
        vendor_metrics = _nvidia_gpus()
        if os.name == "nt":
            vendor_metrics += _amd_adl_gpus()
        percentages = [
            gpu.utilization_percent for gpu in vendor_metrics
            if gpu.utilization_percent is not None
        ]
        memory_values = [
            gpu.vram_used for gpu in vendor_metrics if gpu.vram_used is not None
        ]
        if percentages:
            gpu_percent = max(percentages)
        if memory_values:
            gpu_memory = sum(memory_values)
        temperatures = [
            gpu.temperature_c for gpu in vendor_metrics if gpu.temperature_c is not None
        ]
        if temperatures:
            gpu_temperature = max(temperatures)
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    "INSERT INTO resource_samples "
                    "(task_id, sampled_at, cpu_percent, rss_bytes, ram_available, "
                    "gpu_percent, gpu_memory_used, system_cpu_percent, "
                    "cpu_frequency_mhz, gpu_temperature_c) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id or _task_id.get(), time.time(), cpu, rss, available,
                        gpu_percent, gpu_memory, system_cpu, cpu_frequency,
                        gpu_temperature,
                    ),
                )
        except (sqlite3.Error, OSError) as error:
            _log_telemetry_error("resource sampling", error)

    def sampler(self, interval: float = 1.0, max_samples: int = 3600) -> ResourceSampler:
        """Create a sampler that stops explicitly or after ``max_samples``."""

        return ResourceSampler(self, interval, max_samples)

    def recent_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return newest task summaries."""

        if limit <= 0:
            return []
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT id, name, started_at, finished_at, duration, status, error FROM task_runs ORDER BY started_at DESC LIMIT ?",
                (min(limit, 1000),),
            ).fetchall()
        return [dict(row) for row in rows]

    def aggregate_stage_timings(self, limit: int = 10000) -> list[dict[str, Any]]:
        """Aggregate completed stage durations by name over recent rows."""

        if limit <= 0:
            return []
        query = """
            SELECT name, COUNT(*) AS runs, SUM(status = 'failed') AS failures,
                   AVG(duration) AS average_seconds, MIN(duration) AS min_seconds,
                   MAX(duration) AS max_seconds, SUM(duration) AS total_seconds
            FROM (SELECT name, status, duration FROM stage_runs
                  WHERE duration IS NOT NULL ORDER BY started_at DESC LIMIT ?)
            GROUP BY name ORDER BY total_seconds DESC
        """
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(query, (min(limit, 100000),)).fetchall()
        return [dict(row) for row in rows]

    def latest_resource_sample(self) -> dict[str, Any]:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT sampled_at, cpu_percent, rss_bytes, ram_available, "
                "gpu_percent, gpu_memory_used, system_cpu_percent, "
                "cpu_frequency_mhz, gpu_temperature_c FROM resource_samples "
                "ORDER BY sampled_at DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else {}

    def summary(self) -> dict[str, Any]:
        with closing(self._connect()) as connection, connection:
            task = connection.execute(
                "SELECT COUNT(*) AS runs, AVG(duration) AS average_seconds, "
                "MIN(duration) AS min_seconds, MAX(duration) AS max_seconds "
                "FROM task_runs WHERE status='ok' AND duration IS NOT NULL"
            ).fetchone()
            estimate = connection.execute(
                "SELECT SUM(average_seconds) FROM ("
                "SELECT AVG(duration) AS average_seconds FROM stage_runs "
                "WHERE status='ok' AND duration IS NOT NULL GROUP BY name)"
            ).fetchone()[0]
        result = dict(task) if task else {}
        average = float(result.get("average_seconds") or 0)
        result["tasks_per_hour"] = 3600 / average if average > 0 else 0.0
        result["estimated_task_seconds"] = float(estimate or average or 0)
        return result


_telemetry_lock = threading.Lock()
_default_telemetry: PerformanceTelemetry | None = None


def get_telemetry() -> PerformanceTelemetry:
    global _default_telemetry
    with _telemetry_lock:
        if _default_telemetry is None:
            _default_telemetry = PerformanceTelemetry()
        return _default_telemetry


def instrument_task(name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Record a task and resource samples while preserving the wrapped API."""

    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(function)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            external_task_id = kwargs.get("task_id") or (args[0] if args else "")
            telemetry = get_telemetry()
            with telemetry.task(name, task_id=str(external_task_id)) as run:
                with telemetry.sampler(interval=2.0, max_samples=21600):
                    result = function(*args, **kwargs)
                    if result is None:
                        run.mark_failed("task returned no result")
                    return result

        return wrapped

    return decorate


def instrument_stage(name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(function)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            with get_telemetry().stage(name):
                return function(*args, **kwargs)

        return wrapped

    return decorate


__all__ = [
    "AdaptiveRenderGate", "EncoderBenchmark", "FFmpegCapabilities", "GPUInfo",
    "HardwareInfo", "PerformanceProfile", "PerformanceTelemetry", "ResourceSampler",
    "benchmark_encoder", "configure_slots", "derive_profile", "encoder_preset",
    "detect_hardware", "get_performance_profile", "get_runtime_profile", "get_telemetry",
    "hardware_fingerprint", "inspect_ffmpeg", "instrument_stage", "instrument_task",
    "network_slot", "probe_encoder", "recommended_task_slots", "redact_error", "render_slot",
]
