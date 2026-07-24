import polars as pl

from app.services import stock_analyzer


def test_capital_metrics_expose_amount_activity_without_claiming_net_flow() -> None:
    df = pl.DataFrame(
        {
            "date": pl.date_range(pl.date(2026, 4, 1), pl.date(2026, 6, 29), interval="1d", eager=True),
            "close": [10.0 + i * 0.01 for i in range(90)],
            "amount": [float(i + 1) * 1_000_000 for i in range(90)],
            "volume": [float(i + 1) * 10_000 for i in range(90)],
        }
    )

    metrics = stock_analyzer._build_capital_metrics(df)

    assert metrics["amount"] == 90_000_000
    assert metrics["amount_ma5"] == 88_000_000
    assert metrics["amount_ma20"] == 80_500_000
    assert metrics["amount_ratio_5d"] == 1.0227
    assert metrics["amount_ratio_20d"] == 1.118
    assert metrics["amount_percentile_60d"] == 1.0

    prompt = stock_analyzer._build_system_prompt("hk")
    assert "成交额仅表示交易活跃度" in prompt
    assert "不得表述为资金净流入、主力流入或主力流出" in prompt


def test_hk_prompt_uses_hk_rules_and_hkd_amount_context() -> None:
    prompt = stock_analyzer._build_system_prompt("hk")

    assert "港股" in prompt
    assert "港元" in prompt
    assert "资金状态" in prompt
    assert "涨停/连板/炸板" not in prompt


def test_market_is_inferred_from_symbol_when_request_omits_it() -> None:
    assert stock_analyzer._resolve_market("700.HK", None) == "hk"
    assert stock_analyzer._resolve_market("AAPL.US", None) == "us"
    assert stock_analyzer._resolve_market("600519.SH", None) == "cn"


def test_capital_metrics_are_safe_when_amount_is_missing() -> None:
    metrics = stock_analyzer._build_capital_metrics(pl.DataFrame({"close": [1.0]}))

    assert metrics == {}
