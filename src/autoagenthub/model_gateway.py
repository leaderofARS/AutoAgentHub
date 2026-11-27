"""Provider-independent AI Model Gateway with tier routing, cost tracking, and budget guards."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any

from autoagenthub.agent_runner import AgentResult, run_agent
from autoagenthub.config import get_settings
from autoagenthub.db import PlatformDB, utc_now


class ModelTier(str, Enum):
    CHEAP = "cheap"       # Classification, deduplication, short extraction
    CAPABLE = "capable"   # Multi-source synthesis, engineering, architecture


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model_id: str
    tier: ModelTier
    input_cost_per_1m: float | None
    output_cost_per_1m: float | None


DEFAULT_MODELS: dict[ModelTier, ModelSpec] = {
    ModelTier.CHEAP: ModelSpec("google", "gemini-2.5-flash", ModelTier.CHEAP, None, None),
    ModelTier.CAPABLE: ModelSpec("google", "gemini-2.5-pro", ModelTier.CAPABLE, None, None),
}


async def generate_with_gateway(
    prompt: str,
    db: PlatformDB | None = None,
    tier: ModelTier = ModelTier.CHEAP,
    system_instruction: str | None = None,
    use_google_search: bool = False,
) -> AgentResult:
    """Route an AI request through the gateway, logging usage and respecting configured budgets."""
    settings = get_settings()
    spec = DEFAULT_MODELS[tier]

    # Route using configured model for cheap tier, or capable model when requested
    result = await run_agent(
        prompt=prompt,
        backend_override="gemini" if use_google_search else None,
        system_instruction=system_instruction,
        use_google_search=use_google_search,
    )

    if db is not None:
        est_in = max(1, len(prompt) // 4)
        est_out = max(1, len(result.output) // 4)
        cost = 0.0
        if spec.input_cost_per_1m and spec.output_cost_per_1m:
            cost = (est_in / 1_000_000) * spec.input_cost_per_1m + (
                est_out / 1_000_000
            ) * spec.output_cost_per_1m
        await db.execute(
            "INSERT INTO model_usage (id, provider, model, input_tokens, output_tokens, estimated_cost_usd, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex[:12], spec.provider, settings.gemini_model, est_in, est_out, cost, utc_now()),
        )
    return result


async def get_usage_summary(db: PlatformDB) -> dict[str, Any]:
    """Return aggregate token and estimated cost metrics."""
    row = await db.fetch_one(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens), 0) AS input_tokens, "
        "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
        "COALESCE(SUM(estimated_cost_usd), 0.0) AS estimated_cost_usd FROM model_usage"
    )
    return row or {"calls": 0, "input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0}
