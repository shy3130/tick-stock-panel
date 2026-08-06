import asyncio
from types import SimpleNamespace

import pytest

from app.api.kline import extend_minute_history
from app.services.pipeline_jobs import job_store


class _Request:
    def __init__(self) -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(repo=object()))

    async def json(self) -> dict:
        return {"value": 1, "unit": "month"}


@pytest.mark.asyncio
async def test_month_extend_is_not_blocked_by_capability_gate(monkeypatch):
    scheduled = []

    def discard_task(coro):
        scheduled.append(coro)
        coro.close()

    monkeypatch.setattr(job_store, "create", lambda: "job-1")
    monkeypatch.setattr(job_store, "get", lambda _job_id: None)
    monkeypatch.setattr(asyncio, "create_task", discard_task)

    result = await extend_minute_history(_Request())

    assert result == {"status": "started", "job_id": "job-1"}
    assert len(scheduled) == 1
