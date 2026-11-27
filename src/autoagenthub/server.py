"""24/7 Concurrent FastAPI + Worker Pool + Telegram Bot (Polling/Webhook) + WhatsApp + GitHub Webhook."""

from __future__ import annotations

import asyncio
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
from autoagenthub.github_tools import (
    apply_repo_code_change,
    audit_repository_summary,
    create_github_issue,
)
from autoagenthub.jobs_tools import approve_job_application, find_daily_jobs
from autoagenthub.news_tools import get_latest_news
from autoagenthub.orchestrator import ConcurrentOrchestrator, run_parallel_digest

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("autoagenthub")

orchestrator: ConcurrentOrchestrator | None = None
telegram_app: Application | None = None


def get_orchestrator() -> ConcurrentOrchestrator:
    global orchestrator
    if orchestrator is None:
        orchestrator = ConcurrentOrchestrator()
    return orchestrator


def _main_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Repo Audit", callback_data="menu:repo"),
                InlineKeyboardButton("Latest News", callback_data="menu:news"),
            ],
            [
                InlineKeyboardButton("Find Jobs", callback_data="menu:jobs"),
                InlineKeyboardButton("Parallel Digest", callback_data="menu:digest"),
            ],
            [
                InlineKeyboardButton("System Status", callback_data="menu:status"),
                InlineKeyboardButton("Recent Tasks", callback_data="menu:tasks"),
            ],
        ]
    )


def _chunk_text(text: str, limit: int = 3800) -> list[str]:
    if len(text) <= limit:
        return [text]
    return [text[i : i + limit] for i in range(0, len(text), limit)]


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
    """Direct execution helper for interactive commands and unit tests."""
    cleaned = text.strip()
    lower = cleaned.lower()

    if lower in {"/start", "/help", "/menu", "help"}:
        return (
            "AutoAgentHub Commands:\n"
            "- /menu : Open quick-action buttons\n"
            "- /repo [owner/repo] : Check GitHub bugs, PRs, and doc gaps\n"
            "- /code [owner/repo] <task> : Edit code in GitHub & open a PR\n"
            "- /issue [owner/repo] <title> | <desc> : Open a GitHub issue\n"
            "- /news <topic> : Search the web for latest news\n"
            "- /jobs [query] : Find matching jobs & draft pitches\n"
            "- /apply <job_id> : Approve a queued job application\n"
            "- /digest [topic] : Run GitHub + News + Jobs concurrently\n"
            "- /remind <minutes> <msg> : Set a Telegram reminder\n"
            "- /tasks | /dlq | /status : Inspect background tasks & metrics\n"
            "- /agy <prompt> | /agent <backend> <prompt>\n"
            "Or just type any problem in plain text!"
        )

    if lower == "/status":
        m = await get_orchestrator().metrics()
        q = m["queue"]
        return (
            f"Workers: {m['workers_active']}/{m['workers_configured']} active\n"
            f"Processed: {m['tasks_processed']} | Failed: {m['tasks_failed']} | Avg: {m['avg_latency_ms']} ms\n"
            f"Queue -> queued:{q['queued']} running:{q['running']} completed:{q['completed']} dlq:{q['dlq']}"
        )

    if lower == "/tasks":
        tasks = await get_orchestrator().store.list_tasks(limit=8)
        if not tasks:
            return "No background tasks recorded yet."
        return "Recent Tasks:\n" + "\n".join(
            f"- [{t['id']}] {t['task_type']} | {t['status']} (attempts: {t['attempts']})"
            for t in tasks
        )

    if lower == "/dlq":
        items = await get_orchestrator().store.list_dlq(limit=10)
        if not items:
            return "Dead-Letter Queue is empty."
        return "Failed Tasks (DLQ):\n" + "\n".join(
            f"- [{d['id']}] {d['task_type']} -> {d['error_text'][:120]}" for d in items
        )

    if lower.startswith("/issue"):
        rest = cleaned[6:].strip()
        if not rest:
            return "Usage: /issue [owner/repo] <title> | <description>"
        repo = None
        parts = rest.split(maxsplit=1)
        if len(parts) == 2 and "/" in parts[0] and " " not in parts[0]:
            repo, rest = parts[0], parts[1]
        title, _, body = rest.partition("|")
        created = await create_github_issue(title=title.strip(), body=body.strip(), repo=repo)
        return f"Issue #{created['number']} created: {created['url']}"

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
            backend=payload.get("backend", "auto"),
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


async def _send_tg_reply(message_obj, reply: str, with_job_buttons: bool = False) -> None:
    if with_job_buttons:
        jobs = [line.split("]")[0].lstrip("[") for line in reply.splitlines() if line.startswith("[")]
        if jobs:
            buttons = [
                [InlineKeyboardButton(f"Approve {jid}", callback_data=f"apply:{jid}")]
                for jid in jobs[:5]
            ]
            await message_obj.reply_text(reply[:3800], reply_markup=InlineKeyboardMarkup(buttons))
            return
    for chunk in _chunk_text(reply):
        await message_obj.reply_text(chunk)


async def _tg_handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_chat:
        return
    if not _is_allowed_telegram(update.effective_chat.id):
        await update.effective_message.reply_text("Unauthorized chat ID.")
        return

    text = (update.effective_message.text or "").strip()
    lower = text.lower()

    if lower in {"/start", "/menu"}:
        reply = await route_user_message(text)
        await update.effective_message.reply_text(reply, reply_markup=_main_menu_markup())
        return

    if lower.startswith("/remind"):
        parts = text.split(maxsplit=2)
        if len(parts) < 3 or not parts[1].isdigit():
            await update.effective_message.reply_text("Usage: /remind <minutes> <message>")
            return
        minutes = int(parts[1])
        reminder_msg = parts[2]
        chat_id = update.effective_chat.id

        async def _fire() -> None:
            await asyncio.sleep(minutes * 60)
            await context.bot.send_message(chat_id=chat_id, text=f"Reminder: {reminder_msg}")

        asyncio.create_task(_fire())
        await update.effective_message.reply_text(f"Reminder set for {minutes} minute(s).")
        return

    await update.effective_message.reply_text("Working on it...")
    reply = await route_user_message(text)
    await _send_tg_reply(update.effective_message, reply, with_job_buttons=lower.startswith("/jobs"))


async def _tg_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    if query.data.startswith("menu:"):
        action = query.data.split(":", 1)[1]
        cmd_map = {
            "repo": "/repo",
            "news": "/news AI and technology",
            "jobs": "/jobs",
            "digest": "/digest",
            "status": "/status",
            "tasks": "/tasks",
        }
        cmd = cmd_map.get(action, "/help")
        await query.edit_message_text(f"Running {cmd}...")
        reply = await route_user_message(cmd)
        if query.message:
            await _send_tg_reply(query.message, reply, with_job_buttons=(action == "jobs"))
        return

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
    global telegram_app
    settings = get_settings()
    orch = get_orchestrator()
    await orch.start()

    scheduler = AsyncIOScheduler()
    jh, jm = _parse_hhmm(settings.daily_jobs_time, (9, 0))
    rh, rm = _parse_hhmm(settings.daily_repo_check_time, (8, 30))
    scheduler.add_job(scheduled_daily_jobs, CronTrigger(hour=jh, minute=jm), id="daily_jobs")
    scheduler.add_job(scheduled_daily_repo_check, CronTrigger(hour=rh, minute=rm), id="daily_repo")
    scheduler.start()

    if settings.telegram_bot_token:
        telegram_app = Application.builder().token(settings.telegram_bot_token).build()
        telegram_app.add_handler(
            CommandHandler(
                [
                    "start",
                    "help",
                    "menu",
                    "repo",
                    "code",
                    "issue",
                    "agy",
                    "news",
                    "jobs",
                    "apply",
                    "agent",
                    "digest",
                    "remind",
                    "tasks",
                    "dlq",
                    "status",
                ],
                _tg_handle_text,
            )
        )
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _tg_handle_text))
        telegram_app.add_handler(CallbackQueryHandler(_tg_callback))
        await telegram_app.initialize()
        await telegram_app.start()

        if settings.telegram_webhook_url:
            webhook_endpoint = f"{settings.telegram_webhook_url.rstrip('/')}/webhook/telegram"
            await telegram_app.bot.set_webhook(url=webhook_endpoint, drop_pending_updates=True)
            logger.info("Telegram webhook registered at %s", webhook_endpoint)
        elif telegram_app.updater:
            await telegram_app.updater.start_polling(drop_pending_updates=True)
            logger.info("Telegram bot polling started.")

    try:
        yield
    finally:
        if telegram_app:
            if telegram_app.updater and telegram_app.updater.running:
                await telegram_app.updater.stop()
            await telegram_app.stop()
            await telegram_app.shutdown()
            telegram_app = None
        scheduler.shutdown(wait=False)
        await orch.stop()


app = FastAPI(title="AutoAgentHub", version="0.3.0", lifespan=lifespan)


class TaskSubmitRequest(BaseModel):
    task_type: str = Field(..., description="github_audit | code_change | news | jobs | agent | digest")
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=5, ge=1, le=10)
    dedupe_key: str | None = None


@app.get("/")
@app.head("/")
async def root() -> dict[str, str]:
    return {"service": "AutoAgentHub", "status": "ok", "docs": "/docs"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics")
async def metrics() -> dict[str, Any]:
    return await get_orchestrator().metrics()


@app.get("/api/tasks")
async def list_tasks(limit: int = Query(default=20, ge=1, le=100)) -> list[dict[str, Any]]:
    return await get_orchestrator().store.list_tasks(limit=limit)


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


@app.post("/api/dlq/{dlq_id}/retry", status_code=202)
async def retry_dlq_task(dlq_id: str) -> dict[str, Any]:
    retried = await get_orchestrator().store.retry_dlq_item(dlq_id)
    if not retried:
        raise HTTPException(status_code=404, detail="DLQ entry not found")
    return {"status": "requeued", "task_id": retried["id"]}


@app.post("/api/cron/trigger", status_code=202)
async def trigger_cron(job: str = Query(default="daily_jobs")) -> dict[str, str]:
    """Manually or externally trigger scheduled jobs (useful for cron-job.org)."""
    if job == "daily_jobs":
        await scheduled_daily_jobs()
    elif job == "daily_repo":
        await scheduled_daily_repo_check()
    elif job == "digest":
        settings = get_settings()
        payload: dict[str, Any] = {}
        if settings.telegram_bot_token and settings.telegram_default_chat_id:
            payload = {"reply_channel": "telegram", "reply_to": settings.telegram_default_chat_id}
        await get_orchestrator().submit("digest", payload, priority=3)
    else:
        raise HTTPException(status_code=400, detail="job must be daily_jobs, daily_repo, or digest")
    return {"status": "triggered", "job": job}


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request) -> dict[str, str]:
    """Receive Telegram updates via webhook (wakes Render from sleep on demand)."""
    if telegram_app is None:
        raise HTTPException(status_code=503, detail="Telegram bot is not configured")
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"status": "ok"}


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
