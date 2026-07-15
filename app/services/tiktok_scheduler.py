import json
import os
import threading
import time
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from loguru import logger

from app.config import config
from app.services.tiktok_uploader import tiktok_upload_tracker, tiktok_uploader
from app.utils import utils


def _process_exists(pid: int) -> bool:
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class TikTokScheduler:
    def __init__(self, queue_path: str | None = None):
        self.queue_path = queue_path or os.path.join(utils.storage_dir(create=True), "tiktok_schedule.json")
        self.process_lock_path = f"{self.queue_path}.lock"
        self.data_lock_path = f"{self.queue_path}.data.lock"
        self._owns_process_lock = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _load_unlocked(self) -> list[dict[str, Any]]:
        if not os.path.isfile(self.queue_path):
            return []
        try:
            with open(self.queue_path, "r", encoding="utf-8") as file:
                data = json.load(file)
            jobs = data.get("jobs", []) if isinstance(data, dict) else []
            return jobs if isinstance(jobs, list) else []
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"TikTok schedule is corrupt: {self.queue_path}") from exc
        except OSError as exc:
            raise RuntimeError(f"TikTok schedule cannot be read: {self.queue_path}") from exc

    def _save_unlocked(self, jobs: list[dict[str, Any]]) -> None:
        os.makedirs(os.path.dirname(self.queue_path), exist_ok=True)
        temporary = f"{self.queue_path}.{os.getpid()}.{uuid4().hex}.tmp"
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump({"jobs": jobs}, file, ensure_ascii=False, indent=2)
        os.replace(temporary, self.queue_path)

    @contextmanager
    def _transaction(self):
        with self._lock:
            deadline = time.monotonic() + 10
            owner_token = secrets.token_hex(16)
            while True:
                try:
                    descriptor = os.open(self.data_lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    with os.fdopen(descriptor, "w", encoding="ascii") as file:
                        file.write(f"{owner_token} {time.time()}")
                    break
                except FileExistsError:
                    try:
                        stale = time.time() - os.path.getmtime(self.data_lock_path) > 30
                    except OSError:
                        stale = False
                    if stale:
                        try:
                            os.remove(self.data_lock_path)
                        except FileNotFoundError:
                            pass
                        continue
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Timed out waiting for TikTok schedule lock")
                    time.sleep(0.05)
            try:
                yield
            finally:
                try:
                    with open(self.data_lock_path, "r", encoding="ascii") as file:
                        current_token = file.read().split()[0]
                    if secrets.compare_digest(current_token, owner_token):
                        os.remove(self.data_lock_path)
                except (FileNotFoundError, OSError, IndexError):
                    pass

    def load(self) -> list[dict[str, Any]]:
        with self._transaction():
            return self._load_unlocked()

    def save(self, jobs: list[dict[str, Any]]) -> None:
        with self._transaction():
            self._save_unlocked(jobs)

    def calculate_scheduled_at(self, slot_index: int = 0) -> str:
        tiktok_uploader.sync_from_disk()
        settings = config.tiktok
        hour, minute = 21, 0
        try:
            parsed_time = datetime.strptime(str(settings.get("schedule_at", "21:00")), "%H:%M")
            hour, minute = parsed_time.hour, parsed_time.minute
        except (ValueError, TypeError):
            logger.warning("invalid tiktok.schedule_at, using 21:00")
        now = datetime.now().astimezone()
        base = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if base <= now:
            base += timedelta(days=1)
        interval = max(1, int(settings.get("schedule_interval_minutes", 30) or 30))
        daily_limit = max(1, int(settings.get("daily_upload_limit", 10) or 10))
        remaining_minutes = max(1, 24 * 60 - (hour * 60 + minute))
        slots_per_day = max(1, min(daily_limit, (remaining_minutes + interval - 1) // interval))
        day_offset, daily_slot = divmod(max(0, slot_index), slots_per_day)
        return (base + timedelta(days=day_offset, minutes=daily_slot * interval)).isoformat()

    def add_job(
        self,
        task_id: str,
        subject: str,
        video_path: str,
        caption: str,
        scheduled_at: str,
        provider: str,
        privacy_level: str,
        allow_comment: bool,
        allow_duet: bool,
        allow_stitch: bool,
        index: int = 1,
    ) -> dict[str, Any]:
        datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
        with self._transaction():
            jobs = self._load_unlocked()
            for job in jobs:
                if job.get("task_id") == task_id and int(job.get("index", 1)) == index and job.get("status") == "pending":
                    return job
            if not tiktok_upload_tracker.reserve_schedule(
                task_id, index, subject, video_path, provider, scheduled_at
            ):
                raise RuntimeError("TikTok video is already scheduled or uploaded")
            job = {
                "job_id": str(uuid4()),
                "task_id": task_id,
                "index": index,
                "subject": subject,
                "video_path": video_path,
                "caption": caption,
                "scheduled_at": scheduled_at,
                "provider": provider,
                "privacy_level": privacy_level,
                "allow_comment": allow_comment,
                "allow_duet": allow_duet,
                "allow_stitch": allow_stitch,
                "status": "pending",
                "attempts": 0,
                "error": "",
            }
            jobs.append(job)
            try:
                self._save_unlocked(jobs)
            except Exception:
                tiktok_upload_tracker.update_status(
                    task_id, "failed", index, error="Failed to persist TikTok schedule"
                )
                raise
        return job

    def cancel(self, job_id: str) -> bool:
        with self._transaction():
            jobs = self._load_unlocked()
            for job in jobs:
                if job.get("job_id") == job_id and job.get("status") == "pending":
                    job["status"] = "cancelled"
                    self._save_unlocked(jobs)
                    tiktok_upload_tracker.update_status(job["task_id"], "cancelled", int(job.get("index", 1)))
                    return True
        return False

    def run_due_jobs(self) -> list[dict[str, Any]]:
        tiktok_uploader.sync_from_disk()
        results = []
        now = datetime.now().astimezone()
        invalid_jobs = []
        with self._transaction():
            jobs = self._load_unlocked()
            due = []
            for job in jobs:
                if job.get("status") != "pending":
                    continue
                try:
                    scheduled = datetime.fromisoformat(str(job.get("scheduled_at", "")).replace("Z", "+00:00"))
                    if scheduled.tzinfo is None:
                        scheduled = scheduled.replace(tzinfo=now.tzinfo)
                except ValueError:
                    job.update(status="failed", error="invalid scheduled_at")
                    invalid_jobs.append((job["task_id"], int(job.get("index", 1))))
                    continue
                if scheduled <= now:
                    due.append(dict(job))
            self._save_unlocked(jobs)

        for task_id, index in invalid_jobs:
            tiktok_upload_tracker.update_status(task_id, "failed", index, error="invalid scheduled_at")

        for job in due:
            with self._transaction():
                jobs = self._load_unlocked()
                current = next((item for item in jobs if item.get("job_id") == job.get("job_id")), None)
                if not current or current.get("status") != "pending":
                    continue
                current["status"] = "uploading"
                self._save_unlocked(jobs)
            tiktok_upload_tracker.update_status(
                job["task_id"], "uploading", int(job.get("index", 1)), error=""
            )
            try:
                result = tiktok_uploader.upload_video(
                    job["video_path"],
                    job.get("caption", ""),
                    job.get("privacy_level", ""),
                    job.get("allow_comment"),
                    job.get("allow_duet"),
                    job.get("allow_stitch"),
                    provider=str(job.get("provider") or ""),
                    idempotency_key=str(job.get("job_id") or f"{job['task_id']}-{job.get('index', 1)}"),
                )
            except Exception as exc:
                result = {"success": False, "error": str(exc), "retryable": True, "status": "failed"}
            with self._transaction():
                jobs = self._load_unlocked()
                current = next((item for item in jobs if item.get("job_id") == job.get("job_id")), None)
                if not current:
                    continue
                current["attempts"] = int(current.get("attempts", 0)) + 1
                if result.get("success"):
                    current["status"] = "completed"
                    current["publish_id"] = result.get("publish_id", "")
                else:
                    max_retries = max(1, int(config.tiktok.get("max_retries", 3) or 3))
                    current["error"] = result.get("error", "")
                    if result.get("retryable", True) is False or current["attempts"] >= max_retries:
                        current["status"] = "failed"
                    else:
                        delay = max(1, int(config.tiktok.get("retry_delay_minutes", 10) or 10))
                        current["status"] = "pending"
                        current["scheduled_at"] = (now + timedelta(minutes=delay * current["attempts"])).isoformat()
                self._save_unlocked(jobs)
            status = (
                result.get("status", "processing")
                if result.get("success")
                else "reconcile_required" if result.get("publish_id") else "failed" if current["status"] == "failed" else "scheduled_retry"
            )
            tiktok_upload_tracker.add_entry(
                job["task_id"], int(job.get("index", 1)), job.get("subject", ""), job["video_path"], status,
                provider=job.get("provider", ""), publish_id=result.get("publish_id", ""),
                scheduled_at=job.get("scheduled_at", ""), tiktok_url=result.get("tiktok_url", ""),
                error=result.get("error", ""),
            )
            results.append({"job_id": job["job_id"], **result})
        return results

    def reconcile_processing(self) -> None:
        tiktok_uploader.sync_from_disk()
        now = datetime.now().astimezone()
        for entry in tiktok_upload_tracker.load():
            if entry.get("status") not in {"processing", "reconcile_required"} or not entry.get("publish_id"):
                continue
            try:
                last_check = datetime.fromisoformat(str(entry.get("last_status_check", "")).replace("Z", "+00:00"))
                if last_check.tzinfo is None:
                    last_check = last_check.replace(tzinfo=now.tzinfo)
                if (now - last_check).total_seconds() < 60:
                    continue
            except ValueError:
                pass
            task_id = str(entry.get("task_id", ""))
            index = int(entry.get("index", 1))
            tiktok_upload_tracker.update_status(
                task_id, str(entry.get("status")), index, last_status_check=now.isoformat()
            )
            try:
                result = tiktok_uploader.fetch_status(str(entry["publish_id"]), str(entry.get("provider", "")))
            except Exception as exc:
                logger.warning(f"failed to refresh TikTok publish status {entry['publish_id']}: {exc}")
                continue
            if result.get("success") is False:
                logger.warning(f"TikTok status provider unavailable for {entry['publish_id']}: {result.get('error', '')}")
                continue
            raw_status = str(result.get("status", "processing"))
            upper_status = raw_status.upper()
            if raw_status.lower() in {"published", "failed", "processing"}:
                status = raw_status.lower()
            else:
                status = "published" if upper_status in {"PUBLISH_COMPLETE", "PUBLISHED", "COMPLETED"} else "failed" if "FAIL" in upper_status else "processing"
            data = result.get("data") or {}
            post_ids = data.get("publicaly_available_post_id") or data.get("publicly_available_post_id") or []
            post_id = result.get("post_id") or (str(post_ids[0]) if isinstance(post_ids, list) and post_ids else "")
            error = result.get("error") or data.get("fail_reason") or ""
            tiktok_upload_tracker.update_status(
                task_id,
                status,
                index,
                publish_status=upper_status,
                post_id=post_id,
                tiktok_url=result.get("tiktok_url", ""),
                error=error if status == "failed" else "",
                last_status_check=now.isoformat(),
            )

    def _worker(self) -> None:
        while not self._stop_event.wait(30):
            try:
                self.run_due_jobs()
                self.reconcile_processing()
            except Exception as exc:
                logger.exception(f"TikTok scheduler error: {exc}")

    def _acquire_process_lock(self) -> bool:
        for _ in range(2):
            try:
                descriptor = os.open(self.process_lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(descriptor, "w", encoding="ascii") as file:
                    file.write(str(os.getpid()))
                self._owns_process_lock = True
                return True
            except FileExistsError:
                try:
                    with open(self.process_lock_path, "r", encoding="ascii") as file:
                        owner_pid = int(file.read().strip())
                    if _process_exists(owner_pid):
                        return False
                    raise OSError("stale scheduler lock")
                except (OSError, ValueError):
                    try:
                        if time.time() - os.path.getmtime(self.process_lock_path) < 2:
                            time.sleep(0.05)
                            continue
                    except OSError:
                        pass
                    try:
                        os.remove(self.process_lock_path)
                    except FileNotFoundError:
                        pass
        return False

    def _mark_interrupted_jobs(self) -> None:
        interrupted = []
        active_jobs = set()
        with self._transaction():
            jobs = self._load_unlocked()
            changed = False
            for job in jobs:
                if job.get("status") == "pending":
                    active_jobs.add((job.get("task_id"), int(job.get("index", 1))))
                if job.get("status") == "uploading":
                    job["status"] = "failed"
                    job["error"] = "Publishing was interrupted; verify TikTok before retrying to avoid duplicates"
                    interrupted.append((job["task_id"], int(job.get("index", 1)), job.get("publish_id", "")))
                    changed = True
            if changed:
                self._save_unlocked(jobs)
        for task_id, index, publish_id in interrupted:
            tiktok_upload_tracker.update_status(
                task_id,
                "reconcile_required" if publish_id else "failed",
                index,
                error="Publishing was interrupted; verify TikTok before retrying to avoid duplicates",
                publish_id=publish_id,
            )
        for entry in tiktok_upload_tracker.load():
            try:
                entry_index = int(entry.get("index", 1))
            except (TypeError, ValueError):
                continue
            key = (entry.get("task_id"), entry_index)
            if entry.get("status") in {"scheduled", "scheduled_retry"} and key not in active_jobs:
                tiktok_upload_tracker.update_status(
                    str(entry.get("task_id", "")),
                    "failed",
                    entry_index,
                    error="Scheduled job was missing from the queue",
                )
            elif entry.get("status") == "uploading":
                tiktok_upload_tracker.update_status(
                    str(entry.get("task_id", "")),
                    "reconcile_required",
                    entry_index,
                    error="Publishing was interrupted; verify TikTok before retrying",
                )

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if not self._acquire_process_lock():
            logger.info("TikTok scheduler is already running in another process")
            return
        self._mark_interrupted_jobs()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._worker, name="tiktok-scheduler", daemon=True)
        self._thread.start()
        logger.info("TikTok scheduler started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                logger.warning("TikTok scheduler is still stopping; retaining process lock")
                return
        if self._owns_process_lock:
            try:
                os.remove(self.process_lock_path)
            except FileNotFoundError:
                pass
            self._owns_process_lock = False


tiktok_scheduler = TikTokScheduler()
