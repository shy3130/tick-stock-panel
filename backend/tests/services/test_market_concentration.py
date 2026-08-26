from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import market_concentration as mc


def _relax_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mc, "MIN_STOCK_COUNT", 4)
    monkeypatch.setattr(mc, "MIN_INDUSTRY_COUNT", 2)
    monkeypatch.setattr(mc, "MIN_SYMBOL_COVERAGE", 0.9)
    monkeypatch.setattr(mc, "MIN_TURNOVER_COVERAGE", 0.95)


def test_normalized_hhi_and_industry_turnover_formula(monkeypatch: pytest.MonkeyPatch) -> None:
    _relax_gates(monkeypatch)
    returns = {"A1": 0.02, "A2": 0.01, "B1": -0.01, "B2": -0.02}
    amounts = {"A1": 10.0, "A2": 20.0, "B1": 30.0, "B2": 40.0}
    industries = {"A1": "A", "A2": "A", "B1": "B", "B2": "B"}

    result = mc.compute_day_metrics(returns, amounts, industries, day=date(2026, 8, 24))

    assert result.valid is True
    assert result.symbol_coverage == pytest.approx(1.0)
    assert result.turnover_coverage == pytest.approx(1.0)
    # 行业成交额 30/70，而不是四只股票的个股成交额 HHI。
    assert result.turnover_hhi == pytest.approx(0.16)
    assert result.positive_return_hhi == pytest.approx(1.0)
    assert result.top3_contribution == pytest.approx(1.0)
    assert result.top5_contribution == pytest.approx(1.0)


def test_high_unmapped_amount_fails_turnover_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    _relax_gates(monkeypatch)
    returns = {"A1": 0.02, "A2": 0.01, "B1": -0.01, "X": -0.02}
    amounts = {"A1": 1.0, "A2": 1.0, "B1": 1.0, "X": 100.0}
    industries = {"A1": "A", "A2": "A", "B1": "B"}

    result = mc.compute_day_metrics(returns, amounts, industries)

    assert result.turnover_coverage == pytest.approx(3.0 / 103.0)
    assert result.valid is False
    assert "turnover_coverage_below_minimum" in result.reasons
    assert "symbol_coverage_below_minimum" in result.reasons


def test_zero_positive_contribution_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _relax_gates(monkeypatch)
    returns = {"A1": 0.01, "A2": -0.01, "B1": 0.01, "B2": -0.01}
    amounts = {symbol: 1.0 for symbol in returns}
    industries = {"A1": "A", "A2": "A", "B1": "B", "B2": "B"}

    result = mc.compute_day_metrics(returns, amounts, industries)

    assert result.valid is False
    assert "zero_positive_contribution" in result.reasons


@pytest.mark.parametrize(
    ("percentiles", "expected"),
    [
        ((0.6, 0.85, 0.9, 0.2), "concentrated"),
        ((0.7, 0.3, 0.2, 0.3), "dispersed"),
        ((0.4, 0.6, 0.5, 0.5), "transition"),
        ((None, 0.3, 0.2, 0.3), "unavailable"),
    ],
)
def test_classify_state_uses_empirical_percentiles(percentiles, expected) -> None:
    assert mc.classify_state(*percentiles) == expected


class _HistoryRepo:
    def __init__(self, frame: pl.DataFrame, latest: date, data_dir) -> None:
        self.frame = frame
        self.latest = latest
        self.store = SimpleNamespace(data_dir=data_dir)
        self.cache_generation = 1

    def get_enriched_latest(self):
        return pl.DataFrame(), self.latest

    def get_enriched_range(self, start, end, columns=None):
        result = self.frame.filter((pl.col("date") >= start) & (pl.col("date") <= end))
        requested = [column for column in (columns or result.columns) if column in result.columns]
        return result.select(requested)


def _business_days(start: date, count: int) -> list[date]:
    days: list[date] = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _history_frame(days: list[date]) -> pl.DataFrame:
    symbols = ["000001.SZ", "000002.SZ", "600001.SH", "600002.SH"]
    prices = {symbol: 100.0 for symbol in symbols}
    rows: list[dict] = []
    for index, day in enumerate(days):
        for symbol_index, symbol in enumerate(symbols):
            industry_a = symbol_index < 2
            daily_return = (
                (0.002 + 0.0002 * (index % 5)) if industry_a else (-0.001 + 0.0001 * (index % 3))
            )
            prices[symbol] *= 1.0 + daily_return
            industry_amount = 100.0 + (index % 7) * 8.0 if industry_a else 130.0 - (index % 7) * 5.0
            rows.append(
                {
                    "symbol": symbol,
                    "date": day,
                    "raw_close": prices[symbol],
                    "amount": industry_amount + symbol_index,
                }
            )
    return pl.DataFrame(rows)


def test_compute_market_state_is_strict_t1_and_uses_smoothed_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    days = _business_days(date(2025, 12, 1), 155)
    frame = _history_frame(days)
    repo = _HistoryRepo(frame, days[-1], tmp_path)
    monkeypatch.setattr(mc, "MIN_STOCK_COUNT", 4)
    monkeypatch.setattr(mc, "MIN_INDUSTRY_COUNT", 2)
    monkeypatch.setattr(mc, "MIN_SYMBOL_COVERAGE", 1.0)
    monkeypatch.setattr(mc, "MIN_TURNOVER_COVERAGE", 1.0)
    monkeypatch.setattr(mc, "MIN_OBSERVATION_SESSIONS", 2)
    monkeypatch.setattr(mc, "MIN_CALIBRATION_DAYS", 20)
    monkeypatch.setattr(mc, "CALIBRATION_DAYS", 40)
    monkeypatch.setattr(mc, "FETCH_TRADING_DAYS", 150)
    monkeypatch.setattr(
        mc,
        "_load_industry_events",
        lambda _data_dir: [
            (days[0], "000001.SZ", "A"),
            (days[0], "000002.SZ", "A"),
            (days[0], "600001.SH", "B"),
            (days[0], "600002.SH", "B"),
        ],
    )

    target = days[-1]
    first = mc.compute_market_state(repo, target)
    assert first.available is True
    assert first.target_date == target.isoformat()
    assert first.signal_date == days[-2].isoformat()
    assert first.coverage.calibration_days == 40
    assert all(value is not None for value in first.metrics.model_dump().values())

    # target 当日无论怎样变化都不得进入状态计算。
    repo.frame = repo.frame.with_columns(
        pl.when(pl.col("date") == target)
        .then(pl.col("raw_close") * 50)
        .otherwise(pl.col("raw_close"))
        .alias("raw_close")
    )
    second = mc.compute_market_state(repo, target)
    assert second.model_dump() == first.model_dump()


def test_latest_target_uses_latest_canonical_date(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    days = _business_days(date(2026, 1, 1), 3)
    repo = _HistoryRepo(_history_frame(days), days[-1], tmp_path)
    monkeypatch.setattr(mc, "MIN_OBSERVATION_SESSIONS", 2)
    monkeypatch.setattr(mc, "_load_industry_events", lambda _data_dir: [])

    snapshot = mc.compute_market_state(repo)

    assert snapshot.target_date == days[-1].isoformat()
    assert snapshot.signal_date == days[-2].isoformat()
    assert snapshot.available is False


def test_market_state_cache_prunes_expired_entries_and_caps_size(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        cache_generation=1,
    )
    clock = [1000.0]
    monkeypatch.setattr(mc.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(mc, "_CACHE_MAX_ENTRIES", 3)
    monkeypatch.setattr(
        mc,
        "compute_market_state",
        lambda _repo, target: mc._unavailable_snapshot(
            target,
            None,
            ["no_prior_trading_day"],
            [],
        ),
    )
    mc.invalidate_market_state_cache()
    expired_snapshot = mc._unavailable_snapshot(
        date(2020, 1, 1),
        None,
        ["no_prior_trading_day"],
        [],
    )
    mc._cache[("expired", "latest", 0)] = (0.0, expired_snapshot)

    for offset in range(5):
        mc.market_state_for_date(repo, date(2026, 1, 1) + timedelta(days=offset))
        clock[0] += 1.0

    assert ("expired", "latest", 0) not in mc._cache
    assert len(mc._cache) == 3
    mc.invalidate_market_state_cache()
