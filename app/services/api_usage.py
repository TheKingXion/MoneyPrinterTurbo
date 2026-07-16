"""Privacy-safe, best-effort metering for model API calls."""

from __future__ import annotations

import contextlib
import contextvars
import math
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

from loguru import logger

from app.utils import utils


_usage_context: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar(
    "api_usage_context", default=("other", "model_request")
)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _integer(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def estimate_tokens(text: str | None) -> int:
    """Conservative fallback when a provider does not expose token usage."""
    value = str(text or "").strip()
    return 0 if not value else max(1, math.ceil(len(value.encode("utf-8")) / 4))


def extract_token_usage(response: Any) -> dict[str, int] | None:
    usage = _field(response, "usage") or _field(response, "usage_metadata")
    if usage is None:
        return None
    input_tokens = _integer(
        _field(usage, "prompt_tokens")
        or _field(usage, "input_tokens")
        or _field(usage, "prompt_token_count")
        or _field(usage, "input_token_count")
    )
    output_tokens = _integer(
        _field(usage, "completion_tokens")
        or _field(usage, "output_tokens")
        or _field(usage, "candidates_token_count")
        or _field(usage, "output_token_count")
    )
    total_tokens = _integer(
        _field(usage, "total_tokens") or _field(usage, "total_token_count")
    ) or input_tokens + output_tokens
    prompt_details = _field(usage, "prompt_tokens_details") or {}
    completion_details = _field(usage, "completion_tokens_details") or {}
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": _integer(
            _field(prompt_details, "cached_tokens")
            or _field(usage, "cached_content_token_count")
        ),
        "reasoning_tokens": _integer(
            _field(completion_details, "reasoning_tokens")
            or _field(usage, "thoughts_token_count")
        ),
    }


@contextlib.contextmanager
def usage_context(category: str, operation: str) -> Iterator[None]:
    token = _usage_context.set((category, operation))
    try:
        yield
    finally:
        _usage_context.reset(token)


class ApiUsageStore:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or Path(utils.storage_dir()) / "api_usage.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=2)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=2000")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS api_usage_events (
                    id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    category TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    total_tokens INTEGER NOT NULL,
                    cached_tokens INTEGER NOT NULL,
                    reasoning_tokens INTEGER NOT NULL,
                    estimated INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    duration_seconds REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS api_usage_created_idx
                    ON api_usage_events(created_at);
                CREATE INDEX IF NOT EXISTS api_usage_provider_idx
                    ON api_usage_events(provider, model);
                CREATE INDEX IF NOT EXISTS api_usage_category_idx
                    ON api_usage_events(category, operation);
                """
            )

    def record(
        self,
        *,
        provider: str,
        model: str,
        prompt: str = "",
        output: str = "",
        response: Any = None,
        category: str | None = None,
        operation: str | None = None,
        status: str = "ok",
        duration_seconds: float = 0,
    ) -> None:
        current_category, current_operation = _usage_context.get()
        usage = extract_token_usage(response)
        estimated = usage is None
        usage = usage or {
            "input_tokens": estimate_tokens(prompt),
            "output_tokens": estimate_tokens(output),
            "total_tokens": estimate_tokens(prompt) + estimate_tokens(output),
            "cached_tokens": 0,
            "reasoning_tokens": 0,
        }
        values = (
            uuid.uuid4().hex,
            time.time(),
            str(provider or "unknown"),
            str(model or "unknown"),
            str(category or current_category),
            str(operation or current_operation),
            usage["input_tokens"],
            usage["output_tokens"],
            usage["total_tokens"],
            usage["cached_tokens"],
            usage["reasoning_tokens"],
            int(estimated),
            status,
            max(0.0, float(duration_seconds or 0)),
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO api_usage_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )

    def filters(self, since: float | None = None) -> dict[str, list[str]]:
        where, params = self._where(since=since)
        result = {}
        with self._connect() as connection:
            for column in ("provider", "model", "category"):
                rows = connection.execute(
                    f"SELECT DISTINCT {column} FROM api_usage_events {where} ORDER BY {column}",
                    params,
                ).fetchall()
                result[column] = [str(row[0]) for row in rows]
        return result

    @staticmethod
    def _where(
        *, since: float | None = None, providers=(), models=(), categories=()
    ) -> tuple[str, list[Any]]:
        clauses, params = [], []
        for column, values in (
            ("provider", providers), ("model", models), ("category", categories)
        ):
            if values:
                clauses.append(f"{column} IN ({','.join('?' for _ in values)})")
                params.extend(values)
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
        return ("WHERE " + " AND ".join(clauses) if clauses else ""), params

    def report(self, *, since=None, providers=(), models=(), categories=()) -> dict:
        where, params = self._where(
            since=since, providers=providers, models=models, categories=categories
        )
        totals_sql = f"""SELECT COUNT(*) requests,
            COALESCE(SUM(input_tokens),0) input_tokens,
            COALESCE(SUM(output_tokens),0) output_tokens,
            COALESCE(SUM(total_tokens),0) total_tokens,
            COALESCE(SUM(estimated),0) estimated_requests,
            COALESCE(SUM(CASE WHEN status != 'ok' THEN 1 ELSE 0 END),0) failed_requests
            FROM api_usage_events {where}"""
        group_sql = lambda fields: f"""SELECT {fields}, COUNT(*) requests,
            SUM(input_tokens) input_tokens, SUM(output_tokens) output_tokens,
            SUM(total_tokens) total_tokens, SUM(estimated) estimated_requests,
            ROUND(SUM(duration_seconds),2) duration_seconds
            FROM api_usage_events {where} GROUP BY {fields}
            ORDER BY total_tokens DESC"""
        recent_sql = f"""SELECT created_at, provider, model, category, operation,
            input_tokens, output_tokens, total_tokens, estimated, status, duration_seconds
            FROM api_usage_events {where} ORDER BY created_at DESC LIMIT 100"""
        with self._connect() as connection:
            return {
                "totals": dict(connection.execute(totals_sql, params).fetchone()),
                "by_model": [dict(row) for row in connection.execute(group_sql("provider, model"), params)],
                "by_category": [dict(row) for row in connection.execute(group_sql("category, operation"), params)],
                "recent": [dict(row) for row in connection.execute(recent_sql, params)],
            }


_store: ApiUsageStore | None = None


def get_api_usage_store() -> ApiUsageStore:
    global _store
    if _store is None:
        _store = ApiUsageStore()
    return _store


def record_api_call(**values: Any) -> None:
    try:
        get_api_usage_store().record(**values)
    except Exception as exc:  # Metering must never interrupt video generation.
        logger.warning(f"could not record API usage: {exc}")
