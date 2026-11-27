"""24/7 Concurrent FastAPI + Worker Pool + Telegram Bot + WhatsApp Webhook + Scheduler server."""

from __future__ import annotations

import hashlib
import hmac
import logging
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from autoagenthub.agent_runner import run_agent
from autoagenthub.config import get_settings
from autoagenthub.github_tools import apply_repo_code_change, audit_repository_summary
from autoagenthub.jobs_tools import approve_job_application, find_daily_jobs
from autoagenthub.messaging_tools import send_telegram_message, send_whatsapp_message
from autoagenthub.news_tools import get_latest_news
from autoagenthub.orchestrator import ConcurrentOrchestrator, run_parallel_digest

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("autoagenthub")

orchestrator: ConcurrentOrchestrator | None = None


def get_orchestrator() -> ConcurrentOrchestrator:
    global orchestrator
    if orchestrator is None:
        orchestrator = ConcurrentOrchestrator()
    return orchestrator


def parse_message_to_task(text: str) -> tuple[str, dict[str, Any]]:
    """Map a user message or slash command to a durable (task_type, payload) pair."""
    cleaned = text.strip()
    lower = cleaned.lower()

    if lower.startswith("/repo"):
        parts = cleaned.split(maxsplit=1)
        return "github_audit", {"repo": parts[1].strip() if len(parts) > 1 else None}
    if lower.startswith("/code"):
        rest = cleaned[5:].strip()
        parts = rest.split(maxsplit=1)
        if len(parts) == 2 and "/" in parts[0] and " " not in parts[0]:
            return "code_change", {"repo": parts[0], "instruction": parts[1], "backend": "auto"}
        return "code_change", {"instruction": rest, "backend": "auto"}
    if lower.startswith("/agy"):
        return "agent", {"backend": "agy", "prompt": cleaned[4:].strip()}
    if lower.startswith("/news"):
        return "news", {"topic": cleaned[5:].strip() or "technology and AI"}
    if lower.startswith("/jobs"):
        return "jobs", {"query": cleaned[5:].strip() or None, "limit": 5}
    if lower.startswith("/digest"):
        return "digest", {"news_topic": cleaned[7:].strip() or "AI and software engineering"}
    if lower.startswith("/agent"):
        parts = cleaned.split(maxsplit=2)
        if len(parts) >= 3:
            return "agent", {"backend": parts[1], "prompt": parts[2]}

    if any(k in lower for k in ("fix ", "edit ", "change ", "refactor ", "update code")) and (
        "repo" in lower or "github" in lower
    ):
        return "code_change", {"instruction": cleaned, "backend": "auto"}
    if any(k in lower for k in ("github", "bug", "pull request", "documentation")) and "repo" in lower:
        return "github_audit", {"focus": cleaned}
    if any(k in lower for k in ("latest news", "news about", "search the web", "headlines")):
        return "news", {"topic": cleaned}
    if any(k in lower for k in ("find jobs", "job search", "daily jobs")):
        return "jobs", {"limit": 5}

    return "agent", {"prompt": cleaned}


async def route_user_message(text: str) -> str:
    """Direct synchronous/async execution helper for interactive commands & unit tests."""
    cleaned = text.strip()
    lower = cleaned.lower()

    if lower in {"/start", "/help", "help"}:
        return (
            "AutoAgentHub Commands:\n"
            "- /repo [owner/repo] : Check GitHub bugs, PRs, and doc gaps\n"
            "- /code [owner/repo] <task> : Run Antigravity on your GitHub repo & open a PR\n"
            "- /agy <prompt> : Run Antigravity CLI directly on any task\n"
            "- /news <topic> : Search the web for latest news\n"
            "- /jobs [query] : Find matching jobs & draft pitches\n"
            "- /apply <job_id> : Approve a queued job application\n"
            "- /digest [topic] : Run GitHub + News + Jobs concurrently in parallel\n"
            "- /status : View live worker pool, queue, and circuit breaker metrics\n"
            "- /agent <gemini|agy|custom> <prompt> : Run a specific agent backend\n"
            "Or just type any problem in plain text!"
        )

    if lower == "/status":
        return str(await get_orchestrator().metrics())

    if lower.startswith("/apply"):
        job_id = cleaned[6:].strip()
        if not job_id:
            return "Usage: /apply <job_id>"
        approved = approve_job_application(job_id)
        if not approved:
            return f"Job ID '{job_id}' not found in pending queue."
        return (
            f"Approved: {approved['title']} @ {approved['company']}\n"
            f"Apply URL: {approved['url']}\n\n"
            f"Tailored Note:\n{approved['draft_cover_note']}"
        )

    task_type, payload = parse_message_to_task(cleaned)
    if task_type == "github_audit":
        return await audit_repository_summary(repo=payload.get("repo"), focus=payload.get("focus", ""))
    if task_type == "code_change":
        return await apply_repo_code_change(
            instruction=payload.get("instruction", cleaned),
            repo=payload.get("repo"),
            backend=payload.get("backend", "agy"),
        )
    if task_type == "news":
        return await get_latest_news(payload["topic"])
    if task_type == "jobs":
        jobs = await find_daily_jobs(query=payload.get("query"), limit=5)
        if not jobs:
            return "No new jobs found right now."
        return "New jobs queued:\n\n" + "\n\n".join(
            f"[{j['id']}] {j['title']} @ {j['company']} ({j['location']})\n{j['url']}\nReply: /apply {j['id']}"
            for j in jobs
        )
    if task_type == "digest":
        return await run_parallel_digest(news_topic=payload.get("news_topic", "AI"))

    result = await run_agent(payload["prompt"], backend_override=payload.get("backend"))
    return result.output


async def scheduled_daily_jobs() -> None:
    """Enqueue daily job search onto the durable worker pool."""
    settings = get_settings()
    orch = get_orchestrator()
    if settings.telegram_bot_token and settings.telegram_default_chat_id:
        await orch.submit(
            "jobs",
            {"limit": 5, "reply_channel": "telegram", "reply_to": settings.telegram_default_chat_id},
            priority=3,
        )
    if settings.whatsapp_access_token and settings.whatsapp_default_recipient:
        await orch.submit(
            "jobs",
            {"limit": 5, "reply_channel": "whatsapp", "reply_to": settings.whatsapp_default_recipient},
            priority=3,
        )


async def scheduled_daily_repo_check() -> None:
    """Enqueue daily GitHub repo audit onto the durable worker pool."""
    settings = get_settings()
    if not settings.github_default_repo:
        return
    orch = get_orchestrator()
    if settings.telegram_bot_token and settings.telegram_default_chat_id:
        await orch.submit(
            "github_audit",
            {
                "repo": settings.github_default_repo,
                "reply_channel": "telegram",
                "reply_to": settings.telegram_default_chat_id,
            },
            priority=3,
        )
    if settings.whatsapp_access_token and settings.whatsapp_default_recipient:
        await orch.submit(
            "github_audit",
            {
                "repo": settings.github_default_repo,
                "reply_channel": "whatsapp",
                "reply_to": settings.whatsapp_default_recipient,
            },
            priority=3,
        )


def _is_allowed_telegram(chat_id: int) -> bool:
    allowed = get_settings().allowed_telegram_ids
    return not allowed or chat_id in allowed


async def _tg_handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_chat:
        return
    if not _is_allowed_telegram(update.effective_chat.id):
        await update.effective_message.reply_text("Unauthorized chat ID.")
        return

    text = update.effective_message.text or ""
    await update.effective_message.reply_text("Queued / processing...")
    reply = await route_user_message(text)

    if text.strip().lower().startswith("/jobs"):
        jobs = [line.split("]")[0].lstrip("[") for line in reply.splitlines() if line.startswith("[")]
        if jobs:
            buttons = [
                [InlineKeyboardButton(f"Approve {jid}", callback_data=f"apply:{jid}")]
                for jid in jobs[:5]
            ]
            await update.effective_message.reply_text(
                reply[:4000], reply_markup=InlineKeyboardMarkup(buttons)
            )
            return

    await update.effective_message.reply_text(reply[:4000])


async def _tg_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    if query.data.startswith("apply:"):
        job_id = query.data.split(":", 1)[1]
        approved = approve_job_application(job_id)
        if approved:
            await query.edit_message_text(
                f"Approved: {approved['title']} @ {approved['company']}\n"
                f"URL: {approved['url']}\n\nTailored Note:\n{approved['draft_cover_note']}"
            )
        else:
            await query.edit_message_text(f"Job '{job_id}' not found.")


def _parse_hhmm(value: str, fallback: tuple[int, int]) -> tuple[int, int]:
    try:
        hh, mm = value.strip().split(":")
        return int(hh), int(mm)
    except Exception:
        return fallback


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    orch = get_orchestrator()
    await orch.start()

    scheduler = AsyncIOScheduler()
    jh, jm = _parse_hhmm(settings.daily_jobs_time, (9, 0))
    rh, rm = _parse_hhmm(settings.daily_repo_check_time, (8, 30))
    scheduler.add_job(scheduled_daily_jobs, CronTrigger(hour=jh, minute=jm), id="daily_jobs")
    scheduler.add_job(scheduled_daily_repo_check, CronTrigger(hour=rh, minute=rm), id="daily_repo")
    scheduler.start()

    tg_app: Application | None = None
    if settings.telegram_bot_token:
        tg_app = Application.builder().token(settings.telegram_bot_token).build()
        tg_app.add_handler(
            CommandHandler(
                ["start", "help", "repo", "code", "agy", "news", "jobs", "apply", "agent", "digest", "status"],
                _tg_handle_text,
            )
        )
        tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _tg_handle_text))
        tg_app.add_handler(CallbackQueryHandler(_tg_callback))
        await tg_app.initialize()
        await tg_app.start()
        if tg_app.updater:
            await tg_app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot polling started.")

    try:
        yield
    finally:
        if tg_app:
            if tg_app.updater:
                await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()
        scheduler.shutdown(wait=False)
        await orch.stop()


app = FastAPI(title="AutoAgentHub", version="0.2.0", lifespan=lifespan)


class TaskSubmitRequest(BaseModel):
    task_type: str = Field(..., description="github_audit | news | jobs | agent | digest")
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=5, ge=1, le=10)
    dedupe_key: str | None = None


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics")
async def metrics() -> dict[str, Any]:
    return await get_orchestrator().metrics()


@app.post("/api/tasks", status_code=202)
async def submit_task(req: TaskSubmitRequest) -> dict[str, Any]:
    """Enqueue a task onto the concurrent worker pool (non-blocking 202 Accepted)."""
    try:
        task = await get_orchestrator().submit(
            task_type=req.task_type,
            payload=req.payload,
            priority=req.priority,
            dedupe_key=req.dedupe_key,
        )
        return {"task_id": task["id"], "status": task["status"]}
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc


@app.get("/api/tasks/{task_id}")
async def get_task_status(task_id: str) -> dict[str, Any]:
    task = await get_orchestrator().store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.get("/api/dlq")
async def list_dead_letter_queue() -> list[dict[str, Any]]:
    return await get_orchestrator().store.list_dlq()


@app.get("/webhook/whatsapp")
async def whatsapp_verify(
    hub_mode: str = Query(default="", alias="hub.mode"),
    hub_verify_token: str = Query(default="", alias="hub.verify_token"),
    hub_challenge: str = Query(default="", alias="hub.challenge"),
) -> PlainTextResponse:
    """Meta WhatsApp Cloud API webhook verification endpoint."""
    settings = get_settings()
    if hub_mode == "subscribe" and hub_verify_token == settings.whatsapp_verify_token:
        return PlainTextResponse(hub_challenge)
    raise HTTPException(status_code=403, detail="Invalid verify token")


@app.post("/webhook/whatsapp", status_code=202)
async def whatsapp_incoming(request: Request) -> dict[str, str]:
    """Enqueue incoming WhatsApp messages onto the durable worker pool immediately."""
    payload = await request.json()
    orch = get_orchestrator()
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                sender = msg.get("from")
                msg_id = msg.get("id")
                body = msg.get("text", {}).get("body", "")
                if sender and body:
                    task_type, task_payload = parse_message_to_task(body)
                    task_payload["reply_channel"] = "whatsapp"
                    task_payload["reply_to"] = sender
                    await orch.submit(
                        task_type=task_type,
                        payload=task_payload,
                        priority=2,
                        dedupe_key=f"wa-{msg_id}" if msg_id else None,
                    )
    return {"status": "accepted"}


def _verify_github_signature(body: bytes, signature_header: str, secret: str) -> bool:
    if not secret:
        return True
    if not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


@app.post("/webhook/github", status_code=202)
async def github_webhook(request: Request) -> dict[str, str]:
    """Trigger Antigravity code changes when @antigravity is mentioned in a GitHub issue or PR comment."""
    settings = get_settings()
    raw_body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_github_signature(raw_body, sig, settings.github_webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid GitHub webhook signature")

    event = request.headers.get("X-GitHub-Event", "")
    payload = await request.json()
    repo_name = payload.get("repository", {}).get("full_name")

    text = ""
    if event == "issue_comment" and payload.get("action") == "created":
        text = payload.get("comment", {}).get("body", "")
    elif event == "issues" and payload.get("action") == "opened":
        issue = payload.get("issue", {})
        text = f"{issue.get('title', '')}\n{issue.get('body', '')}"

    if "@antigravity" in text.lower() and repo_name:
        instruction = text.replace("@antigravity", "").strip()
        task_payload: dict[str, Any] = {
            "repo": repo_name,
            "instruction": instruction,
            "backend": "auto",
        }
        if settings.telegram_bot_token and settings.telegram_default_chat_id:
            task_payload["reply_channel"] = "telegram"
            task_payload["reply_to"] = settings.telegram_default_chat_id
        elif settings.whatsapp_access_token and settings.whatsapp_default_recipient:
            task_payload["reply_channel"] = "whatsapp"
            task_payload["reply_to"] = settings.whatsapp_default_recipient

        delivery_id = request.headers.get("X-GitHub-Delivery")
        await get_orchestrator().submit(
            task_type="code_change",
            payload=task_payload,
            priority=2,
            dedupe_key=f"gh-{delivery_id}" if delivery_id else None,
        )
        return {"status": "queued"}

    return {"status": "ignored"}


def main() -> None:
    settings = get_settings()
    uvicorn.run("autoagenthub.server:app", host="0.0.0.0", port=settings.port, reload=False)


if __name__ == "__main__":
    main()
