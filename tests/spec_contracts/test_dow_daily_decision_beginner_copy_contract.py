from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SUMMARY = ROOT / "frontend/src/components/dow-monitor/DailyDecisionSummary.tsx"
TYPES = ROOT / "frontend/src/components/dow-monitor/types.ts"


def test_daily_summary_exposes_same_minute_cost_fields() -> None:
    source = TYPES.read_text(encoding="utf-8")

    assert "current_price?: number | null" in source
    assert "vwap_price?: number | null" in source
    assert "vwap_distance_pct?: number | null" in source


def test_daily_summary_uses_beginner_copy_and_collapsible_details() -> None:
    source = SUMMARY.read_text(encoding="utf-8")

    for text in (
        "当前判断",
        "建议动作",
        "今日平均成交成本",
        "当前价格",
        "当前价相对成本",
        "证据一致度",
        "不是上涨或下跌概率",
        "查看详细说明",
    ):
        assert text in source
    assert "<details" in source
    assert "VWAP" not in source
