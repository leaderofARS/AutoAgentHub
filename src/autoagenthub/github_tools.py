"""GitHub repository inspection, bug/feature checks, code changes via Antigravity, and PR helpers."""

from __future__ import annotations

import asyncio
import base64
import shutil
import uuid
from pathlib import Path
from typing import Any

import httpx

from autoagenthub.agent_runner import run_agent
from autoagenthub.config import get_settings

GITHUB_API = "https://api.github.com"


def _headers(token: str) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _resolve_repo(repo: str | None) -> str:
    settings = get_settings()
    target = (repo or settings.github_default_repo).strip()
    if not target or "/" not in target:
        raise ValueError("Repository must be in 'owner/repo' format (set GITHUB_DEFAULT_REPO or pass repo).")
    return target


async def inspect_repository(repo: str | None = None) -> dict[str, Any]:
    """Fetch open issues, pull requests, recent commits, and documentation file status."""
    settings = get_settings()
    target = _resolve_repo(repo)
    headers = _headers(settings.github_token)

    async with httpx.AsyncClient(timeout=20.0) as client:
        repo_resp = await client.get(f"{GITHUB_API}/repos/{target}", headers=headers)
        repo_resp.raise_for_status()
        repo_data = repo_resp.json()
        default_branch = repo_data.get("default_branch", "main")

        issues_resp = await client.get(
            f"{GITHUB_API}/repos/{target}/issues",
            headers=headers,
            params={"state": "open", "per_page": 20},
        )
        issues_resp.raise_for_status()
        raw_items = issues_resp.json()

        open_issues = [i for i in raw_items if "pull_request" not in i]
        open_prs = [i for i in raw_items if "pull_request" in i]

        tree_resp = await client.get(
            f"{GITHUB_API}/repos/{target}/git/trees/{default_branch}",
            headers=headers,
            params={"recursive": "1"},
        )
        tree_paths: list[str] = []
        if tree_resp.status_code == 200:
            tree_paths = [
                item["path"]
                for item in tree_resp.json().get("tree", [])
                if item.get("type") == "blob"
            ]

    doc_files = [
        p for p in tree_paths
        if p.lower().startswith("docs/") or p.lower() in {"readme.md", "contributing.md", "changelog.md"}
    ]
    bug_issues = [
        {"number": i["number"], "title": i["title"], "url": i["html_url"]}
        for i in open_issues
        if any("bug" in lbl.get("name", "").lower() for lbl in i.get("labels", []))
    ]

    return {
        "repo": target,
        "default_branch": default_branch,
        "stars": repo_data.get("stargazers_count", 0),
        "open_issues_count": len(open_issues),
        "bug_issues": bug_issues,
        "recent_issues": [
            {"number": i["number"], "title": i["title"], "url": i["html_url"]}
            for i in open_issues[:10]
        ],
        "open_prs": [
            {"number": p["number"], "title": p["title"], "url": p["html_url"]}
            for p in open_prs[:10]
        ],
        "doc_files_found": doc_files,
        "missing_readme": "readme.md" not in {p.lower() for p in tree_paths},
        "total_files": len(tree_paths),
    }


async def audit_repository_summary(repo: str | None = None, focus: str = "") -> str:
    """Inspect a GitHub repo and generate an AI summary of bugs, feature PRs, and doc gaps."""
    try:
        info = await inspect_repository(repo)
    except Exception as exc:
        return f"GitHub inspection failed: {exc}"

    prompt = (
        f"Analyze this GitHub repository snapshot and provide a concise report for the owner.\n"
        f"Focus requested: {focus or 'bugs, feature updates, and missing documentation'}\n\n"
        f"Repository: {info['repo']} (branch: {info['default_branch']})\n"
        f"Bug-labeled issues: {info['bug_issues']}\n"
        f"Recent open issues: {info['recent_issues']}\n"
        f"Open pull requests (feature updates/fixes): {info['open_prs']}\n"
        f"Documentation files found: {info['doc_files_found']}\n"
        f"Missing README.md: {info['missing_readme']}\n"
        f"Total files in repo: {info['total_files']}\n\n"
        "Give clear action items under 3 headings: Bugs, Feature Updates/PRs, Documentation Gaps."
    )
    result = await run_agent(prompt)
    if result.success:
        return result.output
    # Fallback plain summary if no AI key is configured yet
    return (
        f"Repo: {info['repo']}\n"
        f"Open Issues: {info['open_issues_count']} (Bugs: {len(info['bug_issues'])})\n"
        f"Open PRs: {len(info['open_prs'])}\n"
        f"Docs Found: {', '.join(info['doc_files_found']) or 'None'}\n"
        f"Missing README: {info['missing_readme']}"
    )


async def upload_doc_pull_request(
    file_path: str,
    content: str,
    commit_message: str,
    pr_title: str,
    repo: str | None = None,
    branch_name: str = "docs/auto-update",
) -> dict[str, Any]:
    """Create a branch, upload/update a documentation file, and open a Pull Request."""
    settings = get_settings()
    if not settings.github_token:
        raise ValueError("GITHUB_TOKEN is required to upload documentation or open a PR.")
    target = _resolve_repo(repo)
    headers = _headers(settings.github_token)

    async with httpx.AsyncClient(timeout=20.0) as client:
        repo_resp = await client.get(f"{GITHUB_API}/repos/{target}", headers=headers)
        repo_resp.raise_for_status()
        default_branch = repo_resp.json()["default_branch"]

        ref_resp = await client.get(
            f"{GITHUB_API}/repos/{target}/git/ref/heads/{default_branch}",
            headers=headers,
        )
        ref_resp.raise_for_status()
        base_sha = ref_resp.json()["object"]["sha"]

        # Create branch (ignore 422 if branch already exists)
        branch_resp = await client.post(
            f"{GITHUB_API}/repos/{target}/git/refs",
            headers=headers,
            json={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
        )
        if branch_resp.status_code not in (201, 422):
            branch_resp.raise_for_status()

        # Check existing file SHA on branch
        existing = await client.get(
            f"{GITHUB_API}/repos/{target}/contents/{file_path}",
            headers=headers,
            params={"ref": branch_name},
        )
        file_sha = existing.json().get("sha") if existing.status_code == 200 else None

        put_body: dict[str, Any] = {
            "message": commit_message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch_name,
        }
        if file_sha:
            put_body["sha"] = file_sha

        put_resp = await client.put(
            f"{GITHUB_API}/repos/{target}/contents/{file_path}",
            headers=headers,
            json=put_body,
        )
        put_resp.raise_for_status()

        pr_resp = await client.post(
            f"{GITHUB_API}/repos/{target}/pulls",
            headers=headers,
            json={
                "title": pr_title,
                "head": branch_name,
                "base": default_branch,
                "body": "Automated documentation update created by AutoAgentHub.",
            },
        )
        if pr_resp.status_code == 422:
            return {"status": "updated_branch", "branch": branch_name, "detail": pr_resp.json()}
        pr_resp.raise_for_status()
        pr_data = pr_resp.json()
        return {"status": "pr_created", "url": pr_data["html_url"], "number": pr_data["number"]}


async def _run_git(args: list[str], cwd: Path) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out_b, err_b = await proc.communicate()
    return (
        proc.returncode or 0,
        out_b.decode("utf-8", errors="replace").strip(),
        err_b.decode("utf-8", errors="replace").strip(),
    )


async def apply_repo_code_change(
    instruction: str,
    repo: str | None = None,
    backend: str | None = None,
) -> str:
    """Clone a GitHub repo, run Antigravity (or configured agent) to edit code, push a branch, and open a PR."""
    settings = get_settings()
    if not settings.github_token:
        return "GITHUB_TOKEN is required to clone, push, and open a Pull Request."
    target = _resolve_repo(repo)
    work_id = uuid.uuid4().hex[:8]
    workspace = settings.data_dir / "workspaces" / work_id
    workspace.mkdir(parents=True, exist_ok=True)
    branch_name = f"autoagent/{work_id}"

    auth_url = f"https://x-access-token:{settings.github_token}@github.com/{target}.git"
    try:
        rc, _, err = await _run_git(["clone", "--depth", "1", auth_url, str(workspace)], cwd=workspace.parent)
        if rc != 0:
            return f"Failed to clone {target}: {err.replace(settings.github_token, '***')}"

        await _run_git(["checkout", "-b", branch_name], cwd=workspace)
        await _run_git(["config", "user.name", "AutoAgentHub"], cwd=workspace)
        await _run_git(["config", "user.email", "autoagenthub@users.noreply.github.com"], cwd=workspace)

        agent_prompt = (
            f"You are working inside the cloned repository '{target}'.\n"
            f"Execute this task by editing files and running relevant tests:\n{instruction}\n\n"
            "Summarize what you changed and test results."
        )
        res = await run_agent(agent_prompt, backend_override=backend, cwd=workspace)
        if not res.success:
            return f"Agent ({res.backend}) failed: {res.output}"

        _, status_out, _ = await _run_git(["status", "--porcelain"], cwd=workspace)
        if not status_out:
            return f"Agent ({res.backend}) finished with no file changes:\n{res.output}"

        await _run_git(["add", "-A"], cwd=workspace)
        rc, _, err = await _run_git(["commit", "-m", f"autoagent: {instruction[:60]}"], cwd=workspace)
        if rc != 0:
            return f"Git commit failed: {err}"

        rc, _, err = await _run_git(["push", "-u", "origin", branch_name], cwd=workspace)
        if rc != 0:
            return f"Git push failed: {err.replace(settings.github_token, '***')}"

        headers = _headers(settings.github_token)
        async with httpx.AsyncClient(timeout=20.0) as client:
            repo_resp = await client.get(f"{GITHUB_API}/repos/{target}", headers=headers)
            repo_resp.raise_for_status()
            default_branch = repo_resp.json().get("default_branch", "main")

            pr_resp = await client.post(
                f"{GITHUB_API}/repos/{target}/pulls",
                headers=headers,
                json={
                    "title": f"AutoAgent: {instruction[:70]}",
                    "head": branch_name,
                    "base": default_branch,
                    "body": f"Automated code change requested via AutoAgentHub.\n\n### Summary\n{res.output[:3000]}",
                },
            )
            pr_resp.raise_for_status()
            pr_url = pr_resp.json()["html_url"]

        return f"PR created: {pr_url}\n\nSummary:\n{res.output}"
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


async def create_github_issue(
    title: str,
    body: str = "",
    repo: str | None = None,
) -> dict[str, Any]:
    """Create a GitHub issue in the target repository."""
    settings = get_settings()
    if not settings.github_token:
        raise ValueError("GITHUB_TOKEN is required to create a GitHub issue.")
    target = _resolve_repo(repo)
    headers = _headers(settings.github_token)
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{GITHUB_API}/repos/{target}/issues",
            headers=headers,
            json={"title": title, "body": body or "Created via AutoAgentHub."},
        )
        resp.raise_for_status()
        data = resp.json()
        return {"number": data["number"], "url": data["html_url"], "title": data["title"]}
