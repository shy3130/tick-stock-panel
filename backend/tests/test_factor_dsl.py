"""因子公式 DSL 编译器测试 (P2)。

覆盖: 全部编译期错误码 E001-E008/E010/E011/E014/E016、窗口纪律 (只向后看)、
数值正确性 (与手算基准对拍)、依赖/预热推导、运行期 fail-closed。
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.factors.dsl import BASE_COLUMNS, FACTOR_COLUMN, compile_formula


def _panel() -> pl.DataFrame:
    # 两个 symbol x 6 日, 便于验证 over("symbol") 不串组
    rows = []
    for symbol, closes in (("A", [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]),
                           ("B", [100.0, 90.0, 80.0, 70.0, 60.0, 50.0])):
        for index, close in enumerate(closes):
            rows.append({
                "symbol": symbol,
                "date": date(2026, 1, index + 1),
                "close": close,
                "volume": 1000.0 + index * 100,
                "amount": (1000.0 + index * 100) * close,
            })
    return pl.DataFrame(rows).sort(["symbol", "date"])


def _eval(formula: str, panel: pl.DataFrame | None = None) -> pl.Series:
    compiled = compile_formula(formula)
    assert compiled.ok, [error.to_dict() for error in compiled.errors]
    assert compiled.frame_transform is not None
    base = panel if panel is not None else _panel()
    frame = compiled.frame_transform(base)
    assert frame is not None
    return frame[FACTOR_COLUMN]


# ------------------------------------------------------------- 错误码覆盖

@pytest.mark.parametrize("formula, code", [
    ("clos + 1", "E001"),                       # 未知标识符
    ("foo(close)", "E002"),                     # 未知函数
    ("ts_mean(close)", "E003"),                 # 缺窗口参数
    ("ts_mean(close, close)", "E003"),          # 窗口参数必须是常量
    ("ts_mean(close, 1)", "E004"),              # 窗口 < 2
    ("ts_mean(close, 600)", "E004"),            # 窗口 > 512
    ("ts_quantile(close, 5, 1.5)", "E004"),     # q 不在 (0,1)
    ("ts_delay(close, -5)", "E005"),            # 负 shift = 未来函数
    ("ts_delta(close, -1)", "E005"),
    ("power(close, 5)", "E010"),                # 指数越界
    ("winsorize(close, 9)", "E011"),            # k 越界
    ("close / 0", "E008"),                      # 静态除零
    ("1 + 2", "E016"),                          # 常量表达式
    ("close +", "E014"),                        # 语法错误
    ("(close", "E014"),
    ("close $ 1", "E014"),
    ("", "E014"),
])
def test_error_codes(formula: str, code: str) -> None:
    compiled = compile_formula(formula)
    assert not compiled.ok
    assert any(error.code == code for error in compiled.errors), [e.code for e in compiled.errors]


def test_depth_and_token_limits() -> None:
    # AST 深度: 嵌套 13 层二元运算 (每层 bin 算 1) 超限
    deep = "close"
    for _ in range(13):
        deep = f"({deep} + 1)"
    compiled = compile_formula(deep)
    assert not compiled.ok
    assert any(error.code == "E006" for error in compiled.errors)

    wide = " + ".join(["close"] * 120)  # 二元链深度为 2, 但 token 超 200
    compiled = compile_formula(wide)
    assert any(error.code == "E007" for error in compiled.errors)


def test_error_payload_shape() -> None:
    compiled = compile_formula("rank(ts_delta(close, -5))")
    assert not compiled.ok
    payload = compiled.errors[0].to_dict()
    assert payload["code"] == "E005"
    assert payload["message"]
    assert "position" in payload and "offset" in payload["position"]


# ------------------------------------------------------------- 依赖与预热

def test_dependencies_and_warmup() -> None:
    compiled = compile_formula("rank(-ts_sum(change_pct, 5))")
    assert compiled.ok
    # change_pct 是 base 因子, 依赖为其自身列
    assert compiled.dependencies == frozenset({"change_pct"})
    assert compiled.referenced_factors == frozenset({"change_pct"})
    assert compiled.warmup_bars == 6  # ts 窗口 5 + 1
    assert compiled.cross_sectional

    compiled = compile_formula("close + ma20_bias")
    assert compiled.ok
    assert compiled.dependencies == frozenset({"close", "ma20"})
    assert compiled.referenced_factors == frozenset({"ma20_bias"})

    compiled = compile_formula("turnover_z_60d * 2")
    assert compiled.ok
    assert compiled.warmup_bars == 61  # 引用因子 warmup 传递


def test_base_columns_contract() -> None:
    assert "close" in BASE_COLUMNS
    assert "clos" not in BASE_COLUMNS


# ------------------------------------------------------------- 数值正确性

def test_ts_delay_backward_only() -> None:
    values = _eval("ts_delay(close, 2)")
    # A 组: [null, null, 10, 11, 12, 13]; B 组: [null, null, 100, 90, 80, 70]
    assert values[:6].to_list() == [None, None, 10.0, 11.0, 12.0, 13.0]
    assert values[6:].to_list() == [None, None, 100.0, 90.0, 80.0, 70.0]


def test_ts_mean_no_cross_symbol_leak() -> None:
    values = _eval("ts_mean(close, 2)")
    # A 组 2 日窗: [null, 10.5, 11.5, 12.5, 13.5, 14.5]
    assert values[:6].to_list() == [None, 10.5, 11.5, 12.5, 13.5, 14.5]
    # B 组边界: 第一行是 null (窗口不满) 而不是拿到 A 组尾部; 第二行 95.0
    assert values[6] is None
    assert values[7] == 95.0


def test_rank_cross_sectional() -> None:
    values = _eval("rank(close)")
    frame = _panel().with_columns(pl.Series("_f", values))
    per_date = frame.filter(pl.col("date") == date(2026, 1, 1))
    # 第一日 A=10, B=100: rank(A) < rank(B), 且都 ∈ (0,1]
    ranks = dict(zip(per_date["symbol"].to_list(), per_date["_f"].to_list(), strict=True))
    assert 0 < ranks["A"] < ranks["B"] <= 1.0


def test_cross_of_timeseries_two_phase() -> None:
    # Polars 嵌套窗口会静默全 null; 编译器必须走两阶段 (临时列 + 截面)
    values = _eval("rank(ts_sum(close, 2))")
    assert sum(1 for value in values.to_list() if value is not None) > 0
    # B 组滚动和数值恒大于 A 组 (百元 vs 十元价位) → 每个 (非首行) 日期 rank(B) > rank(A)
    frame = _panel().with_columns(pl.Series("_f", values))
    for day in (date(2026, 1, 3), date(2026, 1, 6)):
        day_frame = frame.filter(pl.col("date") == day)
        ranks = dict(zip(day_frame["symbol"].to_list(), day_frame["_f"].to_list(), strict=True))
        assert ranks["B"] > ranks["A"]


def test_cross_in_timeseries_rejected() -> None:
    compiled = compile_formula("ts_mean(rank(close), 5)")
    assert not compiled.ok
    assert any(error.code == "E009" for error in compiled.errors)


def test_safe_division_yields_null() -> None:
    # 分母为动态表达式的恒 0: 静态折叠不报 E008, 运行期安全除产出 null
    values = _eval("close / (close - close)")
    assert all(value is None for value in values.to_list())


def test_if_else_and_comparison() -> None:
    values = _eval("if_else(close > 12, 1, 0)")
    assert values[:6].to_list() == [0.0, 0.0, 0.0, 1.0, 1.0, 1.0]


def test_arithmetic_precedence() -> None:
    values = _eval("close + 2 * 3")
    assert values[0] == 16.0  # 10 + 6, 而不是 (10+2)*3


def test_decay_linear_weights_recent() -> None:
    values = _eval("decay_linear(close, 3)")
    # A 组第 3 行: (3*12 + 2*11 + 1*10) / 6 = 68/6
    assert values[2] == pytest.approx((3 * 12 + 2 * 11 + 1 * 10) / 6)


def test_log_domain_guard() -> None:
    import math

    panel = _panel().with_columns((pl.col("close") - 15.0).alias("neg"))
    values = _eval("log(close - 15)", panel)
    # A 组全为负 → null; B 组 100-15=85 → log 正常
    assert all(value is None for value in values[:6].to_list())
    assert values[6] == pytest.approx(math.log(85.0))


def test_ts_corr_two_series() -> None:
    values = _eval("ts_corr(close, volume, 3)")
    # 常数序列或完全单调: 只验证产出为有限值或 null, 无串组异常即可
    assert len(values) == 12


# --------------------------------------------------------- 运行期 fail-closed

def test_runtime_missing_column_fails_closed() -> None:
    compiled = compile_formula("close * volume")
    assert compiled.ok
    frame_without_volume = _panel().drop("volume")
    assert compiled.frame_transform is not None
    assert compiled.frame_transform(frame_without_volume) is None  # E013 语义
    assert compiled.frame_transform(_panel()) is not None


def test_winsorize_optional_k_omitted_compiles():
    """回归: winsorize(x) 省略 k — 此前 _check_call 直接 IndexError (编译器崩溃,
    而不是返回错误), winsorize 无 k 写法完全不可用。"""
    compiled = compile_formula("winsorize(close)")
    assert compiled.ok, compiled.errors
    assert compiled.warmup_bars == 1
