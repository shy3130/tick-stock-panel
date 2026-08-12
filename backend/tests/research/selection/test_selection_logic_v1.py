from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import numpy as np
import pytest

from app.backtest.matrix import MarketDataMatrix, validate_signal_matrix
from app.strategy.builtin.quality_momentum_v1 import (
    MATRIX_STRATEGY,
    compute_quality_components,
)
from research.selection.audit import build_decision_rows
from research.selection.news import news_scores_as_of, validate_news_records


def _market() -> MarketDataMatrix:
    days = 100
    slow = 10.0 * np.power(1.003, np.arange(days))
    hot = 10.0 * np.power(1.03, np.arange(days))
    flat = np.full(days, 10.0)
    close = np.column_stack([slow, hot, flat]).astype(np.float32)
    volume = np.full_like(close, 5_000_000.0)
    amount = close * volume
    return MarketDataMatrix(
        timestamps=np.arange(days, dtype=np.int64),
        timestamp_labels=tuple(f"2026-01-{index + 1:02d}" for index in range(days)),
        session_ids=np.arange(days, dtype=np.int32),
        symbols=("000001.SZ", "000002.SZ", "000003.SZ"),
        names=("稳步上涨", "过热上涨", "横盘"),
        open=close.copy(),
        high=(close * 1.01).astype(np.float32),
        low=(close * 0.99).astype(np.float32),
        close=close,
        volume=volume,
        tradable=np.ones_like(close, dtype=bool),
        limit_up_locked=np.zeros_like(close, dtype=bool),
        limit_down_locked=np.zeros_like(close, dtype=bool),
        fields={"amount": amount.astype(np.float32)},
    )


def test_quality_selector_is_deterministic_finite_and_rejects_overheat() -> None:
    market = _market()
    first = compute_quality_components(market, {})
    second = compute_quality_components(market, {})
    np.testing.assert_array_equal(first["score"], second["score"])
    assert np.isfinite(first["score"]).all()
    assert bool(first["eligible"][-1, 0])
    assert not bool(first["eligible"][-1, 1])
    assert not bool(first["checks"]["momentum_20d_ceiling"][-1, 1])
    assert not bool(first["eligible"][-1, 2])


def test_strategy_output_obeys_matrix_contract() -> None:
    market = _market()
    signals = MATRIX_STRATEGY.compute_signals(market, {})
    validate_signal_matrix(signals, market.shape)
    assert signals.entry[-1].tolist() == [1, 0, 0]
    assert signals.score.dtype == np.float32


def test_strategy_never_enters_on_ma20_breakdown() -> None:
    market = _market()
    days = market.close.shape[0]
    close = (10.0 * np.power(1.004, np.arange(days))).astype(np.float32)
    market.close[:, 0] = close
    market.open[:, 0] = close
    market.high[:, 0] = close * 1.01
    market.low[:, 0] = close * 0.99
    market.fields["amount"][:, 0] = close * market.volume[:, 0]

    previous_close = float(market.close[-2, 0])
    market.close[-1, 0] = previous_close * 0.964
    market.open[-1, 0] = previous_close
    market.high[-1, 0] = previous_close * 1.002
    market.low[-1, 0] = market.close[-1, 0] * 0.998
    market.fields["amount"][-1, 0] = market.close[-1, 0] * market.volume[-1, 0]

    signals = MATRIX_STRATEGY.compute_signals(market, {})
    assert bool(signals.exit[-1, 0])
    assert not bool(signals.entry[-1, 0])
    assert not np.any(signals.entry.astype(bool) & signals.exit.astype(bool))


def test_audit_records_signal_and_portfolio_rejections() -> None:
    market = SimpleNamespace(
        symbols=("A", "B", "C", "D"),
        names=("甲", "乙", "丙", "丁"),
        timestamp_labels=("2026-08-07",),
    )
    ones = np.ones((1, 4), dtype=float)
    result = {
        "eligible": np.array([[True, True, True, False]]),
        "score": np.array([[90.0, 80.0, 70.0, 60.0]]),
        "checks": {"finite_history": np.array([[True, True, True, False]])},
        "features": {"momentum_20d": ones},
        "components": {"trend_quality": ones},
    }
    rows, summary = build_decision_rows(
        market=market,
        result=result,
        time_id=0,
        industries={"A": "芯片", "B": "芯片", "C": "芯片", "D": "软件"},
        max_positions=3,
        max_per_industry=2,
    )
    assert [row["decision"] for row in rows] == [
        "selected_signal", "selected_signal", "rejected_portfolio", "rejected_signal"
    ]
    assert summary["selected_symbols"] == ["A", "B"]
    assert "行业集中度限制" in rows[2]["reasons"][0]
    assert rows[3]["reasons"] == ["历史数据不足或指标非有限值"]


def test_audit_does_not_treat_missing_industries_as_one_sector() -> None:
    market = SimpleNamespace(
        symbols=("A", "B", "C"),
        names=("甲", "乙", "丙"),
        timestamp_labels=("2026-08-07",),
    )
    result = {
        "eligible": np.array([[True, True, True]]),
        "score": np.array([[90.0, 80.0, 70.0]]),
        "checks": {"finite_history": np.ones((1, 3), dtype=bool)},
        "features": {"momentum_20d": np.ones((1, 3), dtype=float)},
        "components": {"trend_quality": np.ones((1, 3), dtype=float)},
    }

    rows, summary = build_decision_rows(
        market=market,
        result=result,
        time_id=0,
        industries={},
        max_positions=3,
        max_per_industry=1,
    )
    assert [row["decision"] for row in rows] == ["selected_signal"] * 3
    assert summary["selected_symbols"] == ["A", "B", "C"]


def test_news_overlay_excludes_future_and_deduplicates() -> None:
    records = [
        {
            "published_at": "2026-08-06T10:00:00+08:00",
            "ts_code": "000001.SZ",
            "source": "exchange",
            "event_type": "announcement",
            "score": 0.8,
        },
        {
            "published_at": "2026-08-06T10:00:00+08:00",
            "ts_code": "000001.SZ",
            "source": "exchange",
            "event_type": "announcement",
            "score": 0.8,
        },
        {
            "published_at": "2026-08-08T10:00:00+08:00",
            "ts_code": "000001.SZ",
            "source": "media",
            "event_type": "policy",
            "score": 1.0,
        },
    ]
    result = news_scores_as_of(
        records,
        as_of=datetime(2026, 8, 7, 8, 0, tzinfo=UTC),
    )
    assert result["000001.SZ"]["score"] == pytest.approx(0.8)
    assert result["000001.SZ"]["event_count"] == 1


def test_news_contract_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone"):
        validate_news_records([{
            "published_at": "2026-08-07T10:00:00",
            "ts_code": "000001.SZ",
            "source": "exchange",
            "event_type": "announcement",
            "score": 0.2,
        }])
