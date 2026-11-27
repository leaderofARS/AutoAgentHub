"""Normalized SQLite WAL database & schema migrations for Personal Intelligence & Execution OS."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS profiles (
    id TEXT PRIMARY KEY,
    user_id TEXT UNIQUE NOT NULL,
    career_json TEXT NOT NULL DEFAULT '{}',
    technical_json TEXT NOT NULL DEFAULT '{}',
    objectives_json TEXT NOT NULL DEFAULT '{}',
    preferences_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    repo_url TEXT DEFAULT '',
    local_path TEXT DEFAULT '',
    description TEXT DEFAULT '',
    architecture TEXT DEFAULT '',
    technologies_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    name TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    parameters_json TEXT NOT NULL DEFAULT '{}',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'planned',
    conclusion TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    memory_type TEXT NOT NULL,
    project_id TEXT DEFAULT '',
    content TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.8,
    tags_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(memory_type, created_at DESC);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    url TEXT NOT NULL,
    location TEXT DEFAULT 'Remote',
    work_mode TEXT DEFAULT 'remote',
    employment_type TEXT DEFAULT 'full-time',
    compensation TEXT DEFAULT '',
    required_skills_json TEXT NOT NULL DEFAULT '[]',
    required_experience_years REAL,
    work_auth_required TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    discovered_at TEXT NOT NULL,
    last_verified_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_evaluations (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    overall_score REAL NOT NULL,
    hard_eligibility TEXT NOT NULL,
    evidence_confidence TEXT NOT NULL,
    breakdown_json TEXT NOT NULL,
    missing_requirements_json TEXT NOT NULL DEFAULT '[]',
    supporting_evidence_json TEXT NOT NULL DEFAULT '[]',
    explanation TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    weighting_profile TEXT NOT NULL DEFAULT 'default',
    evaluated_at TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS applications (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_approval',
    tailored_note TEXT DEFAULT '',
    approval_id TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tech_items (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    classification TEXT NOT NULL,
    radar_ring TEXT NOT NULL DEFAULT 'ASSESS',
    summary TEXT NOT NULL,
    source_url TEXT NOT NULL,
    relevance_score REAL NOT NULL DEFAULT 0.5,
    discovered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tech_name_cat ON tech_items(name, category);

CREATE TABLE IF NOT EXISTS watchlists (
    id TEXT PRIMARY KEY,
    symbol TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    market TEXT NOT NULL,
    alert_threshold_pct REAL NOT NULL DEFAULT 3.0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_observations (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    price REAL,
    change_pct REAL,
    currency TEXT DEFAULT 'USD',
    market_status TEXT DEFAULT 'unknown',
    provider TEXT NOT NULL,
    data_timestamp TEXT NOT NULL,
    analysis_note TEXT DEFAULT '',
    observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_obs_sym ON market_observations(symbol, observed_at DESC);

CREATE TABLE IF NOT EXISTS geopolitical_events (
    id TEXT PRIMARY KEY,
    headline TEXT NOT NULL,
    category TEXT NOT NULL,
    regions_json TEXT NOT NULL DEFAULT '[]',
    priority TEXT NOT NULL DEFAULT 'MEDIUM',
    verification_status TEXT NOT NULL DEFAULT 'Reported',
    sources_json TEXT NOT NULL DEFAULT '[]',
    facts_summary TEXT NOT NULL,
    impact_analysis TEXT DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.6,
    event_date TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    action_type TEXT NOT NULL,
    target TEXT NOT NULL,
    reason TEXT NOT NULL,
    risk_level TEXT NOT NULL DEFAULT 'medium',
    params_json TEXT NOT NULL,
    params_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    decided_by TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    dedupe_key TEXT UNIQUE,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT DEFAULT '',
    outcome TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_usage (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL
);
"""


class PlatformDB:
    """Thread-safe SQLite WAL database for all OS domain entities."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._migrate()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _migrate(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA_SQL)

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        async with self._lock:
            await asyncio.to_thread(self._execute_sync, sql, params)

    def _execute_sync(self, sql: str, params: tuple[Any, ...]) -> None:
        with self.connect() as conn:
            conn.execute(sql, params)

    async def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        async with self._lock:
            return await asyncio.to_thread(self._fetch_all_sync, sql, params)

    def _fetch_all_sync(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    async def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        async with self._lock:
            return await asyncio.to_thread(self._fetch_one_sync, sql, params)

    def _fetch_one_sync(self, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    async def emit_event(
        self, event_type: str, payload: dict[str, Any], dedupe_key: str | None = None
    ) -> str:
        event_id = uuid.uuid4().hex[:12]
        now = utc_now()
        async with self._lock:
            await asyncio.to_thread(
                self._emit_event_sync, event_id, event_type, payload, dedupe_key, now
            )
        return event_id

    def _emit_event_sync(
        self,
        event_id: str,
        event_type: str,
        payload: dict[str, Any],
        dedupe_key: str | None,
        now: str,
    ) -> None:
        with self.connect() as conn:
            if dedupe_key:
                existing = conn.execute(
                    "SELECT id FROM events WHERE dedupe_key = ?", (dedupe_key,)
                ).fetchone()
                if existing:
                    return
            conn.execute(
                "INSERT INTO events (id, event_type, dedupe_key, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (event_id, event_type, dedupe_key, json.dumps(payload), now),
            )
