# Changelog

All notable changes maintained in the TheKingXion fork are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
fork releases use the upstream version plus a `custom.N` local version suffix.

## [1.3.2+custom.7] - 2026-07-15

### Added

- Versioned per-task manifests with atomic persistence and resumable cache hits
  for scripts, search terms, narration, and subtitles.
- Thread-safe local WebUI jobs with isolated configuration snapshots, bounded
  log queues, persistent results, and responsive progress polling.
- Safe upload utilities with streaming limits, generated names, validation,
  filesystem synchronization, and atomic publication.
- Python 3.11 and 3.12 CI coverage with locked development tools and Ruff gates.

### Changed

- Redis task state and queues use separate `mpt:task:*` and `mpt:queue:*`
  namespaces, atomic hash updates, durable processing receipts, legacy queue
  migration, and startup recovery.
- Video concat, mux, image preprocessing, and final rendering publish validated
  non-empty outputs from same-directory partial files.
- Performance hardware and encoder benchmarks now run on demand instead of
  delaying every initial WebUI render.
- Docker CPU and GPU images install the exact `uv.lock` environment, run as an
  unprivileged user through a mount-preparing entrypoint, and release images
  resolve from the TheKingXion namespace.
- Advanced the fork version from `1.3.2+custom.6` to `1.3.2+custom.7`.

### Fixed

- Unhandled generation errors now leave tasks in `FAILED` instead of permanent
  `PROCESSING`, and enqueue failures no longer leave orphan task state.
- Performance telemetry failures are rate-limited and best-effort, preserving
  the original task result or exception when SQLite is unavailable.
- TikTok upload chunk counts use ceiling division, reject incomplete transfers,
  and persist the remote publish ID before the first data chunk is sent.
- Local uploads no longer load complete files into memory, overwrite active
- Shared configuration and upload directories use interprocess file locks, and
  WebUI uploads validate their real audio or visual streams before publication.
- Video and audio resources are closed and temporary outputs are removed across
- Concurrent renders no longer share deterministic partial output or concat-list
  names on Windows and other local platforms.

## [1.3.2+custom.6] - 2026-07-15

### Changed

- Advanced the fork version from `1.3.2+custom.5` to `1.3.2+custom.6`.

### Fixed

- YouTube batch manifests now retry atomic replacement when Windows temporarily
  locks the destination JSON through a reader, antivirus, or file indexer.
- Temporary manifest files are cleaned after both successful and failed saves,
  while the previous valid JSON remains untouched if all retries fail.

## [1.3.2+custom.5] - 2026-07-15

### Added

- Representative 1080x1920 encoder benchmarks with audio, measured FPS, error
  reporting, and cache invalidation when hardware, drivers, or FFmpeg change.
- Adaptive task and render concurrency based on RAM, VRAM, GPU temperature,
  laptop power state, CPU topology, and live resource pressure.
- System CPU, effective CPU frequency, and GPU temperature telemetry.

### Changed

- Native vendor encoders are selected by measured stability and speed instead
  of relying on FFmpeg's advertised encoder list or a tiny synthetic probe.
- FFmpeg thread limits and whole-pipeline concurrency now scale across low-RAM
  desktops, high-memory workstations, and battery-powered laptops.
- Videos without subtitles or background music attach narration with stream
  copy instead of performing a redundant final video encode.
- Advanced the fork version from `1.3.2+custom.4` to `1.3.2+custom.5`.

### Fixed

- AMD AMF no longer receives MoviePy's incompatible `medium` preset; the
  validated `speed` preset and `yuv420p` output are used consistently.
- Process CPU telemetry reuses a primed process counter instead of recording
  zero from a newly created sampler on every interval.

## [1.3.2+custom.4] - 2026-07-15

### Changed

- Replaced Streamlit's removed `use_container_width` argument with the current
  `width="stretch"` API throughout the WebUI.
- Advanced the fork version from `1.3.2+custom.3` to `1.3.2+custom.4`.

### Fixed

- YouTube batch progress controls now use separate widget keys in the active
  generator and batch history views, preventing repeated fragment refreshes
  from crashing the WebUI with `StreamlitDuplicateElementKey`.

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
