"""Personalized Daily Briefing combining Career, Tech Radar, Finance, Geopolitics, and Projects."""

from __future__ import annotations

import asyncio
from typing import Any

from autoagenthub.agents.career_agent import ingest_and_evaluate_jobs
from autoagenthub.agents.finance_agent import scan_watchlist_markets
from autoagenthub.agents.geopolitics_agent import scan_geopolitical_events
from autoagenthub.agents.profile_memory import list_projects
from autoagenthub.agents.tech_radar_agent import scan_technology_radar
from autoagenthub.db import PlatformDB


async def generate_daily_briefing(db: PlatformDB) -> dict[str, Any]:
    """Run all intelligence scans concurrently and build a ranked personal briefing."""
    jobs_res, tech_res, mkt_res, geo_res, projects = await asyncio.gather(
        ingest_and_evaluate_jobs(db, limit=4),
        scan_technology_radar(db),
        scan_watchlist_markets(db),
        scan_geopolitical_events(db, max_per_source=3),
        list_projects(db),
        return_exceptions=True,
    )

    def _safe_list(val: Any) -> list[Any]:
        return val if isinstance(val, list) else []

    jobs = _safe_list(jobs_res)
    tech = _safe_list(tech_res)
    markets = _safe_list(mkt_res)
    geo = _safe_list(geo_res)
    projs = _safe_list(projects)

    lines = ["=== PERSONAL DAILY BRIEFING ==="]
    lines.append(f"\n[Career] {len(jobs)} newly evaluated opportunities:")
    for j in sorted(jobs, key=lambda x: x.get("overall_score", 0), reverse=True)[:3]:
        lines.append(f"- {j['title']} @ {j['company']} | Score: {j.get('overall_score')}/100 ({j.get('hard_eligibility')})\n  {j['url']}")

    lines.append(f"\n[Tech Radar] {len(tech)} new discoveries:")
    for t in tech[:3]:
        lines.append(f"- [{t['radar_ring']}] {t['name']} ({t['classification']})\n  {t['source_url']}")

    lines.append(f"\n[Markets] {len(markets)} watchlist quotes (delayed public feed):")
    for m in markets[:4]:
        if "error" in m:
            lines.append(f"- {m['symbol']}: unavailable")
        else:
            lines.append(f"- {m['name']} ({m['symbol']}): {m['price']} {m['currency']} ({m['change_pct']:+}%)")

    lines.append(f"\n[Geopolitics] {len(geo)} high/medium priority events:")
    for g in geo[:3]:
        lines.append(f"- [{g['priority']} | {g['verification_status']}] {g['headline']}")

    lines.append(f"\n[Projects] {len(projs)} active tracked projects.")
    return {
        "text": "\n".join(lines),
        "counts": {
            "jobs": len(jobs),
            "tech": len(tech),
            "markets": len(markets),
            "geopolitics": len(geo),
            "projects": len(projs),
        },
    }
