"""Concurrency, resilience, circuit-breaker, DLQ, and async API tests."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from autoagenthub.orchestrator import ConcurrentOrchestrator
from autoagenthub.resilience import CircuitBreaker, CircuitOpenError, CircuitState
from autoagenthub.server import app
from autoagenthub.task_store import DurableTaskStore


@pytest.mark.asyncio
async def test_concurrent_worker_pool_processes_parallel_tasks(tmp_path) -> None:
    store = DurableTaskStore(tmp_path / "tasks.db")
    orch = ConcurrentOrchestrator(store=store)
    orch.concurrency = 4

    async def slow_echo(payload: dict) -> str:
        await asyncio.sleep(0.05)
        return f"done:{payload['n']}"

    orch.register_handler("echo", slow_echo)
    await orch.start()

    try:
        tasks = [await orch.submit("echo", {"n": i}, priority=5) for i in range(12)]
        # Wait until all 12 tasks complete
        for _ in range(50):
            stats = await store.get_stats()
            if stats["completed"] == 12:
                break
            await asyncio.sleep(0.05)

        metrics = await orch.metrics()
        assert metrics["queue"]["completed"] == 12
        assert metrics["tasks_processed"] == 12
        assert metrics["workers_configured"] == 4
        t0 = await store.get_task(tasks[0]["id"])
        assert t0 is not None and t0["result_text"] == "done:0"
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_idempotent_dedupe_key(tmp_path) -> None:
    store = DurableTaskStore(tmp_path / "dedupe.db")
    t1 = await store.enqueue("news", {"topic": "ai"}, dedupe_key="wa-msg-999")
    t2 = await store.enqueue("news", {"topic": "ai"}, dedupe_key="wa-msg-999")
    assert t1["id"] == t2["id"]
    stats = await store.get_stats()
    assert stats["queued"] == 1


@pytest.mark.asyncio
async def test_retries_and_dead_letter_queue(tmp_path) -> None:
    store = DurableTaskStore(tmp_path / "dlq.db")
    task = await store.enqueue("agent", {"prompt": "fail"}, max_retries=2)

    claimed1 = await store.claim_next("worker-0")
    assert claimed1 is not None and claimed1["attempts"] == 1
    assert await store.mark_failed_or_retry(task["id"], "boom-1") == "requeued"

    claimed2 = await store.claim_next("worker-0")
    assert claimed2 is not None and claimed2["attempts"] == 2
    assert await store.mark_failed_or_retry(task["id"], "boom-2") == "dead_lettered"

    dlq = await store.list_dlq()
    assert len(dlq) == 1
    assert dlq[0]["original_task_id"] == task["id"]
    assert dlq[0]["error_text"] == "boom-2"


@pytest.mark.asyncio
async def test_circuit_breaker_opens_and_recovers() -> None:
    cb = CircuitBreaker(name="test-api", failure_threshold=2, cooldown_seconds=0.05)

    async def failing_call() -> str:
        raise RuntimeError("upstream 503")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await cb.call(failing_call)

    assert cb.state == CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        await cb.call(failing_call)

    await asyncio.sleep(0.06)

    async def healthy_call() -> str:
        return "ok"

    assert await cb.call(healthy_call) == "ok"
    assert cb.state == CircuitState.CLOSED


def test_fastapi_async_task_and_metrics_endpoints(tmp_path, monkeypatch) -> None:
    import autoagenthub.server as srv

    store = DurableTaskStore(tmp_path / "api.db")
    orch = ConcurrentOrchestrator(store=store)
    orch.register_handler("echo_api", lambda p: asyncio.sleep(0, result=f"hi {p.get('name')}"))
    monkeypatch.setattr(srv, "orchestrator", orch)

    with TestClient(app) as client:
        resp = client.post(
            "/api/tasks",
            json={"task_type": "echo_api", "payload": {"name": "world"}, "priority": 1},
        )
        assert resp.status_code == 202
        task_id = resp.json()["task_id"]

        metrics_resp = client.get("/metrics")
        assert metrics_resp.status_code == 200
        assert "workers_configured" in metrics_resp.json()

        task_resp = client.get(f"/api/tasks/{task_id}")
        assert task_resp.status_code == 200
        assert task_resp.json()["id"] == task_id


def test_code_and_agy_routing_and_github_webhook(tmp_path, monkeypatch) -> None:
    import autoagenthub.server as srv
    from autoagenthub.server import parse_message_to_task

    t_type, payload = parse_message_to_task("/code octocat/hello fix login bug")
    assert t_type == "code_change"
    assert payload["repo"] == "octocat/hello"
    assert payload["instruction"] == "fix login bug"
    assert payload["backend"] == "auto"

    a_type, a_payload = parse_message_to_task("/agy explain this traceback")
    assert a_type == "agent"
    assert a_payload["backend"] == "agy"

    store = DurableTaskStore(tmp_path / "gh_hook.db")
    orch = ConcurrentOrchestrator(store=store)
    monkeypatch.setattr(srv, "orchestrator", orch)

    with TestClient(app) as client:
        resp = client.post(
            "/webhook/github",
            headers={"X-GitHub-Event": "issue_comment", "X-GitHub-Delivery": "deliv-1"},
            json={
                "action": "created",
                "repository": {"full_name": "octocat/hello"},
                "comment": {"body": "@antigravity add unit tests for auth"},
            },
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "queued"


def test_workspace_path_traversal_blocked(tmp_path) -> None:
    from autoagenthub.agent_runner import _safe_resolve

    inside = _safe_resolve(tmp_path, "src/main.py")
    assert inside == (tmp_path / "src/main.py").resolve()

    with pytest.raises(ValueError):
        _safe_resolve(tmp_path, "../../etc/passwd")


def test_root_list_tasks_dlq_retry_and_cron_endpoints(tmp_path, monkeypatch) -> None:
    import autoagenthub.server as srv

    store = DurableTaskStore(tmp_path / "extra.db")
    orch = ConcurrentOrchestrator(store=store)
    monkeypatch.setattr(srv, "orchestrator", orch)

    async def _fake_submit(task_type, payload, priority=5, dedupe_key=None):
        return {"id": "mock123", "status": "queued"}

    monkeypatch.setattr(orch, "submit", _fake_submit)

    with TestClient(app) as client:
        root_resp = client.get("/")
        assert root_resp.status_code == 200
        assert root_resp.json()["status"] == "ok"

        list_resp = client.get("/api/tasks")
        assert list_resp.status_code == 200
        assert isinstance(list_resp.json(), list)

        cron_resp = client.post("/api/cron/trigger?job=digest")
        assert cron_resp.status_code == 202
        assert cron_resp.json()["job"] == "digest"
