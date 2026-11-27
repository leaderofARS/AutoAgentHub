"""Unit tests for AutoAgentHub core routing, config, and job approval queue."""

from __future__ import annotations

import json
import pytest

from autoagenthub.config import Settings
from autoagenthub.jobs_tools import approve_job_application, list_pending_jobs
from autoagenthub.server import route_user_message


def test_settings_allowed_telegram_ids() -> None:
    s = Settings(telegram_allowed_chat_ids="12345, -67890, invalid")
    assert s.allowed_telegram_ids == {12345, -67890}


@pytest.mark.asyncio
async def test_route_help_command() -> None:
    reply = await route_user_message("/help")
    assert "/repo" in reply
    assert "/news" in reply
    assert "/jobs" in reply


def test_job_queue_and_approval(tmp_path, monkeypatch) -> None:
    queue_file = tmp_path / "pending_jobs.json"
    queue_file.write_text(
        json.dumps(
            [
                {
                    "id": "remotive-1",
                    "title": "Python AI Engineer",
                    "company": "Acme",
                    "url": "https://example.com/job/1",
                    "draft_cover_note": "Hello Acme!",
                    "status": "pending_approval",
                }
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "autoagenthub.jobs_tools.get_settings",
        lambda: Settings(data_dir=tmp_path),
    )

    pending = list_pending_jobs()
    assert len(pending) == 1
    assert pending[0]["id"] == "remotive-1"

    approved = approve_job_application("remotive-1")
    assert approved is not None
    assert approved["status"] == "approved"
    assert list_pending_jobs() == []
