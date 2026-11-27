"""Concurrent worker pool, fan-out/fan-in orchestrator, and observability metrics."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from autoagenthub.agent_runner import run_agent
from autoagenthub.config import get_settings
from autoagenthub.github_tools import apply_repo_code_change, audit_repository_summary
from autoagenthub.jobs_tools import find_daily_jobs
from autoagenthub.messaging_tools import send_telegram_message, send_whatsapp_message
from autoagenthub.news_tools import get_latest_news
from autoagenthub.resilience import CircuitBreaker, TokenBucketRateLimiter
from autoagenthub.task_store import DurableTaskStore

logger = logging.getLogger("autoagenthub.orchestrator")

TaskHandler = Callable[[dict[str, Any]], Awaitable[str]]


async def run_parallel_digest(
    repo: str | None = None,
    news_topic: str = "AI and software engineering",
    job_query: str | None = None,
) -> str:
    """Fan-out/fan-in: concurrently audit GitHub, fetch news, and discover jobs in parallel."""
    t0 = time.perf_counter()

    async def _github() -> str:
        if not (repo or get_settings().github_default_repo):
            return "GitHub: Skipped (no GITHUB_DEFAULT_REPO configured)."
        return await audit_repository_summary(repo=repo)

    async def _news() -> str:
        return await get_latest_news(news_topic, max_results=5)

    async def _jobs() -> str:
        jobs = await find_daily_jobs(query=job_query, limit=5)
        if not jobs:
            return "Jobs: No new unseen matches found."
        return "\n".join(f"- [{j['id']}] {j['title']} @ {j['company']} ({j['url']})" for j in jobs)

    gh_res, news_res, jobs_res = await asyncio.gather(
        _github(), _news(), _jobs(), return_exceptions=True
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    def _fmt(val: Any, label: str) -> str:
        if isinstance(val, Exception):
            return f"{label} error: {val}"
        return str(val)

    return (
        f"Parallel Digest (completed in {elapsed_ms:.0f} ms)\n"
        f"{'=' * 40}\n"
        f"[1] GitHub Audit:\n{_fmt(gh_res, 'GitHub')}\n\n"
        f"[2] Latest News ({news_topic}):\n{_fmt(news_res, 'News')}\n\n"
        f"[3] Job Matches:\n{_fmt(jobs_res, 'Jobs')}"
    )


class ConcurrentOrchestrator:
    """Manages an async worker pool pulling from DurableTaskStore with rate limits and circuit breakers."""

    def __init__(self, store: DurableTaskStore | None = None) -> None:
        settings = get_settings()
        self.store = store or DurableTaskStore(settings.data_dir / "autoagenthub.db")
        self.concurrency = settings.worker_concurrency
        self.max_queue_size = settings.max_queue_size
        self.lease_seconds = settings.task_lease_seconds

        self._workers: list[asyncio.Task[None]] = []
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._active_workers = 0
        self._processed = 0
        self._failed = 0
        self._total_latency_ms = 0.0

        self.breakers: dict[str, CircuitBreaker] = {
            "github": CircuitBreaker("github", settings.circuit_breaker_threshold, settings.circuit_breaker_cooldown_sec),
            "code": CircuitBreaker("code", settings.circuit_breaker_threshold, settings.circuit_breaker_cooldown_sec),
            "news": CircuitBreaker("news", settings.circuit_breaker_threshold, settings.circuit_breaker_cooldown_sec),
            "jobs": CircuitBreaker("jobs", settings.circuit_breaker_threshold, settings.circuit_breaker_cooldown_sec),
            "agent": CircuitBreaker("agent", settings.circuit_breaker_threshold, settings.circuit_breaker_cooldown_sec),
            "digest": CircuitBreaker("digest", settings.circuit_breaker_threshold, settings.circuit_breaker_cooldown_sec),
        }
        self.rate_limiter = TokenBucketRateLimiter(
            rate_per_minute=settings.rate_limit_per_min,
            capacity=max(5, settings.rate_limit_per_min),
        )
        self._handlers: dict[str, TaskHandler] = {
            "github_audit": self._handle_github,
            "code_change": self._handle_code_change,
            "news": self._handle_news,
            "jobs": self._handle_jobs,
            "agent": self._handle_agent,
            "digest": self._handle_digest,
        }

    def register_handler(self, task_type: str, handler: TaskHandler) -> None:
        """Register a custom async task handler."""
        self._handlers[task_type] = handler

    async def _handle_github(self, payload: dict[str, Any]) -> str:
        return await audit_repository_summary(
            repo=payload.get("repo"), focus=payload.get("focus", "")
        )

    async def _handle_code_change(self, payload: dict[str, Any]) -> str:
        return await apply_repo_code_change(
            instruction=payload.get("instruction", ""),
            repo=payload.get("repo"),
            backend=payload.get("backend", "agy"),
        )

    async def _handle_news(self, payload: dict[str, Any]) -> str:
        return await get_latest_news(
            topic=payload.get("topic", "technology"),
            max_results=int(payload.get("max_results", 5)),
        )

    async def _handle_jobs(self, payload: dict[str, Any]) -> str:
        jobs = await find_daily_jobs(query=payload.get("query"), limit=int(payload.get("limit", 5)))
        if not jobs:
            return "No new jobs found."
        return "New jobs queued:\n\n" + "\n\n".join(
            f"[{j['id']}] {j['title']} @ {j['company']}\n{j['url']}\nApprove: /apply {j['id']}"
            for j in jobs
        )

    async def _handle_agent(self, payload: dict[str, Any]) -> str:
        res = await run_agent(
            prompt=payload.get("prompt", ""),
            backend_override=payload.get("backend"),
        )
        if not res.success:
            raise RuntimeError(res.output)
        return res.output

    async def _handle_digest(self, payload: dict[str, Any]) -> str:
        return await run_parallel_digest(
            repo=payload.get("repo"),
            news_topic=payload.get("news_topic", "AI and software engineering"),
            job_query=payload.get("job_query"),
        )

    async def submit(
        self,
        task_type: str,
        payload: dict[str, Any],
        priority: int = 5,
        dedupe_key: str | None = None,
    ) -> dict[str, Any]:
        """Enqueue a task with backpressure protection."""
        stats = await self.store.get_stats()
        if stats["queued"] >= self.max_queue_size:
            raise RuntimeError(f"Queue full ({stats['queued']}/{self.max_queue_size}). Try again later.")

        settings = get_settings()
        task = await self.store.enqueue(
            task_type=task_type,
            payload=payload,
            priority=priority,
            max_retries=settings.task_max_retries,
            dedupe_key=dedupe_key,
        )
        self._wake_event.set()
        return task

    async def start(self) -> None:
        """Start the concurrent worker pool."""
        self._stop_event.clear()
        self._workers = [
            asyncio.create_task(self._worker_loop(f"worker-{i}"), name=f"worker-{i}")
            for i in range(self.concurrency)
        ]
        logger.info("Started %d concurrent workers.", self.concurrency)

    async def stop(self) -> None:
        """Gracefully stop all workers."""
        self._stop_event.set()
        self._wake_event.set()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers.clear()
        logger.info("Stopped concurrent worker pool.")

    async def _deliver_reply(self, payload: dict[str, Any], message: str) -> None:
        channel = payload.get("reply_channel")
        target = payload.get("reply_to")
        if channel == "telegram" and target:
            await send_telegram_message(message, chat_id=target)
        elif channel == "whatsapp" and target:
            await send_whatsapp_message(message, recipient=target)

    async def _worker_loop(self, worker_id: str) -> None:
        while not self._stop_event.is_set():
            task = await self.store.claim_next(worker_id, lease_seconds=self.lease_seconds)
            if not task:
                self._wake_event.clear()
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
                continue

            self._active_workers += 1
            t0 = time.perf_counter()
            payload = json.loads(task["payload_json"])
            task_type = task["task_type"]
            handler = self._handlers.get(task_type)
            breaker_key = task_type.split("_")[0]
            breaker = self.breakers.get(breaker_key) or self.breakers["agent"]

            try:
                if not handler:
                    raise ValueError(f"Unknown task_type '{task_type}'")
                await self.rate_limiter.acquire()
                result_text = await breaker.call(lambda: handler(payload))
                await self.store.mark_completed(task["id"], result_text)
                self._processed += 1
                self._total_latency_ms += (time.perf_counter() - t0) * 1000.0
                await self._deliver_reply(payload, result_text)
            except Exception as exc:
                self._failed += 1
                outcome = await self.store.mark_failed_or_retry(task["id"], str(exc))
                logger.warning("Task %s (%s) %s: %s", task["id"], task_type, outcome, exc)
                if outcome == "dead_lettered":
                    await self._deliver_reply(
                        payload, f"Task {task['id']} failed after retries: {exc}"
                    )
            finally:
                self._active_workers -= 1

    async def metrics(self) -> dict[str, Any]:
        """Return live concurrency, queue, circuit-breaker, and latency metrics."""
        queue_stats = await self.store.get_stats()
        avg_ms = (self._total_latency_ms / self._processed) if self._processed else 0.0
        return {
            "workers_configured": self.concurrency,
            "workers_active": self._active_workers,
            "tasks_processed": self._processed,
            "tasks_failed": self._failed,
            "avg_latency_ms": round(avg_ms, 2),
            "queue": queue_stats,
            "circuit_breakers": {
                name: {"state": cb.state.value, "failures": cb.failure_count}
                for name, cb in self.breakers.items()
            },
        }
