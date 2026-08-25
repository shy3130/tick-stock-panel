"""attribution_report 专用测试。

直接调用公开函数，覆盖合同五类情形：
- 有效多行业 Brinson 恒等式与口径
- 无行业映射 fail-closed
- 单行业不足
- 非有限/零资金交易过滤
- Fama-French unavailable

纯函数测试，无副作用、不依赖项目运行时。
"""

import math

import numpy as np

from app.backtest.attribution_report import (
    build_trade_industry_brinson_report,
    fama_french_unavailable_report,
)


def test_valid_multi_industry_brinson_identity_and_full_scope():
    trades = [
        {"symbol": "600000.SH", "entry_value": 1000.0, "pnl_pct": 0.05},
        {"symbol": "600519.SH", "entry_value": 2000.0, "pnl_pct": -0.01},
        {"symbol": "000001.SZ", "entry_value": 1500.0, "pnl_pct": 0.02},
    ]
    ind_map = {
        "600000.SH": ["银行", "股份制银行"],
        "600519.SH": ["白酒"],
        "000001.SZ": ["银行"],
    }
    rep = build_trade_industry_brinson_report(trades, ind_map)

    assert rep["status"] == "ok"
    assert "交易窗口、按当前行业分类、相对等权已执行交易样本的 Brinson-Fachler 归因（非官方指数归因）" in rep["scope"]
    assert "非官方指数归因" in rep["scope"]
    assert "调用时刻映射，非 point-in-time" in rep["classification_note"]

    assert rep["input_trades"] == 3
    assert rep["classified_trades"] == 3
    cov = rep["capital_coverage"]
    assert cov is not None and 0.0 < cov <= 1.0

    br = rep["brinson"]
    assert br is not None
    assert br["status"] == "ok"
    # Brinson-Fachler 恒等式
    assert br["total_effect"] is not None
    assert br["excess_return"] is not None
    assert np.isclose(br["total_effect"], br["excess_return"], atol=1e-12)

    gnames = [g["group"] for g in br["groups"]]
    assert set(gnames) == {"银行", "白酒"}

    ff = rep["fama_french"]
    assert ff["reason"] == "factor_return_series_unavailable"


def test_no_industry_mapping_fail_closed():
    trades = [
        {"symbol": "600000.SH", "entry_value": 1000.0, "pnl_pct": 0.05},
        {"symbol": "600519.SH", "entry_value": 2000.0, "pnl_pct": -0.02},
    ]
    rep = build_trade_industry_brinson_report(trades, None)
    assert rep["status"] == "insufficient_data"
    assert rep["reason"] == "no_industry_mapping"
    assert rep["classified_trades"] == 0
    assert rep["capital_coverage"] == 0.0 or rep["capital_coverage"] is None
    assert rep["brinson"] is None
    assert rep["fama_french"]["status"] == "unavailable"
    assert "no_industry_mapping" in str(rep.get("warnings", []))

    # 空 map 也触发
    rep2 = build_trade_industry_brinson_report(trades, {})
    assert rep2["reason"] == "no_industry_mapping"


def test_single_industry_insufficient():
    trades = [
        {"symbol": "A", "entry_value": 1000.0, "pnl_pct": 0.10},
        {"symbol": "B", "entry_value": 2000.0, "pnl_pct": 0.03},
        {"symbol": "C", "entry_value": 500.0, "pnl_pct": -0.01},
    ]
    ind_map = {"A": ["银行"], "B": ["银行"], "C": ["银行"]}
    rep = build_trade_industry_brinson_report(trades, ind_map)
    assert rep["status"] == "insufficient_data"
    assert rep["reason"] == "insufficient_industries"
    assert rep["classified_trades"] == 3
    assert rep["brinson"] is None
    assert any("less than 2 distinct" in w for w in rep["warnings"])


def test_nonfinite_zero_fund_trades_are_filtered():
    trades = [
        {"symbol": "GOOD1", "entry_value": 1000.0, "pnl_pct": 0.05},   # kept
        {"symbol": "ZERO", "entry_value": 0.0, "pnl_pct": 0.10},       # zero fund -> skip early
        {"symbol": "NAN", "entry_value": 800.0, "pnl_pct": float("nan")},  # non-finite
        {"symbol": "INF", "entry_value": 1200.0, "pnl_pct": float("inf")},  # non-finite
        {"symbol": "GOOD2", "entry_value": 3000.0, "pnl_pct": -0.02},  # kept
        {"symbol": "MISSING_EV", "pnl_pct": 0.01},                     # no entry_value
        {"symbol": "NOIND", "entry_value": 500.0, "pnl_pct": 0.04},    # >0 but no ind in map
    ]
    ind_map = {
        "GOOD1": ["银行"],
        "GOOD2": ["科技"],
        # ZERO, NAN, INF, NOIND either filtered or no ind
    }
    rep = build_trade_industry_brinson_report(trades, ind_map)

    assert rep["status"] == "ok"
    assert rep["input_trades"] == 7
    assert rep["classified_trades"] == 2  # only GOOD1 + GOOD2

    # positive entry caps: 1000(GOOD1) +800(NAN)+1200(INF)+3000(GOOD2)+500(NOIND) = 6500
    # classified cap: 1000+3000=4000
    assert np.isclose(rep["capital_coverage"], 4000.0 / 6500.0)

    assert any("non-finite pnl_pct" in w for w in rep["warnings"])
    assert any("without mappable industry" in w for w in rep["warnings"])

    br = rep["brinson"]
    assert br["status"] == "ok"
    gnames = {g["group"] for g in br["groups"]}
    assert gnames == {"银行", "科技"}


def test_fama_french_unavailable_report_is_explicit_and_safe():
    rep = fama_french_unavailable_report()
    assert rep["status"] == "unavailable"
    assert rep["reason"] == "factor_return_series_unavailable"
    assert "不得生成代理因子或假结果" in rep["detail"]
    assert rep["alpha"] is None
    assert rep["betas"] == {}
    assert rep["contributions"] == {}
    assert rep["r_squared"] is None
    assert rep["residual_volatility"] is None
    assert rep["observations"] == 0

    # 多次调用稳定
    rep2 = fama_french_unavailable_report()
    assert rep2 == rep
