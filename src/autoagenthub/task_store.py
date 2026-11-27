"""Durable SQLite WAL-backed task queue, dead-letter queue, and metrics store."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DurableTaskStore:
    """Thread-safe, WAL-enabled SQLite store for concurrent task execution and DLQ."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    task_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 5,
                    status TEXT NOT NULL DEFAULT 'queued',
                    dedupe_key TEXT UNIQUE,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 3,
                    worker_id TEXT,
                    lease_expires_at TEXT,
                    result_text TEXT,
                    error_text TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_status_prio
                    ON tasks(status, priority ASC, created_at ASC);

                CREATE TABLE IF NOT EXISTS dead_letter_queue (
                    id TEXT PRIMARY KEY,
                    original_task_id TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    error_text TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    failed_at TEXT NOT NULL
                );
                """
            )

    async def enqueue(
        self,
        task_type: str,
        payload: dict[str, Any],
        priority: int = 5,
        max_retries: int = 3,
        dedupe_key: str | None = None,
    ) -> dict[str, Any]:
        """Insert a task into the durable queue, honoring idempotency via dedupe_key."""
        async with self._lock:
            return await asyncio.to_thread(
                self._enqueue_sync, task_type, payload, priority, max_retries, dedupe_key
            )

    def _enqueue_sync(
        self,
        task_type: str,
        payload: dict[str, Any],
        priority: int,
        max_retries: int,
        dedupe_key: str | None,
    ) -> dict[str, Any]:
        now = _utc_now().isoformat()
        with self._connect() as conn:
            if dedupe_key:
                existing = conn.execute(
                    "SELECT * FROM tasks WHERE dedupe_key = ?", (dedupe_key,)
                ).fetchone()
                if existing:
                    return dict(existing)

            task_id = uuid.uuid4().hex[:12]
            conn.execute(
                """
                INSERT INTO tasks (
                    id, task_type, payload_json, priority, status, dedupe_key,
                    attempts, max_retries, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, 0, ?, ?, ?)
                """,
                (task_id, task_type, json.dumps(payload), priority, dedupe_key, max_retries, now, now),
            )
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return dict(row)

    async def claim_next(self, worker_id: str, lease_seconds: int = 120) -> dict[str, Any] | None:
        """Atomically claim the highest-priority queued (or lease-expired) task."""
        async with self._lock:
            return await asyncio.to_thread(self._claim_next_sync, worker_id, lease_seconds)

    def _claim_next_sync(self, worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
        now_dt = _utc_now()
        now = now_dt.isoformat()
        lease_until = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            row = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status = 'queued'
                   OR (status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?)
                ORDER BY priority ASC, created_at ASC
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if not row:
                conn.commit()
                return None

            conn.execute(
                """
                UPDATE tasks
                SET status = 'running',
                    worker_id = ?,
                    lease_expires_at = ?,
                    attempts = attempts + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (worker_id, lease_until, now, row["id"]),
            )
            updated = conn.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()
            conn.commit()
            return dict(updated)

    async def mark_completed(self, task_id: str, result_text: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._mark_completed_sync, task_id, result_text)

    def _mark_completed_sync(self, task_id: str, result_text: str) -> None:
        now = _utc_now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = 'completed',
                    result_text = ?,
                    lease_expires_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (result_text, now, task_id),
            )

    async def mark_failed_or_retry(self, task_id: str, error_text: str) -> str:
        """Re-queue if attempts < max_retries, otherwise move to Dead-Letter Queue."""
        async with self._lock:
            return await asyncio.to_thread(self._mark_failed_sync, task_id, error_text)

    def _mark_failed_sync(self, task_id: str, error_text: str) -> str:
        now = _utc_now().isoformat()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                return "missing"

            if row["attempts"] < row["max_retries"]:
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'queued',
                        error_text = ?,
                        worker_id = NULL,
                        lease_expires_at = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (error_text, now, task_id),
                )
                return "requeued"

            conn.execute(
                """
                UPDATE tasks
                SET status = 'failed',
                    error_text = ?,
                    lease_expires_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (error_text, now, task_id),
            )
            dlq_id = uuid.uuid4().hex[:12]
            conn.execute(
                """
                INSERT INTO dead_letter_queue (
                    id, original_task_id, task_type, payload_json, error_text, attempts, failed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dlq_id,
                    row["id"],
                    row["task_type"],
                    row["payload_json"],
                    error_text,
                    row["attempts"],
                    now,
                ),
            )
            return "dead_lettered"

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        async with self._lock:
            return await asyncio.to_thread(self._get_task_sync, task_id)

    def _get_task_sync(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return dict(row) if row else None

    async def list_dlq(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self._lock:
            return await asyncio.to_thread(self._list_dlq_sync, limit)

    def _list_dlq_sync(self, limit: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM dead_letter_queue ORDER BY failed_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    async def list_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        """List recent tasks ordered by creation time descending."""
        async with self._lock:
            return await asyncio.to_thread(self._list_tasks_sync, limit)

    def _list_tasks_sync(self, limit: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, task_type, priority, status, attempts, max_retries, created_at, updated_at "
                "FROM tasks ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    async def retry_dlq_item(self, dlq_id: str) -> dict[str, Any] | None:
        """Re-enqueue a dead-lettered task and remove it from the DLQ."""
        async with self._lock:
            return await asyncio.to_thread(self._retry_dlq_sync, dlq_id)

    def _retry_dlq_sync(self, dlq_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM dead_letter_queue WHERE id = ?", (dlq_id,)
            ).fetchone()
            if not row:
                return None
            new_task = self._enqueue_sync(
                task_type=row["task_type"],
                payload=json.loads(row["payload_json"]),
                priority=3,
                max_retries=3,
                dedupe_key=None,
            )
            conn.execute("DELETE FROM dead_letter_queue WHERE id = ?", (dlq_id,))
            return new_task

    async def get_stats(self) -> dict[str, int]:
        async with self._lock:
            return await asyncio.to_thread(self._get_stats_sync)

    def _get_stats_sync(self) -> dict[str, int]:
        with self._connect() as conn:
            counts = {"queued": 0, "running": 0, "completed": 0, "failed": 0, "dlq": 0}
            for row in conn.execute("SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status"):
                counts[row["status"]] = row["cnt"]
            dlq_row = conn.execute("SELECT COUNT(*) as cnt FROM dead_letter_queue").fetchone()
            counts["dlq"] = dlq_row["cnt"] if dlq_row else 0
            return counts
