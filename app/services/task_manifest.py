"""Small, task-local cache manifest for restartable generation stages."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from loguru import logger


MANIFEST_VERSION = 1
MANIFEST_NAME = "manifest.json"


def hash_inputs(inputs: Any) -> str:
    payload = json.dumps(
        inputs,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def hash_file(file_path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(file_path, "rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class TaskManifest:
    def __init__(self, task_id: str, task_dir: str | os.PathLike[str]):
        self.task_id = str(task_id)
        self.task_dir = Path(task_dir).resolve()
        self.path = self.task_dir / MANIFEST_NAME

    def _empty(self) -> dict[str, Any]:
        return {
            "version": MANIFEST_VERSION,
            "task_id": self.task_id,
            "stages": {},
        }

    def load(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as manifest_file:
                data = json.load(manifest_file)
            if (
                not isinstance(data, dict)
                or data.get("version") != MANIFEST_VERSION
                or data.get("task_id") != self.task_id
                or not isinstance(data.get("stages"), dict)
            ):
                raise ValueError("unsupported manifest structure")
            return data
        except FileNotFoundError:
            return self._empty()
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning(
                f"ignoring invalid task manifest: task_id={self.task_id}, error={exc}"
            )
            return self._empty()

    def _artifact_path(self, relative_path: str) -> Path | None:
        if not isinstance(relative_path, str) or not relative_path:
            return None
        if Path(relative_path).is_absolute():
            return None
        candidate = (self.task_dir / relative_path).resolve()
        try:
            candidate.relative_to(self.task_dir)
        except ValueError:
            return None
        return candidate

    def restore(self, stage: str, inputs: Any) -> dict[str, Any] | None:
        entry = self.load()["stages"].get(stage)
        if (
            not isinstance(entry, dict)
            or entry.get("status") != "complete"
            or entry.get("input_hash") != hash_inputs(inputs)
            or not isinstance(entry.get("outputs"), dict)
            or not isinstance(entry.get("artifacts"), dict)
        ):
            return None

        artifacts: dict[str, str] = {}
        for name, artifact in entry["artifacts"].items():
            if not isinstance(artifact, dict):
                return None
            artifact_path = self._artifact_path(artifact.get("path", ""))
            try:
                if (
                    artifact_path is None
                    or not artifact_path.is_file()
                    or hash_file(artifact_path) != artifact.get("sha256")
                ):
                    return None
            except OSError:
                return None
            artifacts[name] = str(artifact_path)

        return {"outputs": entry["outputs"], "artifacts": artifacts}

    def complete(
        self,
        stage: str,
        inputs: Any,
        outputs: dict[str, Any],
        artifacts: dict[str, str | os.PathLike[str]],
    ) -> None:
        artifact_records = {}
        for name, artifact in artifacts.items():
            artifact_path = Path(artifact).resolve()
            try:
                relative_path = artifact_path.relative_to(self.task_dir)
            except ValueError as exc:
                raise ValueError("manifest artifacts must be task-local") from exc
            if not artifact_path.is_file():
                raise FileNotFoundError(artifact_path)
            artifact_records[name] = {
                "path": relative_path.as_posix(),
                "sha256": hash_file(artifact_path),
            }

        data = self.load()
        data["stages"][stage] = {
            "status": "complete",
            "input_hash": hash_inputs(inputs),
            "outputs": outputs,
            "artifacts": artifact_records,
        }
        self._write_atomic(data)

    def _write_atomic(self, data: dict[str, Any]) -> None:
        self.task_dir.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.task_dir,
                prefix=f".{MANIFEST_NAME}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_path = temp_file.name
                json.dump(data, temp_file, ensure_ascii=False, sort_keys=True, indent=2)
                temp_file.write("\n")
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, self.path)
            temp_path = None
            self._fsync_directory()
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass

    def _fsync_directory(self) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            directory_fd = os.open(self.task_dir, flags)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        except OSError:
            pass
        finally:
            os.close(directory_fd)
