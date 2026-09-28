"""开放事件流测试 — 票据生命周期 / 事件总线 / SSE 端点。"""
from __future__ import annotations

import asyncio
import json
import queue
from pathlib import Path

import pytest

from app.services import alert_store, event_tickets
from app.services.events import EventBus, bus, sse_format

# ── 票据 ──────────────────────────────────────────────

def test_ticket_roundtrip_single_use():
    ticket, ttl = event_tickets.issue(["read:analysis"])
    assert ticket.startswith("tse_") and ttl == 60
    assert event_tickets.consume(ticket) == ["read:analysis"]
    assert event_tickets.consume(ticket) is None  # 一次性


def test_ticket_invalid_and_expired(monkeypatch: pytest.MonkeyPatch):
    assert event_tickets.consume("tse_nonexistent") is None
    assert event_tickets.consume("") is None
    assert event_tickets.consume("not_a_ticket") is None

    # 过期: 签发后把时钟推过 TTL
    ticket, _ = event_tickets.issue(["read:market"])
    real_monotonic = event_tickets.time.monotonic
    monkeypatch.setattr(event_tickets.time, "monotonic", lambda: real_monotonic() + 61)
    assert event_tickets.consume(ticket) is None


def test_ticket_overflow_guard(monkeypatch: pytest.MonkeyPatch):
    # 上限调小 (避免 1000 张票据污染模块态, 影响后续测试)
    monkeypatch.setattr(event_tickets, "_MAX_OUTSTANDING", 2)
    event_tickets.issue(["read:market"])
    event_tickets.issue(["read:market"])
    with pytest.raises(RuntimeError):
        event_tickets.issue(["read:market"])


# ── 总线 ──────────────────────────────────────────────

def test_bus_pubsub_and_scope_tag():
    b = EventBus()
    q = b.subscribe()
    b.publish("alert", {"rule_id": "r1"}, scope="read:analysis")
    ev = q.get_nowait()
    assert ev["type"] == "alert" and ev["scope"] == "read:analysis"
    assert ev["data"] == {"rule_id": "r1"}
    b.unsubscribe(q)
    b.publish("alert", {"x": 1}, scope="read:analysis")
    with pytest.raises(queue.Empty):
        q.get_nowait()


def test_bus_slow_consumer_drops_oldest():
    b = EventBus()
    q = b.subscribe()
    for i in range(250):  # 超过 _QUEUE_MAX=200, 前 50 个被挤掉
        b.publish("tick", {"i": i}, scope="*")
    first = q.get_nowait()["data"]["i"]
    assert first == 50
    assert q.get_nowait()["data"]["i"] == 51


def test_sse_format_frame():
    frame = sse_format({"type": "alert", "scope": "read:analysis", "data": {"a": 1}, "ts": 1700000000.123})
    lines = frame.splitlines()
    assert lines[0] == "id: 1700000000123"
    assert lines[1] == "event: alert"
    assert json.loads(lines[2].removeprefix("data: ")) == {"a": 1}
    assert frame.endswith("\n\n")


# ── 告警落盘 → 总线联动 ────────────────────────────────

def test_alert_append_publishes_to_bus(tmp_path: Path):
    q = bus.subscribe()
    try:
        alert_store.append(tmp_path, {"ts": 1, "rule_id": "r1", "source": "test"})
        ev = q.get_nowait()
        assert ev["type"] == "alert"
        assert ev["scope"] == "read:analysis"
        assert ev["data"]["rule_id"] == "r1"
    finally:
        bus.unsubscribe(q)


def test_alert_append_many_publishes_each(tmp_path: Path):
    q = bus.subscribe()
    try:
        alert_store.append_many(tmp_path, [
            {"ts": 1, "rule_id": "a"}, {"ts": 2, "rule_id": "b"},
        ])
        assert q.get_nowait()["data"]["rule_id"] == "a"
        assert q.get_nowait()["data"]["rule_id"] == "b"
    finally:
        bus.unsubscribe(q)


# ── SSE 端点 (直接调函数, 消费前两帧后关闭) ────────────

@pytest.mark.asyncio
async def test_event_stream_endpoint_frames(tmp_path: Path):
    from app.api import events as events_api

    ticket, _ = event_tickets.issue(["read:analysis"])
    response = await events_api.event_stream(ticket=ticket)
    assert response.media_type == "text/event-stream"

    agen = response.body_iterator
    # 帧 1: hello (回显票据 scope)
    hello = await asyncio.wait_for(agen.__anext__(), timeout=3)
    assert "event: hello" in hello
    assert "read:analysis" in hello
    # 帧 2: 总线事件, 票据 scope 覆盖 → 推送
    bus.publish("alert", {"rule_id": "r9"}, scope="read:analysis")
    # 帧 3: 票据 scope 不覆盖 → 不推送 (只发覆盖的)
    bus.publish("secret", {"x": 1}, scope="paper:trade")
    bus.publish("alert", {"rule_id": "r10"}, scope="read:analysis")
    second = await asyncio.wait_for(agen.__anext__(), timeout=3)
    assert "event: alert" in second and "r9" in second
    third = await asyncio.wait_for(agen.__anext__(), timeout=3)
    assert "r10" in third and "secret" not in third
    await agen.aclose()


@pytest.mark.asyncio
async def test_event_stream_rejects_bad_ticket():
    from app.api import events as events_api

    response = await events_api.event_stream(ticket="tse_gone")
    assert response.status_code == 401
    response = await events_api.event_stream(ticket="")
    assert response.status_code == 401
