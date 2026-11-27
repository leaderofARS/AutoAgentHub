"""Daily job discovery, matching, cover-letter drafting, and approval queue."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from autoagenthub.agent_runner import run_agent
from autoagenthub.config import get_settings


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


async def fetch_remotive_jobs(query: str, limit: int = 8) -> list[dict[str, str]]:
    """Fetch remote jobs from Remotive's public API."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            "https://remotive.com/api/remote-jobs",
            params={"search": query, "limit": limit},
        )
        resp.raise_for_status()
        jobs = resp.json().get("jobs", [])
        return [
            {
                "id": f"remotive-{j['id']}",
                "title": j.get("title", ""),
                "company": j.get("company_name", ""),
                "location": j.get("candidate_required_location", "Remote"),
                "url": j.get("url", ""),
                "source": "Remotive",
            }
            for j in jobs[:limit]
        ]


async def fetch_arbeitnow_jobs(query: str, limit: int = 8) -> list[dict[str, str]]:
    """Fetch jobs from Arbeitnow's public API and filter by query keywords."""
    keywords = [k.lower() for k in query.split() if len(k) > 2]
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get("https://www.arbeitnow.com/api/job-board-api")
        resp.raise_for_status()
        data = resp.json().get("data", [])

    matched: list[dict[str, str]] = []
    for item in data:
        haystack = f"{item.get('title', '')} {' '.join(item.get('tags', []))}".lower()
        if not keywords or any(k in haystack for k in keywords):
            matched.append(
                {
                    "id": f"arbeitnow-{item.get('slug', '')}",
                    "title": item.get("title", ""),
                    "company": item.get("company_name", ""),
                    "location": item.get("location", "Remote"),
                    "url": item.get("url", ""),
                    "source": "Arbeitnow",
                }
            )
        if len(matched) >= limit:
            break
    return matched


async def find_daily_jobs(query: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
    """Find new unseen jobs, draft tailored pitches, and queue them for approval."""
    settings = get_settings()
    search_query = (query or settings.job_query).strip()
    seen_path = settings.data_dir / "seen_jobs.json"
    queue_path = settings.data_dir / "pending_jobs.json"

    seen_ids: set[str] = set(_load_json(seen_path, []))
    queue: list[dict[str, Any]] = _load_json(queue_path, [])

    candidates: list[dict[str, str]] = []
    for fetcher in (fetch_remotive_jobs, fetch_arbeitnow_jobs):
        try:
            candidates.extend(await fetcher(search_query, limit=limit))
        except Exception:
            continue

    fresh = [j for j in candidates if j["id"] not in seen_ids][:limit]
    for job in fresh:
        pitch_prompt = (
            f"Write a short, tailored 3-sentence job application message for:\n"
            f"Role: {job['title']} at {job['company']}\n"
            f"Applicant: {settings.job_user_name}\n"
            f"Bio: {settings.job_user_bio}"
        )
        pitch = await run_agent(pitch_prompt)
        entry = {
            **job,
            "draft_cover_note": pitch.output if pitch.success else settings.job_user_bio,
            "status": "pending_approval",
            "found_at": datetime.now(timezone.utc).isoformat(),
        }
        queue.append(entry)
        seen_ids.add(job["id"])

    _save_json(seen_path, sorted(seen_ids))
    _save_json(queue_path, queue)
    return fresh


def list_pending_jobs() -> list[dict[str, Any]]:
    """Return jobs currently awaiting user approval."""
    settings = get_settings()
    queue = _load_json(settings.data_dir / "pending_jobs.json", [])
    return [j for j in queue if j.get("status") == "pending_approval"]


def approve_job_application(job_id: str) -> dict[str, Any] | None:
    """Mark a queued job as approved/ready and return its tailored note and URL."""
    settings = get_settings()
    queue_path = settings.data_dir / "pending_jobs.json"
    queue: list[dict[str, Any]] = _load_json(queue_path, [])
    for item in queue:
        if item.get("id") == job_id:
            item["status"] = "approved"
            item["approved_at"] = datetime.now(timezone.utc).isoformat()
            _save_json(queue_path, queue)
            return item
    return None
