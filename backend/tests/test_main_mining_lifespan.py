from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import app.main as main_module


def test_lifespan_holds_mining_process_lock_around_application(monkeypatch) -> None:
    events: list[str] = []

    class LockStub:
        def __init__(self, data_dir) -> None:
            del data_dir
            events.append("lock_created")

        def acquire(self) -> None:
            events.append("lock_acquired")

        def release(self) -> None:
            events.append("lock_released")

    @asynccontextmanager
    async def application_lifespan(_app):
        events.append("application_started")
        try:
            yield
        finally:
            events.append("application_stopped")

    monkeypatch.setattr(main_module, "MiningProcessLock", LockStub)
    monkeypatch.setattr(main_module, "_application_lifespan", application_lifespan)

    async def exercise() -> None:
        async with main_module.lifespan(SimpleNamespace()):
            events.append("request_serving")

    asyncio.run(exercise())

    assert events == [
        "lock_created",
        "lock_acquired",
        "application_started",
        "request_serving",
        "application_stopped",
        "lock_released",
    ]


def test_lifespan_releases_lock_when_application_shutdown_raises(monkeypatch) -> None:
    events: list[str] = []

    class LockStub:
        def __init__(self, data_dir) -> None:
            del data_dir

        def acquire(self) -> None:
            events.append("acquired")

        def release(self) -> None:
            events.append("released")

    @asynccontextmanager
    async def application_lifespan(_app):
        try:
            yield
        finally:
            raise RuntimeError("shutdown failed")

    monkeypatch.setattr(main_module, "MiningProcessLock", LockStub)
    monkeypatch.setattr(main_module, "_application_lifespan", application_lifespan)

    async def exercise() -> None:
        async with main_module.lifespan(SimpleNamespace()):
            pass

    with pytest.raises(RuntimeError, match="shutdown failed"):
        asyncio.run(exercise())

    assert events == ["acquired", "released"]


def test_lifespan_logs_true_cause_when_lock_held(monkeypatch, caplog) -> None:
    """抢锁失败: 先落 ERROR 日志 (桌面版失败页指向 desktop.log 的排查依据) 再上抛。

    uvicorn 会吞掉 lifespan 异常只留一行笼统的 startup failed, 真因必须由
    app 自己先写日志, 否则用户按失败页指引打开 desktop.log 查无对证。
    """
    from app.services.mining_process_lock import MiningProcessLockError

    class LockStub:
        def __init__(self, data_dir) -> None:
            del data_dir

        def acquire(self) -> None:
            raise MiningProcessLockError("another process owns mining")

        def release(self) -> None:
            raise AssertionError("acquire 失败后不应调用 release")

    monkeypatch.setattr(main_module, "MiningProcessLock", LockStub)

    async def exercise() -> None:
        async with main_module.lifespan(SimpleNamespace()):
            pass

    with (
        caplog.at_level("ERROR", logger="app.main"),
        pytest.raises(MiningProcessLockError),
    ):
        asyncio.run(exercise())

    assert "挖掘锁" in caplog.text
