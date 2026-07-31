from __future__ import annotations

import asyncio
import time as wall_time
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import polars as pl

from app.services.dow_monitor_minute_result_models import (
    MinuteBar,
    MinuteResultContext,
    MinuteResultKey,
)
from app.services.dow_monitor_minute_result_materializer import (
    DowMonitorMinuteResultMaterializer,
)
from app.services.dow_monitor_models import DowMinuteDecision, DowTimeframeState
from app.services.dow_monitor_service import DowMonitorService
from app.services.dow_monitor_store import DowMonitorStore


HONG_KONG = ZoneInfo("Asia/Hong_Kong")


def _decision(symbol: str, source_timestamp: datetime) -> DowMinuteDecision:
    return DowMinuteDecision(
        symbol=symbol,
        market="hk",
        decision_minute=source_timestamp + timedelta(minutes=1),
        direction="RANGE",
        direction_label="震荡",
        action="OBSERVE",
        action_label="继续观察",
        confidence=50,
        dominant_timeframe="15m",
        confirmation_timeframes=("30m",),
        supporting_reasons=("结构待确认",),
        contrary_risks=(),
        invalidation_conditions=("等待新分钟",),
        data_status="COMPLETE",
        status_label="数据完整",
        source_timestamp=source_timestamp,
    )


def _context(
    symbol: str = "1347.HK",
    decision_minute: datetime | None = None,
) -> MinuteResultContext:
    decision = decision_minute or datetime(2026, 7, 31, 10, 1, tzinfo=HONG_KONG)
    source = decision - timedelta(minutes=1)
    return MinuteResultContext(
        market="hk",
        market_day=date(2026, 7, 31),
        symbol=symbol,
        display_symbol="01347.HK",
        decision_minute=decision,
        source_bar_time=source,
        backfill=False,
        minute_bar=MinuteBar(
            timestamp=source,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=1_000.0,
            turnover=100_500.0,
        ),
        last_price=100.5,
        prev_close=99.5,
        day_high=101.0,
        day_low=98.5,
        source_timestamps={"candlestick": source},
        updated_at=decision,
    )


def test_monitor_cycle_does_not_wait_for_blocking_history_materializer(tmp_path) -> None:
    now = datetime(2026, 7, 31, 18, 0, tzinfo=HONG_KONG)
    source = datetime(2026, 7, 31, 15, 59, tzinfo=HONG_KONG)
    store = DowMonitorStore(tmp_path)
    store.upsert_symbol("01347.HK", "hk", True)
    store.save_minute_decision(_decision("01347.HK", source))

    class Gateway:
        def fetch_since(self, _starts, _now):
            return SimpleNamespace(
                quotes=[], minute_rows=pl.DataFrame(), freshness_by_symbol={}
            )

    class BlockingMaterializer:
        def materialize(self, _enabled, _now, **_kwargs):
            wall_time.sleep(0.25)

        def status(self):
            return SimpleNamespace(
                enabled=True,
                model_dump=lambda **_kwargs: {"enabled": True},
            )

    service = DowMonitorService(
        store=store,
        data_gateway=Gateway(),
        dow_client=None,
        daily_loader=lambda _symbol, _now: pl.DataFrame(),
        now_fn=lambda: now,
        minute_result_materializer=BlockingMaterializer(),
    )
    service._fetch_plan = lambda enabled, _now: (
        dict.fromkeys((item.symbol for item in enabled), now),
        set(),
    )
    service._load_notification_index = lambda: {}
    service._intraday_capital_by_symbol = lambda _symbols: {}
    service._evaluate_symbol = lambda *_args, **_kwargs: (None, True)
    service._refresh_minute_decision = lambda *_args, **_kwargs: None

    async def run() -> None:
        await asyncio.wait_for(service.run_once(), timeout=0.08)

    asyncio.run(run())
    assert service.status()["last_completed_at"] is not None


def test_append_queue_deduplicates_logical_key_and_flushes() -> None:
    from app.services.dow_monitor_minute_result_append import MinuteResultAppendQueue

    inserted = []

    class Repository:
        def insert_results(self, rows):
            inserted.extend(rows)
            return len(rows)

    async def run() -> None:
        queue = MinuteResultAppendQueue(Repository(), flush_seconds=0.01)
        await queue.start()
        context = _context()
        assert queue.submit(context) is True
        assert queue.submit(context) is False
        await asyncio.sleep(0.03)
        await queue.stop()

    asyncio.run(run())
    assert len(inserted) == 1
    assert inserted[0].symbol == "1347.HK"


def test_append_failure_is_fail_open_and_counted() -> None:
    from app.services.dow_monitor_minute_result_append import MinuteResultAppendQueue

    class Repository:
        def insert_results(self, _rows):
            raise RuntimeError("clickhouse unavailable")

    async def run() -> dict:
        queue = MinuteResultAppendQueue(Repository(), flush_seconds=0.01)
        await queue.start()
        assert queue.submit(_context()) is True
        await asyncio.sleep(0.03)
        await queue.stop()
        return queue.status().model_dump()

    status = asyncio.run(run())
    assert status["append_failures"] == 1
    assert status["queue_depth"] == 0
    assert status["last_error"] == "clickhouse unavailable"


def test_any_open_market_defers_heavy_backfill(tmp_path) -> None:
    from app.services.dow_monitor_minute_result_scheduler import (
        BackfillScheduleState,
        decide_backfill_job,
    )

    store = DowMonitorStore(tmp_path)
    hk = store.upsert_symbol("01347.HK", "hk", True)
    us = store.upsert_symbol("NBIS.US", "us", True)
    now = datetime(2026, 7, 31, 10, 0, tzinfo=HONG_KONG)

    decision = decide_backfill_job([hk, us], now, BackfillScheduleState())
    assert decision.mode == "IDLE"
    assert decision.deferred_reason == "MARKET_OPEN"


def test_post_close_and_beijing_audit_windows_are_deterministic(tmp_path) -> None:
    from app.services.dow_monitor_minute_result_scheduler import (
        BackfillScheduleState,
        decide_backfill_job,
    )

    store = DowMonitorStore(tmp_path)
    item = store.upsert_symbol("01347.HK", "hk", True)
    state = BackfillScheduleState()

    post_close = decide_backfill_job(
        [item],
        datetime(2026, 7, 31, 16, 20, tzinfo=HONG_KONG),
        state,
    )
    before_audit = decide_backfill_job(
        [item],
        datetime(2026, 8, 3, 6, 29, tzinfo=ZoneInfo("Asia/Shanghai")),
        state,
    )
    audit = decide_backfill_job(
        [item],
        datetime(2026, 8, 3, 6, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        state,
    )

    assert post_close.mode == "POST_CLOSE_BACKFILL"
    assert post_close.market == "hk"
    assert before_audit.deferred_reason == "NOT_DUE"
    assert audit.mode == "NIGHT_AUDIT"
    assert audit.market_day == date(2026, 7, 31)


def test_night_audit_passes_completed_market_day_to_materializer(tmp_path) -> None:
    from app.services.dow_monitor_minute_result_scheduler import (
        MinuteResultBackfillScheduler,
    )

    store = DowMonitorStore(tmp_path)
    item = store.upsert_symbol("01347.HK", "hk", True)
    captured: dict[str, object] = {}

    class Materializer:
        repository = None

        def status(self):
            return SimpleNamespace(enabled=True, last_error=None)

        def materialize(self, symbols, now, **kwargs):
            captured.update(symbols=symbols, now=now, **kwargs)
            return SimpleNamespace(
                scanned_keys=0,
                written_rows=0,
                remaining_keys=0,
                error=None,
            )

    scheduler = MinuteResultBackfillScheduler(Materializer())
    audit_time = datetime(2026, 8, 3, 6, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

    asyncio.run(scheduler._execute_due_job((item,), audit_time))

    assert captured["market_day"] == date(2026, 7, 31)


def test_zero_gap_backfill_does_not_load_raw_history(tmp_path) -> None:
    now = datetime(2026, 7, 31, 10, 2, tzinfo=HONG_KONG)
    store = DowMonitorStore(tmp_path)
    item = store.upsert_symbol("01347.HK", "hk", True)
    key = MinuteResultKey(
        market="hk",
        symbol="1347.HK",
        decision_minute=datetime(2026, 7, 31, 10, 1, tzinfo=HONG_KONG),
    )

    class Source:
        def candidate_minute_keys(self, _items, _market_day, _end):
            return {key}

        def load_raw_history(self, *_args, **_kwargs):
            raise AssertionError("zero gap must not load ten-day history")

    class Repository:
        def existing_keys(self, _symbols, _start, _end):
            return {key}

        def insert_results(self, _rows):
            raise AssertionError("zero gap must not write")

    materializer = DowMonitorMinuteResultMaterializer(
        source=Source(),
        repository=Repository(),
        history_builder=object(),
        notifications_fn=lambda: (),
        now_fn=lambda: now,
    )

    run = materializer.materialize([item], now, max_rows=2_000)
    assert run.scanned_keys == 1
    assert run.written_rows == 0
    assert run.remaining_keys == 0


def test_backfill_respects_row_budget_and_preserves_remaining_keys(tmp_path) -> None:
    now = datetime(2026, 7, 31, 10, 4, tzinfo=HONG_KONG)
    store = DowMonitorStore(tmp_path)
    item = store.upsert_symbol("01347.HK", "hk", True)
    keys = {
        MinuteResultKey(
            market="hk",
            symbol="1347.HK",
            decision_minute=datetime(2026, 7, 31, 10, minute, tzinfo=HONG_KONG),
        )
        for minute in (1, 2, 3)
    }
    inserted = []

    class Source:
        def candidate_minute_keys(self, _items, _market_day, _end):
            return keys

        def load_raw_history(self, *_args, **_kwargs):
            return object()

    class Repository:
        def existing_keys(self, _symbols, _start, _end):
            return set()

        def insert_results(self, rows):
            inserted.extend(rows)
            return len(rows)

    class HistoryBuilder:
        def build_contexts(
            self,
            _history,
            _item,
            _market_day,
            _backfill,
            *,
            notifications,
            decision_minutes,
        ):
            assert notifications == ()
            return [
                _context(decision_minute=decision).model_copy(
                    update={"backfill": True}
                )
                for decision in sorted(decision_minutes)
            ]

    materializer = DowMonitorMinuteResultMaterializer(
        source=Source(),
        repository=Repository(),
        history_builder=HistoryBuilder(),
        notifications_fn=lambda: (),
        now_fn=lambda: now,
    )

    run = materializer.materialize([item], now, max_rows=2)
    assert run.scanned_keys == 3
    assert run.written_rows == 2
    assert run.remaining_keys == 1
    assert len(inserted) == 2


def test_live_context_uses_only_current_cycle_inputs(tmp_path) -> None:
    from app.services.dow_monitor_minute_result_live import (
        build_live_minute_result_context,
    )

    source = datetime(2026, 7, 31, 10, 0, tzinfo=HONG_KONG)
    decision = _decision("01347.HK", source).model_copy(
        update={"direction": "BULLISH"}
    )
    store = DowMonitorStore(tmp_path)
    item = store.upsert_symbol("01347.HK", "hk", True)
    bars = [
        {
            "timestamp": source.isoformat(),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1_000.0,
        }
    ]
    states = [
        DowTimeframeState(
            symbol="01347.HK",
            market="hk",
            timeframe=timeframe,
            freshness_state="LIVE",
            source_timestamp=source,
            snapshot={
                "bar_completion": "FINAL",
                "provisional": False,
                "price_to_line_pct": 1.25,
                "line_role": "SUPPORT",
                "volume_ratio_20": 1.8,
            },
            chart={"bars": bars},
            updated_at=decision.decision_minute,
        )
        for timeframe in ("5m", "15m", "30m")
    ]
    minute_rows = pl.DataFrame(
        [
            {
                "symbol": "01347.HK",
                "datetime": source,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 1_000.0,
                "amount": 100_500.0,
            }
        ]
    )

    context = build_live_minute_result_context(
        item=item,
        decision=decision,
        minute_rows=minute_rows,
        states=states,
        quote={
            "last_price": 100.5,
            "prev_close": 99.5,
            "high": 101.0,
            "low": 98.5,
            "updated_at": source,
        },
        depth={"bid_volumes": [100, 80], "ask_volumes": [50, 50]},
        capital={
            "total_in": 1_200_000.0,
            "total_out": 800_000.0,
            "quality": "COMPLETE",
            "capital_minute": source,
        },
        notifications=(),
        updated_at=decision.decision_minute,
    )

    assert context.backfill is False
    assert context.symbol == "1347.HK"
    assert set(context.states) == {"5m", "15m", "30m"}
    assert context.last_price == 100.5
    assert context.capital_quality == "COMPLETE"


def test_live_context_ignores_stable_states_from_other_symbols(tmp_path) -> None:
    from app.services.dow_monitor_minute_result_live import (
        build_live_minute_result_context,
    )

    source = datetime(2026, 7, 31, 10, 0, tzinfo=HONG_KONG)
    decision = _decision("01347.HK", source)
    store = DowMonitorStore(tmp_path)
    item = store.upsert_symbol("01347.HK", "hk", True)

    def state(symbol: str, distance: float) -> DowTimeframeState:
        return DowTimeframeState(
            symbol=symbol,
            market="hk",
            timeframe="5m",
            freshness_state="LIVE",
            source_timestamp=source,
            snapshot={
                "bar_completion": "FINAL",
                "provisional": False,
                "price_to_line_pct": distance,
            },
            chart={"bars": []},
            updated_at=decision.decision_minute,
        )

    context = build_live_minute_result_context(
        item=item,
        decision=decision,
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
                    "amount": 100_500.0,
                }
            ]
        ),
        states=[state("01347.HK", 1.25), state("09988.HK", -9.0)],
        quote=None,
        depth=None,
        capital=None,
        notifications=(),
        updated_at=decision.decision_minute,
    )

    assert context.states["5m"].price_to_line_pct == 1.25
