"""#201 回归: 旧信号回测的 _load_panel 必须带指标 warmup 窗口。

直接按 [start,end] 过滤后 compute_all, 区间头部的 MA/MACD/RSI 会因缺
历史窗口而失真 (回测起始段信号不可信)。
"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl

from app.services.backtest import BacktestService


def _synthetic_enriched(n_days: int) -> pl.DataFrame:
    base = date(2026, 1, 1)
    days = [base + timedelta(days=i) for i in range(n_days)]
    n = len(days)
    closes = [10.0 + (i % 7) * 0.3 + i * 0.01 for i in range(n)]
    return pl.DataFrame(
        {
            "symbol": ["600000.SH"] * n,
            "date": days,
            "open": [c - 0.05 for c in closes],
            "high": [c + 0.1 for c in closes],
            "low": [c - 0.1 for c in closes],
            "close": closes,
            "volume": [10000.0] * n,
            "amount": [c * 10000.0 for c in closes],
            "raw_close": closes,
            "raw_high": [c + 0.1 for c in closes],
            "raw_low": [c - 0.1 for c in closes],
        }
    )


def test_load_panel_warms_up_indicators(monkeypatch, tmp_path) -> None:
    df = _synthetic_enriched(250)
    monkeypatch.setattr(
        "app.services.backtest.scan_enriched_parquet", lambda glob: df.lazy()
    )
    svc = BacktestService(repo=SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path), get_instruments_asset=lambda _: pl.DataFrame(),
    ))

    start = df["date"][-30]
    end = df["date"][-1]
    panel = svc._load_panel(["600000.SH"], start, end)

    # warmup 行不进入结果面板。
    assert panel["date"].min() == start
    assert panel["date"].max() == end

    # 数值复现: 区间头部的指标必须与「全历史计算后裁剪」的基准一致。
    # 只断言非 NaN 区分不了新旧代码 —— ewm 类指标 (RSI/MACD) 从首个值
    # 播种, 无 warmup 时首行也有值, 只是数值失真 (#201 的实际危害)
    from app.indicators.pipeline import compute_all
    reference = compute_all(df).filter(pl.col("date") >= start)
    ref_rsi = float(reference["rsi_14"][0])
    first_rsi = panel["rsi_14"][0]
    assert abs(first_rsi - ref_rsi) < 1.0, (
        f"rsi_14 without warmup: {first_rsi} vs reference {ref_rsi}"
    )


def test_load_panel_insufficient_history_degrades_gracefully(monkeypatch, tmp_path) -> None:
    # 数据起点晚于 warmup 起点时自然退化: 有多少算多少, 不抛异常
    df = _synthetic_enriched(20)
    monkeypatch.setattr(
        "app.services.backtest.scan_enriched_parquet", lambda glob: df.lazy()
    )
    svc = BacktestService(repo=SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path), get_instruments_asset=lambda _: pl.DataFrame(),
    ))

    panel = svc._load_panel(["600000.SH"], df["date"][0], df["date"][-1])
    assert len(panel) == 20
