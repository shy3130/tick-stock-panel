from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from app.services.dow_monitor_minute_result_materializer import (
    DowMonitorMinuteResultMaterializer,
)
from app.services.dow_monitor_minute_result_models import (
    MinuteBar,
    MinuteResultContext,
    MinuteResultKey,
    RawMinuteHistory,
)
from app.services.dow_monitor_models import MonitoredSymbol


NOW = datetime(2026, 7, 30, 1, 5, 30, tzinfo=UTC)


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
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], datetime, datetime, datetime]] = []

    def load_raw_history(
        self,
        symbols,
        start,
        end,
        *,
        candle_start,
    ) -> RawMinuteHistory:
        self.calls.append((tuple(symbols), start, end, candle_start))
        return RawMinuteHistory()


class HistoryBuilder:
    def __init__(self) -> None:
        self.market_days: list[tuple[str, date]] = []

    def build_contexts(
        self,
        history,
        symbol,
        market_day,
        backfill,
        notifications,
    ) -> list[MinuteResultContext]:
        self.market_days.append((symbol.symbol, market_day))
        base = datetime.combine(market_day, datetime.min.time(), tzinfo=UTC)
        return [
            _context(symbol, market_day, base + timedelta(hours=1, minutes=1)),
            _context(symbol, market_day, base + timedelta(hours=1, minutes=2)),
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

    def existing_keys(self, symbols, start, end):
        return set(self.existing)

    def insert_results(self, rows):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("clickhouse unavailable")
        self.inserted.extend(rows)
        return len(rows)


def _materializer(repository: Repository):
    source = Source()
    history = HistoryBuilder()
    materializer = DowMonitorMinuteResultMaterializer(
        source=source,
        repository=repository,
        history_builder=history,
        notifications_fn=lambda: [],
        now_fn=lambda: NOW,
    )
    return materializer, source, history


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
