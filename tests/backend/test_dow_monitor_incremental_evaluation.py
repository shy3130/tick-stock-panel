from __future__ import annotations

import asyncio
import threading
import time as wall_time
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import polars as pl

from app.services.dow_monitor_data import SymbolFreshness
from app.services import dow_monitor_service as monitor_service_module
from app.services.dow_monitor_models import DowTimeframeState
from app.services.dow_monitor_service import (
    DowMonitorService,
    due_timeframes_for_minute,
)
from app.services.dow_monitor_store import DowMonitorStore


HONG_KONG = ZoneInfo("Asia/Hong_Kong")
SHANGHAI = ZoneInfo("Asia/Shanghai")
NEW_YORK = ZoneInfo("America/New_York")
TIMEFRAMES = ("5m", "15m", "30m", "60m", "day")


def _state(
    symbol: str,
    timeframe: str,
    source_timestamp: datetime,
    *,
    freshness_state: str = "LIVE",
) -> DowTimeframeState:
    return DowTimeframeState(
        symbol=symbol,
        market=(
            "hk"
            if symbol.endswith(".HK")
            else "cn"
            if symbol.endswith((".SS", ".SZ"))
            else "us"
        ),
        timeframe=timeframe,
        freshness_state=freshness_state,
        source_timestamp=source_timestamp,
        snapshot={"bar_completion": "FINAL", "provisional": False},
        chart={"bars": [{"timestamp": source_timestamp.isoformat()}]},
        updated_at=source_timestamp,
    )


def test_non_boundary_minute_reuses_all_live_timeframe_states() -> None:
    source = datetime(2026, 7, 31, 14, 41, tzinfo=HONG_KONG)
    previous = {
        timeframe: _state("01347.HK", timeframe, source.replace(minute=40))
        for timeframe in TIMEFRAMES
    }

    assert due_timeframes_for_minute("01347.HK", source, previous) == ()


def test_exact_close_labeled_state_reuses_final_intraday_buckets() -> None:
    cases = (
        (
            "002714.SZ",
            datetime(2026, 7, 31, 14, 59, tzinfo=SHANGHAI),
            datetime(2026, 7, 31, 15, 0, tzinfo=SHANGHAI),
        ),
        (
            "2714.HK",
            datetime(2026, 7, 31, 15, 59, tzinfo=HONG_KONG),
            datetime(2026, 7, 31, 16, 0, tzinfo=HONG_KONG),
        ),
    )

    for symbol, last_regular_minute, exact_close in cases:
        previous = {
            timeframe: _state(symbol, timeframe, exact_close)
            for timeframe in TIMEFRAMES
        }

        assert due_timeframes_for_minute(
            symbol,
            last_regular_minute,
            previous,
        ) == ()


def test_hk_display_alias_reuses_canonical_persisted_state(tmp_path) -> None:
    store = DowMonitorStore(tmp_path)
    source = datetime(2026, 7, 31, 14, 40, tzinfo=HONG_KONG)
    canonical = _state("981.HK", "5m", source)
    store.save_state(canonical)

    assert store.get_state("00981.HK", "5m") == canonical

    displayed = canonical.model_copy(update={"symbol": "00981.HK"})
    store.save_state(displayed)

    assert store.get_state("981.HK", "5m") == displayed
    assert store.list_states() == [displayed]


def test_hk_display_alias_is_not_reclassified_as_cold_or_reevaluated(
    tmp_path,
) -> None:
    store = DowMonitorStore(tmp_path)
    item = store.upsert_symbol("00981.HK", "hk", True)
    source = datetime(2026, 7, 31, 14, 41, tzinfo=HONG_KONG)
    previous_source = source.replace(minute=40)
    for timeframe in TIMEFRAMES:
        store.save_state(_state("981.HK", timeframe, previous_source))
    service = DowMonitorService(
        store,
        object(),
        object(),
        lambda *_args: pl.DataFrame(),
        now_fn=lambda: source + timedelta(minutes=1, seconds=10),
    )

    starts, cold = service._fetch_plan(
        [item],
        source + timedelta(minutes=1, seconds=10),
    )

    assert cold == set()
    assert starts == {item.symbol: previous_source}

    batch = SimpleNamespace(
        minute_rows=pl.DataFrame(
            [
                {
                    "symbol": "981.HK",
                    "datetime": source,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "volume": 1_000.0,
                }
            ]
        ),
        freshness_by_symbol={
            item.symbol: SymbolFreshness(state="LIVE", reason=None)
        },
    )

    error, ready = service._evaluate_symbol(
        item,
        batch,
        source + timedelta(minutes=1, seconds=10),
        {},
        False,
        pl.DataFrame(),
    )

    assert error is None
    assert ready is True
    assert service.status()["evaluation_request_count"] == 0
    assert service.status()["cache_skip_count"] == 5


def test_fifteen_minute_boundary_only_evaluates_due_intraday_frames() -> None:
    source = datetime(2026, 7, 31, 14, 44, tzinfo=HONG_KONG)
    previous = {
        timeframe: _state("01347.HK", timeframe, source.replace(minute=43))
        for timeframe in TIMEFRAMES
    }

    assert due_timeframes_for_minute("01347.HK", source, previous) == (
        "5m",
        "15m",
    )


def test_paused_period_is_retried_without_waiting_for_next_boundary() -> None:
    source = datetime(2026, 7, 31, 14, 41, tzinfo=HONG_KONG)
    previous = {
        timeframe: _state("01347.HK", timeframe, source.replace(minute=40))
        for timeframe in TIMEFRAMES
    }
    previous["30m"] = previous["30m"].model_copy(
        update={"freshness_state": "ANALYSIS_PAUSED"}
    )

    assert due_timeframes_for_minute("01347.HK", source, previous) == ("30m",)


def test_failed_period_keeps_existing_live_snapshot_and_retries(tmp_path) -> None:
    store = DowMonitorStore(tmp_path)
    item = store.upsert_symbol("01347.HK", "hk", True)
    source = datetime(2026, 7, 31, 14, 40, tzinfo=HONG_KONG)
    for timeframe in TIMEFRAMES:
        stable = _state(item.symbol, timeframe, source)
        if timeframe == "5m":
            stable = stable.model_copy(
                update={"snapshot": {"bar_completion": "FINAL", "phase": "UP"}}
            )
        store.save_state(stable)
    service = DowMonitorService(
        store,
        object(),
        object(),
        lambda *_args: pl.DataFrame(),
    )

    service._mark_timeframe_failure(
        item,
        "5m",
        RuntimeError("engine unavailable"),
        source + timedelta(minutes=1),
    )

    saved = store.get_state(item.symbol, "5m")
    assert saved is not None
    assert saved.freshness_state == "LIVE"
    assert saved.snapshot["phase"] == "UP"
    assert saved.snapshot["evaluation_error"] == "engine unavailable"
    states = {state.timeframe: state for state in store.list_states()}
    assert due_timeframes_for_minute(
        item.symbol,
        source + timedelta(minutes=1),
        states,
    ) == ("5m",)


def test_failed_period_without_stable_snapshot_is_paused(tmp_path) -> None:
    store = DowMonitorStore(tmp_path)
    item = store.upsert_symbol("01347.HK", "hk", True)
    now = datetime(2026, 7, 31, 14, 41, tzinfo=HONG_KONG)
    service = DowMonitorService(
        store,
        object(),
        object(),
        lambda *_args: pl.DataFrame(),
    )

    service._mark_timeframe_failure(
        item,
        "5m",
        RuntimeError("engine unavailable"),
        now,
    )

    saved = store.get_state(item.symbol, "5m")
    assert saved is not None
    assert saved.freshness_state == "ANALYSIS_PAUSED"


def test_new_symbol_requires_all_timeframes() -> None:
    source = datetime(2026, 7, 31, 14, 41, tzinfo=HONG_KONG)

    assert due_timeframes_for_minute("01347.HK", source, {}) == TIMEFRAMES


def test_day_waits_for_a_completed_session_before_reevaluation() -> None:
    previous_source = datetime(2026, 7, 30, 15, 59, tzinfo=HONG_KONG)
    previous = {
        timeframe: _state("01347.HK", timeframe, previous_source)
        for timeframe in TIMEFRAMES
    }

    midday = datetime(2026, 7, 31, 14, 41, tzinfo=HONG_KONG)
    close = datetime(2026, 7, 31, 15, 59, tzinfo=HONG_KONG)

    assert "day" not in due_timeframes_for_minute("01347.HK", midday, previous)
    assert "day" in due_timeframes_for_minute("01347.HK", close, previous)


def test_forming_day_is_reevaluated_when_same_day_session_completes() -> None:
    previous_source = datetime(2026, 7, 31, 14, 0, tzinfo=HONG_KONG)
    previous = {
        timeframe: _state("01347.HK", timeframe, previous_source)
        for timeframe in TIMEFRAMES
    }
    previous["day"] = previous["day"].model_copy(
        update={
            "snapshot": {"bar_completion": "FORMING", "provisional": True}
        }
    )

    close = datetime(2026, 7, 31, 15, 59, tzinfo=HONG_KONG)

    assert "day" in due_timeframes_for_minute("01347.HK", close, previous)


def test_symbol_evaluation_calls_only_due_timeframes(
    tmp_path,
    monkeypatch,
) -> None:
    store = DowMonitorStore(tmp_path)
    item = store.upsert_symbol("01347.HK", "hk", True)
    source = datetime(2026, 7, 31, 14, 44, tzinfo=HONG_KONG)
    now = source + timedelta(minutes=1, seconds=10)
    for timeframe in TIMEFRAMES:
        store.save_state(
            _state("01347.HK", timeframe, source.replace(minute=43))
        )
    evaluated: list[str] = []

    class Client:
        def evaluate(self, _symbol, timeframe, _bars, _completion, _now):
            evaluated.append(timeframe)
            return SimpleNamespace()

    frame = SimpleNamespace(
        source_timestamp=source,
        all_bars=[],
        completion="FINAL",
    )
    monkeypatch.setattr(
        monitor_service_module,
        "build_timeframes",
        lambda *_args, **_kwargs: {timeframe: frame for timeframe in TIMEFRAMES},
    )
    service = DowMonitorService(
        store,
        object(),
        Client(),
        lambda *_args: pl.DataFrame(),
        now_fn=lambda: now,
    )
    monkeypatch.setattr(service, "_save_result", lambda *_args, **_kwargs: None)
    batch = SimpleNamespace(
        minute_rows=pl.DataFrame(
            [
                {
                    "symbol": "01347.HK",
                    "datetime": source,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "volume": 1_000.0,
                }
            ]
        ),
        freshness_by_symbol={
            "01347.HK": SymbolFreshness(state="LIVE", reason=None)
        },
    )

    error, ready = service._evaluate_symbol(
        item,
        batch,
        now,
        {},
        False,
        pl.DataFrame(),
    )

    assert error is None
    assert ready is True
    assert evaluated == ["5m", "15m"]
    assert service.status()["evaluation_request_count"] == 2
    assert service.status()["cache_skip_count"] == 3


def test_partial_cold_start_evaluates_only_missing_timeframe(
    tmp_path,
    monkeypatch,
) -> None:
    store = DowMonitorStore(tmp_path)
    item = store.upsert_symbol("01347.HK", "hk", True)
    source = datetime(2026, 7, 31, 14, 41, tzinfo=HONG_KONG)
    now = source + timedelta(minutes=1, seconds=10)
    for timeframe in TIMEFRAMES[:-1]:
        store.save_state(_state("01347.HK", timeframe, source.replace(minute=40)))
    evaluated: list[str] = []

    class Client:
        def evaluate(self, _symbol, timeframe, _bars, _completion, _now):
            evaluated.append(timeframe)
            return SimpleNamespace()

    frame = SimpleNamespace(
        source_timestamp=source,
        all_bars=[],
        completion="FINAL",
    )
    monkeypatch.setattr(
        monitor_service_module,
        "build_timeframes",
        lambda *_args, **_kwargs: {timeframe: frame for timeframe in TIMEFRAMES},
    )
    service = DowMonitorService(
        store,
        object(),
        Client(),
        lambda *_args: pl.DataFrame(),
        now_fn=lambda: now,
    )
    monkeypatch.setattr(service, "_save_result", lambda *_args, **_kwargs: None)
    batch = SimpleNamespace(
        minute_rows=pl.DataFrame(
            [
                {
                    "symbol": "01347.HK",
                    "datetime": source,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "volume": 1_000.0,
                }
            ]
        ),
        freshness_by_symbol={
            "01347.HK": SymbolFreshness(state="LIVE", reason=None)
        },
    )

    error, ready = service._evaluate_symbol(
        item,
        batch,
        now,
        {},
        True,
        pl.DataFrame(),
    )

    assert error is None
    assert ready is True
    assert evaluated == ["day"]


def test_partial_history_warmup_failure_keeps_existing_live_states(tmp_path) -> None:
    store = DowMonitorStore(tmp_path)
    item = store.upsert_symbol("01347.HK", "hk", True)
    source = datetime(2026, 7, 31, 14, 41, tzinfo=HONG_KONG)
    now = source + timedelta(minutes=1, seconds=10)
    for timeframe in TIMEFRAMES[:-1]:
        store.save_state(_state(item.symbol, timeframe, source.replace(minute=40)))
    batch = SimpleNamespace(
        quotes=[],
        minute_rows=pl.DataFrame(),
        freshness_by_symbol={
            item.symbol: SymbolFreshness(state="LIVE", reason=None)
        },
    )

    class Gateway:
        def fetch_since(self, _starts, _now):
            return batch

        def load_history(self, _symbols, _now):
            raise RuntimeError("history unavailable")

    service = DowMonitorService(
        store,
        Gateway(),
        object(),
        lambda *_args: pl.DataFrame(),
        now_fn=lambda: now,
    )
    evaluated: list[str] = []
    service._load_notification_index = lambda: {}
    service._intraday_capital_by_symbol = lambda _symbols: {}
    service._refresh_minute_decision = lambda *_args, **_kwargs: None

    def evaluate_symbol(symbol_item, *_args, **_kwargs):
        evaluated.append(symbol_item.symbol)
        return None, True

    service._evaluate_symbol = evaluate_symbol

    asyncio.run(service.run_once())

    assert evaluated == [item.symbol]
    assert store.get_state(item.symbol, "5m").freshness_state == "LIVE"


def test_monitor_evaluates_symbols_with_bounded_parallelism(tmp_path) -> None:
    store = DowMonitorStore(tmp_path)
    symbols = [f"TEST{index}.US" for index in range(6)]
    for symbol in symbols:
        store.upsert_symbol(symbol, "us", True)
    now = datetime(2026, 7, 31, 10, 0, tzinfo=NEW_YORK)
    batch = SimpleNamespace(
        quotes=[],
        minute_rows=pl.DataFrame(),
        freshness_by_symbol={},
    )
    gateway = SimpleNamespace(fetch_since=lambda _starts, _now: batch)
    service = DowMonitorService(
        store,
        gateway,
        object(),
        lambda *_args: pl.DataFrame(),
        now_fn=lambda: now,
    )
    service._fetch_plan = lambda enabled, _now: (
        dict.fromkeys((item.symbol for item in enabled), now),
        set(),
    )
    service._load_notification_index = lambda: {}
    service._intraday_capital_by_symbol = lambda _symbols: {}
    service._refresh_minute_decision = lambda *_args, **_kwargs: None

    lock = threading.Lock()
    active = 0
    max_active = 0

    def evaluate(item, *_args):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        wall_time.sleep(0.05)
        with lock:
            active -= 1
        return None, True

    service._evaluate_symbol = evaluate

    asyncio.run(service.run_once())

    status = service.status()
    assert 2 <= max_active <= 3
    assert status["max_parallel_symbols"] == 3
    assert status["evaluated_symbols"] == []
    assert "evaluation_request_count" in status
    assert status["last_completed_at"] is not None


def test_one_symbol_failure_does_not_cancel_other_symbols(tmp_path) -> None:
    store = DowMonitorStore(tmp_path)
    for symbol in ("FAIL.US", "OK.US"):
        store.upsert_symbol(symbol, "us", True)
    now = datetime(2026, 7, 31, 10, 0, tzinfo=NEW_YORK)
    batch = SimpleNamespace(quotes=[], minute_rows=pl.DataFrame(), freshness_by_symbol={})
    service = DowMonitorService(
        store,
        SimpleNamespace(fetch_since=lambda _starts, _now: batch),
        object(),
        lambda *_args: pl.DataFrame(),
        now_fn=lambda: now,
    )
    service._fetch_plan = lambda enabled, _now: (
        dict.fromkeys((item.symbol for item in enabled), now),
        set(),
    )
    service._load_notification_index = lambda: {}
    service._intraday_capital_by_symbol = lambda _symbols: {}
    refreshed: list[str] = []
    service._refresh_minute_decision = (
        lambda item, *_args, **_kwargs: refreshed.append(item.symbol)
    )

    def evaluate(item, *_args):
        if item.symbol == "FAIL.US":
            raise RuntimeError("test failure")
        return None, True

    service._evaluate_symbol = evaluate

    asyncio.run(service.run_once())

    assert refreshed == ["OK.US"]
    assert "FAIL.US" in service.status()["errors"]
    assert service.status()["last_success_at"] == now.isoformat()
