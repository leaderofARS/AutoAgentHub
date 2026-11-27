"""Agent 1: Career & Job Intelligence with evidence-based scoring & approval-gated applications."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

from autoagenthub.agents.profile_memory import get_profile, list_projects
from autoagenthub.db import PlatformDB, utc_now
from autoagenthub.jobs_tools import fetch_arbeitnow_jobs, fetch_remotive_jobs
from autoagenthub.security import create_approval_request

DEFAULT_WEIGHTS: dict[str, float] = {
    "technical_skills": 0.30,
    "project_evidence": 0.25,
    "responsibilities": 0.15,
    "experience": 0.10,
    "domain": 0.10,
    "preferences": 0.10,
}


def validate_weights(weights: dict[str, float]) -> dict[str, float]:
    total = round(sum(weights.values()), 4)
    if abs(total - 1.0) > 0.01:
        raise ValueError(f"Job matching weights must total 1.0 (100%), got {total}")
    return weights


def evaluate_job_deterministically(
    job: dict[str, Any],
    profile: dict[str, Any],
    projects: list[dict[str, Any]],
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Transparent, evidence-based job evaluation separating hard eligibility from weighted fit."""
    w = validate_weights(weights or DEFAULT_WEIGHTS)
    career = profile.get("career", {})
    technical = profile.get("technical", {})

    user_skills = {
        s.lower().strip()
        for s in (technical.get("languages", []) + technical.get("frameworks", []) + career.get("skills", []))
        if isinstance(s, str) and s.strip()
    }
    project_techs = {
        t.lower().strip()
        for p in projects
        for t in json.loads(p.get("technologies_json", "[]") or "[]")
        if isinstance(t, str)
    }
    all_evidenced = user_skills | project_techs

    haystack = f"{job.get('title', '')} {job.get('description', '')}".lower()
    req_skills = [s.lower().strip() for s in job.get("required_skills", []) if s.strip()]
    if not req_skills:
        # Extract candidate keywords from title/description without inventing qualifications
        common = ["python", "rust", "c++", "typescript", "javascript", "go", "java", "sql", "docker", "ai", "ml", "linux", "fastapi"]
        req_skills = [k for k in common if k in haystack]

    matched_direct = [s for s in req_skills if s in project_techs]
    matched_claimed = [s for s in req_skills if s in user_skills and s not in project_techs]
    missing = [s for s in req_skills if s not in all_evidenced]

    # Hard eligibility: never silently assume missing info satisfies a requirement
    hard_eligibility = "PASS"
    if not career.get("work_authorization") and "visa" in haystack:
        hard_eligibility = "UNKNOWN"
    if job.get("required_experience_years") is not None:
        user_exp = career.get("years_experience")
        if user_exp is None:
            hard_eligibility = "UNKNOWN"
        elif float(user_exp) < float(job["required_experience_years"]):
            hard_eligibility = "FAIL"

    tech_ratio = (len(matched_direct) + 0.7 * len(matched_claimed)) / max(1, len(req_skills))
    proj_ratio = len(matched_direct) / max(1, len(req_skills))
    pref_loc = str(career.get("preferred_locations", "remote")).lower()
    job_loc = str(job.get("location", "remote")).lower()
    pref_score = 1.0 if ("remote" in job_loc or pref_loc in job_loc or not pref_loc) else 0.4

    breakdown = {
        "technical_skills": round(min(1.0, tech_ratio) * 100, 1),
        "project_evidence": round(min(1.0, proj_ratio) * 100, 1),
        "responsibilities": round(min(1.0, tech_ratio * 0.9) * 100, 1),
        "experience": 100.0 if hard_eligibility == "PASS" else (50.0 if hard_eligibility == "UNKNOWN" else 10.0),
        "domain": round(min(1.0, (0.5 + 0.5 * tech_ratio)) * 100, 1),
        "preferences": round(pref_score * 100, 1),
    }

    overall = round(sum(breakdown[k] * w[k] for k in w), 1)
    confidence = "Direct evidence" if matched_direct else ("Strong related evidence" if matched_claimed else "No evidence")

    if overall >= 85 and hard_eligibility == "PASS":
        rec = "High priority — review and approve"
    elif overall >= 75:
        rec = "Worth reviewing"
    else:
        rec = "Low fit / missing evidence"

    return {
        "overall_score": overall,
        "hard_eligibility": hard_eligibility,
        "evidence_confidence": confidence,
        "breakdown": breakdown,
        "missing_requirements": missing,
        "supporting_evidence": [f"Project tech: {s}" for s in matched_direct] + [f"Profile skill: {s}" for s in matched_claimed],
        "explanation": f"Matched {len(matched_direct)} project-backed skills and {len(matched_claimed)} profile skills out of {len(req_skills)} detected requirements.",
        "recommendation": rec,
        "weighting_profile": "default",
    }


async def fetch_greenhouse_jobs(board_token: str, limit: int = 8) -> list[dict[str, Any]]:
    """Fetch public jobs from a Greenhouse job board API."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs")
        resp.raise_for_status()
        return [
            {
                "id": f"gh-{board_token}-{j['id']}",
                "source": f"Greenhouse:{board_token}",
                "company": board_token,
                "title": j.get("title", ""),
                "location": (j.get("location") or {}).get("name", "Remote"),
                "url": j.get("absolute_url", ""),
                "description": "",
            }
            for j in resp.json().get("jobs", [])[:limit]
        ]


async def ingest_and_evaluate_jobs(
    db: PlatformDB,
    query: str = "python software engineer",
    limit: int = 6,
    greenhouse_boards: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Ingest jobs from permitted sources, deduplicate, evaluate, and store in DB."""
    profile = await get_profile(db)
    projects = await list_projects(db)

    raw_jobs: list[dict[str, Any]] = []
    for fetcher in (fetch_remotive_jobs, fetch_arbeitnow_jobs):
        try:
            raw_jobs.extend(await fetcher(query, limit=limit))
        except Exception:
            continue
    for board in greenhouse_boards or []:
        try:
            raw_jobs.extend(await fetch_greenhouse_jobs(board, limit=4))
        except Exception:
            continue

    results: list[dict[str, Any]] = []
    now = utc_now()
    for j in raw_jobs[: limit * 2]:
        existing = await db.fetch_one("SELECT id FROM jobs WHERE id = ?", (j["id"],))
        if existing:
            await db.execute("UPDATE jobs SET last_verified_at = ? WHERE id = ?", (now, j["id"]))
            continue

        await db.execute(
            "INSERT INTO jobs (id, source, company, title, description, url, location, status, discovered_at, last_verified_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
            (j["id"], j["source"], j["company"], j["title"], j.get("description", ""), j["url"], j.get("location", "Remote"), now, now),
        )
        ev = evaluate_job_deterministically(j, profile, projects)
        await db.execute(
            "INSERT INTO job_evaluations (id, job_id, overall_score, hard_eligibility, evidence_confidence, breakdown_json, "
            "missing_requirements_json, supporting_evidence_json, explanation, recommendation, weighting_profile, evaluated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex[:12],
                j["id"],
                ev["overall_score"],
                ev["hard_eligibility"],
                ev["evidence_confidence"],
                json.dumps(ev["breakdown"]),
                json.dumps(ev["missing_requirements"]),
                json.dumps(ev["supporting_evidence"]),
                ev["explanation"],
                ev["recommendation"],
                ev["weighting_profile"],
                now,
            ),
        )
        await db.emit_event("JOB_DISCOVERED", {"job_id": j["id"], "score": ev["overall_score"]}, dedupe_key=f"job-{j['id']}")
        results.append({**j, **ev})

    return results


async def queue_job_application_for_approval(db: PlatformDB, job_id: str, note: str = "") -> dict[str, Any]:
    """Never auto-submit; create a parameter-bound approval request for the user."""
    job = await db.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not job:
        raise ValueError(f"Job '{job_id}' not found")
    approval = await create_approval_request(
        db=db,
        action_type="job_application_submit",
        target=job["url"],
        reason=f"Approve preparing/marking application for {job['title']} at {job['company']}",
        params={"job_id": job_id, "url": job["url"]},
        risk_level="high",
    )
    app_id = uuid.uuid4().hex[:12]
    await db.execute(
        "INSERT INTO applications (id, job_id, status, tailored_note, approval_id, updated_at) VALUES (?, ?, 'pending_approval', ?, ?, ?)",
        (app_id, job_id, note, approval["id"], utc_now()),
    )
    return {"application_id": app_id, "approval": approval}
