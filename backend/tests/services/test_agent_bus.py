import asyncio

from app.services.agent_bus import AgentBus, get_bus


async def _drain(bus: AgentBus, session_id: str) -> list[dict]:
    return [event async for event in bus.subscribe(session_id)]


async def test_late_subscriber_replays_buffer_then_ends_after_close():
    bus = AgentBus()
    bus.begin("s1")
    bus.publish("s1", {"type": "delta", "content": "a"})
    bus.publish("s1", {"type": "done"})
    bus.close("s1")

    events = await _drain(bus, "s1")

    assert events == [{"type": "delta", "content": "a"}, {"type": "done"}]


async def test_live_subscriber_receives_events_until_close():
    bus = AgentBus()
    bus.begin("s1")
    collected: list[dict] = []

    async def watch() -> None:
        async for event in bus.subscribe("s1"):
            collected.append(event)

    task = asyncio.create_task(watch())
    await asyncio.sleep(0)
    bus.publish("s1", {"type": "delta", "content": "x"})
    bus.publish("s1", {"type": "done"})
    bus.close("s1")
    await task

    assert collected == [{"type": "delta", "content": "x"}, {"type": "done"}]


async def test_subscriber_that_joins_mid_run_gets_replay_plus_live():
    bus = AgentBus()
    bus.begin("s1")
    bus.publish("s1", {"type": "delta", "content": "1"})
    collected: list[dict] = []

    async def watch() -> None:
        async for event in bus.subscribe("s1"):
            collected.append(event)

    task = asyncio.create_task(watch())
    await asyncio.sleep(0)
    bus.publish("s1", {"type": "delta", "content": "2"})
    bus.close("s1")
    await task

    assert collected == [{"type": "delta", "content": "1"}, {"type": "delta", "content": "2"}]


async def test_subscribe_unknown_session_returns_immediately():
    bus = AgentBus()
    assert await _drain(bus, "missing") == []


async def test_closed_channel_can_be_dropped_to_release_replay_buffer():
    bus = AgentBus(closed_retain_seconds=0)
    bus.begin("s1")
    bus.publish("s1", {"type": "delta", "content": "x"})
    bus.close("s1")

    assert await _drain(bus, "s1") == []
    assert bus._channels == {}


async def test_cleanup_callback_does_not_drop_new_attempt_for_same_session():
    bus = AgentBus()
    bus.begin("s1")
    old = bus._channels["s1"]
    bus.close("s1")
    bus.begin("s1")

    bus._drop_if_same("s1", old)

    bus.publish("s1", {"type": "delta", "content": "new"})
    bus.close("s1")
    assert await _drain(bus, "s1") == [{"type": "delta", "content": "new"}]


async def test_get_bus_is_singleton():
    assert get_bus() is get_bus()
