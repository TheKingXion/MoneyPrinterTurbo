import errno
import json
import os
import re
import threading
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from difflib import SequenceMatcher
from itertools import product
from typing import Any, Iterable
from uuid import uuid4

from app.utils import utils

SCHEMA_VERSION = 4
EXECUTION_MODES = {"interleaved", "generate_all_first"}
IDEA_MODES = {"ai", "manual", "legacy"}

_REPLACE_RETRY_DELAYS = (
    0.05, 0.1, 0.2, 0.4, 0.8,
    1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0,
)

_IDEA_STOPWORDS = {
    "a", "al", "con", "de", "del", "el", "ella", "en", "la", "las", "lo",
    "los", "para", "por", "que", "se", "su", "sus", "un", "una", "unos", "unas",
    "joven", "persona", "historia", "video",
}
_IDEA_CANONICAL = {
    "barrio": "comunidad",
    "vecindario": "comunidad",
    "comunitaria": "comunidad",
    "comunitario": "comunidad",
    "estanteria": "biblioteca",
    "libreria": "biblioteca",
    "libros": "libro",
    "desechado": "reciclado",
    "desechados": "reciclado",
    "desechada": "reciclado",
    "desechadas": "reciclado",
    "usado": "reciclado",
    "usados": "reciclado",
    "usada": "reciclado",
    "usadas": "reciclado",
    "convierte": "crear",
    "convirtiendo": "crear",
    "transformo": "crear",
    "convirtio": "crear",
    "creo": "crear",
    "construyo": "crear",
    "gratuita": "gratis",
    "gratuito": "gratis",
}


_OPENINGS = [
    "La historia desconocida de",
    "El secreto detrás de",
    "El descubrimiento inesperado de",
    "La decisión que transformó",
    "El misterio que rodeó",
    "La idea improbable de",
    "El error que cambió",
    "La señal que reveló",
]

_SUBJECTS = [
    "una ciudad olvidada",
    "un invento adelantado a su época",
    "una expedición científica",
    "una comunidad aislada",
    "un archivo perdido",
    "una máquina abandonada",
    "una ruta comercial antigua",
    "un experimento sencillo",
    "una fotografía centenaria",
    "una tradición que sobrevivió al tiempo",
]

_ENDINGS = [
    "y sorprendió a los investigadores",
    "cuando nadie esperaba encontrar respuestas",
    "y terminó resolviendo un problema enorme",
    "después de décadas sin explicación",
    "y conectó dos mundos diferentes",
    "gracias a un detalle que todos ignoraron",
    "y dejó una lección que todavía usamos",
    "cuando una coincidencia cambió el resultado",
]


def normalize_idea_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(re.findall(r"[^\W_]+", text.casefold(), flags=re.UNICODE))


def _idea_tokens(value: str) -> set[str]:
    tokens = set()
    for token in normalize_idea_text(value).split():
        if token in _IDEA_STOPWORDS:
            continue
        canonical = _IDEA_CANONICAL.get(token, token)
        if len(canonical) > 5 and canonical.endswith("es"):
            canonical = canonical[:-2]
        elif len(canonical) > 4 and canonical.endswith("s"):
            canonical = canonical[:-1]
        tokens.add(canonical)
    return tokens


def idea_similarity(left: str, right: str) -> float:
    normalized_left = normalize_idea_text(left)
    normalized_right = normalize_idea_text(right)
    if not normalized_left or not normalized_right:
        return 0.0
    if normalized_left == normalized_right:
        return 1.0
    sequence = SequenceMatcher(None, normalized_left, normalized_right).ratio()
    left_tokens = _idea_tokens(left)
    right_tokens = _idea_tokens(right)
    if not left_tokens or not right_tokens:
        return sequence
    intersection = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    jaccard = intersection / union
    containment = intersection / min(len(left_tokens), len(right_tokens))
    semantic = (
        (jaccard * 0.7) + (containment * 0.3)
        if min(len(left_tokens), len(right_tokens)) >= 3
        else jaccard
    )
    if sequence >= 0.92 and semantic >= 0.5:
        return sequence
    return semantic


def find_duplicate_idea(candidate: str, existing: Iterable[str], threshold: float = 0.74) -> tuple[str, float] | None:
    best_value = ""
    best_score = 0.0
    for value in existing:
        score = idea_similarity(candidate, value)
        if score > best_score:
            best_value, best_score = str(value), score
    return (best_value, best_score) if best_score >= threshold else None


def validate_unique_ideas(candidates: Iterable[str], existing: Iterable[str] = ()) -> list[dict]:
    accepted = [str(value).strip() for value in existing if str(value).strip()]
    results = []
    for candidate in candidates:
        value = " ".join(str(candidate).split()).strip(" .")
        duplicate = find_duplicate_idea(value, accepted) if value else None
        results.append(
            {
                "subject": value,
                "duplicate": bool(duplicate),
                "duplicate_of": duplicate[0] if duplicate else "",
                "similarity": round(duplicate[1], 3) if duplicate else 0.0,
            }
        )
        if value and not duplicate:
            accepted.append(value)
    return results


def audit_story_idea(subject: str, title: str = "") -> dict:
    normalized = normalize_idea_text(subject)
    combined = f"{normalized} {normalize_idea_text(title)}"
    tokens = normalized.split()
    score = 100
    issues = []
    generic_patterns = (
        "la historia desconocida de",
        "el secreto detras de",
        "un invento adelantado a su epoca",
        "una ciudad olvidada",
    )
    unsupported_patterns = (
        "salvo al mundo",
        "reescribio la historia",
        "tecnologia imposible",
        "nadie puede explicar",
        "desafia toda la historia",
    )
    action_terms = {
        "ayudo", "adapto", "construyo", "convirtio", "creo", "enseno", "instalo",
        "organizo", "recolecto", "recupero", "reparo", "transformo", "crear",
    }
    if len(tokens) < 8:
        score -= 35
        issues.append("La premisa es demasiado breve")
    elif len(tokens) < 14:
        score -= 15
        issues.append("Conviene agregar un obstáculo y un resultado concreto")
    if any(pattern in normalized for pattern in generic_patterns):
        score -= 40
        issues.append("Usa una plantilla genérica de misterio")
    if any(pattern in combined for pattern in unsupported_patterns):
        score -= 30
        issues.append("Contiene una afirmación sensacional difícil de sustentar")
    if not (_idea_tokens(subject) & action_terms):
        score -= 15
        issues.append("Falta una acción concreta del protagonista")
    if title and len(normalize_idea_text(title).split()) < 5:
        score -= 10
        issues.append("El título es poco específico")
    score = max(0, min(score, 100))
    return {
        "score": score,
        "status": "approved" if score >= 60 else "review",
        "issues": issues,
    }


def collect_task_subjects(tasks_root: str = "") -> list[str]:
    root = tasks_root or utils.task_dir()
    subjects = []
    try:
        task_names = os.listdir(root)
    except OSError:
        return subjects
    for task_name in task_names:
        script_path = os.path.join(root, task_name, "script.json")
        try:
            with open(script_path, "r", encoding="utf-8") as file:
                data = json.load(file)
            subject = str((data.get("params") or {}).get("video_subject", "")).strip()
            if subject:
                subjects.append(subject)
        except (OSError, json.JSONDecodeError, TypeError):
            continue
    return subjects


def build_unique_batch_subjects(
    total: int,
    existing_subjects: Iterable[str] = (),
    preferred_subjects: Iterable[str] = (),
) -> list[str]:
    total = int(total)
    if total < 1:
        raise ValueError("Batch video count must be at least 1")

    used = {str(subject).strip().casefold() for subject in existing_subjects if str(subject).strip()}
    selected: list[str] = []

    def add(subject: str) -> None:
        cleaned = " ".join(str(subject).split()).strip(" .")
        key = cleaned.casefold()
        if cleaned and key not in used and len(selected) < total:
            used.add(key)
            selected.append(cleaned)

    for subject in preferred_subjects:
        add(subject)
    for opening, subject, ending in product(_OPENINGS, _SUBJECTS, _ENDINGS):
        add(f"{opening} {subject} {ending}")
        if len(selected) == total:
            break

    if len(selected) != total:
        raise RuntimeError(f"Unable to prepare {total} unique batch subjects")
    return selected


def videos_per_day_for_days(total_videos: int, total_days: int) -> int:
    total_videos = int(total_videos)
    total_days = int(total_days)
    if total_videos < 1:
        raise ValueError("total_videos must be at least 1")
    if total_days < 1 or total_days > total_videos:
        raise ValueError("total_days must be between 1 and total_videos")
    return (total_videos + total_days - 1) // total_days


class YouTubeBatchStore:
    def __init__(self, directory: str = ""):
        self.directory = directory or os.path.join(utils.storage_dir(), "youtube_batches")
        self._lock = threading.RLock()

    def _path(self, batch_id: str) -> str:
        return os.path.join(self.directory, f"{batch_id}.json")

    def _execution_lock_path(self, batch_id: str) -> str:
        return f"{self._path(batch_id)}.run.lock"

    @contextmanager
    def execution_lock(self, batch_id: str, timeout_seconds: float = 0.0):
        """Nonblocking process-wide ownership for one batch execution."""
        os.makedirs(self.directory, exist_ok=True)
        path = self._execution_lock_path(batch_id)
        lock_file = open(path, "a+b")
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        acquired = False
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        try:
            yield acquired
        finally:
            if acquired:
                lock_file.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()

    def create(
        self,
        subjects: list[str],
        publish_plan: list[dict[str, Any]],
        settings: dict[str, Any],
        execution_mode: str = "interleaved",
        *,
        title_overrides: list[str] | None = None,
        idea_mode: str = "manual",
        blocked_subjects: Iterable[str] = (),
    ) -> dict:
        with self.execution_lock("_batch_creation") as acquired:
            if not acquired:
                raise RuntimeError("Another process is preparing a YouTube batch")
            return self._create_locked(
                subjects,
                publish_plan,
                settings,
                execution_mode,
                title_overrides=title_overrides,
                idea_mode=idea_mode,
                blocked_subjects=blocked_subjects,
            )

    def _create_locked(
        self,
        subjects: list[str],
        publish_plan: list[dict[str, Any]],
        settings: dict[str, Any],
        execution_mode: str = "interleaved",
        *,
        title_overrides: list[str] | None = None,
        idea_mode: str = "manual",
        blocked_subjects: Iterable[str] = (),
    ) -> dict:
        if len(subjects) != len(publish_plan) and publish_plan:
            raise ValueError("Batch subjects and publish plan must have the same length")
        if settings.get("scheduled") and len(publish_plan) != len(subjects):
            raise ValueError("Scheduled batches require one publish slot per subject")
        if execution_mode not in EXECUTION_MODES:
            raise ValueError(f"Unsupported execution mode: {execution_mode}")
        if idea_mode not in IDEA_MODES - {"legacy"}:
            raise ValueError(f"Unsupported idea mode: {idea_mode}")
        title_overrides = list(title_overrides or [""] * len(subjects))
        if len(title_overrides) != len(subjects):
            raise ValueError("Batch title overrides must match the subject count")
        existing_subjects = [
            item.get("subject", "")
            for batch in self.list_batches(10000)
            for item in batch.get("items", [])
        ]
        validation = validate_unique_ideas(subjects, [*blocked_subjects, *existing_subjects])
        duplicates = [item for item in validation if item["duplicate"]]
        if duplicates:
            first = duplicates[0]
            raise ValueError(
                f"Duplicate batch idea: {first['subject']} (similar to: {first['duplicate_of']})"
            )
        requested_slots = [
            str(slot.get("publish_at", "")).strip() for slot in publish_plan
        ]
        allow_shared_publish_time = bool(settings.get("allow_shared_publish_time", False))
        if settings.get("scheduled") and any(not value for value in requested_slots):
            raise ValueError("Scheduled batch slots require publish_at")
        if (
            not allow_shared_publish_time
            and len(set(requested_slots)) != len([value for value in requested_slots if value])
        ):
            raise ValueError("Batch publish plan contains duplicate slots")
        reserved_slots = {
            str(item.get("current_publish_slot", {}).get("publish_at", "")).strip()
            for batch in self.list_batches(10000)
            if batch.get("status") != "cancelled"
            for item in batch.get("items", [])
        }
        try:
            from app.services.youtube_uploader import upload_tracker

            reserved_slots.update(upload_tracker.future_publish_times())
        except Exception:
            pass
        collision = next((value for value in requested_slots if value in reserved_slots), "")
        if collision and not allow_shared_publish_time:
            raise ValueError(f"YouTube schedule slot is already reserved: {collision}")
        batch_id = str(uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        items = []
        for index, subject in enumerate(subjects):
            slot = dict(publish_plan[index]) if publish_plan else {}
            items.append(
                {
                    "index": index,
                    "subject": subject,
                    "subject_fingerprint": normalize_idea_text(subject),
                    "title_override": str(title_overrides[index]).strip()[:100],
                    "generated_title": "",
                    "quality_status": "pending",
                    "task_id": "",
                    "upload_index": 1,
                    "video_path": "",
                    "generation_status": "pending",
                    "upload_status": "pending",
                    # publish_slot remains the compatibility alias used by the UI.
                    "publish_slot": dict(slot),
                    "original_publish_slot": dict(slot),
                    "current_publish_slot": dict(slot),
                    "youtube_id": "",
                    "error": "",
                    "failure_type": "",
                    "retryable": False,
                    "retry_count": 0,
                    "automatic_retry_count": 0,
                    "automatic_retries_exhausted": False,
                    "next_retry_at": "",
                    "requires_resume": False,
                    "rescheduled_reason": "",
                    "updated_at": created_at,
                }
            )
        batch = {
            "schema_version": SCHEMA_VERSION,
            "batch_id": batch_id,
            "status": "pending",
            "execution_mode": execution_mode,
            "idea_mode": idea_mode,
            "requested": len(subjects),
            "created_at": created_at,
            "updated_at": created_at,
            "settings": settings,
            "items": items,
        }
        self.save(batch)
        return batch

    def load(self, batch_id: str) -> dict:
        try:
            with open(self._path(batch_id), "r", encoding="utf-8") as file:
                data = json.load(file)
            return self._normalize(data) if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self, batch: dict) -> None:
        with self._lock:
            batch = self._normalize(batch)
            os.makedirs(self.directory, exist_ok=True)
            batch["updated_at"] = datetime.now(timezone.utc).isoformat()
            destination = self._path(batch["batch_id"])
            temporary = f"{destination}.{uuid4().hex}.tmp"
            try:
                with open(temporary, "w", encoding="utf-8") as file:
                    json.dump(batch, file, ensure_ascii=False, indent=2)
                self._replace_with_retry(temporary, destination)
            finally:
                try:
                    os.remove(temporary)
                except FileNotFoundError:
                    pass
                except OSError:
                    # A scanner may briefly retain the temporary file too. It is
                    # harmless and can be cleaned by the next storage maintenance.
                    pass

    @staticmethod
    def _replace_with_retry(source: str, destination: str) -> None:
        """Atomically replace a manifest despite transient Windows file locks."""

        delays = (*_REPLACE_RETRY_DELAYS, None)
        for delay in delays:
            try:
                os.replace(source, destination)
                return
            except OSError as exc:
                transient = (
                    isinstance(exc, PermissionError)
                    or getattr(exc, "winerror", None) in {5, 32, 33}
                    or getattr(exc, "errno", None)
                    in {errno.EACCES, errno.EPERM, errno.EBUSY}
                )
                if not transient or delay is None:
                    raise
                time.sleep(delay)

    def _normalize(self, batch: dict) -> dict:
        """Add durable runner fields without invalidating older manifests."""
        if not batch:
            return batch
        source_version = int(batch.get("schema_version", 1) or 1)
        if source_version > SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported YouTube batch schema version: {source_version}"
            )
        batch["schema_version"] = SCHEMA_VERSION
        batch.setdefault("execution_mode", "interleaved")
        batch.setdefault("idea_mode", "legacy")
        batch.setdefault("status", "pending")
        batch.setdefault("settings", {})
        batch.setdefault("items", [])
        batch.setdefault("requested", len(batch["items"]))
        batch.setdefault("control", "running")
        for index, item in enumerate(batch.get("items", [])):
            item.setdefault("index", index)
            item.setdefault("subject", "")
            item.setdefault("task_id", "")
            item.setdefault("video_path", "")
            item.setdefault("generation_status", "pending")
            item.setdefault("upload_status", "pending")
            item.setdefault("updated_at", batch.get("updated_at", batch.get("created_at", "")))
            slot = item.get("current_publish_slot", item.get("publish_slot", {})) or {}
            item.setdefault("original_publish_slot", dict(item.get("publish_slot", slot) or {}))
            item.setdefault("current_publish_slot", dict(slot))
            item["publish_slot"] = dict(item["current_publish_slot"])
            item.setdefault("failure_type", "")
            item.setdefault("upload_index", 1)
            item.setdefault("subject_fingerprint", normalize_idea_text(item.get("subject", "")))
            item.setdefault("title_override", "")
            item.setdefault("generated_title", "")
            item.setdefault("quality_status", "legacy" if batch.get("idea_mode") == "legacy" else "pending")
            item.setdefault("retryable", False)
            item.setdefault("retry_count", 0)
            item.setdefault("automatic_retry_count", 0)
            item.setdefault("automatic_retries_exhausted", False)
            item.setdefault("next_retry_at", "")
            item.setdefault("requires_resume", False)
            item.setdefault("rescheduled_reason", "")
        return batch

    def list_batches(self, limit: int = 10) -> list[dict]:
        try:
            filenames = [
                os.path.join(self.directory, name)
                for name in os.listdir(self.directory)
                if name.endswith(".json")
            ]
        except OSError:
            return []
        filenames.sort(key=os.path.getmtime, reverse=True)
        batches = []
        for filename in filenames[: max(0, int(limit))]:
            batch_id = os.path.splitext(os.path.basename(filename))[0]
            try:
                batch = self.load(batch_id)
            except ValueError:
                continue
            if batch:
                batches.append(batch)
        return batches

    def reserved_publish_slots(self, exclude_batch_id: str = "") -> set[str]:
        reserved = {
            str(item.get("current_publish_slot", {}).get("publish_at", "")).strip()
            for batch in self.list_batches(10000)
            if batch.get("status") != "cancelled"
            and batch.get("batch_id") != exclude_batch_id
            for item in batch.get("items", [])
            if item.get("current_publish_slot", {}).get("publish_at")
        }
        try:
            from app.services.youtube_uploader import upload_tracker

            reserved.update(upload_tracker.future_publish_times())
        except Exception:
            pass
        return reserved

    def update_item(self, batch: dict, index: int, **changes: Any) -> None:
        def apply(current: dict) -> None:
            if "original_publish_slot" in changes:
                existing = current["items"][index].get("original_publish_slot", {})
                if existing and changes["original_publish_slot"] != existing:
                    raise ValueError("original_publish_slot is immutable")
            item_changes = dict(changes)
            if "publish_slot" in item_changes and "current_publish_slot" not in item_changes:
                item_changes["current_publish_slot"] = item_changes["publish_slot"]
            if "current_publish_slot" in item_changes:
                item_changes["publish_slot"] = item_changes["current_publish_slot"]
            item_changes["updated_at"] = datetime.now(timezone.utc).isoformat()
            current["items"][index].update(item_changes)

        updated = self.mutate(batch["batch_id"], apply)
        batch.clear()
        batch.update(updated)

    def set_status(self, batch: dict, status: str) -> None:
        updated = self.mutate(batch["batch_id"], lambda current: current.update(status=status))
        batch.clear()
        batch.update(updated)

    def mutate(self, batch_id: str, callback) -> dict:
        with self.execution_lock(f"{batch_id}.data", timeout_seconds=10) as acquired:
            if not acquired:
                raise RuntimeError("YouTube batch is being updated by another process")
            batch = self.load(batch_id)
            if not batch:
                raise ValueError(f"YouTube batch not found: {batch_id}")
            callback(batch)
            self.save(batch)
            return batch

    def delete(self, batch_id: str) -> None:
        try:
            os.remove(self._path(batch_id))
        except FileNotFoundError:
            pass


youtube_batch_store = YouTubeBatchStore()
