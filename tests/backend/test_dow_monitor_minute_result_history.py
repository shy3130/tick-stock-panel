from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.services.dow_monitor_minute_result_history import (
    DowEngineStableStateBuilder,
    DowMonitorMinuteResultHistoryBuilder,
)
from app.services.dow_monitor_minute_result_models import (
    MinuteBar,
    RawCandlestick,
    RawMinuteHistory,
    RawQuoteSnapshot,
    StableTimeframeState,
)
from app.services.dow_monitor_models import DowNotification, MonitoredSymbol


SHANGHAI = ZoneInfo("Asia/Shanghai")
NEW_YORK = ZoneInfo("America/New_York")


def _raw_bar(
    timestamp: datetime,
    *,
    symbol: str = "700.HK",
    market: str = "hk",
    period: str = "min_1",
    close: float = 100.0,
) -> RawCandlestick:
    return RawCandlestick(
        symbol=symbol,
        market=market,
        period=period,
        bar_time=timestamp,
        open=close - 1,
        high=close + 1,
        low=close - 2,
        close=close,
        volume=100,
        turnover=close * 100,
        updated_at=timestamp + timedelta(seconds=30),
    )


def _symbol(symbol: str = "700.HK", market: str = "hk") -> MonitoredSymbol:
    now = datetime(2026, 7, 29, 9, 0, tzinfo=SHANGHAI)
    return MonitoredSymbol(
        symbol=symbol,
        market=market,
        enabled=True,
        created_at=now,
        updated_at=now,
    )


class CountingStableStateBuilder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, datetime]] = []

    def build(
        self,
        symbol: str,
        timeframe: str,
        bars: tuple[RawCandlestick, ...],
        as_of: datetime,
    ) -> StableTimeframeState:
        self.calls.append((symbol, timeframe, as_of))
        converted = tuple(
            MinuteBar(
                timestamp=bar.bar_time,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                turnover=bar.turnover,
                ma5=bar.close - 1,
                ma10=bar.close - 2,
                ma20=bar.close - 3,
            )
            for bar in bars
        )
        return StableTimeframeState(
            timeframe=timeframe,
            bar_completion="FINAL",
            provisional=False,
            price_to_line_pct=1.2,
            line_role="SUPPORT",
            volume_ratio_20=1.6,
            bars=converted,
        )


def test_context_never_uses_a_quote_from_the_future() -> None:
    bar_time = datetime(2026, 7, 29, 9, 30, tzinfo=SHANGHAI)
    decision = bar_time + timedelta(minutes=1)
    history = RawMinuteHistory(
        candlesticks=(_raw_bar(bar_time),),
        quotes=(
            RawQuoteSnapshot(
                symbol="700.HK",
                market="hk",
                snapshot_time=decision - timedelta(seconds=1),
                last_price=100,
                prev_close=99,
                high=101,
                low=98,
                updated_at=decision - timedelta(seconds=1),
            ),
            RawQuoteSnapshot(
                symbol="700.HK",
                market="hk",
                snapshot_time=decision + timedelta(seconds=1),
                last_price=999,
                prev_close=99,
                high=999,
                low=98,
                updated_at=decision + timedelta(seconds=1),
            ),
        ),
    )

    contexts = DowMonitorMinuteResultHistoryBuilder(
        CountingStableStateBuilder(),
    ).build_contexts(history, _symbol(), date(2026, 7, 29), True, notifications=[])

    assert len(contexts) == 1
    assert contexts[0].last_price == 100
    assert contexts[0].source_timestamps["quote"] == decision - timedelta(seconds=1)


def test_us_market_day_uses_new_york_date_across_shanghai_midnight() -> None:
    bar_time = datetime(2026, 7, 30, 1, 30, tzinfo=SHANGHAI)
    assert bar_time.astimezone(NEW_YORK).date() == date(2026, 7, 29)
    history = RawMinuteHistory(
        candlesticks=(
            _raw_bar(
                bar_time,
                symbol="AAPL.US",
                market="us",
            ),
        ),
    )

    contexts = DowMonitorMinuteResultHistoryBuilder(
        CountingStableStateBuilder(),
    ).build_contexts(
        history,
        _symbol("AAPL.US", "us"),
        date(2026, 7, 29),
        True,
        notifications=[],
    )

    assert len(contexts) == 1
    assert {item.market_day for item in contexts} == {date(2026, 7, 29)}


def test_padded_hk_monitor_symbol_matches_unpadded_raw_history() -> None:
    bar_time = datetime(2026, 7, 29, 9, 30, tzinfo=SHANGHAI)
    history = RawMinuteHistory(
        candlesticks=(
            _raw_bar(bar_time, symbol="1347.HK"),
        ),
    )

    contexts = DowMonitorMinuteResultHistoryBuilder(
        CountingStableStateBuilder(),
    ).build_contexts(
        history,
        _symbol("01347.HK", "hk"),
        date(2026, 7, 29),
        True,
        notifications=[],
    )

    assert len(contexts) == 1
    assert contexts[0].symbol == "1347.HK"
    assert contexts[0].display_symbol == "01347.HK"


def test_stable_state_replay_is_cached_per_completed_bucket() -> None:
    minute_start = datetime(2026, 7, 29, 9, 45, tzinfo=SHANGHAI)
    minute_bars = tuple(
        _raw_bar(minute_start + timedelta(minutes=index), close=101 + index)
        for index in range(5)
    )
    stable_bar = _raw_bar(
        datetime(2026, 7, 29, 9, 30, tzinfo=SHANGHAI),
        period="min_15",
    )
    stable_builder = CountingStableStateBuilder()

    contexts = DowMonitorMinuteResultHistoryBuilder(stable_builder).build_contexts(
        RawMinuteHistory(candlesticks=(*minute_bars, stable_bar)),
        _symbol(),
        date(2026, 7, 29),
        True,
        notifications=[],
    )

    assert len(contexts) == 5
    assert sum(timeframe == "15m" for _, timeframe, _ in stable_builder.calls) == 1


def test_formal_signal_is_visible_only_at_or_after_trigger_time() -> None:
    first = datetime(2026, 7, 29, 9, 30, tzinfo=SHANGHAI)
    notification = DowNotification(
        notification_id="n-1",
        event_key="evt-1",
        symbol="700.HK",
        market="hk",
        timeframe="15m",
        side="BUY",
        action_name="买入确认",
        shape_name="双重突破",
        triggered_at=first + timedelta(minutes=1, seconds=30),
        trigger_price=101,
        snapshot_payload={},
    )

    contexts = DowMonitorMinuteResultHistoryBuilder(
        CountingStableStateBuilder(),
    ).build_contexts(
        RawMinuteHistory(
            candlesticks=(
                _raw_bar(first),
                _raw_bar(first + timedelta(minutes=1), close=101),
            )
        ),
        _symbol(),
        date(2026, 7, 29),
        True,
        notifications=[notification],
    )

    assert contexts[0].formal_signal is None
    assert contexts[1].formal_signal.event_key == "evt-1"


def test_engine_adapter_returns_authoritative_snapshot_and_enriched_bars() -> None:
    class Client:
        def __init__(self) -> None:
            self.completion: str | None = None

        def evaluate(self, symbol, timeframe, bars, completion, as_of):
            self.completion = completion
            return SimpleNamespace(
                snapshot=SimpleNamespace(
                    bar_completion="FINAL",
                    provisional=False,
                    price_to_line_pct=0.7,
                    line_role="SUPPORT",
                    volume_ratio_20=2.4,
                ),
                bars=(
                    SimpleNamespace(
                        model_dump=lambda mode="python": {
                            "index": 0,
                            "timestamp": bars[0]["timestamp"],
                            "open": 99,
                            "high": 101,
                            "low": 98,
                            "close": 100,
                            "volume": 500,
                        }
                    ),
                ),
            )

    client = Client()
    raw = _raw_bar(
        datetime(2026, 7, 29, 9, 30, tzinfo=SHANGHAI),
        period="min_15",
    )

    state = DowEngineStableStateBuilder(client).build(
        "700.HK",
        "15m",
        (raw,),
        datetime(2026, 7, 29, 9, 45, tzinfo=SHANGHAI),
    )

    assert client.completion == "FINAL"
    assert state.price_to_line_pct == 0.7
    assert state.volume_ratio_20 == 2.4
    assert state.bars[0].close == 100
