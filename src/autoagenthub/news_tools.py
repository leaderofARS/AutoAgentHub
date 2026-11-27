"""Web search and latest news aggregation with Tavily, Gemini Search Grounding, and RSS fallback."""

from __future__ import annotations

import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from autoagenthub.agent_runner import run_agent
from autoagenthub.config import get_settings


async def search_tavily(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """Query Tavily search API if TAVILY_API_KEY is configured."""
    settings = get_settings()
    if not settings.tavily_api_key:
        return []
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": settings.tavily_api_key,
                "query": query,
                "topic": "news",
                "max_results": max_results,
            },
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", "")[:300],
            }
            for r in results
        ]


async def fetch_google_news_rss(topic: str, max_results: int = 6) -> list[dict[str, str]]:
    """Fetch recent headlines via public Google News RSS without requiring an API key."""
    encoded = urllib.parse.quote_plus(topic)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    root = ET.fromstring(resp.text)
    items: list[dict[str, str]] = []
    for item in root.findall("./channel/item")[:max_results]:
        items.append(
            {
                "title": (item.findtext("title") or "").strip(),
                "url": (item.findtext("link") or "").strip(),
                "published": (item.findtext("pubDate") or "").strip(),
            }
        )
    return items


async def get_latest_news(topic: str, max_results: int = 5) -> str:
    """Search the web for the latest news on any topic and summarize cleanly."""
    settings = get_settings()

    # 1. Try Tavily first if configured
    if settings.tavily_api_key:
        try:
            items = await search_tavily(topic, max_results=max_results)
            if items:
                bullets = "\n".join(
                    f"- {i['title']}\n  {i['url']}\n  {i['snippet']}" for i in items
                )
                summary = await run_agent(
                    f"Summarize the latest news about '{topic}' from these sources with links:\n\n{bullets}"
                )
                return summary.output if summary.success else bullets
        except Exception:
            pass

    # 2. Try Gemini with built-in Google Search grounding
    if settings.gemini_api_key:
        grounded = await run_agent(
            f"Find and summarize the latest news about: {topic}. Include key dates and source links.",
            use_google_search=True,
        )
        if grounded.success:
            return grounded.output

    # 3. Fallback to Google News RSS
    try:
        rss_items = await fetch_google_news_rss(topic, max_results=max_results)
        if not rss_items:
            return f"No recent news found for '{topic}'."
        return f"Latest news for '{topic}':\n\n" + "\n".join(
            f"- {i['title']} ({i['published']})\n  {i['url']}" for i in rss_items
        )
    except Exception as exc:
        return f"Failed to fetch news for '{topic}': {exc}"
