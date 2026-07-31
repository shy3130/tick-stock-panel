from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import re
from zoneinfo import ZoneInfo

import pytest

from app.services import dow_monitor_minute_result_materializer as materializer_module
from app.services.dow_monitor_minute_result_materializer import (
    DowMonitorMinuteResultMaterializer,
)
from app.services.dow_monitor_minute_result_models import (
    MinuteBar,
    MinuteResultContext,
    MinuteResultKey,
    RawMinuteHistory,
)
from app.services.dow_monitor_minute_result_repository import (
    DowMonitorMinuteResultRepository,
)
from app.services.dow_monitor_models import MonitoredSymbol


NOW = datetime(2026, 7, 30, 1, 5, 30, tzinfo=UTC)
BEIJING = ZoneInfo("Asia/Shanghai")


def beijing(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=BEIJING)


def _symbol(symbol: str, market: str) -> MonitoredSymbol:
    return MonitoredSymbol(
        symbol=symbol,
        market=market,
        enabled=True,
        created_at=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(days=1),
    )


def _context(
    item: MonitoredSymbol,
    market_day: date,
    decision_minute: datetime,
) -> MinuteResultContext:
    source_bar_time = decision_minute - timedelta(minutes=1)
    return MinuteResultContext(
        market=item.market,
        market_day=market_day,
        symbol=item.symbol,
        display_symbol=item.symbol,
        decision_minute=decision_minute,
        source_bar_time=source_bar_time,
        backfill=True,
        minute_bar=MinuteBar(
            timestamp=source_bar_time,
            open=100,
            high=102,
            low=99,
            close=101,
            volume=80,
            turnover=8_080,
        ),
        source_timestamps={"candlestick": source_bar_time},
        updated_at=NOW,
    )


class Source:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.calls: list[tuple[tuple[str, ...], datetime, datetime, datetime]] = []
        self.failure = failure

    def load_raw_history(
        self,
        symbols,
        start,
        end,
        *,
        candle_start,
    ) -> RawMinuteHistory:
        self.calls.append((tuple(symbols), start, end, candle_start))
        if self.failure is not None:
            raise self.failure
        return RawMinuteHistory()


class HistoryBuilder:
    def __init__(
        self,
        decision_minutes: tuple[datetime, ...] | None = None,
        *,
        failure: Exception | None = None,
    ) -> None:
        self.market_days: list[tuple[str, date]] = []
        self.build_calls = 0
        self.decision_minutes = decision_minutes
        self.failure = failure
        self.requested_decision_minutes: list[set[datetime] | None] = []

    def _decision_minutes(self, market_day: date) -> tuple[datetime, ...]:
        if self.decision_minutes is not None:
            return self.decision_minutes
        base = datetime.combine(market_day, datetime.min.time(), tzinfo=UTC)
        return (
            base + timedelta(hours=1, minutes=1),
            base + timedelta(hours=1, minutes=2),
        )

    def candidate_keys(self, history, symbol, market_day) -> set[MinuteResultKey]:
        return {
            MinuteResultKey(
                market=symbol.market,
                symbol=symbol.symbol,
                decision_minute=decision_minute,
            )
            for decision_minute in self._decision_minutes(market_day)
        }

    def build_contexts(
        self,
        history,
        symbol,
        market_day,
        backfill,
        notifications,
        decision_minutes=None,
    ) -> list[MinuteResultContext]:
        self.build_calls += 1
        self.market_days.append((symbol.symbol, market_day))
        self.requested_decision_minutes.append(decision_minutes)
        if self.failure is not None:
            raise self.failure
        contexts = [
            _context(symbol, market_day, decision_minute)
            for decision_minute in self._decision_minutes(market_day)
        ]
        if decision_minutes is None:
            return contexts
        return [
            context
            for context in contexts
            if context.decision_minute in decision_minutes
        ]


class Repository:
    def __init__(
        self,
        existing: set[MinuteResultKey] | None = None,
        *,
        failures: int = 0,
    ) -> None:
        self.existing = existing or set()
        self.failures = failures
        self.inserted = []
        self.existing_calls: list[tuple[tuple[str, ...], datetime, datetime]] = []

    def existing_keys(self, symbols, start, end):
        self.existing_calls.append((tuple(symbols), start, end))
        return set(self.existing)

    def insert_results(self, rows):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("clickhouse unavailable")
        self.inserted.extend(rows)
        return len(rows)


def _materializer(
    repository: Repository,
    *,
    source: Source | None = None,
    history: HistoryBuilder | None = None,
):
    source = source or Source()
    history = history or HistoryBuilder()
    materializer = DowMonitorMinuteResultMaterializer(
        source=source,
        repository=repository,
        history_builder=history,
        notifications_fn=lambda: [],
        now_fn=lambda: NOW,
    )
    return materializer, source, history


def _checkpoint_minutes() -> tuple[datetime, ...]:
    return (
        beijing("2026-07-31T21:31:00"),
        beijing("2026-07-31T21:45:00"),
        beijing("2026-07-31T22:00:00"),
    )


def _checkpoint_symbol() -> MonitoredSymbol:
    return _symbol("RNG.US", "us")


# Catches a materializer that widens the checkpoint range, uses a live row,
# omits the warmup, or loads symbols other than the requested one.
def test_materialize_checkpoint_writes_only_missing_rows_through_cutoff() -> None:
    repository = Repository()
    minutes = _checkpoint_minutes()
    history = HistoryBuilder(minutes)
    materializer, source, history = _materializer(repository, history=history)
    session_open = beijing("2026-07-31T21:30:00")
    window_end = beijing("2026-07-31T22:00:00")

    run = materializer.materialize_checkpoint(
        symbol=_checkpoint_symbol(),
        session_open=session_open,
        window_end=window_end,
        max_rows=500,
    )

    assert run.error is None
    assert all(
        session_open < row.decision_minute <= window_end
        for row in repository.inserted
    )
    assert all(row.backfill for row in repository.inserted)
    assert run.written_rows == len(repository.inserted)
    assert source.calls == [
        (("RNG.US",), session_open, window_end, session_open - timedelta(days=10))
    ]
    assert repository.existing_calls == [
        (("RNG.US",), session_open, window_end + timedelta(milliseconds=1))
    ]
    assert history.requested_decision_minutes == [set(minutes)]


def test_checkpoint_exact_cutoff_dedup_respects_datetime64_milliseconds(
    monkeypatch,
) -> None:
    session_open = beijing("2026-07-31T21:30:00")
    window_end = beijing("2026-07-31T22:00:00")
    inserted_payloads: list[bytes | None] = []
    calculated: list[datetime] = []

    def query(sql: str) -> list[dict]:
        bounds = re.findall(
            r"parseDateTime64BestEffort\('([^']+)'\)",
            sql,
        )
        assert len(bounds) == 2
        exclusive_upper = datetime.fromisoformat(bounds[-1])
        exclusive_upper = exclusive_upper.replace(
            microsecond=(exclusive_upper.microsecond // 1_000) * 1_000
        )
        if window_end < exclusive_upper:
            return [
                {
                    "market": "us",
                    "symbol": "RNG.US",
                    "decision_minute": window_end,
                }
            ]
        return []

    repository = DowMonitorMinuteResultRepository(
        query_fn=query,
        execute_fn=lambda _sql, payload=None: inserted_payloads.append(payload)
        or b"",
    )
    real_calculate = materializer_module.calculate_minute_result

    def calculate(context):
        calculated.append(context.decision_minute)
        return real_calculate(context)

    monkeypatch.setattr(
        materializer_module,
        "calculate_minute_result",
        calculate,
    )
    history = HistoryBuilder((window_end,))
    materializer = DowMonitorMinuteResultMaterializer(
        source=Source(),
        repository=repository,
        history_builder=history,
        notifications_fn=lambda: [],
        now_fn=lambda: NOW,
    )

    run = materializer.materialize_checkpoint(
        symbol=_checkpoint_symbol(),
        session_open=session_open,
        window_end=window_end,
    )

    assert run.error is None
    assert run.written_rows == 0
    assert calculated == []
    assert inserted_payloads == []
    assert history.build_calls == 0


# Catches a materializer that replays logical minutes already persisted.
def test_materialize_checkpoint_skips_already_materialized_logical_minutes() -> None:
    minutes = _checkpoint_minutes()
    existing = MinuteResultKey(
        market="us",
        symbol="RNG.US",
        decision_minute=minutes[1],
    )
    repository = Repository({existing})
    history = HistoryBuilder(minutes)
    materializer, _, history = _materializer(repository, history=history)

    run = materializer.materialize_checkpoint(
        symbol=_checkpoint_symbol(),
        session_open=beijing("2026-07-31T21:30:00"),
        window_end=beijing("2026-07-31T22:00:00"),
    )

    assert run.error is None
    assert [row.decision_minute for row in repository.inserted] == [
        minutes[0],
        minutes[2],
    ]
    assert history.requested_decision_minutes == [{minutes[0], minutes[2]}]


# Catches a materializer that begins inserting before it rejects an over-budget
# checkpoint, which would leave a partially materialized logical window.
def test_materialize_checkpoint_does_not_insert_when_row_budget_is_exceeded() -> None:
    repository = Repository()
    materializer, _, _ = _materializer(
        repository,
        history=HistoryBuilder(_checkpoint_minutes()),
    )

    run = materializer.materialize_checkpoint(
        symbol=_checkpoint_symbol(),
        session_open=beijing("2026-07-31T21:30:00"),
        window_end=beijing("2026-07-31T22:00:00"),
        max_rows=2,
    )

    assert run.error is not None
    assert run.error.code == "BACKFILL_BUDGET_EXCEEDED"
    assert repository.inserted == []


# Catches a caller-provided max_rows value that raises the authoritative
# 500-row ceiling and permits an oversized checkpoint write.
def test_materialize_checkpoint_caps_caller_budget_at_500_rows() -> None:
    session_open = beijing("2026-07-31T09:30:00")
    minutes = tuple(
        session_open + timedelta(minutes=offset)
        for offset in range(1, 502)
    )
    repository = Repository()
    materializer, _, _ = _materializer(
        repository,
        history=HistoryBuilder(minutes),
    )

    run = materializer.materialize_checkpoint(
        symbol=_checkpoint_symbol(),
        session_open=session_open,
        window_end=beijing("2026-07-31T22:00:00"),
        max_rows=1_000,
    )

    assert run.error is not None
    assert run.error.code == "BACKFILL_BUDGET_EXCEEDED"
    assert repository.inserted == []


# Catches a materializer that lets an upstream exception escape or erases its
# diagnostic message instead of returning a typed checkpoint failure.
@pytest.mark.parametrize(
    ("source", "history", "repository"),
    [
        (Source(failure=RuntimeError("raw source unavailable")), None, Repository()),
        (None, HistoryBuilder(_checkpoint_minutes(), failure=RuntimeError("history unavailable")), Repository()),
        (None, None, Repository(failures=1)),
    ],
    ids=["source", "history", "repository"],
)
def test_materialize_checkpoint_returns_typed_error_for_upstream_failure(
    source: Source | None,
    history: HistoryBuilder | None,
    repository: Repository,
) -> None:
    materializer, _, _ = _materializer(
        repository,
        source=source,
        history=history or HistoryBuilder(_checkpoint_minutes()),
    )

    run = materializer.materialize_checkpoint(
        symbol=_checkpoint_symbol(),
        session_open=beijing("2026-07-31T21:30:00"),
        window_end=beijing("2026-07-31T22:00:00"),
    )

    assert run.error is not None
    assert run.error.code == "BACKFILL_FAILED"
    assert "unavailable" in run.error.message
    assert repository.inserted == []


# Catches a materializer that accepts an invalid budget or an ambiguous
# checkpoint boundary and could read/write an unbounded interval.
@pytest.mark.parametrize(
    ("session_open", "window_end", "max_rows"),
    [
        (beijing("2026-07-31T21:30:00"), beijing("2026-07-31T22:00:00"), 0),
        (datetime(2026, 7, 31, 21, 30), beijing("2026-07-31T22:00:00"), 500),
        (beijing("2026-07-31T21:30:00"), datetime(2026, 7, 31, 22, 0), 500),
        (beijing("2026-07-31T22:00:00"), beijing("2026-07-31T22:00:00"), 500),
    ],
    ids=["non-positive-budget", "naive-session-open", "naive-window-end", "empty-window"],
)
def test_materialize_checkpoint_rejects_invalid_bounds(
    session_open: datetime,
    window_end: datetime,
    max_rows: int,
) -> None:
    materializer, _, _ = _materializer(Repository())

    with pytest.raises(ValueError):
        materializer.materialize_checkpoint(
            symbol=_checkpoint_symbol(),
            session_open=session_open,
            window_end=window_end,
            max_rows=max_rows,
        )


def test_backfills_only_missing_logical_keys() -> None:
    item = _symbol("700.HK", "hk")
    market_day = NOW.date()
    first_minute = datetime.combine(
        market_day,
        datetime.min.time(),
        tzinfo=UTC,
    ) + timedelta(hours=1, minutes=1)
    existing = MinuteResultKey(
        market="hk",
        symbol=item.symbol,
        decision_minute=first_minute,
    )
    repository = Repository({existing})
    materializer, _, _ = _materializer(repository)

    run = materializer.materialize([item], NOW)

    assert run.written_rows == 1
    assert len(repository.inserted) == 1
    assert repository.inserted[0].decision_minute != first_minute
    assert materializer.status().pending_minutes == 0


def test_skips_history_replay_when_all_candidate_keys_already_exist() -> None:
    item = _symbol("700.HK", "hk")
    market_day = NOW.date()
    base = datetime.combine(market_day, datetime.min.time(), tzinfo=UTC)
    existing = {
        MinuteResultKey(
            market="hk",
            symbol=item.symbol,
            decision_minute=base + timedelta(hours=1, minutes=offset),
        )
        for offset in (1, 2)
    }
    repository = Repository(existing)
    materializer, _, history = _materializer(repository)

    run = materializer.materialize([item], NOW)

    assert run.written_rows == 0
    assert history.build_calls == 0


def test_uses_each_markets_local_current_day_and_separate_warmup() -> None:
    repository = Repository()
    materializer, source, history = _materializer(repository)

    materializer.materialize(
        [
            _symbol("600000.SH", "cn"),
            _symbol("700.HK", "hk"),
            _symbol("AAPL.US", "us"),
        ],
        NOW,
    )

    assert dict(history.market_days) == {
        "600000.SH": date(2026, 7, 30),
        "700.HK": date(2026, 7, 30),
        "AAPL.US": date(2026, 7, 29),
    }
    assert len(source.calls) == 3
    assert all(candle_start < start for _, start, _, candle_start in source.calls)


def test_clickhouse_failure_is_reported_without_raising_and_retries_gap() -> None:
    repository = Repository(failures=1)
    materializer, _, _ = _materializer(repository)
    item = _symbol("700.HK", "hk")

    failed = materializer.materialize([item], NOW)

    assert failed.written_rows == 0
    assert failed.error == "clickhouse unavailable"
    assert materializer.status().pending_minutes == 2
    assert materializer.status().last_success_at is None

    recovered = materializer.materialize([item], NOW + timedelta(seconds=15))

    assert recovered.written_rows == 2
    assert recovered.error is None
    assert materializer.status().pending_minutes == 0
    assert materializer.status().last_success_at == NOW + timedelta(seconds=15)
