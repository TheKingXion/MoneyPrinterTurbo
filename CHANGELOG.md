# Changelog

All notable changes maintained in the TheKingXion fork are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
fork releases use the upstream version plus a `custom.N` local version suffix.

## [1.3.2+custom.3] - 2026-07-15

### Added

- Privilege-free Windows GPU inventory using the 64-bit display-driver registry
  values, including accurate dedicated VRAM above 4 GB.
- Native AMD ADL telemetry for GPU temperature, utilization, and dedicated VRAM
  usage without requiring a separate monitoring application.
- CPU model detection and an explicit explanation when Windows cannot expose a
  trustworthy CPU temperature sensor.

### Changed

- Hardware encoder selection now ignores vendor-specific encoders when the
  matching GPU vendor is absent.
- AMD and NVIDIA live metrics feed both the hardware panel and task telemetry.
- Windows avoids slow or permission-sensitive WMI inventory when the display
  registry already contains complete adapter information.
- Advanced the fork version from `1.3.2+custom.2` to `1.3.2+custom.3`.

### Fixed

- GPU detection no longer fails when `Win32_VideoController` access is denied.
- AMD cards report their real 64-bit VRAM instead of an overflowed 4 GB value.
- RX 400/500-series and newer supported Radeon cards can report temperature and
  load through the AMD driver shipped with Windows.
- Adaptive rendering no longer selects NVIDIA, AMD, Intel, or Apple encoders
  solely because a stale encoder probe succeeds.
- Queued generation work is restored if a background worker cannot start,
  preventing silent task loss and blocking queue reads.
- Redis queue serialization no longer mutates the caller's `VideoParams`, and
  task failure logging handles callable objects without a `__name__`.
- Restored the agent helper's provider reuse, secret-safe configuration,
  Pexels validation, dependency execution, and final-video result contract.

## [1.3.2+custom.2] - 2026-07-15

### Added

- Cross-vendor GPU inventory for Windows, Linux, and macOS, including NVIDIA,
  AMD, Intel, and unknown display adapters.
- Optional CPU package temperature detection through psutil on supported Unix
  systems and LibreHardwareMonitor or OpenHardwareMonitor on Windows.
- Per-adapter GPU details in the performance panel, with explicit unavailable
  states for telemetry that the installed driver tools do not expose.
- A short hardware-probe cache to keep Streamlit reruns responsive while still
  allowing explicit hardware redetection.
- AMD AMF and Apple VideoToolbox to automatic FFmpeg encoder probing.

### Changed

- Clarified CPU core and thread labels so core counts are not mistaken for
  Celsius temperatures.
- Generalized GPU hardware data beyond NVIDIA and retained `nvidia-smi` as an
  optional high-detail telemetry provider.
- Increased the adaptive profile cache version to account for the expanded
  hardware fingerprint and encoder list.
- Advanced the fork version from `1.3.2+custom.1` to `1.3.2+custom.2`.

### Fixed

- AMD and Intel adapters no longer appear as missing solely because
  `nvidia-smi` is unavailable.
- Missing resource values display as `N/A` instead of misleading zeroes.

### Known limitations

- Windows CIM does not reliably expose a PCI bus identifier for display
  adapters. Telemetry for multiple identical NVIDIA adapters is therefore
  paired in provider order.
