from __future__ import annotations

import asyncio

import pytest

from app.services.ai_attempts import AttemptRegistry
from app.services.ai_structured import CancellationToken


@pytest.mark.asyncio
async def test_registry_singleflight_cancel_and_cleanup():
    registry = AttemptRegistry()
    token = CancellationToken()
    started = asyncio.Event()

    async def worker():
        started.set()
        await token.wait()
        token.raise_if_cancelled()

    task = asyncio.create_task(worker())
    first = registry.register(attempt_id="att-1", request_id="req-1", task=task, token=token)
    second = registry.register(attempt_id="att-1")
    assert first is second
    assert registry.is_running("att-1")
    await started.wait()

    assert registry.cancel("att-1") is True
    assert registry.cancel("missing") is False
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert registry.get("att-1") is None
    assert registry.cancel("att-1") is False


@pytest.mark.asyncio
async def test_registry_releases_successful_and_failed_tasks():
    registry = AttemptRegistry()

    async def ok():
        return 1

    async def fail():
        raise RuntimeError("boom")

    ok_task = asyncio.create_task(ok())
    failed_task = asyncio.create_task(fail())
    registry.register(attempt_id="att-ok", task=ok_task)
    registry.register(attempt_id="att-fail", task=failed_task)
    assert await ok_task == 1
    with pytest.raises(RuntimeError):
        await failed_task
    await asyncio.sleep(0)
    assert registry.get("att-ok") is None
    assert registry.get("att-fail") is None
