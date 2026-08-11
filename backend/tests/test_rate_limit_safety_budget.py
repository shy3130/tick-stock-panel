"""TickFlow Pro 80% rpm safety budget (process-local)."""

from __future__ import annotations

import time

from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet
from app.tickflow.rate_limits import (
    SAFETY_RPM_FACTOR,
    _next_slot,
    _slot_lock,
    apply_safety_rpm,
    resolve_limit,
    sleep_between_batches,
)


def test_apply_safety_rpm_scales_to_80_percent() -> None:
    assert SAFETY_RPM_FACTOR == 0.8
    assert apply_safety_rpm(120) == 96
    assert apply_safety_rpm(60) == 48
    assert apply_safety_rpm(30) == 24
    assert apply_safety_rpm(None) is None


def test_resolve_limit_applies_safety_by_default() -> None:
    capset = CapabilitySet({Cap.KLINE_DAILY_BATCH: CapabilityLimits(rpm=60, batch=100)})
    lim = resolve_limit(capset, Cap.KLINE_DAILY_BATCH)
    assert lim.rpm == 48
    assert lim.batch == 100


def test_resolve_limit_can_skip_safety_for_diagnostics() -> None:
    capset = CapabilitySet({Cap.KLINE_DAILY_BATCH: CapabilityLimits(rpm=60, batch=100)})
    lim = resolve_limit(capset, Cap.KLINE_DAILY_BATCH, apply_safety=False)
    assert lim.rpm == 60


def test_two_first_batches_do_not_wait_even_when_slot_reserved() -> None:
    """Known limitation: concurrent/serial index=0 calls do not sleep (first-batch burst).

    Comments document that aggregate rpm is therefore not guaranteed when many
    pipelines start with index=0; Phase 1 must avoid concurrent probe/sync.
    """
    with _slot_lock:
        _next_slot.clear()
    t0 = time.perf_counter()
    sleep_between_batches(0, rpm=60)
    sleep_between_batches(0, rpm=60)
    assert time.perf_counter() - t0 < 0.05


def test_second_batch_waits_for_shared_slot() -> None:
    with _slot_lock:
        _next_slot.clear()
    t0 = time.perf_counter()
    sleep_between_batches(0, rpm=60)
    sleep_between_batches(1, rpm=60)
    assert time.perf_counter() - t0 >= 0.9
