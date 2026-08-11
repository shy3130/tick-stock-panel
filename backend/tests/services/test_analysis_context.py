from __future__ import annotations

from datetime import date, datetime, timedelta

import polars as pl

from app.errors import DATA_INCOMPLETE, STALE_INPUT
from app.services.analysis_context import assemble_prompt, build_analysis_frame, preflight_analysis


def _frame(rows: int = 65, *, forming: bool = False) -> pl.DataFrame:
    start = date(2026, 5, 1)
    dates = [start + timedelta(days=index) for index in range(rows)]
    opens = [10.0 + index * 0.1 for index in range(rows)]
    closes = [value + (0.05 if index % 2 == 0 else -0.03) for index, value in enumerate(opens)]
    data = {
        "symbol": ["600519.SH"] * rows,
        "date": dates,
        "open": opens,
        "high": [max(o, c) + 0.2 for o, c in zip(opens, closes, strict=True)],
        "low": [min(o, c) - 0.2 for o, c in zip(opens, closes, strict=True)],
        "close": closes,
        "volume": [1000.0 + index for index in range(rows)],
        "ema20": [9.8 + index * 0.1 for index in range(rows)],
        "atr_14": [0.4] * rows,
        "vol_ma5": [1000.0] * rows,
        "closed": [True] * (rows - 1) + [not forming],
    }
    return pl.DataFrame(data)


def _build(df: pl.DataFrame, *, as_of: datetime) -> object:
    return build_analysis_frame(
        df,
        symbol="600519.SH",
        market="a_share",
        timeframe="1d",
        data_as_of=as_of,
        source="canonical_enriched",
        adjustment="qfq",
        key_levels=[12.0],
    )


def test_builder_excludes_forming_bar_and_keeps_input_immutable():
    source = _frame(forming=True)
    original_columns = source.columns
    frame = _build(source, as_of=datetime(2026, 8, 5, 16, 0))
    assert len(frame.bars) == 64
    assert len(frame.features) == 64
    assert source.columns == original_columns
    assert any("closed=false" in warning for warning in frame.warnings)
    assert frame.features[-1].dist_to_key is not None


def test_builder_drops_non_finite_ohlc_and_nulls_non_finite_volume():
    source = _frame()
    source = source.with_columns(
        pl.when(pl.int_range(pl.len()) == pl.len() - 1)
        .then(float("nan"))
        .otherwise(pl.col("close"))
        .alias("close"),
        pl.when(pl.int_range(pl.len()) == pl.len() - 2)
        .then(float("inf"))
        .otherwise(pl.col("volume"))
        .alias("volume"),
    )
    frame = _build(source, as_of=datetime(2026, 8, 5, 16, 0))
    assert len(frame.bars) == 64
    assert frame.bars[-1].volume is None
    assert any(warning == "dropped_non_finite_or_undated_bars:1" for warning in frame.warnings)


def test_preflight_distinguishes_incomplete_and_stale():
    short = _build(_frame(30), as_of=datetime(2026, 8, 5, 16, 0))
    rejected = preflight_analysis(short, now=datetime(2026, 8, 6, 16, 0))
    assert not rejected.ok
    assert rejected.error is not None and rejected.error.code == DATA_INCOMPLETE

    complete = _build(_frame(), as_of=datetime(2026, 8, 5, 16, 0))
    stale = preflight_analysis(complete, now=datetime(2026, 8, 20, 16, 0))
    assert not stale.ok
    assert stale.error is not None and stale.error.code == STALE_INPUT


def test_preflight_rejects_external_fallback_for_plan_only():
    frame = build_analysis_frame(
        _frame(),
        symbol="600519.SH",
        market="a_share",
        timeframe="1d",
        data_as_of=datetime(2026, 8, 5, 16, 0),
        source="external_fallback:tencent",
        adjustment="qfq",
        degraded=True,
    )
    result = preflight_analysis(
        frame,
        purpose="trading_plan",
        now=datetime(2026, 8, 6, 16, 0),
    )
    assert not result.ok
    assert result.error is not None and result.error.code == DATA_INCOMPLETE


def test_prompt_preserves_provenance_and_does_not_duplicate_system_contract():
    frame = _build(_frame(), as_of=datetime(2026, 8, 5, 16, 0))
    messages, meta = assemble_prompt(
        frame,
        purpose="stock_analysis",
        user_question="关注量价",
        methodology="只使用量价结构",
        invariants={"source": frame.source},
        max_tokens=1200,
        contract="SYSTEM-CONTRACT",
    )
    assert messages[0] == {"role": "system", "content": "SYSTEM-CONTRACT"}
    assert "SYSTEM-CONTRACT" not in messages[1]["content"]
    assert "source: canonical_enriched" in messages[1]["content"]
    assert meta["estimated_tokens"] <= 1200
