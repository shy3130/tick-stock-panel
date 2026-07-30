from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PRESENTATION = ROOT / "frontend/src/components/dow-monitor/monitorListPresentation.ts"
LIST = ROOT / "frontend/src/components/dow-monitor/DowMonitorList.tsx"
HELP = ROOT / "frontend/src/pages/DowMonitorHelp.tsx"
PRESENTATION_TEST = ROOT / "frontend/src/components/dow-monitor/monitorListPresentation.test.ts"
LIST_TEST = ROOT / "frontend/src/components/dow-monitor/DowMonitorList.test.tsx"
HELP_TEST = ROOT / "frontend/src/pages/DowMonitorHelp.test.tsx"


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_monitor_p0_semantics_are_named_and_behaviorally_covered() -> None:
    presentation = text(PRESENTATION)
    monitor_list = text(LIST)
    help_page = text(HELP)
    presentation_test = text(PRESENTATION_TEST)
    list_test = text(LIST_TEST)
    help_test = text(HELP_TEST)

    assert "vwap_price" in presentation
    assert "vwap_distance_pct" in presentation
    assert "资金流入" in monitor_list
    assert "主买 " not in monitor_list
    assert "成本位置" not in monitor_list
    assert "周期 {row.breakoutRisk.confirmedTimeframes}" in monitor_list
    assert "confirmationTimeframes.map" in monitor_list

    assert "trendPosition.vwap" in presentation_test
    assert "volumeFunds.capitalInflow" in presentation_test
    assert "15m✓" in list_test and "30m✓" in list_test
    assert "不是用户持仓成本" in help_page
    assert "不是逐笔主动买入占比" in help_page
    assert "资金流入占比" in help_test


def test_monitor_p0_position_risk_and_freshness_have_formula_boundaries() -> None:
    presentation = text(PRESENTATION)
    monitor_list = text(LIST)
    presentation_test = text(PRESENTATION_TEST)
    list_test = text(LIST_TEST)

    assert "(quote.lastDone - low) / (high - low) * 100" in presentation
    assert "(high - low) / atrAbsolute" in presentation
    assert "ageSeconds > 90" in presentation
    assert "Math.max(0, Math.floor((nowMs - sourceMs) / 1000))" in presentation
    assert "realtime?.depth?.timestamp" in presentation
    assert "realtime?.candlestick?.timestamp" in presentation
    assert "item.minute_decision?.source_timestamp" in presentation
    assert "data-testid={`freshness-${item.symbol}`}" in monitor_list

    assert "dayRangeAtrRatio" in presentation_test
    assert "intradayPositionPct" in presentation_test
    assert "freshness.quote" in presentation_test
    assert "数据时效，行情0s，盘口5s，1m K线30s，分析30s" in list_test
