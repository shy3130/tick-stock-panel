"""维表涨停价为「无涨跌停限制」哨兵值 (>= 10000) 时, 盘后全量与实时路径同口径不判涨跌停。

注册制新股上市前 5 个交易日无涨跌幅限制, 数据源在维表 limit_up 填上万的占位值。
实时路径 (_compute_limit_signals_today) 识别该哨兵后涨停/跌停/炸板/翘板一律为 False;
盘后全量 (compute_limit_signals) 只把哨兵排除出「权威价」, 随后回退按 10%/20%/30%
理论价判断, 于是新股当日涨超 10% 就被记为涨停、连板数 +1, 收盘后看板涨停家数、
连板梯队与涨停类策略都会把它算进去, 与盘中结论相反。

listing_date 窗口判定 (price_limits.is_no_limit_day) 补齐哨兵覆盖不到的部分:
维表 as_of 只命中一个行情日, 窗口内其余交易日与 as_of 漂移后的历史重算由
listing_date 判定兜底 (601091.SH 2026-09-18: 昨收 20.8 收 57.77 被误记涨停+1 连板)。
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.indicators import pipeline
from app.price_limits import (
    BJ_OPENING_DATE,
    GEM_REGISTRATION_DATE,
    MAIN_BOARD_REGISTRATION_DATE,
    is_no_limit_day,
    no_limit_window_days,
)

YESTERDAY = date(2026, 9, 16)
TODAY = date(2026, 9, 17)


def _rows(prev_close: float, *, open_: float, high: float, low: float, close: float) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["001234.SZ", "001234.SZ"],
        "date": [YESTERDAY, TODAY],
        "open": [prev_close, open_],
        "high": [prev_close, high],
        "low": [prev_close, low],
        "close": [prev_close, close],
        "raw_close": [prev_close, close],
        "raw_high": [prev_close, high],
        "raw_low": [prev_close, low],
        "volume": [1000.0, 1000.0],
    })


def _instruments(limit_up: float, limit_down: float, as_of: date = TODAY, symbol: str = "001234.SZ") -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [symbol],
        "name": ["新股"],
        "limit_up": [limit_up],
        "limit_down": [limit_down],
        "as_of": [as_of],
    })


def _realtime(rows: pl.DataFrame, instruments: pl.DataFrame) -> dict:
    today = rows.filter(pl.col("date") == TODAY).with_columns(
        pl.lit(rows["close"][0]).alias("_prev_close_raw"),
    )
    return pipeline._compute_limit_signals_today(today, instruments).row(0, named=True)


# 昨收 20.00, 主板理论涨停 22.00 / 跌停 18.00
CASES = {
    "up_close": dict(open_=21.0, high=23.5, low=20.5, close=23.4),        # 涨超 10% 收盘
    "up_touch_fade": dict(open_=21.0, high=22.5, low=20.5, close=21.5),   # 冲过理论涨停价后回落
    "down_close": dict(open_=19.0, high=19.5, low=17.5, close=17.6),      # 跌超 10% 收盘
    "down_touch_up": dict(open_=18.2, high=19.0, low=17.8, close=18.8),   # 跌破理论跌停价后收阳
}
SIGNALS = (
    "signal_limit_up", "signal_broken_limit_up",
    "signal_limit_down", "signal_limit_down_recovery",
)


@pytest.mark.parametrize("case", list(CASES))
def test_full_path_no_price_limit_sentinel_matches_realtime(case):
    rows = _rows(20.0, **CASES[case])
    instruments = _instruments(limit_up=100000.0, limit_down=0.0)

    full = pipeline.compute_limit_signals(rows, instruments).row(-1, named=True)
    realtime = _realtime(rows, instruments)

    for signal in SIGNALS:
        assert realtime[signal] is False, signal
        assert full[signal] is False, signal
    assert full["consecutive_limit_ups"] == 0
    assert full["consecutive_limit_downs"] == 0


def test_down_signals_alone_also_respect_sentinel():
    rows = _rows(20.0, **CASES["down_close"])
    instruments = _instruments(limit_up=100000.0, limit_down=0.0)

    full = pipeline.compute_limit_signals(
        rows, instruments, needed={"signal_limit_down", "consecutive_limit_downs"},
    ).row(-1, named=True)

    assert full["signal_limit_down"] is False
    assert full["consecutive_limit_downs"] == 0


def test_sentinel_only_applies_to_matching_instrument_date():
    """维表日期不是该行情日 (历史行) 时无法得知是否无限制, 保持理论价口径。"""
    rows = _rows(20.0, **CASES["up_close"])
    stale = _instruments(limit_up=100000.0, limit_down=0.0, as_of=YESTERDAY)

    full = pipeline.compute_limit_signals(rows, stale).row(-1, named=True)

    assert full["signal_limit_up"] is True
    assert full["consecutive_limit_ups"] == 1


def test_regular_stock_limit_up_unchanged():
    rows = _rows(20.0, open_=21.0, high=22.0, low=20.5, close=22.0)
    instruments = _instruments(limit_up=22.0, limit_down=18.0)

    full = pipeline.compute_limit_signals(rows, instruments).row(-1, named=True)

    assert full["signal_limit_up"] is True
    assert full["consecutive_limit_ups"] == 1


# ================================================================
# listing_date 无涨跌幅窗口判定 (哨兵覆盖不到的历史行兜底)
# ================================================================

def test_window_days_by_board_and_listing_date():
    listing = date(2026, 9, 17)
    # 沪主板 / 创业板 / 科创板 / 北交所: 注册制后上市 → 窗口 5 / 5 / 5 / 1
    assert no_limit_window_days("601091.SH", listing) == 5
    assert no_limit_window_days("301234.SZ", listing) == 5
    assert no_limit_window_days("688001.SH", listing) == 5
    assert no_limit_window_days("920001.BJ", listing) == 1
    # 注册制改革前上市的老股 → 不适用
    assert no_limit_window_days("600001.SH", MAIN_BOARD_REGISTRATION_DATE) == 5
    assert no_limit_window_days("600001.SH", date(2023, 2, 16)) == 0
    assert no_limit_window_days("300001.SZ", GEM_REGISTRATION_DATE) == 5
    assert no_limit_window_days("300001.SZ", date(2020, 8, 23)) == 0
    assert no_limit_window_days("920001.BJ", BJ_OPENING_DATE) == 1
    assert no_limit_window_days("600001.SH", None) == 0


def test_is_no_limit_day_with_day_rank_exact():
    listing = date(2026, 9, 10)
    # 序号精确门控: 前 5 个交易日内 True, 第 6 日起 False
    assert is_no_limit_day("601091.SH", listing, date(2026, 9, 10), day_rank=1) is True
    assert is_no_limit_day("601091.SH", listing, date(2026, 9, 16), day_rank=5) is True
    assert is_no_limit_day("601091.SH", listing, date(2026, 9, 17), day_rank=6) is False
    # 北交所仅首日
    assert is_no_limit_day("920001.BJ", listing, listing, day_rank=1) is True
    assert is_no_limit_day("920001.BJ", listing, date(2026, 9, 11), day_rank=2) is False


def test_full_path_listing_date_window_marks_no_limit_on_stale_as_of():
    """C沈鼓场景: 维表 as_of (09-20) 与行情日 (09-17) 不一致, 哨兵失效;
    listing_date 窗口兜底, 涨超 10% 不再被误记涨停。"""
    rows = _rows(20.0, **CASES["up_close"])
    instruments = _instruments(limit_up=0.0, limit_down=0.0, as_of=date(2026, 9, 20))
    instruments = instruments.with_columns(
        pl.lit(TODAY).cast(pl.Date).alias("listing_date")
    )

    full = pipeline.compute_limit_signals(rows, instruments).row(-1, named=True)

    for signal in SIGNALS:
        assert full[signal] is False, signal
    assert full["consecutive_limit_ups"] == 0
    assert full["consecutive_limit_downs"] == 0


def test_sixth_trading_day_after_window_resumes_limit_signals():
    """窗口第 6 个交易日起恢复正常涨跌停判定 (601091: 上市 09-10, 09-17 是第 6 日)。"""
    dates = [date(2026, 9, 10), date(2026, 9, 11), date(2026, 9, 14),
             date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 17)]
    closes = [20.0, 20.0, 20.0, 20.0, 20.0, 22.0]  # 第 6 日收盘 = 理论涨停 22.00
    rows = pl.DataFrame({
        "symbol": ["601091.SH"] * 6,
        "date": dates,
        "open": closes, "high": closes, "low": [19.8] * 5 + [21.0], "close": closes,
        "raw_close": closes, "raw_high": closes, "raw_low": [19.8] * 5 + [21.0],
        "volume": [1000.0] * 6,
    })
    instruments = _instruments(limit_up=0.0, limit_down=0.0, as_of=date(2026, 9, 20), symbol="601091.SH")
    instruments = instruments.with_columns(
        pl.lit(date(2026, 9, 10)).cast(pl.Date).alias("listing_date")
    )

    full = pipeline.compute_limit_signals(rows, instruments).row(-1, named=True)

    assert full["signal_limit_up"] is True
    assert full["consecutive_limit_ups"] == 1


def test_pre_registration_listing_keeps_theoretical_limit():
    """注册制改革前上市的老股不受窗口判定影响, 理论价照判。"""
    rows = _rows(20.0, **CASES["up_close"])
    instruments = _instruments(limit_up=0.0, limit_down=0.0, as_of=date(2026, 9, 20))
    instruments = instruments.with_columns(
        pl.lit(date(2019, 6, 1)).cast(pl.Date).alias("listing_date")
    )

    full = pipeline.compute_limit_signals(rows, instruments).row(-1, named=True)

    assert full["signal_limit_up"] is True
    assert full["consecutive_limit_ups"] == 1


def test_partial_local_history_old_stock_not_marked_no_limit():
    """listing 近但本地日K首行晚于窗口边界 (历史被裁剪) 时序号不可信, 不标记。"""
    # 上市 2026-09-10, 本地历史从 09-16 开始 (首行在窗口外) → 回退理论价
    rows = _rows(20.0, **CASES["up_close"])  # 行情日 09-16/09-17
    instruments = _instruments(limit_up=0.0, limit_down=0.0, as_of=date(2026, 9, 20))
    instruments = instruments.with_columns(
        pl.lit(date(2026, 9, 1)).cast(pl.Date).alias("listing_date")
    )

    full = pipeline.compute_limit_signals(rows, instruments).row(-1, named=True)

    assert full["signal_limit_up"] is True


def test_realtime_path_listing_date_window_without_sentinel():
    """实时路径: 维表无哨兵 (limit_up=0) 但 listing_date 命中窗口 → 不判涨停。"""
    rows = _rows(20.0, **CASES["up_close"])
    instruments = _instruments(limit_up=0.0, limit_down=0.0).with_columns(
        pl.lit(TODAY).cast(pl.Date).alias("listing_date")
    )

    realtime = _realtime(rows, instruments)

    for signal in SIGNALS:
        assert realtime[signal] is False, signal
