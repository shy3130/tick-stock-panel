"""复盘分区聚合口径测试。

只测纯函数(逐日聚合 / 晋级率派生 / 噪声题材过滤),用合成面板,不依赖真实 parquet。
锁的是**口径**:涨停判定、封板率分母、晋级率的跨日错位、百分数单位。
"""
from __future__ import annotations
from datetime import date


import polars as pl

from app.services.review_series import (
    NON_THEME_TAGS,
    _REVIEW_HISTORY_COLUMNS,
    _daily_agg,
    _load_review_history,
    _with_promotion,
)


def _panel(rows: list[dict]) -> pl.DataFrame:
    """构造 enriched 面板片段(change_pct 为小数,与 pipeline 一致)。"""
    return pl.DataFrame(rows).with_columns([
        pl.col("signal_limit_up").cast(pl.Boolean),
        pl.col("signal_limit_down").cast(pl.Boolean),
        pl.col("signal_broken_limit_up").cast(pl.Boolean),
    ])


def _row(symbol, date, chg, *, lu=False, ld=False, broken=False, boards=0, amount=100.0):
    return {
        "symbol": symbol, "date": date, "change_pct": chg, "amount": amount, "volume": 1000,
        "signal_limit_up": lu, "signal_limit_down": ld, "signal_broken_limit_up": broken,
        "consecutive_limit_ups": boards,
    }



def test_review_history_uses_projected_fast_path_and_preserves_streaks():
    class Repo:
        def __init__(self):
            self.columns = None

        def get_enriched_range(self, start, end, columns=None):
            self.columns = columns
            return pl.DataFrame(
                {
                    "symbol": ["600001.SH", "600001.SH"],
                    "date": [date(2026, 7, 2), date(2026, 7, 3)],
                    "open": [10.0, 10.5],
                    "high": [10.2, 11.0],
                    "low": [9.9, 10.4],
                    "close": [10.0, 10.5],
                    "volume": [1000, 1200],
                    "amount": [10_000.0, 12_600.0],
                    "raw_close": [10.0, 10.5],
                    "raw_high": [10.2, 11.0],
                    "raw_low": [9.9, 10.4],
                    "turnover_rate": [1.0, 1.2],
                    "consecutive_limit_ups": [0, 0],
                    "consecutive_limit_downs": [0, 0],
                    "prev_close": [None, 10.0],
                    "change_pct": [None, 0.05],
                }
            )

        @staticmethod
        def get_instruments():
            return pl.DataFrame(
                {
                    "symbol": ["600001.SH"],
                    "name": ["测试股份"],
                }
            )

    repo = Repo()
    df = _load_review_history(
        repo,
        date(2026, 7, 2),
        date(2026, 7, 3),
    )

    assert repo.columns == _REVIEW_HISTORY_COLUMNS
    # 展示契约: instruments JOIN 后 name 必须可用(风险线索/题材龙头依赖)
    assert df["name"].to_list() == ["测试股份", "测试股份"]
    assert df["consecutive_limit_ups"].to_list() == [0, 0]
    assert df["signal_limit_up"].to_list() == [False, False]
    assert df["signal_broken_limit_up"].to_list() == [None, True]

def test_daily_agg_counts_and_seal_rate():
    df = _panel([
        # D1: 2 涨停(其中 1 只靠 consecutive 判定)、1 跌停、1 炸板、1 平
        _row("A", "D1", 0.10, lu=True, boards=1),
        _row("B", "D1", 0.10, boards=2),          # signal 缺失但连板>0 → 仍算涨停
        _row("C", "D1", -0.10, ld=True),
        _row("D", "D1", 0.04, broken=True),
        _row("E", "D1", 0.0),
    ])
    [day] = _daily_agg(df)

    assert day["limit_up_count"] == 2       # A + B
    assert day["limit_down_count"] == 1
    assert day["break_count"] == 1
    assert day["up_count"] == 3             # A B D
    assert day["down_count"] == 1
    assert day["flat_count"] == 1
    # 封板率 = 涨停 / (涨停 + 炸板) = 2/3
    assert day["seal_rate"] == 2 / 3 * 100
    assert day["max_board_count"] == 2
    assert day["board_1"] == 1 and day["board_2"] == 1
    assert day["connected_board_count"] == 1  # 仅 B(2 板)算连板


def test_daily_agg_emits_percent_units():
    """enriched 存小数,对外必须是百分数 —— 单位串错会让前端再乘一次 100。"""
    df = _panel([_row("A", "D1", 0.05), _row("B", "D1", -0.01)])
    [day] = _daily_agg(df)
    assert day["avg_change"] == 2.0  # mean(0.05, -0.01) = 0.02 → 2.0%


def test_daily_agg_amount_change_rate_vs_prev_day():
    df = _panel([
        _row("A", "D1", 0.01, amount=100.0),
        _row("A", "D2", 0.01, amount=150.0),
    ])
    d1, d2 = _daily_agg(df)
    assert d1["amount_change_rate"] is None   # 首日无前值
    assert d2["amount_change_rate"] == 50.0   # +50%


def test_promotion_rate_is_cross_day_offset():
    """晋级率 = 今日 N+1 板 / 昨日 N 板(错一天),不是同日相除。"""
    series = [
        {"trade_date": "D1", "limit_up_count": 10, "connected_board_count": 0,
         "board_1": 10, "board_2": 0, "board_3": 0, "board_4": 0, "board_5": 0, "high_board": 0},
        {"trade_date": "D2", "limit_up_count": 6, "connected_board_count": 4,
         "board_1": 2, "board_2": 4, "board_3": 0, "board_4": 0, "board_5": 0, "high_board": 0},
        {"trade_date": "D3", "limit_up_count": 3, "connected_board_count": 1,
         "board_1": 2, "board_2": 0, "board_3": 1, "board_4": 0, "board_5": 0, "high_board": 0},
    ]
    d1, d2, d3 = _with_promotion(series)

    assert d1["first_to_second_rate"] is None          # 无昨日 → 无样本
    assert d2["first_to_second_rate"] == 40.0          # D2 的 2 板(4) / D1 的首板(10)
    assert d2["promotion_rate"] == 40.0                # D2 连板(4) / D1 涨停(10)
    assert d3["second_to_third_rate"] == 25.0          # D3 的 3 板(1) / D2 的 2 板(4)
    # 分母为 0 → None(表示"无样本"),而不是 0%,否则图上会画出一条假的 0 线
    assert d3["fourth_to_fifth_rate"] is None


def test_non_theme_tags_cover_the_known_noise():
    """两融/互联互通标的覆盖率极高却不含题材信息,必须排除;真题材不能误伤。"""
    assert {"融资融券", "沪股通", "深股通"} <= NON_THEME_TAGS
    assert "国企改革" not in NON_THEME_TAGS  # 覆盖 26% 但是真题材
    assert "芯片概念" not in NON_THEME_TAGS
