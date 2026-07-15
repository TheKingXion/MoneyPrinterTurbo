import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

from app.models.schema import VideoParams
from app.services import state as sm
from app.services import task as tm
from app.services.youtube_batch import YouTubeBatchStore, youtube_batch_store

GenerateFunction = Callable[[str, VideoParams], Any]
UploadFunction = Callable[[dict, dict, dict], dict]
SleepFunction = Callable[[float], None]
SlotFunction = Callable[[dict, int], dict]


def classify_upload_failure(error: Any) -> str:
    text = str(error or "").casefold()
    if any(value in text for value in ("quota", "uploadlimitexceeded", "dailylimit")):
        return "quota"
    if any(value in text for value in ("unauthorized", "invalid_grant", "authentication", "credentials", "oauth", "401", "403")):
        return "auth"
    if any(value in text for value in ("timeout", "timed out", "temporar", "connection", "429", "500", "502", "503", "504")):
        return "transient"
    return "permanent"


def _default_generate(task_id: str, params: VideoParams) -> Any:
    return tm.start(
        task_id=task_id,
        params=params,
        suppress_youtube_upload=True,
        suppress_tiktok_upload=True,
    )


def _default_upload(item: dict, slot: dict, settings: dict) -> dict:
    from app.services.youtube_uploader import parse_metadata_file, youtube_uploader

    publish_at = slot.get("publish_at", "") if settings.get("scheduled") else ""
    metadata_path = item.get("metadata_path", "")
    if not metadata_path and item.get("video_path"):
        metadata_path = os.path.join(os.path.dirname(item["video_path"]), "METADATOS.md")
    metadata = parse_metadata_file(metadata_path)
    subject = item["subject"]
    return youtube_uploader.upload_video(
        video_path=item["video_path"],
        title=item.get("title_override") or metadata.get("title") or subject,
        description=metadata.get("description") or subject,
        tags=metadata.get("tags") or ["#shorts"],
        publish_at=publish_at,
        privacy_status=settings.get("privacy_status", ""),
        task_id=item.get("task_id", ""),
        index=int(item.get("upload_index", 1)),
    )


class YouTubeBatchRunner:
    def __init__(
        self,
        store: YouTubeBatchStore | None = None,
        generate: GenerateFunction | None = None,
        upload: UploadFunction | None = None,
        retry_attempts: int = 3,
        retry_backoff_seconds: float = 1.0,
        sleep: SleepFunction = time.sleep,
        now: Callable[[], datetime] | None = None,
        allocate_slot: SlotFunction | None = None,
    ):
        self.store = store or youtube_batch_store
        self.generate = generate or _default_generate
        self.upload = upload or _default_upload
        self.retry_attempts = max(1, int(retry_attempts))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self.sleep = sleep
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.allocate_slot = allocate_slot or self._default_allocate_slot
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()
        self._monitor_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None

    def start(self, batch_id: str, explicit_resume: bool = False) -> dict:
        batch = self._required(batch_id)
        with self._lock:
            thread = self._threads.get(batch_id)
            if thread and thread.is_alive():
                return batch
            thread = threading.Thread(
                target=self.run,
                args=(batch_id, explicit_resume),
                daemon=True,
                name=f"youtube-batch-{batch_id}",
            )
            self._threads[batch_id] = thread
            thread.start()
        return self.store.load(batch_id)

    def pause(self, batch_id: str) -> dict:
        return self._control(batch_id, "paused")

    def cancel(self, batch_id: str) -> dict:
        return self._control(batch_id, "cancelled")

    def retry(self, batch_id: str) -> dict:
        def reset(batch: dict) -> None:
            for item in batch.get("items", []):
                if item.get("generation_status") == "failed":
                    item["generation_status"] = "pending"
                if item.get("upload_status") in {"failed", "waiting_quota", "waiting_retry", "needs_review"}:
                    item["upload_status"] = "pending"
                    item["retry_count"] = int(item.get("retry_count", 0)) + 1
                item.update(
                    error="", failure_type="", retryable=False, requires_resume=False,
                    automatic_retry_count=0, automatic_retries_exhausted=False,
                    next_retry_at="", deferred_retry_count=0,
                )
            batch.update(control="running", status="pending")

        self.store.mutate(batch_id, reset)
        return self.start(batch_id)

    def shutdown(self) -> None:
        self._monitor_stop.set()

    def resume_pending(self) -> None:
        self.start_monitor()
        for batch in self.store.list_batches(10000):
            if (
                batch.get("settings", {}).get("runner_managed")
                and batch.get("control", "running") == "running"
                and batch.get("status") not in {"completed", "cancelled"}
            ):
                self.start(batch["batch_id"], explicit_resume=False)

    def start_monitor(self, interval_seconds: float = 60.0) -> None:
        with self._lock:
            if self._monitor_thread and self._monitor_thread.is_alive():
                return
            self._monitor_stop.clear()
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                args=(max(1.0, float(interval_seconds)),),
                daemon=True,
                name="youtube-batch-retry-monitor",
            )
            self._monitor_thread.start()

    def _monitor_loop(self, interval_seconds: float) -> None:
        while not self._monitor_stop.wait(interval_seconds):
            for batch in self.store.list_batches(10000):
                if (
                    batch.get("settings", {}).get("runner_managed")
                    and batch.get("control", "running") == "running"
                    and self._has_due_retry(batch)
                ):
                    self.start(batch["batch_id"], explicit_resume=False)

    def _has_due_retry(self, batch: dict) -> bool:
        return any(
            item.get("upload_status") in {"waiting_quota", "waiting_retry"}
            and self._retry_due(item.get("next_retry_at", ""))
            for item in batch.get("items", [])
        )

    def reconcile(self, batch_id: str) -> dict:
        def apply(batch: dict) -> None:
            for item in batch.get("items", []):
                if item.get("generation_status") == "generating":
                    output = self._task_output(item)
                    if output:
                        item.update(generation_status="generated", video_path=output, error="")
                    else:
                        item.update(generation_status="pending", error="generation interrupted")
                if item.get("upload_status") == "uploading":
                    item.update(
                        upload_status="needs_review",
                        failure_type="unknown",
                        retryable=False,
                        error="upload interrupted; verify YouTube before retrying",
                    )

        return self.store.mutate(batch_id, apply)

    def run(self, batch_id: str, explicit_resume: bool = False) -> dict:
        with self.store.execution_lock(batch_id) as acquired:
            if not acquired:
                return self._required(batch_id)
            def prepare(batch: dict) -> None:
                batch.setdefault("settings", {})["runner_managed"] = True
                if explicit_resume:
                    batch["control"] = "running"
                    for item in batch.get("items", []):
                        if item.get("requires_resume"):
                            item.update(
                                upload_status="pending", requires_resume=False,
                                failure_type="", error="",
                            )
                if explicit_resume and batch.get("status") in {"paused", "cancelled"}:
                    batch["status"] = "pending"

            self.store.mutate(batch_id, prepare)
            return self._run_locked(batch_id)

    def _run_locked(self, batch_id: str) -> dict:
        batch = self.reconcile(batch_id)
        if batch.get("execution_mode", "interleaved") == "generate_all_first":
            for index in range(len(batch.get("items", []))):
                if not self._can_continue(batch_id):
                    return self.store.load(batch_id)
                self._generate_one(batch_id, index)
            for index in range(len(batch.get("items", []))):
                if not self._can_continue(batch_id):
                    return self.store.load(batch_id)
                self._upload_one(batch_id, index)
        else:
            for index in range(len(batch.get("items", []))):
                if not self._can_continue(batch_id):
                    return self.store.load(batch_id)
                self._generate_one(batch_id, index)
                if not self._can_continue(batch_id):
                    return self.store.load(batch_id)
                self._upload_one(batch_id, index)
        return self.store.mutate(
            batch_id,
            lambda current: current.update(status=self._final_status(current)),
        )

    def _generate_one(self, batch_id: str, index: int) -> None:
        batch = self.store.load(batch_id)
        item = batch["items"][index]
        output = self._task_output(item)
        if item.get("generation_status") == "generated" and output:
            if not item.get("video_path"):
                self.store.update_item(batch, index, video_path=output)
            return
        if item.get("generation_status") == "generated" or item.get("upload_status") in {"uploaded", "scheduled"}:
            return
        task_id = item.get("task_id") or str(uuid4())
        self.store.update_item(batch, index, task_id=task_id, generation_status="generating", error="")
        settings = batch.get("settings", {})
        raw_params = dict(settings.get("video_params", {}))
        raw_params["video_subject"] = item["subject"]
        raw_params["video_count"] = 1
        try:
            params = VideoParams(**raw_params)
            result = self.generate(task_id, params)
            output = self._result_output(result) or self._task_output({**item, "task_id": task_id})
            if not output:
                raise RuntimeError("video generation returned no output")
            batch = self.store.load(batch_id)
            metadata_path = os.path.join(os.path.dirname(output), "METADATOS.md")
            generated_title = ""
            if os.path.isfile(metadata_path):
                from app.services.youtube_uploader import parse_metadata_file

                generated_title = parse_metadata_file(metadata_path).get("title", "")
            self.store.update_item(
                batch,
                index,
                generation_status="generated",
                video_path=output,
                metadata_path=metadata_path if os.path.isfile(metadata_path) else "",
                generated_title=generated_title,
                quality_status="passed",
                error="",
            )
        except Exception as exc:
            batch = self.store.load(batch_id)
            self.store.update_item(batch, index, generation_status="failed", error=str(exc))

    def _upload_one(self, batch_id: str, index: int) -> None:
        batch = self.store.load(batch_id)
        item = batch["items"][index]
        if item.get("upload_status") in {"uploaded", "scheduled", "needs_review"}:
            return
        if item.get("requires_resume"):
            return
        if item.get("upload_status") in {"waiting_quota", "waiting_retry"}:
            if not self._retry_due(item.get("next_retry_at", "")):
                return
            item["automatic_retries_exhausted"] = False
        if item.get("generation_status") != "generated" or not item.get("video_path"):
            return
        settings = batch.get("settings", {})
        slot = item.get("current_publish_slot", {})
        if settings.get("scheduled") and self._slot_expired(slot):
            try:
                with self.store.execution_lock(
                    "_batch_creation", timeout_seconds=10
                ) as acquired:
                    if not acquired:
                        raise RuntimeError("timed out reserving a replacement publish slot")
                    batch = self.store.load(batch_id)
                    slot = self.allocate_slot(batch, index)
                    if not slot.get("publish_at") or self._slot_expired(slot):
                        raise RuntimeError("replacement publish slot is not in the future")
                    self.store.update_item(
                        batch,
                        index,
                        current_publish_slot=slot,
                        rescheduled_reason="publish slot expired before upload",
                        rescheduled_at=self._iso(self.now()),
                    )
            except Exception as exc:
                self.store.update_item(
                    batch,
                    index,
                    upload_status="failed",
                    failure_type="schedule",
                    retryable=False,
                    error=f"unable to reschedule expired publish slot: {exc}",
                )
                return
        attempts = max(1, int(settings.get("upload_retry_attempts", self.retry_attempts)))
        backoff = max(0.0, float(settings.get("upload_retry_backoff_seconds", self.retry_backoff_seconds)))
        for attempt in range(attempts):
            batch = self.store.load(batch_id)
            item = batch["items"][index]
            self.store.update_item(
                batch,
                index,
                upload_status="uploading",
                automatic_retry_count=attempt,
                error="",
            )
            result: dict = {}
            error = ""
            try:
                result = self.upload(item, slot, settings) or {}
                if result.get("success"):
                    status = "scheduled" if result.get("scheduled") else "uploaded"
                    batch = self.store.load(batch_id)
                    self.store.update_item(
                        batch,
                        index,
                        upload_status=status,
                        youtube_id=result.get("video_id", ""),
                        failure_type="",
                        retryable=False,
                        automatic_retries_exhausted=False,
                        next_retry_at="",
                        requires_resume=False,
                        error="",
                    )
                    return
                error = str(result.get("error") or "YouTube upload failed")
                failure_type = classify_upload_failure(error)
                if result.get("outcome_unknown"):
                    failure_type = "outcome_unknown"
                elif result.get("retryable") and failure_type == "permanent":
                    failure_type = "transient"
            except Exception as exc:
                error = str(exc)
                failure_type = "outcome_unknown" if getattr(exc, "outcome_unknown", False) else classify_upload_failure(exc)

            if failure_type == "transient" and attempt + 1 < attempts:
                batch = self.store.load(batch_id)
                self.store.update_item(batch, index, automatic_retry_count=attempt + 1, error=error)
                self.sleep(backoff * (2**attempt))
                continue

            status = "failed"
            next_retry_at = ""
            requires_resume = failure_type == "auth"
            exhausted = False
            if failure_type == "quota":
                status = "waiting_quota"
                quota_backoff = max(0.0, float(settings.get("quota_retry_backoff_seconds", 3600)))
                next_retry_at = self._iso(self.now() + timedelta(seconds=quota_backoff))
            elif failure_type == "transient":
                deferred_count = int(item.get("deferred_retry_count", 0)) + 1
                deferred_limit = max(1, int(settings.get("deferred_retry_attempts", 10)))
                if deferred_count < deferred_limit:
                    status = "waiting_retry"
                    deferred_backoff = max(
                        1.0,
                        float(settings.get("deferred_retry_backoff_seconds", 900)),
                    )
                    next_retry_at = self._iso(
                        self.now() + timedelta(seconds=deferred_backoff * deferred_count)
                    )
                else:
                    exhausted = True
            elif failure_type == "outcome_unknown":
                status = "needs_review"
            batch = self.store.load(batch_id)
            self.store.update_item(
                batch,
                index,
                upload_status=status,
                failure_type=failure_type,
                retryable=failure_type in {"quota", "transient"},
                automatic_retry_count=attempt,
                automatic_retries_exhausted=exhausted,
                deferred_retry_count=(
                    int(item.get("deferred_retry_count", 0)) + 1
                    if failure_type == "transient"
                    else int(item.get("deferred_retry_count", 0))
                ),
                next_retry_at=next_retry_at,
                requires_resume=requires_resume,
                error=error,
            )
            return

    def _default_allocate_slot(self, batch: dict, index: int) -> dict:
        from app.services.youtube_uploader import build_publish_plan, upload_tracker

        settings = batch.get("settings", {})
        occupied = upload_tracker.future_publish_times()
        for other_batch in self.store.list_batches(10000):
            if other_batch.get("batch_id") == batch.get("batch_id"):
                continue
            for other_item in other_batch.get("items", []):
                value = other_item.get("current_publish_slot", {}).get("publish_at")
                if value:
                    occupied.add(str(value))
        for item_index, item in enumerate(batch.get("items", [])):
            if item_index != index and item.get("current_publish_slot", {}).get("publish_at"):
                occupied.add(str(item["current_publish_slot"]["publish_at"]))
        return build_publish_plan(
            1,
            schedule_mode=settings.get("schedule_mode", "interval"),
            schedule_at=settings.get("schedule_at", "21:00"),
            videos_per_day=int(settings.get("videos_per_day", 4)),
            interval_minutes=int(settings.get("schedule_interval_minutes", 15)),
            timezone_name=settings.get("timezone", "local"),
            now=self.now(),
            occupied_publish_at=occupied,
            occupied_counts_toward_daily_capacity=False,
            allow_shared_publish_time=bool(
                settings.get("allow_shared_publish_time", False)
            ),
        )[0]

    def _slot_expired(self, slot: dict) -> bool:
        try:
            publish_at = datetime.fromisoformat(str(slot.get("publish_at", "")).replace("Z", "+00:00"))
            if publish_at.tzinfo is None:
                publish_at = publish_at.replace(tzinfo=timezone.utc)
            return publish_at <= self.now().astimezone(timezone.utc)
        except (TypeError, ValueError):
            return True

    def _retry_due(self, value: str) -> bool:
        try:
            retry_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return retry_at <= self.now().astimezone(timezone.utc)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _task_output(self, item: dict) -> str:
        path = item.get("video_path", "")
        if path and os.path.isfile(path):
            return path
        task_id = item.get("task_id", "")
        task = sm.state.get_task(task_id) if task_id else None
        output = self._result_output(task)
        if output:
            return output
        if task_id:
            # Memory task state is lost on restart, but scanner metadata is durable.
            from app.services.youtube_uploader import scan_pending_videos

            for video in scan_pending_videos().get("videos", []):
                if video.get("task_id") == task_id and video.get("video_path"):
                    return str(video["video_path"])
        return ""

    @staticmethod
    def _result_output(result: Any) -> str:
        videos = result.get("videos", []) if isinstance(result, dict) else []
        return str(videos[0]) if videos else ""

    def _can_continue(self, batch_id: str) -> bool:
        batch = self.store.load(batch_id)
        control = batch.get("control", "running")
        if control in {"paused", "cancelled"}:
            self.store.mutate(batch_id, lambda current: current.update(status=control))
            return False
        return True

    def _control(self, batch_id: str, control: str) -> dict:
        return self.store.mutate(
            batch_id,
            lambda batch: batch.update(control=control, status=control),
        )

    def _required(self, batch_id: str) -> dict:
        batch = self.store.load(batch_id)
        if not batch:
            raise KeyError(batch_id)
        return batch

    @staticmethod
    def _final_status(batch: dict) -> str:
        items = batch.get("items", [])
        if items and all(item.get("upload_status") in {"uploaded", "scheduled"} for item in items):
            return "completed"
        if any(item.get("generation_status") == "failed" for item in items):
            return "generation_incomplete"
        return "upload_incomplete"


youtube_batch_runner = YouTubeBatchRunner()
