"""断板反包 (broken_board_recovery) 多日形态测试 — F2。

合成 6 日面板验证文案语义:
    连板≥2 → 断板 1~2 天 (断板日非涨停) → 当日收阳 + 放量 + 涨幅达标且未涨停。
"""
from datetime import date
from pathlib import Path

import polars as pl

from app.strategy.builtin import broken_board_recovery as bbr

# 6 个连续交易日 (面板行序即交易日序)
DATES = [
    date(2026, 8, 11), date(2026, 8, 12), date(2026, 8, 13),
    date(2026, 8, 14), date(2026, 8, 17), date(2026, 8, 18),
]

# 日型模板: (signal_limit_up, open, close, vol_ratio_5d, change_pct)
LU = (True, 10.0, 11.0, 1.0, 0.10)        # 涨停板
PLAIN = (False, 10.0, 9.8, 0.9, -0.01)    # 普通阴跌日 (断板日)
YANG_VOL = (False, 10.0, 10.8, 2.0, 0.06)  # 放量收阳 (反包候选日)
LU_TODAY = (True, 10.0, 11.0, 2.0, 0.10)  # 当日又涨停 (反包日直接封板)
LOW_VOL_YANG = (False, 10.0, 10.5, 1.0, 0.05)  # 收阳但缩量
VOL_YIN = (False, 10.5, 10.0, 2.0, -0.02)  # 放量但收阴

# symbol → 6 日形态; 未列满的尾日用 PLAIN 补齐
PATTERNS = {
    "600001.SH": [LU, LU, PLAIN, YANG_VOL, PLAIN, PLAIN],      # 断板1天后反包 → 命中 d4
    "600002.SH": [LU, LU, PLAIN, PLAIN, YANG_VOL, PLAIN],      # 断板2天后反包 → 命中 d5
    "600003.SH": [LU, PLAIN, YANG_VOL, PLAIN, PLAIN, PLAIN],   # 从未连板≥2 → 不命中
    "600004.SH": [LU, LU, PLAIN, LU_TODAY, PLAIN, PLAIN],      # 反包日又涨停 → 不命中
    "600005.SH": [LU, LU, PLAIN, LOW_VOL_YANG, PLAIN, PLAIN],  # 收阳但量比不足 → 不命中
    "600006.SH": [LU, LU, PLAIN, VOL_YIN, PLAIN, PLAIN],       # 放量但收阴 → 不命中
}


def _panel() -> pl.DataFrame:
    rows = []
    for symbol, days in PATTERNS.items():
        for i, (lu, open_, close, vr, chg) in enumerate(days):
            rows.append({
                "symbol": symbol,
                "date": DATES[i],
                "signal_limit_up": lu,
                "open": open_,
                "close": close,
                "vol_ratio_5d": vr,
                "change_pct": chg,
            })
    return pl.DataFrame(rows)


def test_filter_history_keeps_only_true_broken_board_recovery():
    hits = bbr.filter_history(_panel(), {})

    got = {(r["symbol"], r["date"]) for r in hits.iter_rows(named=True)}
    assert got == {
        ("600001.SH", DATES[3]),  # 断板 1 天后放量收阳反包
        ("600002.SH", DATES[4]),  # 断板 2 天后放量收阳反包
    }
    # 反包日当日均未涨停
    assert not hits["signal_limit_up"].any()


def test_filter_respects_params_over_defaults():
    # 量比阈值抬到 2.5: 两个默认命中日 (vol_ratio=2.0) 全部落选
    hits = bbr.filter_history(_panel(), {"vol_ratio_min": 2.5})
    assert hits.is_empty()


def test_module_declares_history_backend():
    # LOOKBACK_DAYS 是自然日窗口: 形态最深回看 5 个交易日 (d-3 连板段 ≥2 根),
    # 需覆盖周末/长假缓冲 (对齐 sequence 路径 ceil(5*7/5)+4 = 11),
    # 否则周一/节后窗口只剩 2-4 个交易日会截断连板段漏命中。
    assert bbr.LOOKBACK_DAYS == 11
    # 买点不再叠加 signal_limit_up (反包日本身要求未涨停)
    assert "signal_limit_up" not in bbr.ENTRY_SIGNALS


def test_lookback_window_survives_long_holiday():
    """长假场景: 6 自然日窗口不够, 11 自然日能覆盖跨春节的形态回看。

    节前 02-05/02-06 两连板 → 节后 02-18 断板 1 天 (非涨停) → 02-19 反包。
    as_of=02-19 时窗口须回看到 02-05 (自然日差 14 天): LOOKBACK=6 会把
    连板段首根截掉, streak 降到 1 → 漏命中。
    """
    rows = [
        {"symbol": "600003.SH", "date": date(2026, 2, 5), "open": 9.5, "close": 9.8,
         "vol_ratio_5d": 2.0, "change_pct": 0.05, "signal_limit_up": True},
        {"symbol": "600003.SH", "date": date(2026, 2, 6), "open": 9.8, "close": 10.0,
         "vol_ratio_5d": 2.0, "change_pct": 0.05, "signal_limit_up": True},
        {"symbol": "600003.SH", "date": date(2026, 2, 18), "open": 10.2, "close": 10.5,
         "vol_ratio_5d": 2.0, "change_pct": 0.05, "signal_limit_up": True},
        {"symbol": "600003.SH", "date": date(2026, 2, 19), "open": 10.4, "close": 10.4,
         "vol_ratio_5d": 2.0, "change_pct": 0.0, "signal_limit_up": False},
        {"symbol": "600003.SH", "date": date(2026, 2, 20), "open": 10.5, "close": 11.0,
         "vol_ratio_5d": 2.0, "change_pct": 0.05, "signal_limit_up": False},
    ]
    hits = bbr.filter_history(pl.DataFrame(rows), {})
    # 断板 1 天 (02-20): 截至 02-19 连板 3 根 ≥2 → 02-20 反包日命中。
    assert hits["date"].to_list() == [date(2026, 2, 20)]


def test_engine_runs_broken_board_recovery_through_history_path():
    """引擎链路: filter_history 判形态 → Stage-2 filter 复核 → 只剩 as_of 命中行。"""
    from app.strategy.engine import StrategyEngine

    panel = _panel()
    engine = StrategyEngine(
        enriched_loader=lambda _d: pl.DataFrame(),
        enriched_history_loader=lambda _d, _lb: panel,
        strategy_dirs=[Path(bbr.__file__).parent],
    )

    # as_of=第4日: 只有断板1天反包的 600001 命中
    r4 = engine.run(
        "broken_board_recovery", DATES[3],
        overrides={"basic_filter": {"enabled": False}},
    )
    assert [row["symbol"] for row in r4.rows] == ["600001.SH"]

    # as_of=第5日: 只有断板2天反包的 600002 命中
    r5 = engine.run(
        "broken_board_recovery", DATES[4],
        overrides={"basic_filter": {"enabled": False}},
    )
    assert [row["symbol"] for row in r5.rows] == ["600002.SH"]
