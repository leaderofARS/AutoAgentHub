"""Agent 3: Financial Intelligence (Watchlists, Delayed/Live Quotes, Threshold Alerts — No Trading)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from autoagenthub.db import PlatformDB, utc_now

DEFAULT_WATCHLIST = [
    ("NVDA", "NVIDIA Corp", "US", 3.0),
    ("^NSEI", "NIFTY 50", "NSE", 1.5),
    ("^GSPC", "S&P 500", "US", 1.5),
    ("USDINR=X", "USD/INR", "FX", 0.8),
]


async def ensure_default_watchlist(db: PlatformDB) -> list[dict[str, Any]]:
    existing = await db.fetch_all("SELECT * FROM watchlists ORDER BY created_at ASC")
    if existing:
        return existing
    now = utc_now()
    for sym, name, market, thresh in DEFAULT_WATCHLIST:
        await db.execute(
            "INSERT OR IGNORE INTO watchlists (id, symbol, name, market, alert_threshold_pct, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex[:12], sym, name, market, thresh, now),
        )
    return await db.fetch_all("SELECT * FROM watchlists ORDER BY created_at ASC")


async def add_watchlist_symbol(db: PlatformDB, symbol: str, name: str, market: str = "US", threshold_pct: float = 3.0) -> dict[str, Any]:
    wid = uuid.uuid4().hex[:12]
    now = utc_now()
    await db.execute(
        "INSERT OR REPLACE INTO watchlists (id, symbol, name, market, alert_threshold_pct, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (wid, symbol.upper().strip(), name, market, threshold_pct, now),
    )
    return {"id": wid, "symbol": symbol.upper().strip(), "name": name, "market": market, "alert_threshold_pct": threshold_pct}


async def fetch_yahoo_quote(symbol: str) -> dict[str, Any]:
    """Fetch delayed market observation from Yahoo Finance public chart endpoint."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    headers = {"User-Agent": "AutoAgentHub/1.0"}
    async with httpx.AsyncClient(timeout=12.0) as client:
        resp = await client.get(url, params={"interval": "1d", "range": "2d"}, headers=headers)
        resp.raise_for_status()
        meta = resp.json()["chart"]["result"][0]["meta"]
    price = float(meta.get("regularMarketPrice", 0.0))
    prev = float(meta.get("chartPreviousClose") or meta.get("previousClose") or price or 1.0)
    change_pct = round(((price - prev) / prev) * 100.0, 2) if prev else 0.0
    ts = meta.get("regularMarketTime")
    data_ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else utc_now()
    return {
        "symbol": symbol,
        "price": round(price, 4),
        "change_pct": change_pct,
        "currency": meta.get("currency", "USD"),
        "market_status": "delayed (public feed)",
        "provider": "yahoo_finance_chart",
        "data_timestamp": data_ts,
    }


async def scan_watchlist_markets(db: PlatformDB) -> list[dict[str, Any]]:
    """Observe all watchlist symbols, record timestamped snapshots, and emit threshold alerts."""
    items = await ensure_default_watchlist(db)
    observations: list[dict[str, Any]] = []
    now = utc_now()

    for w in items:
        try:
            q = await fetch_yahoo_quote(w["symbol"])
        except Exception as exc:
            observations.append({"symbol": w["symbol"], "name": w["name"], "error": str(exc)})
            continue

        note = (
            f"Threshold triggered (|{q['change_pct']}%| >= {w['alert_threshold_pct']}%)"
            if abs(q["change_pct"]) >= float(w["alert_threshold_pct"])
            else "Within normal range"
        )
        oid = uuid.uuid4().hex[:12]
        await db.execute(
            "INSERT INTO market_observations (id, symbol, price, change_pct, currency, market_status, provider, data_timestamp, analysis_note, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (oid, q["symbol"], q["price"], q["change_pct"], q["currency"], q["market_status"], q["provider"], q["data_timestamp"], note, now),
        )
        if abs(q["change_pct"]) >= float(w["alert_threshold_pct"]):
            await db.emit_event("MARKET_THRESHOLD_TRIGGERED", {**q, "name": w["name"]})
        observations.append({**q, "name": w["name"], "analysis_note": note})

    return observations
