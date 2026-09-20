"""A-share price-limit rules shared by indicators, backtests, and APIs."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta

import numpy as np
import polars as pl

MAIN_BOARD_ST_LIMIT_CHANGE_DATE = date(2026, 7, 6)

MAIN_BOARD_LIMIT = 0.10
LEGACY_MAIN_BOARD_ST_LIMIT = 0.05
GROWTH_BOARD_LIMIT = 0.20
BEIJING_BOARD_LIMIT = 0.30


def is_risk_warning_name(name: str | None) -> bool:
    return "ST" in str(name or "").upper()


def board_limit_pct(symbol: str) -> float:
    if symbol.endswith(".BJ"):
        return BEIJING_BOARD_LIMIT
    if symbol.startswith(("300", "301", "688", "689")):
        return GROWTH_BOARD_LIMIT
    return MAIN_BOARD_LIMIT


def price_limit_pct(
    symbol: str,
    trade_date: date,
    *,
    is_risk_warning: bool = False,
) -> float:
    base = board_limit_pct(symbol)
    if (
        base == MAIN_BOARD_LIMIT
        and is_risk_warning
        and trade_date < MAIN_BOARD_ST_LIMIT_CHANGE_DATE
    ):
        return LEGACY_MAIN_BOARD_ST_LIMIT
    return base


# ================================================================
# 注册制新股上市初期「无涨跌幅限制」窗口
#
# 规则 (按板块与上市日):
#   - 沪深主板: 2023-02-17 全面注册制起, 上市前 5 个交易日
#   - 创业板:   2020-08-24 注册制首批起, 上市前 5 个交易日
#   - 科创板:   2019-07-22 开板即注册制, 上市前 5 个交易日
#   - 北交所:   2021-11-15 开市即注册制, 仅上市首日 (次日起 ±30%)
# 改革前上市的老股不适用 (窗口数 0), 维持板块涨跌幅规则。
#
# 判定数据源: instruments 维表的 listing_date (instrument_sync 已落库)。
# ================================================================
MAIN_BOARD_REGISTRATION_DATE = date(2023, 2, 17)
GEM_REGISTRATION_DATE = date(2020, 8, 24)
STAR_REGISTRATION_DATE = date(2019, 7, 22)
BJ_OPENING_DATE = date(2021, 11, 15)

NO_LIMIT_WINDOW_TRADING_DAYS = 5      # 沪深主板/创业板/科创板
NO_LIMIT_WINDOW_TRADING_DAYS_BJ = 1   # 北交所仅上市首日

# 无交易日历可用时的保守日历天边际: 5 个交易日最坏跨 ~15 个日历天
# (含国庆中秋连休), 1 个交易日跨 3 天。近似路径宁多标勿漏标 — 窗口外
# 多标的后果是漏一个涨停标记, 把无涨跌幅日误判成涨停的代价高得多。
# 精确性由调用方补足: pipeline 盘后路径用「symbol 内日期序号」门控,
# 实时路径无历史序列, 接受近似 (哨兵值路径仍精确)。
NO_LIMIT_CALENDAR_MARGIN_DAYS = 15
NO_LIMIT_CALENDAR_MARGIN_DAYS_BJ = 3


def no_limit_window_days(symbol: str, listing_date: date | None) -> int:
    """上市初期无涨跌幅限制的交易日窗口数; 0 = 不适用 (老股/无上市日)。"""
    if listing_date is None:
        return 0
    if symbol.endswith(".BJ"):
        return NO_LIMIT_WINDOW_TRADING_DAYS_BJ if listing_date >= BJ_OPENING_DATE else 0
    if symbol.startswith(("300", "301")):
        return NO_LIMIT_WINDOW_TRADING_DAYS if listing_date >= GEM_REGISTRATION_DATE else 0
    if symbol.startswith(("688", "689")):
        return NO_LIMIT_WINDOW_TRADING_DAYS if listing_date >= STAR_REGISTRATION_DATE else 0
    return NO_LIMIT_WINDOW_TRADING_DAYS if listing_date >= MAIN_BOARD_REGISTRATION_DATE else 0


def no_limit_window_margin_days(symbol: str) -> int:
    """窗口的日历天安全边际 (北交所 3 天, 其余 15 天)。"""
    return NO_LIMIT_CALENDAR_MARGIN_DAYS_BJ if symbol.endswith(".BJ") else NO_LIMIT_CALENDAR_MARGIN_DAYS


def parse_listing_date(value: object) -> date | None:
    """instruments 维表 listing_date 列值 → date (兼容 str / date / datetime / None)。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def is_no_limit_day(
    symbol: str,
    listing_date: date | None,
    trade_date: date,
    *,
    day_rank: int | None = None,
) -> bool:
    """trade_date 是否落在上市初期无涨跌幅窗口内 (API 层与实时路径共用)。

    day_rank 提供时 (该 symbol 日K按日期升序的序号, 上市首日=1) 精确按
    交易日数判定; 否则退化为 listing_date + 保守日历天边际的近似。
    """
    days = no_limit_window_days(symbol, listing_date)
    if days == 0 or listing_date is None or trade_date < listing_date:
        return False
    boundary = listing_date + timedelta(days=no_limit_window_margin_days(symbol))
    if trade_date > boundary:
        return False
    return day_rank is None or day_rank <= days


def polars_price_limit_pct(
    symbol: pl.Expr,
    trade_date: pl.Expr,
    is_risk_warning: pl.Expr,
) -> pl.Expr:
    """Return a vectorized Polars expression for the effective daily limit."""
    is_growth = symbol.str.starts_with("300") | symbol.str.starts_with("301")
    is_star = symbol.str.starts_with("688") | symbol.str.starts_with("689")
    is_beijing = symbol.str.ends_with(".BJ")
    is_non_main = is_growth | is_star | is_beijing
    base = (
        pl.when(is_growth | is_star).then(GROWTH_BOARD_LIMIT)
        .when(is_beijing).then(BEIJING_BOARD_LIMIT)
        .otherwise(MAIN_BOARD_LIMIT)
    )
    legacy_main_st = (
        is_risk_warning.fill_null(False)
        & ~is_non_main
        & (trade_date < pl.lit(MAIN_BOARD_ST_LIMIT_CHANGE_DATE))
    )
    return (
        pl.when(legacy_main_st).then(LEGACY_MAIN_BOARD_ST_LIMIT)
        .otherwise(base)
        .cast(pl.Float64)
    )


def polars_is_risk_warning_name(name: pl.Expr) -> pl.Expr:
    """Return whether an instrument name contains the ST risk-warning marker."""
    return name.fill_null("").str.to_uppercase().str.contains("ST", literal=True)


def polars_limit_price(previous: pl.Expr, limit_pct: pl.Expr, *, up: bool) -> pl.Expr:
    """Calculate exchange half-up prices with integer-cent arithmetic."""
    sign = 1 if up else -1
    numerator = ((1 + sign * limit_pct) * 100).round(0).cast(pl.Int64)
    cents = (previous * 100 + 0.5).floor().cast(pl.Int64)
    return ((cents * numerator + 50) // 100) / 100


def numpy_limit_pct_vectors(
    symbols: Sequence[str],
    names: Sequence[str],
) -> tuple[np.ndarray, np.ndarray]:
    """Return pre/post-change vectors once; callers select one per date."""
    current = np.fromiter(
        (board_limit_pct(str(symbol)) for symbol in symbols),
        dtype=np.float64,
        count=len(symbols),
    )
    legacy = current.copy()
    for asset_id, (_symbol, name) in enumerate(zip(symbols, names, strict=True)):
        if current[asset_id] == MAIN_BOARD_LIMIT and is_risk_warning_name(name):
            legacy[asset_id] = LEGACY_MAIN_BOARD_ST_LIMIT
    return legacy, current


def numpy_price_limit_matrix(
    trading_dates: Sequence[date],
    symbols: Sequence[str],
    names: Sequence[str],
) -> np.ndarray:
    """Build a float32 time-by-asset matrix only for strategies that request it."""
    result = np.empty((len(trading_dates), len(symbols)), dtype=np.float32)
    return write_numpy_price_limit_matrix(result, trading_dates, symbols, names)


def write_numpy_price_limit_matrix(
    target: np.ndarray,
    trading_dates: Sequence[date],
    symbols: Sequence[str],
    names: Sequence[str],
    *,
    valid: np.ndarray | None = None,
) -> np.ndarray:
    """Write date-aware limits directly into an existing matrix or memmap."""
    expected_shape = (len(trading_dates), len(symbols))
    if target.shape != expected_shape:
        raise ValueError("price-limit output shape mismatch")
    if valid is not None and valid.shape != expected_shape:
        raise ValueError("price-limit validity mask shape mismatch")

    legacy, current = numpy_limit_pct_vectors(symbols, names)
    target[:] = current.astype(np.float32, copy=False)
    legacy_rows = np.fromiter(
        (value < MAIN_BOARD_ST_LIMIT_CHANGE_DATE for value in trading_dates),
        dtype=bool,
        count=len(trading_dates),
    )
    if legacy_rows.any():
        target[legacy_rows] = legacy.astype(np.float32, copy=False)
    if valid is not None:
        target[~valid] = np.nan
    return target


def numpy_limit_price(
    previous: np.ndarray,
    limit_pct: np.ndarray,
    *,
    up: bool,
) -> np.ndarray:
    """NumPy counterpart of :func:`polars_limit_price`."""
    sign = 1 if up else -1
    numerator = np.rint((1.0 + sign * limit_pct) * 100.0).astype(np.int64)
    result = np.full(previous.shape, np.nan, dtype=np.float64)
    finite = np.isfinite(previous)
    cents = np.floor(previous[finite] * 100.0 + 0.5).astype(np.int64)
    result[finite] = (
        ((cents * numerator[finite] + 50) // 100).astype(np.float64) / 100.0
    )
    return result
