import os
import subprocess
import tempfile
import threading
from typing import BinaryIO, Callable
from uuid import uuid4

from app.utils.file_lock import interprocess_file_lock


COPY_CHUNK_BYTES = 1024 * 1024
_upload_lock = threading.Lock()


class UploadRejectedError(ValueError):
    pass


class UploadStorageError(RuntimeError):
    pass


def validate_visual_media(file_path: str) -> None:
    """Require one decodable visual frame without trusting the file extension."""
    from app.utils.utils import get_ffmpeg_binary

    try:
        result = subprocess.run(
            [
                get_ffmpeg_binary(), "-nostdin", "-v", "error", "-xerror",
                "-i", file_path, "-map", "0:v:0", "-frames:v", "1",
                "-f", "null", "-",
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise UploadStorageError("FFmpeg media validation timed out") from exc
    except OSError as exc:
        raise UploadStorageError("failed to run FFmpeg media validation") from exc
    if result.returncode != 0:
        raise UploadRejectedError(
            "uploaded file must contain a decodable visual stream"
        )


def validate_audio_media(file_path: str) -> None:
    """Require one decodable audio stream without trusting the file extension."""
    from app.utils.utils import get_ffmpeg_binary

    try:
        result = subprocess.run(
            [
                get_ffmpeg_binary(), "-nostdin", "-v", "error", "-xerror",
                "-i", file_path, "-map", "0:a:0", "-t", "0.1",
                "-f", "null", "-",
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise UploadStorageError("FFmpeg audio validation timed out") from exc
    except OSError as exc:
        raise UploadStorageError("failed to run FFmpeg audio validation") from exc
    if result.returncode != 0:
        raise UploadRejectedError(
            "uploaded file must contain a decodable audio stream"
        )


def _directory_size(directory: str) -> int:
    total = 0
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.name == ".upload.lock":
                continue
            if entry.is_file(follow_symlinks=False):
                total += entry.stat(follow_symlinks=False).st_size
    return total


def _remove_file(file_path: str) -> None:
    if not file_path:
        return
    try:
        os.remove(file_path)
    except FileNotFoundError:
        pass


def save_upload_atomically(
    source: BinaryIO,
    directory: str,
    suffix: str,
    *,
    max_file_bytes: int,
    max_total_bytes: int,
    validate: Callable[[str], None] | None = None,
) -> tuple[str, int]:
    """Persist an upload under a generated name without exposing partial content."""
    if max_file_bytes <= 0 or max_total_bytes <= 0:
        raise UploadStorageError("upload limits must be positive")

    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        raise UploadStorageError("failed to prepare upload directory") from exc

    temp_path = ""
    with _upload_lock, interprocess_file_lock(os.path.join(directory, ".upload.lock")):
        try:
            existing_bytes = _directory_size(directory)
            descriptor, temp_path = tempfile.mkstemp(
                prefix=".upload-", suffix=suffix, dir=directory
            )
            total_bytes = 0
            with os.fdopen(descriptor, "wb") as output:
                while True:
                    chunk = source.read(COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise UploadRejectedError("upload must be binary")
                    total_bytes += len(chunk)
                    if total_bytes > max_file_bytes:
                        raise UploadRejectedError("upload exceeds the per-file size limit")
                    if existing_bytes + total_bytes > max_total_bytes:
                        raise UploadRejectedError("upload exceeds the total storage limit")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())

            if total_bytes == 0:
                raise UploadRejectedError("upload is empty")
            if validate is not None:
                validate(temp_path)

            for _ in range(10):
                stored_name = f"{uuid4().hex}{suffix}"
                target_path = os.path.join(directory, stored_name)
                if not os.path.lexists(target_path):
                    break
            else:
                raise UploadStorageError("failed to allocate a unique upload name")
            os.replace(temp_path, target_path)
            temp_path = ""

            # Persist the directory entry where the platform supports directory fsync.
            try:
                directory_fd = os.open(directory, os.O_RDONLY)
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            return stored_name, total_bytes
        except UploadRejectedError:
            raise
        except OSError as exc:
            raise UploadStorageError("failed to persist upload") from exc
        finally:
            _remove_file(temp_path)
            try:
                source.seek(0)
            except (AttributeError, OSError):
                pass
