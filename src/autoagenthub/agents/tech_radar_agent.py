"""Agent 2: Technology Intelligence & Personal Technology Radar (ADOPT/TRIAL/ASSESS/HOLD)."""

from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from autoagenthub.db import PlatformDB, utc_now

VALID_RINGS = {"ADOPT", "TRIAL", "ASSESS", "HOLD"}
VALID_CLASSES = {"Emerging", "Promising", "Established", "Overhyped", "Security-critical"}


async def fetch_arxiv_papers(query: str = "LLM inference OR quantization OR distributed systems", max_results: int = 5) -> list[dict[str, str]]:
    """Fetch recent papers from the official arXiv Atom API."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            "https://export.arxiv.org/api/query",
            params={"search_query": f"all:{query}", "start": 0, "max_results": max_results, "sortBy": "submittedDate", "sortOrder": "descending"},
        )
        resp.raise_for_status()

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(resp.text)
    papers: list[dict[str, str]] = []
    for entry in root.findall("atom:entry", ns):
        title = (entry.findtext("atom:title", "", ns) or "").strip().replace("\n", " ")
        summary = (entry.findtext("atom:summary", "", ns) or "").strip().replace("\n", " ")[:400]
        link = (entry.findtext("atom:id", "", ns) or "").strip()
        papers.append({"name": title[:120], "category": "AI & Systems Research", "classification": "Emerging", "summary": summary, "source_url": link})
    return papers


async def fetch_github_releases(repos: list[str] | None = None) -> list[dict[str, str]]:
    """Fetch latest releases from key open-source engineering repositories."""
    targets = repos or ["pydantic/pydantic", "fastapi/fastapi", "vllm-project/vllm"]
    items: list[dict[str, str]] = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        for repo in targets:
            try:
                resp = await client.get(f"https://api.github.com/repos/{repo}/releases/latest")
                if resp.status_code != 200:
                    continue
                data = resp.json()
                tag = data.get("tag_name", "")
                items.append(
                    {
                        "name": f"{repo} {tag}",
                        "category": "Open Source Releases",
                        "classification": "Established",
                        "summary": (data.get("body") or data.get("name") or "New release")[:350],
                        "source_url": data.get("html_url", f"https://github.com/{repo}"),
                    }
                )
            except Exception:
                continue
    return items


async def scan_technology_radar(db: PlatformDB, query: str = "LLM inference OR Rust OR C++") -> list[dict[str, Any]]:
    """Scan arXiv + GitHub releases, deduplicate, and store in the personal Technology Radar."""
    discovered: list[dict[str, str]] = []
    try:
        discovered.extend(await fetch_arxiv_papers(query=query, max_results=4))
    except Exception:
        pass
    try:
        discovered.extend(await fetch_github_releases())
    except Exception:
        pass

    stored: list[dict[str, Any]] = []
    now = utc_now()
    for item in discovered:
        existing = await db.fetch_one(
            "SELECT id FROM tech_items WHERE name = ? AND category = ?",
            (item["name"], item["category"]),
        )
        if existing:
            continue
        tid = uuid.uuid4().hex[:12]
        await db.execute(
            "INSERT INTO tech_items (id, name, category, classification, radar_ring, summary, source_url, relevance_score, discovered_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'ASSESS', ?, ?, 0.75, ?, ?)",
            (tid, item["name"], item["category"], item["classification"], item["summary"], item["source_url"], now, now),
        )
        await db.emit_event("TECHNOLOGY_DISCOVERED", {"tech_id": tid, "name": item["name"]}, dedupe_key=f"tech-{item['name'][:60]}")
        stored.append({"id": tid, **item, "radar_ring": "ASSESS"})
    return stored


async def move_radar_ring(db: PlatformDB, tech_id: str, ring: str) -> dict[str, Any] | None:
    ring_upper = ring.upper().strip()
    if ring_upper not in VALID_RINGS:
        raise ValueError(f"Ring must be one of {VALID_RINGS}")
    await db.execute("UPDATE tech_items SET radar_ring = ?, updated_at = ? WHERE id = ?", (ring_upper, utc_now(), tech_id))
    return await db.fetch_one("SELECT * FROM tech_items WHERE id = ?", (tech_id,))
