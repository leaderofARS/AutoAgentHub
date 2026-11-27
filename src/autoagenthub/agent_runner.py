"""Unified agent runner supporting Antigravity CLI (`agy`), Gemini API (with repo tools), or custom CLI."""

from __future__ import annotations

import asyncio
import json
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from google import genai
from google.genai import types

from autoagenthub.config import Settings, get_settings


@dataclass
class AgentResult:
    """Normalized output from any agent backend."""

    backend: str
    success: bool
    output: str


def _safe_resolve(root: Path, rel_path: str) -> Path:
    target = (root / rel_path).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise ValueError(f"Path '{rel_path}' escapes workspace root.")
    return target


async def run_gemini_agent(
    prompt: str,
    settings: Settings,
    system_instruction: str | None = None,
    use_google_search: bool = False,
    cwd: Path | None = None,
) -> AgentResult:
    """Execute a prompt using Gemini API, with repo read/write/test tools when `cwd` is set."""
    if not settings.gemini_api_key:
        return AgentResult(
            backend="gemini",
            success=False,
            output="GEMINI_API_KEY is not set in .env.",
        )

    def _call() -> str:
        client = genai.Client(api_key=settings.gemini_api_key)

        if cwd is None:
            tools = [types.Tool(google_search=types.GoogleSearch())] if use_google_search else None
            config = types.GenerateContentConfig(
                system_instruction=system_instruction,
                tools=tools,
            )
            response = client.models.generate_content(
                model=settings.gemini_model,
                contents=prompt,
                config=config,
            )
            return response.text or "(No text returned)"

        # Cloud-native repo editing tools when running inside a cloned workspace without `agy`
        def list_files(subdir: str = ".") -> str:
            """List files in the repository workspace."""
            base = _safe_resolve(cwd, subdir)
            files = [
                str(p.relative_to(cwd)).replace("\\", "/")
                for p in base.rglob("*")
                if p.is_file() and ".git" not in p.parts
            ]
            return "\n".join(files[:200]) or "(empty)"

        def read_file(path: str) -> str:
            """Read a UTF-8 file from the repository workspace."""
            target = _safe_resolve(cwd, path)
            return target.read_text(encoding="utf-8", errors="replace")[:20000]

        def write_file(path: str, content: str) -> str:
            """Write or overwrite a file in the repository workspace."""
            target = _safe_resolve(cwd, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"Wrote {path} ({len(content)} chars)"

        def run_shell(command: str) -> str:
            """Run a shell command (e.g. pytest, npm test, git diff) inside the workspace."""
            proc = subprocess.run(
                command,
                cwd=str(cwd),
                shell=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            out = (proc.stdout + "\n" + proc.stderr).strip()
            return f"exit={proc.returncode}\n{out[:8000]}"

        config = types.GenerateContentConfig(
            system_instruction=system_instruction
            or "You are a senior software engineer editing the cloned repository. Inspect files, make minimal clean edits, and run tests to verify.",
            tools=[list_files, read_file, write_file, run_shell],
        )
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config=config,
        )
        return response.text or "(Completed with no text summary)"

    try:
        text = await asyncio.to_thread(_call)
        return AgentResult(backend="gemini", success=True, output=text)
    except Exception as exc:
        return AgentResult(backend="gemini", success=False, output=f"Gemini error: {exc}")


async def run_agy_cli_agent(
    prompt: str,
    cwd: Path | None = None,
    timeout_seconds: int | None = None,
    skip_permissions: bool | None = None,
) -> AgentResult:
    """Execute a prompt using Antigravity CLI headless mode (`agy -p`) in an optional workspace."""
    settings = get_settings()
    if not shutil.which("agy"):
        return AgentResult(
            backend="agy",
            success=False,
            output="Antigravity CLI (`agy`) is not installed or not on PATH on this machine.",
        )

    cmd = ["agy", "-p", prompt, "--output-format", "json"]
    use_skip = settings.agy_skip_permissions if skip_permissions is None else skip_permissions
    if use_skip:
        cmd.append("--dangerously-skip-permissions")

    effective_timeout = timeout_seconds or settings.agy_timeout_seconds
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=effective_timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return AgentResult(backend="agy", success=False, output="Antigravity CLI timed out.")

    stdout = stdout_b.decode("utf-8", errors="replace").strip()
    stderr = stderr_b.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        return AgentResult(
            backend="agy",
            success=False,
            output=f"agy exited with {proc.returncode}: {stderr or stdout}",
        )

    try:
        payload = json.loads(stdout)
        return AgentResult(
            backend="agy",
            success=payload.get("status") == "SUCCESS",
            output=payload.get("response") or payload.get("error") or stdout,
        )
    except json.JSONDecodeError:
        return AgentResult(backend="agy", success=True, output=stdout)


async def run_custom_cli_agent(
    prompt: str,
    template: str,
    cwd: Path | None = None,
    timeout_seconds: int = 300,
) -> AgentResult:
    """Execute an external agent CLI command configured via CUSTOM_AGENT_CMD."""
    if not template.strip():
        return AgentResult(
            backend="custom",
            success=False,
            output="CUSTOM_AGENT_CMD is empty.",
        )

    cmd_str = template.replace("{prompt}", prompt)
    args = shlex.split(cmd_str)
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        return AgentResult(backend="custom", success=False, output="Custom agent timed out.")

    stdout = stdout_b.decode("utf-8", errors="replace").strip()
    stderr = stderr_b.decode("utf-8", errors="replace").strip()
    return AgentResult(
        backend="custom",
        success=proc.returncode == 0,
        output=stdout if proc.returncode == 0 else f"Exit {proc.returncode}: {stderr or stdout}",
    )


async def run_agent(
    prompt: str,
    backend_override: str | None = None,
    system_instruction: str | None = None,
    use_google_search: bool = False,
    cwd: Path | None = None,
) -> AgentResult:
    """Dispatch a prompt to `auto` (`agy` if available, else `gemini`), `agy`, `gemini`, or `custom`."""
    settings = get_settings()
    backend = (backend_override or settings.agent_backend or "auto").lower()

    if backend == "auto":
        backend = "agy" if shutil.which("agy") else "gemini"

    if backend == "agy":
        return await run_agy_cli_agent(prompt, cwd=cwd)
    if backend == "custom":
        return await run_custom_cli_agent(prompt, settings.custom_agent_cmd, cwd=cwd)
    return await run_gemini_agent(
        prompt=prompt,
        settings=settings,
        system_instruction=system_instruction,
        use_google_search=use_google_search,
        cwd=cwd,
    )
