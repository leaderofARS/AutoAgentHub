"""Control Plane API routes for the Personal Intelligence & Execution OS."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from autoagenthub.agents.briefing import generate_daily_briefing
from autoagenthub.agents.career_agent import (
    ingest_and_evaluate_jobs,
    queue_job_application_for_approval,
)
from autoagenthub.agents.finance_agent import (
    add_watchlist_symbol,
    ensure_default_watchlist,
    scan_watchlist_markets,
)
from autoagenthub.agents.geopolitics_agent import scan_geopolitical_events
from autoagenthub.agents.profile_memory import (
    add_memory,
    create_experiment,
    create_project,
    delete_memory,
    get_profile,
    list_experiments,
    list_projects,
    search_memories,
    update_profile,
)
from autoagenthub.agents.tech_radar_agent import move_radar_ring, scan_technology_radar
from autoagenthub.config import get_settings
from autoagenthub.db import PlatformDB
from autoagenthub.model_gateway import get_usage_summary
from autoagenthub.security import decide_approval
from autoagenthub.tool_registry import ToolRegistry

_platform_db: PlatformDB | None = None
_tool_registry = ToolRegistry()


def get_platform_db() -> PlatformDB:
    global _platform_db
    if _platform_db is None:
        settings = get_settings()
        _platform_db = PlatformDB(settings.data_dir / "platform_os.db")
    return _platform_db


def verify_os_auth(authorization: str = Header(default="")) -> None:
    token = get_settings().dashboard_api_token.strip()
    if not token:
        return
    if authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="Invalid or missing Bearer token")


router = APIRouter(prefix="/api/os", tags=["Personal OS"], dependencies=[Depends(verify_os_auth)])


@router.get("/overview")
async def os_overview() -> dict[str, Any]:
    db = get_platform_db()
    jobs = await db.fetch_one("SELECT COUNT(*) AS c FROM jobs")
    tech = await db.fetch_one("SELECT COUNT(*) AS c FROM tech_items")
    geo = await db.fetch_one("SELECT COUNT(*) AS c FROM geopolitical_events")
    projs = await db.fetch_one("SELECT COUNT(*) AS c FROM projects")
    approvals = await db.fetch_one("SELECT COUNT(*) AS c FROM approvals WHERE status = 'pending'")
    usage = await get_usage_summary(db)
    return {
        "counts": {
            "jobs": (jobs or {}).get("c", 0),
            "tech_items": (tech or {}).get("c", 0),
            "geopolitics": (geo or {}).get("c", 0),
            "projects": (projs or {}).get("c", 0),
            "pending_approvals": (approvals or {}).get("c", 0),
        },
        "model_usage": usage,
    }


@router.get("/profile")
async def api_get_profile() -> dict[str, Any]:
    return await get_profile(get_platform_db())


class ProfileUpdateReq(BaseModel):
    data: dict[str, Any]


@router.put("/profile/{section}")
async def api_update_profile(section: str, req: ProfileUpdateReq) -> dict[str, Any]:
    try:
        return await update_profile(get_platform_db(), section, req.data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class MemoryReq(BaseModel):
    memory_type: str = "semantic"
    content: str
    source: str = "user"
    confidence: float = 0.9
    tags: list[str] = Field(default_factory=list)


@router.get("/memories")
async def api_list_memories(q: str = "", memory_type: str = "") -> list[dict[str, Any]]:
    return await search_memories(get_platform_db(), query=q, memory_type=memory_type)


@router.post("/memories", status_code=201)
async def api_add_memory(req: MemoryReq) -> dict[str, Any]:
    return await add_memory(
        get_platform_db(),
        memory_type=req.memory_type,
        content=req.content,
        source=req.source,
        confidence=req.confidence,
        tags=req.tags,
    )


@router.delete("/memories/{memory_id}")
async def api_delete_memory(memory_id: str) -> dict[str, str]:
    await delete_memory(get_platform_db(), memory_id)
    return {"status": "deleted"}


class ProjectReq(BaseModel):
    name: str
    repo_url: str = ""
    description: str = ""
    technologies: list[str] = Field(default_factory=list)
    architecture: str = ""


@router.get("/projects")
async def api_list_projects() -> list[dict[str, Any]]:
    return await list_projects(get_platform_db())


@router.post("/projects", status_code=201)
async def api_create_project(req: ProjectReq) -> dict[str, Any]:
    return await create_project(
        get_platform_db(),
        name=req.name,
        repo_url=req.repo_url,
        description=req.description,
        technologies=req.technologies,
        architecture=req.architecture,
    )


class ExperimentReq(BaseModel):
    name: str
    hypothesis: str
    project_id: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)


@router.get("/experiments")
async def api_list_experiments() -> list[dict[str, Any]]:
    return await list_experiments(get_platform_db())


@router.post("/experiments", status_code=201)
async def api_create_experiment(req: ExperimentReq) -> dict[str, Any]:
    return await create_experiment(
        get_platform_db(),
        name=req.name,
        hypothesis=req.hypothesis,
        project_id=req.project_id,
        parameters=req.parameters,
    )


@router.get("/jobs")
async def api_list_jobs() -> list[dict[str, Any]]:
    return await get_platform_db().fetch_all(
        "SELECT j.*, e.overall_score, e.hard_eligibility, e.evidence_confidence, e.recommendation "
        "FROM jobs j LEFT JOIN job_evaluations e ON j.id = e.job_id "
        "ORDER BY COALESCE(e.overall_score, 0) DESC, j.discovered_at DESC LIMIT 50"
    )


@router.post("/jobs/scan")
async def api_scan_jobs(query: str = "python software engineer", limit: int = 6) -> list[dict[str, Any]]:
    return await ingest_and_evaluate_jobs(get_platform_db(), query=query, limit=limit)


@router.post("/jobs/{job_id}/queue-apply")
async def api_queue_job_apply(job_id: str) -> dict[str, Any]:
    try:
        return await queue_job_application_for_approval(get_platform_db(), job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/tech")
async def api_list_tech() -> list[dict[str, Any]]:
    return await get_platform_db().fetch_all("SELECT * FROM tech_items ORDER BY discovered_at DESC LIMIT 50")


@router.post("/tech/scan")
async def api_scan_tech(query: str = "LLM inference OR Rust OR C++") -> list[dict[str, Any]]:
    return await scan_technology_radar(get_platform_db(), query=query)


@router.patch("/tech/{tech_id}")
async def api_move_tech_ring(tech_id: str, ring: str = Query(...)) -> dict[str, Any]:
    try:
        updated = await move_radar_ring(get_platform_db(), tech_id, ring)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="Tech item not found")
    return updated


@router.get("/finance")
async def api_finance() -> dict[str, Any]:
    db = get_platform_db()
    watchlist = await ensure_default_watchlist(db)
    obs = await db.fetch_all("SELECT * FROM market_observations ORDER BY observed_at DESC LIMIT 30")
    return {"watchlist": watchlist, "observations": obs}


@router.post("/finance/scan")
async def api_scan_finance() -> list[dict[str, Any]]:
    return await scan_watchlist_markets(get_platform_db())


class WatchlistReq(BaseModel):
    symbol: str
    name: str
    market: str = "US"
    threshold_pct: float = 3.0


@router.post("/finance/watchlist", status_code=201)
async def api_add_watchlist(req: WatchlistReq) -> dict[str, Any]:
    return await add_watchlist_symbol(get_platform_db(), req.symbol, req.name, req.market, req.threshold_pct)


@router.get("/geopolitics")
async def api_list_geo() -> list[dict[str, Any]]:
    return await get_platform_db().fetch_all("SELECT * FROM geopolitical_events ORDER BY created_at DESC LIMIT 40")


@router.post("/geopolitics/scan")
async def api_scan_geo() -> list[dict[str, Any]]:
    return await scan_geopolitical_events(get_platform_db())


@router.post("/briefing")
async def api_briefing() -> dict[str, Any]:
    return await generate_daily_briefing(get_platform_db())


@router.get("/approvals")
async def api_list_approvals() -> list[dict[str, Any]]:
    return await get_platform_db().fetch_all("SELECT * FROM approvals ORDER BY created_at DESC LIMIT 40")


@router.post("/approvals/{approval_id}/approve")
async def api_approve(approval_id: str) -> dict[str, Any]:
    res = await decide_approval(get_platform_db(), approval_id, approve=True, actor="dashboard")
    if not res:
        raise HTTPException(status_code=404, detail="Approval not found")
    return res


@router.post("/approvals/{approval_id}/reject")
async def api_reject(approval_id: str) -> dict[str, Any]:
    res = await decide_approval(get_platform_db(), approval_id, approve=False, actor="dashboard")
    if not res:
        raise HTTPException(status_code=404, detail="Approval not found")
    return res


@router.get("/audit")
async def api_audit() -> list[dict[str, Any]]:
    return await get_platform_db().fetch_all("SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT 50")


@router.get("/events")
async def api_events() -> list[dict[str, Any]]:
    return await get_platform_db().fetch_all("SELECT * FROM events ORDER BY created_at DESC LIMIT 50")


@router.get("/tools")
async def api_tools() -> list[dict[str, Any]]:
    return [
        {
            "id": t.id,
            "name": t.name,
            "category": t.category,
            "risk_category": t.risk_category,
            "requires_approval": t.requires_approval,
            "allowed_agents": list(t.allowed_agents),
        }
        for t in _tool_registry.list_all()
    ]
