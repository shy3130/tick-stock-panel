"""Gates for TickFlow Pro Phase 1 probe CLI."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from scripts.probe_tickflow_pro import evaluate_gate

SH = ZoneInfo("Asia/Shanghai")


def test_dry_run_always_allowed_without_key() -> None:
    gate = evaluate_gate(dry_run=True, force=False, has_key=False)
    assert gate.allowed is True
    assert gate.reason == "dry_run"


def test_live_blocked_without_key() -> None:
    gate = evaluate_gate(dry_run=False, force=True, has_key=False)
    assert gate.allowed is False
    assert "missing" in gate.reason


def test_live_blocked_before_offpeak_without_force() -> None:
    now = datetime(2026, 7, 19, 10, 0, tzinfo=SH)
    gate = evaluate_gate(dry_run=False, force=False, has_key=True, now=now)
    assert gate.allowed is False
    assert "before_16" in gate.reason


def test_live_allowed_after_offpeak() -> None:
    now = datetime(2026, 7, 19, 16, 5, tzinfo=SH)
    gate = evaluate_gate(dry_run=False, force=False, has_key=True, now=now)
    assert gate.allowed is True


def test_live_allowed_before_offpeak_with_force() -> None:
    now = datetime(2026, 7, 19, 10, 0, tzinfo=SH)
    gate = evaluate_gate(dry_run=False, force=True, has_key=True, now=now)
    assert gate.allowed is True
