import copy
import os
import shutil
import socket
import tempfile
import threading
from contextlib import contextmanager
from contextvars import ContextVar

import toml
from loguru import logger

from app.utils.file_lock import interprocess_file_lock

root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
config_file = os.path.abspath(
    os.getenv("MPT_CONFIG_FILE", os.path.join(root_dir, "config.toml"))
)
_CONTAINER_CGROUP_MARKERS = ("docker", "containerd", "kubepods", "libpod", "podman")
_DOCKER_HOST_GATEWAY_NAME = "host.docker.internal"
_config_save_lock = threading.RLock()
_runtime_config_snapshot = ContextVar("runtime_config_snapshot", default=None)
_MISSING = object()


class _SynchronizedConfig(dict):
    """Keep runtime config mutations synchronized with atomic saves."""

    def __init__(self, section_name, values):
        super().__init__(values)
        self.section_name = section_name

    def _read_values(self):
        snapshot = _runtime_config_snapshot.get()
        if snapshot is not None:
            return snapshot[self.section_name]
        return self

    def __getitem__(self, key):
        values = self._read_values()
        if values is self:
            return super().__getitem__(key)
        return values[key]

    def __contains__(self, key):
        values = self._read_values()
        if values is self:
            return super().__contains__(key)
        return key in values

    def __iter__(self):
        values = self._read_values()
        if values is self:
            return super().__iter__()
        return iter(values)

    def __len__(self):
        values = self._read_values()
        if values is self:
            return super().__len__()
        return len(values)

    def get(self, key, default=None):
        values = self._read_values()
        if values is self:
            return super().get(key, default)
        return values.get(key, default)

    def keys(self):
        values = self._read_values()
        return super().keys() if values is self else values.keys()

    def items(self):
        values = self._read_values()
        return super().items() if values is self else values.items()

    def values(self):
        values = self._read_values()
        return super().values() if values is self else values.values()

    def copy(self):
        values = self._read_values()
        return super().copy() if values is self else values.copy()

    def snapshot(self):
        """Return a deep copy of the global values, ignoring contextual views."""
        return copy.deepcopy(super().copy())

    def __setitem__(self, key, value):
        with _config_save_lock:
            super().__setitem__(key, value)

    def __delitem__(self, key):
        with _config_save_lock:
            super().__delitem__(key)

    def clear(self):
        with _config_save_lock:
            super().clear()

    def pop(self, key, default=_MISSING):
        with _config_save_lock:
            if default is _MISSING:
                return super().pop(key)
            return super().pop(key, default)

    def setdefault(self, key, default=None):
        with _config_save_lock:
            return super().setdefault(key, default)

    def update(self, *args, **kwargs):
        with _config_save_lock:
            super().update(*args, **kwargs)


@contextmanager
def runtime_config_lock():
    """Prevent other sessions from changing global config during an operation."""
    with _config_save_lock:
        yield


def snapshot_runtime_config():
    """Capture an isolated copy of every runtime configuration section."""
    with _config_save_lock:
        return {
            section.section_name: section.snapshot()
            for section in _runtime_config_sections
        }


@contextmanager
def use_runtime_config(snapshot):
    """Make config reads in the current execution context use a snapshot."""
    token = _runtime_config_snapshot.set(snapshot)
    try:
        yield
    finally:
        _runtime_config_snapshot.reset(token)


def is_running_in_container(
    dockerenv_path: str = "/.dockerenv",
    containerenv_path: str = "/run/.containerenv",
    cgroup_path: str = "/proc/1/cgroup",
) -> bool:
    """
    判断当前进程是否运行在容器内。

    这个判断主要用于 Ollama 默认地址选择：
    - 普通本机运行时，`localhost` 指向用户机器本身；
    - Docker 容器内，`localhost` 指向容器自己，访问宿主机 Ollama
      通常需要使用 `host.docker.internal`。

    不能只判断 `/proc/1/cgroup` 是否存在，因为普通 Linux 也会有这个文件。
    这里只在检测到明确的容器标记时返回 True，避免误伤非 Docker Linux 用户。
    参数保留为可注入路径，便于单元测试覆盖不同运行环境。
    """
    if os.path.isfile(dockerenv_path) or os.path.isfile(containerenv_path):
        return True

    try:
        with open(cgroup_path, mode="r", encoding="utf-8") as fp:
            cgroup_content = fp.read().lower()
    except OSError:
        return False

    return any(marker in cgroup_content for marker in _CONTAINER_CGROUP_MARKERS)


def _can_resolve_hostname(hostname: str) -> bool:
    try:
        socket.gethostbyname(hostname)
    except OSError:
        return False
    return True


def _decode_linux_route_gateway(hex_gateway: str) -> str:
    # /proc/net/route 里的 Gateway 是 16 进制小端序，例如 010011AC 表示
    # 172.17.0.1。这里单独解析，是为了在原生 Linux Docker 没有
    # host.docker.internal DNS 记录时，还能尝试访问容器默认网关上的宿主机。
    if len(hex_gateway) != 8:
        raise ValueError("invalid gateway length")

    octets = [
        str(int(hex_gateway[index : index + 2], 16))
        for index in range(6, -1, -2)
    ]
    return ".".join(octets)


def get_container_default_gateway_ip(route_path: str = "/proc/net/route") -> str:
    """
    读取 Linux 容器里的默认网关 IP。

    Docker Desktop 通常提供 `host.docker.internal`，但原生 Linux Docker
    默认不一定提供这个 DNS 名称。默认网关通常可以作为访问宿主机服务的
    兜底地址；如果用户的 Ollama 只监听 127.0.0.1，则仍需要用户让
    Ollama 监听宿主机网卡或手动配置 `ollama_base_url`。
    """
    try:
        with open(route_path, mode="r", encoding="utf-8") as fp:
            route_lines = fp.readlines()
    except OSError:
        return ""

    for line in route_lines[1:]:
        fields = line.strip().split()
        if len(fields) < 3:
            continue

        destination = fields[1]
        gateway = fields[2]
        if destination != "00000000" or gateway == "00000000":
            continue

        try:
            return _decode_linux_route_gateway(gateway)
        except ValueError:
            logger.warning(f"invalid container gateway route entry: {line.strip()}")
            return ""

    return ""


def get_default_ollama_base_url() -> str:
    """
    返回 Ollama 的默认 OpenAI-compatible base_url。

    用户显式配置 `ollama_base_url` 时不会走这里；这里只处理“未配置时的
    最佳默认值”。容器内默认指向宿主机，普通本机运行默认指向 localhost。
    """
    if not is_running_in_container():
        return "http://localhost:11434/v1"

    if _can_resolve_hostname(_DOCKER_HOST_GATEWAY_NAME):
        return f"http://{_DOCKER_HOST_GATEWAY_NAME}:11434/v1"

    gateway_ip = get_container_default_gateway_ip()
    if gateway_ip:
        logger.info(
            "host.docker.internal is not resolvable, fallback to container "
            f"default gateway for Ollama: {gateway_ip}"
        )
        return f"http://{gateway_ip}:11434/v1"

    logger.warning(
        "failed to resolve host.docker.internal and container default gateway; "
        "fallback to host.docker.internal for Ollama"
    )
    return f"http://{_DOCKER_HOST_GATEWAY_NAME}:11434/v1"


def load_config():
    config_dir = os.path.dirname(config_file) or root_dir
    os.makedirs(config_dir, exist_ok=True)
    if os.path.isdir(config_file):
        raise IsADirectoryError(f"configuration path is a directory: {config_file}")

    if not os.path.isfile(config_file):
        example_file = f"{root_dir}/config.example.toml"
        if os.path.isfile(example_file):
            shutil.copyfile(example_file, config_file)
            logger.info("copy config.example.toml to config.toml")

    logger.info(f"load config from file: {config_file}")

    try:
        _config_ = toml.load(config_file)
    except Exception as e:
        logger.warning(f"load config failed: {str(e)}, try to load as utf-8-sig")
        with open(config_file, mode="r", encoding="utf-8-sig") as fp:
            _cfg_content = fp.read()
            _config_ = toml.loads(_cfg_content)
    return _config_


def save_config():
    """Synchronize and atomically persist all runtime-managed config sections."""
    lock_path = f"{config_file}.lock"
    with _config_save_lock, interprocess_file_lock(lock_path):
        config_to_save = dict(_cfg)
        config_to_save["app"] = dict(app)
        config_to_save["azure"] = dict(azure)
        config_to_save["siliconflow"] = dict(siliconflow)
        config_to_save["elevenlabs"] = dict(elevenlabs)
        config_to_save["chatterbox"] = dict(chatterbox)
        config_to_save["youtube"] = dict(youtube)
        config_to_save["tiktok"] = dict(tiktok)
        config_to_save["ui"] = dict(ui)
        serialized_config = toml.dumps(config_to_save)

        try:
            with open(config_file, mode="r", encoding="utf-8") as f:
                if f.read() == serialized_config:
                    _cfg.clear()
                    _cfg.update(config_to_save)
                    return
        except (OSError, UnicodeError):
            pass

        temp_path = ""
        try:
            fd, temp_path = tempfile.mkstemp(
                prefix=".config-",
                suffix=".toml.tmp",
                dir=os.path.dirname(config_file) or root_dir,
            )
            with os.fdopen(fd, mode="w", encoding="utf-8") as f:
                f.write(serialized_config)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, config_file)
            _cfg.clear()
            _cfg.update(config_to_save)
        finally:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)


_cfg = load_config()
app = _SynchronizedConfig("app", _cfg.get("app", {}))
whisper = _SynchronizedConfig("whisper", _cfg.get("whisper", {}))
proxy = _SynchronizedConfig("proxy", _cfg.get("proxy", {}))
azure = _SynchronizedConfig("azure", _cfg.get("azure", {}))
siliconflow = _SynchronizedConfig("siliconflow", _cfg.get("siliconflow", {}))
elevenlabs = _SynchronizedConfig("elevenlabs", _cfg.get("elevenlabs", {}))
chatterbox = _SynchronizedConfig("chatterbox", _cfg.get("chatterbox", {}))
youtube = _SynchronizedConfig("youtube", {
    "enabled": False,
    "auto_upload": False,
    "privacy_status": "private",
    "schedule_enabled": False,
    "schedule_at": "21:00",
    "schedule_mode": "interval",
    "schedule_videos_per_day": 4,
    "schedule_timezone": "local",
    "schedule_interval_minutes": 15,
    "daily_api_limit": 7,
    "upload_interval_minutes": 5,
    "client_id": "",
    "client_secret": "",
    "allow_remote_api": False,
    **_cfg.get("youtube", {}),
})
tiktok = _SynchronizedConfig("tiktok", {
    "enabled": False,
    "provider": "official",
    "auto_upload": False,
    "client_key": "",
    "client_secret": "",
    "redirect_uri": "http://127.0.0.1:8080/api/v1/tiktok/callback",
    "privacy_level": "SELF_ONLY",
    "allow_comments": True,
    "allow_duet": False,
    "allow_stitch": False,
    "schedule_enabled": False,
    "schedule_at": "21:00",
    "schedule_interval_minutes": 30,
    "upload_interval_minutes": 5,
    "daily_upload_limit": 10,
    "max_retries": 3,
    "retry_delay_minutes": 10,
    "upload_post_api_key": "",
    "upload_post_username": "",
    "allow_remote_api": False,
    **_cfg.get("tiktok", {}),
})
if os.getenv("TIKTOK_CLIENT_KEY"):
    tiktok["client_key"] = os.environ["TIKTOK_CLIENT_KEY"]
if os.getenv("TIKTOK_CLIENT_SECRET"):
    tiktok["client_secret"] = os.environ["TIKTOK_CLIENT_SECRET"]
ui = _SynchronizedConfig("ui", _cfg.get(
    "ui",
    {
        "hide_log": False,
    },
))
_runtime_config_sections = (
    app,
    whisper,
    proxy,
    azure,
    siliconflow,
    elevenlabs,
    chatterbox,
    youtube,
    tiktok,
    ui,
)

hostname = socket.gethostname()

log_level = _cfg.get("log_level", "DEBUG")
listen_host = _cfg.get("listen_host", "127.0.0.1")
listen_port = _cfg.get("listen_port", 8080)
project_name = _cfg.get("project_name", "MoneyPrinterTurbo")
project_description = _cfg.get(
    "project_description",
    "<a href='https://github.com/harry0703/MoneyPrinterTurbo'>https://github.com/harry0703/MoneyPrinterTurbo</a>"
    "<br><small>Supported by <a href='https://aihubmix.com/?aff=CEve'>AIHubMix</a></small>",
)
project_version = "1.3.2+custom.7"
reload_debug = False

app["redis_host"] = os.getenv(
    "MPT_APP_REDIS_HOST",
    os.getenv("REDIS_HOST", app.get("redis_host", "localhost")),
)

imagemagick_path = app.get("imagemagick_path", "")
if imagemagick_path and os.path.isfile(imagemagick_path):
    os.environ["IMAGEMAGICK_BINARY"] = imagemagick_path

ffmpeg_path = app.get("ffmpeg_path", "")
if ffmpeg_path and os.path.isfile(ffmpeg_path):
    os.environ["IMAGEIO_FFMPEG_EXE"] = ffmpeg_path

logger.info(f"{project_name} v{project_version}")
