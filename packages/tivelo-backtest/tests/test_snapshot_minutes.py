from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from tivelo_contracts.data import Bar, BarQuery
from tivelo_contracts.errors import ErrorCode, TiveloError

from tivelo_backtest.cli import main
from tivelo_backtest.definitions import DefinitionLoader
from tivelo_backtest.legacy.minute_strategy import run_legacy_minute
from tivelo_backtest.legacy.simulator import MatcherConfig, SimulationOptions
from tivelo_backtest.legacy.snapshot_minutes import LegacySnapshotMinutes, read_legacy_minute_frame

DAY = date(2025, 7, 1)
OPEN = datetime(2025, 7, 1, 1, 30, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
OBSERVED = OPEN + timedelta(days=1)


def bar(start=OPEN, **changes):
    return Bar(
        **{
            **dict(
                instrument_id="I",
                timeframe="1m",
                bar_start=start,
                bar_end=start + MINUTE,
                trading_date=DAY,
                session_id="morning",
                open="10",
                high="12",
                low="9",
                close="11",
                volume="250",
                quantity_unit="share",
                quote_currency="CNY",
                turnover="2625",
                price_basis="raw",
                available_at=OBSERVED,
                ingested_at=OBSERVED,
                source_id="local",
                revision_id="v1",
            ),
            **changes,
        }
    )


def make(bars, as_of=OBSERVED, sessions=None):
    spans = (
        sessions
        if sessions is not None
        else [(OPEN, OPEN + MINUTE), (OPEN + 4 * MINUTE, OPEN + 5 * MINUTE)]
    )
    calendar = SimpleNamespace(
        regular_sessions=lambda day, as_of: tuple(SimpleNamespace(start=s, end=e) for s, e in spans)
    )
    return LegacySnapshotMinutes(
        SimpleNamespace(bars=lambda query: iter(bars)), {"legacy-A": ("I", calendar)}, as_of=as_of
    )


@pytest.mark.parametrize("daily_scale", [1.0, 0.5, 2.0])
def test_minute_strategy_uses_observed_sessions_prior_daily_bars_and_local_trigger(tmp_path, daily_scale):
    query = BarQuery(
        instrument_ids=("I", "J"), timeframe="1m", price_basis="raw",
        start=OPEN, end=OPEN + 2 * MINUTE, as_of=OBSERVED,
    )
    rows = [bar(close="10.7"), bar(instrument_id="J", close="11.0")]
    reader = SimpleNamespace(bars=lambda query: iter(rows))
    history = read_legacy_minute_frame(
        reader, query, symbols={"I": "600000.SH", "J": "000001.SZ"}, minute_label="end",
    )
    assert history["datetime"].to_list() == [datetime(2025, 7, 1, 9, 31)] * 2
    assert history["volume"].to_list() == [2.5, 2.5]
    source = tmp_path / "minute.py"
    source.write_text("""import polars as pl
META = {
    'id': 'demo.minute', 'timeframes': ['1m'], 'daily_history_bars': 3,
    'params': {'gain': 0.06}, 'basic_filter': {'enabled': False},
}
EXECUTION_BACKEND = 'minute_filter'
def filter_minute_history(frame, params, *, daily):
    assert daily['date'].max() < frame['datetime'].dt.date().min()
    return frame.join(
        daily.group_by('symbol').agg(pl.col('raw_close').last().alias('previous')),
        on='symbol',
    ).filter(pl.col('close') >= pl.col('previous') * (1 + params['gain'])).select(
        'symbol', 'close', pl.col('datetime').alias('last_datetime'),
    )
""", encoding="utf-8")
    sealed = tmp_path / "sealed"
    assert main(["seal-legacy-minute", "--source", str(source), "--version", "1", "--output", str(sealed)]) == 0
    source.write_text("raise AssertionError('must load the sealed source')", encoding="utf-8")
    prior, following = DAY - timedelta(days=1), DAY + timedelta(days=1)
    panel = pl.DataFrame([
        dict(symbol=symbol, date=day, open=close, high=close, low=close, close=close, raw_close=close, volume=100)
        for symbol in ("000001.SZ", "600000.SH")
        for day, close in ((prior, 10.0), (DAY, 10.7), (following, 11.1))
    ]).with_columns(pl.col("open", "high", "low", "close") * daily_scale)
    matcher = MatcherConfig(
        initial_capital=10_000, max_positions=1, max_hold_days=1, commission_pct=0, slippage_bps=0,
    )
    loader = DefinitionLoader()
    with loader.load_trusted_legacy_minute(loader.inspect(sealed)) as strategy:
        result = run_legacy_minute(
            panel, strategy, matcher, start=DAY, end=following,
            minute_days=(DAY,), load_day=lambda day: history, limit_for=lambda symbol, day, name: 0.1,
            options=SimulationOptions(include_monte_carlo=False),
        )
    assert result.replay.skipped_days == [following]
    assert result.replay.replayed_days == 1
    assert result.replay.strategy_matches == 2
    assert result.replay.buy_limit_up == 1
    assert result.trigger_times == {("600000.SH", DAY): "09:31"}
    trade, = result.simulation.trades
    assert trade.symbol == "600000.SH"
    assert trade.entry_price == pytest.approx(10.7)
    assert trade.exit_price == pytest.approx(11.1)


def test_observed_minute_frame_rejects_duplicate_bars_without_requiring_a_dense_grid():
    query = BarQuery(
        instrument_ids=("I",), timeframe="1m", price_basis="raw",
        start=OPEN, end=OPEN + 5 * MINUTE, as_of=OBSERVED,
    )
    observed = [bar(), bar(OPEN + 4 * MINUTE)]
    reader = SimpleNamespace(bars=lambda query: iter(observed))
    assert read_legacy_minute_frame(reader, query).height == 2
    observed.append(observed[0])
    with pytest.raises(TiveloError) as error:
        read_legacy_minute_frame(reader, query)
    assert error.value.code == ErrorCode.DATA_CONFLICT


def test_verified_session_order_and_exact_lot_quantity():
    later = bar(OPEN + 4 * MINUTE, volume="0.123456789012", turnover="2.5")
    loader = make([later, bar(OPEN + MINUTE), bar()])
    array = loader(["legacy-A"], {str(DAY)}, "stock")["legacy-A", str(DAY)]
    assert array.tolist() == [[10, 12, 9, 11, 2.5, 2625], [10, 12, 9, 11, 0.00123456789012, 2.5]]
    assert not array.flags.writeable and loader([], set(), "stock") == {}


@pytest.mark.parametrize(
    "rows,code",
    [
        ([], ErrorCode.DATA_COVERAGE_INSUFFICIENT),
        ([bar(), bar()], ErrorCode.DATA_CONFLICT),
        ([bar(price_basis="qfq")], ErrorCode.CONTRACT_INVALID),
        ([bar(quantity_unit="contract")], ErrorCode.CONTRACT_INVALID),
        ([bar(turnover=None)], ErrorCode.DATA_COVERAGE_INSUFFICIENT),
        ([bar(bar_end=OPEN + 2 * MINUTE)], ErrorCode.CONTRACT_INVALID),
        (
            [bar(available_at=OBSERVED + MINUTE, ingested_at=OBSERVED + MINUTE)],
            ErrorCode.FUTURE_INFORMATION,
        ),
        ([bar(is_final=False)], ErrorCode.FUTURE_INFORMATION),
    ],
)
def test_loader_rejects_unsupported_or_incomplete_minute_evidence(rows, code):
    with pytest.raises(TiveloError) as error:
        make(rows)(["legacy-A"], {str(DAY)}, "stock")
    assert error.value.code == code


def test_binding_scope_session_and_date_guards():
    for loader, symbol, asset, code in [
        (make([]), "other", "stock", ErrorCode.AMBIGUOUS_INSTRUMENT),
        (make([]), "legacy-A", "etf", ErrorCode.UNSUPPORTED_ASSET),
        (make([], as_of=OPEN), "legacy-A", "stock", ErrorCode.FUTURE_INFORMATION),
        (make([], sessions=[]), "legacy-A", "stock", ErrorCode.DATA_COVERAGE_INSUFFICIENT),
        (
            make([], sessions=[(OPEN, OPEN + MINUTE), (OPEN, OPEN + 3 * MINUTE)]),
            "legacy-A",
            "stock",
            ErrorCode.CONTRACT_INVALID,
        ),
    ]:
        with pytest.raises(TiveloError) as error:
            loader([symbol], {str(DAY)}, asset)
        assert error.value.code == code
