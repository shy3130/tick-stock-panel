"""成交可达性诊断 (fill_reachability) 契约测试。

覆盖:
- 价格带过滤边界与 headroom 计算 (含 band 内外/null close);
- 分钟空 df / provider 异常 / 缺列 → no_data, fail-soft 不中断;
- 确定性抽样 (同 seed 复现 + 与 default_rng 抽样镜像对账);
- trades 为空 / sample<1 → 结构化空结果;
- 汇总分位数小样本手算对账、worst 截断与排序、JSON-safe。
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta

import numpy as np
import polars as pl

from app.backtest.fill_reachability import diagnose_fill_reachability

_DAY_E = date(2024, 1, 2)
_DAY_X = date(2024, 1, 3)

_SUMMARY_KEYS = {
    "n_trades",
    "n_sampled",
    "sample_seed",
    "price_band_pct",
    "sides_checked",
    "n_no_data",
    "no_data_pct",
    "n_reachable",
    "reachable_pct",
    "headroom_p10",
    "headroom_p50",
    "worst",
    "note",
}


def _minutes(closes: list[float | None], amounts: list[float]) -> pl.DataFrame:
    """构造 normalized 风格分钟 df (close 可含 null, 模拟真实数据缺口)。"""
    base = datetime(2024, 1, 2, 9, 31)
    return pl.DataFrame(
        {
            "symbol": ["TEST"] * len(closes),
            "datetime": [base + timedelta(minutes=i) for i in range(len(closes))],
            "close": closes,
            "volume": [1000.0] * len(closes),
            "amount": amounts,
        }
    )


class _Recorder:
    """可注入的假分钟源: 记录调用; 未映射的 (symbol, day) 模拟 provider 异常。"""

    def __init__(self, data: dict[tuple[str, date], pl.DataFrame]):
        self.data = data
        self.calls: list[tuple[str, date]] = []

    def __call__(self, symbol: str, day: date) -> pl.DataFrame:
        self.calls.append((symbol, day))
        df = self.data.get((symbol, day))
        if df is None:
            raise RuntimeError(f"no minute data for {symbol} {day}")
        return df


def _trade(symbol: str = "TEST", **over) -> dict:
    """持久化 Run 风格的成交 dict (成交口径日期为 ISO 字符串)。"""
    trade = {
        "symbol": symbol,
        "entry_date": _DAY_E.isoformat(),
        "entry_price": 10.0,
        "shares": 100.0,
        "exit_date": _DAY_X.isoformat(),
        "exit_price": 10.5,
        "entry_fill": "close_t",
        "exit_fill": "close_t",
    }
    trade.update(over)
    return trade


def test_band_filter_and_headroom_math():
    """band 内/外/null close 过滤 + 双侧 headroom 与 worst 列表内容。"""
    data = {
        # entry: 10.0 ±0.5% → [9.95, 10.05]; 9.96 在带内, 10.06/9.94 带外, null 不计
        ("TEST", _DAY_E): _minutes(
            [10.0, 9.96, 10.06, 9.94, None],
            [600.0, 400.0, 999999.0, 888888.0, 777777.0],
        ),
        # exit: 10.5 ±0.5% → [10.4475, 10.5525]; 10.5/10.46 带内, 10.56 带外
        ("TEST", _DAY_X): _minutes([10.5, 10.46, 10.56], [300.0, 300.0, 50000.0]),
    }
    result = diagnose_fill_reachability([_trade()], _Recorder(data))

    assert set(result) == _SUMMARY_KEYS
    assert result["n_trades"] == 1
    assert result["n_sampled"] == 1
    assert result["sides_checked"] == 2
    assert result["n_no_data"] == 0
    assert result["no_data_pct"] == 0.0
    # 样本最差 = exit 600/1050 = 4/7 < 1 → 不可达
    assert result["n_reachable"] == 0
    assert result["reachable_pct"] == 0.0
    assert math.isclose(result["headroom_p10"], 600.0 / 1050.0, abs_tol=1e-12)
    assert math.isclose(result["headroom_p50"], 600.0 / 1050.0, abs_tol=1e-12)

    worst = result["worst"]
    assert len(worst) == 2
    # 按 headroom 升序: exit 在前
    assert worst[0]["side"] == "exit"
    assert math.isclose(worst[0]["headroom"], 600.0 / 1050.0, abs_tol=1e-12)
    assert worst[0]["band_notional"] == 600.0
    assert worst[0]["trade_notional"] == 1050.0
    assert worst[0]["date"] == _DAY_X.isoformat()
    assert worst[0]["symbol"] == "TEST"
    assert worst[1]["side"] == "entry"
    assert worst[1]["band_notional"] == 1000.0
    assert worst[1]["trade_notional"] == 1000.0
    assert worst[1]["headroom"] == 1.0
    assert worst[1]["date"] == _DAY_E.isoformat()
    assert set(worst[0]) == {
        "symbol",
        "date",
        "side",
        "headroom",
        "band_notional",
        "trade_notional",
    }


def test_no_data_paths_empty_df_exception_and_missing_column():
    """空 df / provider 异常 / 缺 amount 列 → no_data; 两侧都 no_data 只计一次。"""
    both_missing = _Recorder(
        {
            ("TEST", _DAY_E): pl.DataFrame(
                {"symbol": [], "datetime": [], "close": [], "amount": []}
            ),
            # exit 日未映射 → 模拟 provider 抛异常
        }
    )
    result = diagnose_fill_reachability([_trade()], both_missing)
    assert result["n_sampled"] == 1
    assert result["sides_checked"] == 0
    assert result["n_no_data"] == 1
    assert result["no_data_pct"] == 1.0
    assert result["n_reachable"] == 0
    assert result["headroom_p10"] is None
    assert result["headroom_p50"] is None
    assert result["worst"] == []

    # 缺 amount 列 (close 在) → 该侧 no_data, 另一侧正常 → 样本不进 n_no_data
    missing_col = _Recorder(
        {
            ("TEST", _DAY_E): pl.DataFrame(
                {
                    "symbol": ["TEST"],
                    "datetime": [datetime(2024, 1, 2, 9, 31)],
                    "close": [10.0],
                }
            ),
            ("TEST", _DAY_X): _minutes([10.5], [1050.0]),
        }
    )
    result2 = diagnose_fill_reachability([_trade()], missing_col)
    assert result2["sides_checked"] == 1
    assert result2["n_no_data"] == 0
    assert result2["n_reachable"] == 1
    assert [w["side"] for w in result2["worst"]] == ["exit"]


def test_sampling_deterministic_and_reproducible():
    """同 seed 两次运行结果一致, 且抽样索引与 default_rng 镜像对账、保持原顺序。"""
    n = 20
    data: dict[tuple[str, date], pl.DataFrame] = {}
    trades = []
    for i in range(n):
        symbol = f"T{i}"
        entry_day = _DAY_E + timedelta(days=i)
        exit_day = entry_day + timedelta(days=1)
        data[(symbol, entry_day)] = _minutes([10.0], [1000.0])
        data[(symbol, exit_day)] = _minutes([10.5], [1050.0])
        trades.append(
            _trade(
                symbol=symbol,
                entry_date=entry_day.isoformat(),
                exit_date=exit_day.isoformat(),
            )
        )

    run1 = diagnose_fill_reachability(trades, _Recorder(data), sample=5, seed=7)
    recorder = _Recorder(data)
    run2 = diagnose_fill_reachability(trades, recorder, sample=5, seed=7)
    assert run1 == run2

    # 镜像复算抽样索引 (同 seed 同调用序列), 验证抽中样本与调用次数
    mirror = np.random.default_rng(7)
    expected_idx = sorted(mirror.choice(n, size=5, replace=False).tolist())
    called_symbols = {sym for sym, _ in recorder.calls}
    assert called_symbols == {f"T{i}" for i in expected_idx}
    # 每笔 entry/exit 各一次 → 10 次调用
    assert len(recorder.calls) == 10
    # 保持原顺序: worst 按数值排序不受影响, 但样本数正确
    assert run1["n_sampled"] == 5
    assert run1["sides_checked"] == 10
    assert run1["n_no_data"] == 0


def test_sample_larger_than_trades_covers_all():
    """sample 上限超过 trades 数 → 全量覆盖。"""
    data = {("TEST", _DAY_E): _minutes([10.0], [2000.0])}
    trades = [
        _trade(symbol="TEST", entry_date=_DAY_E.isoformat(), exit_date=None, exit_price=None)
    ]
    result = diagnose_fill_reachability(trades, _Recorder(data))
    assert result["n_trades"] == 1
    assert result["n_sampled"] == 1
    # exit 侧字段缺失 → no_data; entry 侧 headroom=2 可达
    assert result["sides_checked"] == 1
    assert result["n_no_data"] == 0
    assert result["n_reachable"] == 1


def test_empty_trades_or_sample_below_one_returns_structured_empty():
    """trades 为空 / sample<1 → 结构化空结果, 且不触发分钟调用。"""
    empty = diagnose_fill_reachability([], _Recorder({}))
    assert set(empty) == _SUMMARY_KEYS
    assert empty["n_trades"] == 0
    assert empty["n_sampled"] == 0
    assert empty["sides_checked"] == 0
    assert empty["n_no_data"] == 0
    assert empty["no_data_pct"] == 0.0
    assert empty["n_reachable"] == 0
    assert empty["reachable_pct"] == 0.0
    assert empty["headroom_p10"] is None
    assert empty["headroom_p50"] is None
    assert empty["worst"] == []
    assert isinstance(empty["note"], str) and empty["note"]

    recorder = _Recorder({("TEST", _DAY_E): _minutes([10.0], [1000.0])})
    zero_sample = diagnose_fill_reachability([_trade()], recorder, sample=0)
    assert zero_sample["n_trades"] == 1
    assert zero_sample["n_sampled"] == 0
    assert zero_sample["worst"] == []
    assert recorder.calls == []


def test_summary_quantiles_and_reachable_hand_computed():
    """小样本手算对账: headrooms=[0.5,1,2,4,8] → p10=0.7, p50=2.0, reachable=4/5。"""
    data: dict[tuple[str, date], pl.DataFrame] = {}
    trades = []
    for i, h in enumerate([0.5, 1.0, 2.0, 4.0, 8.0]):
        symbol = f"H{i}"
        entry_day = _DAY_E + timedelta(days=i)
        exit_day = entry_day + timedelta(days=1)
        # shares=100 × price=10 → trade_notional=1000; band 单行 amount=1000*h → 双侧同 h
        data[(symbol, entry_day)] = _minutes([10.0], [1000.0 * h])
        data[(symbol, exit_day)] = _minutes([10.0], [1000.0 * h])
        trades.append(
            _trade(
                symbol=symbol,
                entry_date=entry_day.isoformat(),
                exit_date=exit_day.isoformat(),
                exit_price=10.0,
            )
        )

    result = diagnose_fill_reachability(trades, _Recorder(data), sample=50, seed=0)
    assert result["n_sampled"] == 5
    assert result["sides_checked"] == 10
    assert result["n_no_data"] == 0
    # 线性插值: p10 位置 0.4 → 0.5+0.4×(1-0.5)=0.7; p50 位置 2 → 恰为 2.0
    assert math.isclose(result["headroom_p10"], 0.7, abs_tol=1e-12)
    assert math.isclose(result["headroom_p50"], 2.0, abs_tol=1e-12)
    assert result["n_reachable"] == 4
    assert math.isclose(result["reachable_pct"], 0.8, abs_tol=1e-12)
    assert result["no_data_pct"] == 0.0


def test_worst_list_capped_at_five_sorted_ascending():
    """8 个有效侧 → worst 截断为 5 且按 headroom 升序。"""
    headrooms = [0.2, 0.4, 0.6, 0.8, 1.2, 1.6, 2.0, 3.0]
    data: dict[tuple[str, date], pl.DataFrame] = {}
    trades = []
    for i in range(4):
        symbol = f"W{i}"
        entry_day = _DAY_E + timedelta(days=i)
        exit_day = entry_day + timedelta(days=1)
        h_entry, h_exit = headrooms[2 * i], headrooms[2 * i + 1]
        data[(symbol, entry_day)] = _minutes([10.0], [1000.0 * h_entry])
        data[(symbol, exit_day)] = _minutes([10.0], [1000.0 * h_exit])
        trades.append(
            _trade(
                symbol=symbol,
                entry_date=entry_day.isoformat(),
                exit_date=exit_day.isoformat(),
                exit_price=10.0,
            )
        )

    result = diagnose_fill_reachability(trades, _Recorder(data))
    worst = result["worst"]
    assert len(worst) == 5
    values = [w["headroom"] for w in worst]
    assert values == sorted(values)
    assert values == [0.2, 0.4, 0.6, 0.8, 1.2]


def test_zero_trade_notional_or_empty_band_heads_to_zero():
    """shares<=0 或 band 内无成交额 → headroom=0.0 (status ok, 非 no_data)。"""
    data = {
        # shares=0 → trade_notional=0 → headroom 0
        ("Z1", _DAY_E): _minutes([10.0], [5000.0]),
        ("Z1", _DAY_X): _minutes([10.5], [5000.0]),
        # 全部 close 在带外 → band_notional=0 → headroom 0
        ("Z2", _DAY_E): _minutes([11.0, 9.0], [9999.0, 9999.0]),
        ("Z2", _DAY_X): _minutes([10.5], [3000.0]),
    }
    trades = [
        _trade(symbol="Z1", shares=0.0),
        _trade(symbol="Z2"),
    ]
    result = diagnose_fill_reachability(trades, _Recorder(data))
    assert result["sides_checked"] == 4
    assert result["n_no_data"] == 0
    assert result["n_reachable"] == 0
    assert result["reachable_pct"] == 0.0
    # worst 4 个侧: Z1 双侧 0 (shares=0), Z2 entry 0 (band 空), Z2 exit=3000/1050
    assert [w["headroom"] for w in result["worst"]] == [0.0, 0.0, 0.0, 3000.0 / 1050.0]


def test_result_is_json_serializable():
    """全部字段 JSON-safe: 严格模式 dumps 不抛错, 日期为 ISO 字符串。"""
    data = {("TEST", _DAY_E): _minutes([10.0, None], [1000.0, 500.0])}
    result = diagnose_fill_reachability(
        [_trade(entry_date=_DAY_E, exit_date=None, exit_price=None)],
        _Recorder(data),
        sample=3,
        seed=11,
        price_band_pct=0.01,
    )
    text = json.dumps(result, allow_nan=False)
    assert "NaN" not in text and "Infinity" not in text
    assert result["sample_seed"] == 11
    assert result["price_band_pct"] == 0.01
    assert result["worst"][0]["date"] == _DAY_E.isoformat()
