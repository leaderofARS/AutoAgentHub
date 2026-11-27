"""FastMCP server exposing GitHub, News, Jobs, Messaging, and Agent tools over MCP."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from autoagenthub.agent_runner import run_agent
from autoagenthub.github_tools import (
    apply_repo_code_change,
    audit_repository_summary,
    inspect_repository,
    upload_doc_pull_request,
)
from autoagenthub.jobs_tools import (
    approve_job_application,
    find_daily_jobs,
    list_pending_jobs,
)
from autoagenthub.messaging_tools import send_telegram_message, send_whatsapp_message
from autoagenthub.news_tools import get_latest_news

mcp = FastMCP("AutoAgentHub")


@mcp.tool()
async def inspect_github_repo(repo: str = "") -> dict:
    """Inspect a GitHub repo (owner/repo) for open issues, bug labels, PRs, and documentation files."""
    return await inspect_repository(repo or None)


@mcp.tool()
async def audit_github_repo(repo: str = "", focus: str = "") -> str:
    """Run an AI audit on a GitHub repo for bugs, feature updates, and missing documentation."""
    return await audit_repository_summary(repo or None, focus=focus)


@mcp.tool()
async def upload_github_doc_pr(
    file_path: str,
    content: str,
    commit_message: str,
    pr_title: str,
    repo: str = "",
) -> dict:
    """Create a branch, upload/update a documentation file in GitHub, and open a Pull Request."""
    return await upload_doc_pull_request(
        file_path=file_path,
        content=content,
        commit_message=commit_message,
        pr_title=pr_title,
        repo=repo or None,
    )


@mcp.tool()
async def search_latest_news(topic: str, max_results: int = 5) -> str:
    """Search the web for the latest news about any topic and return a summary with links."""
    return await get_latest_news(topic, max_results=max_results)


@mcp.tool()
async def find_jobs(query: str = "", limit: int = 5) -> list[dict]:
    """Search remote job boards for new roles and draft tailored application notes."""
    return await find_daily_jobs(query=query or None, limit=limit)


@mcp.tool()
def list_queued_jobs() -> list[dict]:
    """List jobs waiting for user approval."""
    return list_pending_jobs()


@mcp.tool()
def approve_job(job_id: str) -> dict:
    """Approve a queued job application and return its tailored note and application URL."""
    approved = approve_job_application(job_id)
    return approved or {"error": f"Job ID '{job_id}' not found."}


@mcp.tool()
async def send_telegram(text: str, chat_id: str = "") -> dict:
    """Send a message via Telegram Bot API."""
    return await send_telegram_message(text=text, chat_id=chat_id or None)


@mcp.tool()
async def send_whatsapp(text: str, recipient: str = "") -> dict:
    """Send a message via Meta WhatsApp Cloud API."""
    return await send_whatsapp_message(text=text, recipient=recipient or None)


@mcp.tool()
async def run_external_agent(prompt: str, backend: str = "") -> str:
    """Run a prompt using the configured agent backend (gemini, agy, or custom)."""
    result = await run_agent(prompt=prompt, backend_override=backend or None)
    return f"[{result.backend} | ok={result.success}]\n{result.output}"


@mcp.tool()
async def run_antigravity_code_change(instruction: str, repo: str = "", backend: str = "agy") -> str:
    """Clone a GitHub repo, run Antigravity (`agy`) to make code changes, and open a Pull Request."""
    return await apply_repo_code_change(
        instruction=instruction,
        repo=repo or None,
        backend=backend,
    )


def main() -> None:
    """Run the AutoAgentHub MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
