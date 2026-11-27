"""Agent 6: Personal Knowledge, User Profile, Projects, Experiments, and Memory System."""

from __future__ import annotations

import json
import uuid
from typing import Any

from autoagenthub.db import PlatformDB, utc_now


async def get_profile(db: PlatformDB, user_id: str = "default") -> dict[str, Any]:
    """Fetch the structured user profile (or initialize an empty one)."""
    row = await db.fetch_one("SELECT * FROM profiles WHERE user_id = ?", (user_id,))
    if not row:
        now = utc_now()
        pid = uuid.uuid4().hex[:12]
        await db.execute(
            "INSERT INTO profiles (id, user_id, career_json, technical_json, objectives_json, preferences_json, updated_at) "
            "VALUES (?, ?, '{}', '{}', '{}', '{}', ?)",
            (pid, user_id, now),
        )
        return {
            "id": pid,
            "user_id": user_id,
            "career": {},
            "technical": {},
            "objectives": {},
            "preferences": {},
            "updated_at": now,
        }
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "career": json.loads(row["career_json"]),
        "technical": json.loads(row["technical_json"]),
        "objectives": json.loads(row["objectives_json"]),
        "preferences": json.loads(row["preferences_json"]),
        "updated_at": row["updated_at"],
    }


async def update_profile(
    db: PlatformDB,
    section: str,
    data: dict[str, Any],
    user_id: str = "default",
) -> dict[str, Any]:
    """Update one profile section: career | technical | objectives | preferences."""
    if section not in {"career", "technical", "objectives", "preferences"}:
        raise ValueError("section must be career, technical, objectives, or preferences")
    await get_profile(db, user_id)
    col = f"{section}_json"
    await db.execute(
        f"UPDATE profiles SET {col} = ?, updated_at = ? WHERE user_id = ?",
        (json.dumps(data), utc_now(), user_id),
    )
    return await get_profile(db, user_id)


async def add_memory(
    db: PlatformDB,
    memory_type: str,
    content: str,
    source: str,
    confidence: float = 0.85,
    project_id: str = "",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Store a sourced, timestamped memory record with deduplication."""
    existing = await db.fetch_one(
        "SELECT id FROM memories WHERE memory_type = ? AND content = ?",
        (memory_type, content.strip()),
    )
    if existing:
        return existing
    mid = uuid.uuid4().hex[:12]
    now = utc_now()
    await db.execute(
        "INSERT INTO memories (id, memory_type, project_id, content, source, confidence, tags_json, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (mid, memory_type, project_id, content.strip(), source, confidence, json.dumps(tags or []), now, now),
    )
    return {"id": mid, "memory_type": memory_type, "content": content.strip(), "source": source, "created_at": now}


async def search_memories(db: PlatformDB, query: str = "", memory_type: str = "", limit: int = 30) -> list[dict[str, Any]]:
    """Retrieve memories filtered by type and keyword."""
    sql = "SELECT * FROM memories WHERE 1=1"
    params: list[Any] = []
    if memory_type:
        sql += " AND memory_type = ?"
        params.append(memory_type)
    if query:
        sql += " AND (content LIKE ? OR tags_json LIKE ?)"
        params.extend([f"%{query}%", f"%{query}%"])
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    return await db.fetch_all(sql, tuple(params))


async def delete_memory(db: PlatformDB, memory_id: str) -> None:
    await db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))


async def create_project(
    db: PlatformDB,
    name: str,
    repo_url: str = "",
    description: str = "",
    technologies: list[str] | None = None,
    architecture: str = "",
) -> dict[str, Any]:
    pid = uuid.uuid4().hex[:12]
    now = utc_now()
    await db.execute(
        "INSERT INTO projects (id, name, repo_url, description, architecture, technologies_json, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)",
        (pid, name, repo_url, description, architecture, json.dumps(technologies or []), now, now),
    )
    return {"id": pid, "name": name, "repo_url": repo_url, "status": "active", "created_at": now}


async def list_projects(db: PlatformDB) -> list[dict[str, Any]]:
    return await db.fetch_all("SELECT * FROM projects ORDER BY updated_at DESC")


async def create_experiment(
    db: PlatformDB,
    name: str,
    hypothesis: str,
    project_id: str | None = None,
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    eid = uuid.uuid4().hex[:12]
    now = utc_now()
    await db.execute(
        "INSERT INTO experiments (id, project_id, name, hypothesis, parameters_json, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'planned', ?)",
        (eid, project_id, name, hypothesis, json.dumps(parameters or {}), now),
    )
    return {"id": eid, "name": name, "hypothesis": hypothesis, "status": "planned", "created_at": now}


async def list_experiments(db: PlatformDB) -> list[dict[str, Any]]:
    return await db.fetch_all("SELECT * FROM experiments ORDER BY created_at DESC")
