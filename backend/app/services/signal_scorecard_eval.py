"""Signal Scorecard outcome 评估引擎 — 纯函数, 无 DB/provider 依赖。

分类规则移植自 daily_stock_analysis BacktestEngine._classify_signal_outcome:
    direction=up    : return% >= +band → hit, <= -band → miss, 中间 → neutral
    direction=not_up: return% <= +band → hit, 否则 → miss
    (band = NEUTRAL_BAND_PCT = 2.0)

T+N 冻结口径:
    锚定日 = 信号列触发交易日 (signal 当日 close 即 anchor_price);
    第 N 个交易日 = enriched 分区中 date > 锚定日 的 distinct trading date
    升序第 N 个 (天然跳过周末/假日/停牌缺口)。

前向交易日不足 (< N) 的 horizon 保持 pending: evaluate_outcome 返回 eval_status=unable,
但调用方 (job/api) 不得 append 该 unable 行, 以免冻结未来评估。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.services.signal_scorecard_store import NEUTRAL_BAND_PCT

# 前向窗口查询所需的 OHLC 列 (均为 ENRICHED_STORAGE_COLS, 走快速扫描路径)。
_FWD_COLUMNS = ["date", "open", "high", "low", "close"]


def direction_for_signal(signal_kind: str, direction_override: str | None = None) -> str:
    """推断信号期望方向。

    entry / builtin → up (看涨); exit → not_up。
    direction_override (显式 up/not_up) 优先, 用于 tracked_signals 逐信号覆盖。
    """
    if direction_override in ("up", "not_up"):
        return direction_override
    if signal_kind == "exit":
        return "not_up"
    return "up"  # entry, builtin, 未知 → 默认看涨


def compute_forward_window(
    repo: Any,
    symbol: str,
    anchor_date: date,
    n_trading_days: int,
) -> list[dict[str, float]] | None:
    """取锚定日之后的前 n_trading_days 个交易日的 OHLC。

    返回 [{"date": str, "open", "high", "low", "close"}, ...], 按日期升序。
    实际行数可能少于 n_trading_days (前向不足), 由调用方判断。
    repo 查询失败 (返回 None 或抛异常) → 返回 None。
    """
    import polars as pl

    start = anchor_date + timedelta(days=1)
    end = date.today()
    if start > end:
        return []
    try:
        df = repo.get_enriched_range(
            start, end, symbols=[symbol], columns=list(_FWD_COLUMNS)
        )
    except Exception:
        return None
    if df is None:
        return None
    if df.is_empty():
        return []

    if "symbol" in df.columns:
        df = df.filter(pl.col("symbol") == symbol)
    if df.is_empty():
        return []

    # 取 distinct trading date 升序前 N 个, 再投影对应行。
    try:
        all_dates = sorted(df["date"].unique().to_list())
    except Exception:
        return None
    selected = all_dates[:n_trading_days]
    if not selected:
        return []

    df_n = df.filter(pl.col("date").is_in(selected)).sort("date")
    rows: list[dict[str, float]] = []
    cols_present = set(df_n.columns)
    for rec in df_n.iter_rows(named=True):
        d = rec.get("date")
        rows.append({
            "date": d.isoformat() if hasattr(d, "isoformat") else str(d),
            "open": _to_float(rec.get("open")),
            "high": _to_float(rec.get("high")),
            "low": _to_float(rec.get("low")),
            "close": _to_float(rec.get("close")),
        })
    # 兜底: 若 OHLC 列缺失则不影响结构 (字段为 None)
    _ = cols_present
    return rows


def evaluate_outcome(
    event: dict[str, Any],
    forward_bars: list[dict[str, float]] | None,
    n_trading_days: int,
    neutral_band_pct: float = NEUTRAL_BAND_PCT,
) -> dict[str, Any]:
    """评估单事件单 horizon 的 outcome (纯函数, 无副作用)。

    event: SignalEvent dict (含 anchor_price, direction_expected, signal_kind)。
    forward_bars: compute_forward_window 返回的前向 OHLC 行 (调用方按 horizon 切片)。
    n_trading_days: 该 horizon 对应的交易日数 (1/3/5/10)。

    返回 dict:
        eval_status     completed | unable
        outcome         hit | miss | neutral | None
        direction_expected  up | not_up
        direction_correct   bool | None
        start_price     T+1 开盘 (前向窗口首根 bar 的 open) | None
        end_close       第 N 个交易日收盘 | None
        max_high        窗口最高 | None
        min_low         窗口最低 | None
        stock_return_pct (end_close - anchor)/anchor * 100 | None
        unable_reason   insufficient_forward_bars | invalid_anchor_price |
                        forward_window_query_failed | None
    """
    direction = event.get("direction_expected") or direction_for_signal(
        event.get("signal_kind", "builtin")
    )

    # 前向窗口查询失败 (repo 返回 None)
    if forward_bars is None:
        return _unable("forward_window_query_failed", direction)

    # 前向交易日不足 → unable (调用方应跳过, 不 append)
    if len(forward_bars) < n_trading_days:
        return _unable("insufficient_forward_bars", direction)

    anchor = event.get("anchor_price")
    if anchor is None or anchor <= 0:
        return _unable("invalid_anchor_price", direction)

    last = forward_bars[n_trading_days - 1]
    end_close = last.get("close")
    if end_close is None or end_close != end_close:  # NaN check
        return _unable("invalid_end_close", direction)

    first = forward_bars[0]
    start_price = first.get("open")
    highs = [b["high"] for b in forward_bars[:n_trading_days]
             if b.get("high") is not None]
    lows = [b["low"] for b in forward_bars[:n_trading_days]
            if b.get("low") is not None]
    max_high = max(highs) if highs else None
    min_low = min(lows) if lows else None

    stock_return_pct = (end_close - anchor) / anchor * 100.0

    if direction == "up":
        if stock_return_pct >= neutral_band_pct:
            outcome = "hit"
        elif stock_return_pct <= -neutral_band_pct:
            outcome = "miss"
        else:
            outcome = "neutral"
        direction_correct = stock_return_pct > 0
    else:  # not_up
        if stock_return_pct <= neutral_band_pct:
            outcome = "hit"
        else:
            outcome = "miss"
        direction_correct = stock_return_pct <= 0

    return {
        "eval_status": "completed",
        "outcome": outcome,
        "direction_expected": direction,
        "direction_correct": direction_correct,
        "start_price": start_price,
        "end_close": end_close,
        "max_high": max_high,
        "min_low": min_low,
        "stock_return_pct": round(stock_return_pct, 6),
        "unable_reason": None,
    }


# ── helpers ──────────────────────────────────────────────
def _unable(reason: str, direction: str) -> dict[str, Any]:
    return {
        "eval_status": "unable",
        "outcome": None,
        "direction_expected": direction,
        "direction_correct": None,
        "start_price": None,
        "end_close": None,
        "max_high": None,
        "min_low": None,
        "stock_return_pct": None,
        "unable_reason": reason,
    }


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f
