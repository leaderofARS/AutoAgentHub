"""Typed Tool Registry with least-privilege agent scoping and risk classification."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolSpec:
    id: str
    name: str
    category: str
    description: str
    risk_category: str  # low | medium | high
    requires_approval: bool = False
    timeout_seconds: int = 30
    allowed_agents: tuple[str, ...] = field(default_factory=tuple)


class ToolRegistry:
    """Central registry enforcing which agents may invoke which tools."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        defaults = [
            ToolSpec("github.inspect", "Inspect GitHub Repo", "github", "Read issues, PRs, tree", "low", False, 20, ("engineering", "career")),
            ToolSpec("github.pr_code", "Modify Repo & Open PR", "coding", "Clone, edit, test, push branch, open PR", "high", True, 300, ("engineering",)),
            ToolSpec("github.issue", "Create GitHub Issue", "github", "Create issue in repo", "medium", False, 15, ("engineering",)),
            ToolSpec("career.ingest", "Ingest Job Boards", "search", "Fetch Remotive/Arbeitnow/Greenhouse/Lever", "low", False, 20, ("career",)),
            ToolSpec("career.apply_gate", "Queue Job Application", "career", "Create approval-gated job application", "high", True, 15, ("career",)),
            ToolSpec("tech.radar_scan", "Scan Tech & arXiv", "rss", "Fetch GitHub releases & arXiv papers", "low", False, 20, ("tech_radar",)),
            ToolSpec("finance.quote", "Fetch Market Quotes", "financial", "Read equity/FX/commodity observations", "low", False, 15, ("finance",)),
            ToolSpec("geo.scan", "Scan Geopolitics RSS", "rss", "Fetch multi-source geopolitical news", "low", False, 20, ("geopolitics",)),
            ToolSpec("memory.rw", "Profile & Memory Store", "database", "Read/write/export user memories", "medium", False, 10, ("profile", "career", "engineering")),
        ]
        for t in defaults:
            self._tools[t.id] = t

    def get(self, tool_id: str) -> ToolSpec | None:
        return self._tools.get(tool_id)

    def authorize(self, agent_name: str, tool_id: str) -> ToolSpec:
        spec = self._tools.get(tool_id)
        if not spec:
            raise KeyError(f"Unknown tool '{tool_id}'")
        if spec.allowed_agents and agent_name not in spec.allowed_agents:
            raise PermissionError(f"Agent '{agent_name}' is not authorized to use tool '{tool_id}'")
        return spec

    def list_all(self) -> list[ToolSpec]:
        return list(self._tools.values())
