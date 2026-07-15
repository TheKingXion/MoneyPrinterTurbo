# Changelog

All notable changes maintained in the TheKingXion fork are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
fork releases use the upstream version plus a `custom.N` local version suffix.

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
