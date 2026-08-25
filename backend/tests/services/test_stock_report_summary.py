"""F13 程序化结构化摘要：schema 拒绝方向性字段；组装纯程序、无第二次 LLM。"""
import json
from datetime import datetime
from types import SimpleNamespace

import polars as pl
import pytest
from pydantic import ValidationError

import app.services.stock_analyzer as sa
from app.services.stock_report_summary import StockReportSummary, build_report_summary

# ================================================================
# Schema
# ================================================================


def test_schema_accepts_exact_three_fields():
    s = StockReportSummary(trend="区间震荡", key_levels=["压力支撑·POC=10.00"], data_gaps=["样本不足"])
    assert s.model_dump() == {
        "trend": "区间震荡",
        "key_levels": ["压力支撑·POC=10.00"],
        "data_gaps": ["样本不足"],
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"action": "buy"},
        {"direction": "看涨"},
        {"buy": True},
        {"sell": False},
        {"target_price": 100.0},
        {"target": "12 元"},
        {"position": "半仓"},
    ],
)
def test_schema_rejects_directional_and_extra_keys(extra):
    with pytest.raises(ValidationError):
        StockReportSummary(trend="t", key_levels=[], data_gaps=[], **extra)


def test_schema_rejects_wrong_types():
    with pytest.raises(ValidationError):
        StockReportSummary(trend="t", key_levels="不是列表", data_gaps=[])


# ================================================================
# 程序组装
# ================================================================


def test_build_summary_includes_warnings_as_data_gaps():
    levels = {"sr": [{"value": 11.0, "label": "POC"}, {"value": 9.0, "label": "HVN"}]}
    summary = build_report_summary(levels, 10.0, ["K线样本不足 30 根", "复权方式降级"])
    assert summary is not None
    assert summary.data_gaps == ["K线样本不足 30 根", "复权方式降级"]
    # 距现价近的关键位优先
    assert [k for k in summary.key_levels] == ["压力支撑·POC=11.00", "压力支撑·HVN=9.00"]
    assert "现价 10.00" in summary.trend
    assert "上方关键位 1 个" in summary.trend and "下方 1 个" in summary.trend


def test_build_summary_skips_invalid_points():
    levels = {
        "sr": [
            {"value": float("nan"), "label": "NaN位"},
            {"value": "bad", "label": "非数值"},
            {"value": True, "label": "布尔"},
        ],
    }
    summary = build_report_summary(levels, 10.0, [])
    assert summary is not None
    assert summary.key_levels == []
    # 脏点位使 summarize_levels 抛错时，trend 退化为纯现价描述
    assert summary.trend == "现价 10.00"


def test_build_summary_without_close_falls_back_to_text():
    levels = {"sr": [{"value": 11.0, "label": "POC"}]}
    summary = build_report_summary(levels, None, ["现价缺失"])
    assert summary is not None
    assert summary.trend == "无价位数据"
    assert summary.key_levels == ["压力支撑·POC=11.00"]
    assert summary.data_gaps == ["现价缺失"]


def test_build_summary_caps_key_levels():
    points = [{"value": 10.0 + i * 0.1, "label": f"L{i}"} for i in range(20)]
    summary = build_report_summary({"sr": points}, 10.0, [])
    assert summary is not None
    assert len(summary.key_levels) == 8


def test_build_summary_returns_none_on_assembly_failure():
    class _BadStr:
        def __str__(self):
            raise RuntimeError("boom")

    assert build_report_summary({"sr": []}, 10.0, [_BadStr()]) is None


def test_build_summary_tolerates_non_dict_levels():
    summary = build_report_summary(None, 10.0, [])
    assert summary is not None
    assert summary.key_levels == []


# ================================================================
# analyze_stock_stream 集成：usage 之后、done 之前 yield summary
# ================================================================


@pytest.mark.asyncio
async def test_analyze_stock_stream_yields_summary_between_usage_and_done(monkeypatch, tmp_path):
    df = pl.DataFrame({"date": ["2026-08-19", "2026-08-20"], "close": [9.8, 10.0]})
    monkeypatch.setattr(sa, "_load_kline", lambda repo, symbol: df)
    monkeypatch.setattr(sa, "_filter_valid_dated_rows", lambda d, market: d)
    monkeypatch.setattr(sa, "_analysis_data_as_of", lambda d, market: datetime(2026, 8, 20))
    monkeypatch.setattr(sa, "compute_levels", lambda d: {"sr": [{"value": 10.5, "label": "POC"}]})
    monkeypatch.setattr(sa, "_level_prices", lambda levels: [10.5])
    frame = SimpleNamespace(
        data_as_of=datetime(2026, 8, 20),
        source="canonical_enriched",
        adjustment="qfq",
        degraded=False,
    )
    monkeypatch.setattr(sa, "build_analysis_frame", lambda *a, **k: frame)
    preflight = SimpleNamespace(ok=True, warnings=["K线样本仅 2 根"], error=None)
    monkeypatch.setattr(sa, "preflight_analysis", lambda *a, **k: preflight)
    monkeypatch.setattr(sa, "_load_financials", lambda data_dir, symbol: {})
    monkeypatch.setattr(sa, "_detect_pattern_summary", lambda d: [])
    monkeypatch.setattr(sa, "_build_auxiliary_prompt", lambda *a, **k: "辅助上下文")
    monkeypatch.setattr("app.services.skill_context.load_skill_context_safe", lambda scenario, max_chars=12000: "")
    monkeypatch.setattr(
        "app.services.ai_budgets.resolve_budget",
        lambda *a, **k: SimpleNamespace(context_max_tokens=12000, temperature=0.2, max_tokens=1000, timeout=30),
    )
    monkeypatch.setattr(
        sa, "assemble_prompt", lambda *a, **k: ([{"role": "user", "content": "q"}], {"context_max_tokens": 12000})
    )

    async def fake_stream_ai_text(messages, **kw):
        yield "结构结论"

    monkeypatch.setattr("app.services.ai_provider.stream_ai_text", fake_stream_ai_text)

    events = [json.loads(line) async for line in sa.analyze_stock_stream(None, tmp_path, "600519.SH")]

    types = [e["type"] for e in events]
    assert types[-1] == "done"
    assert "usage" in types and "summary" in types
    assert types.index("usage") < types.index("summary") < types.index("done")
    summary_event = next(e for e in events if e["type"] == "summary")
    assert set(summary_event["summary"].keys()) == {"trend", "key_levels", "data_gaps"}
    assert summary_event["summary"]["data_gaps"] == ["K线样本仅 2 根"]
    assert summary_event["summary"]["key_levels"] == ["压力支撑·POC=10.50"]
    assert "现价 10.00" in summary_event["summary"]["trend"]
