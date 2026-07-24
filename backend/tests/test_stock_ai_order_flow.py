import json

from app.services import stock_analyzer


def _snapshot() -> dict:
    return {
        "available": True,
        "snapshotMinute": "2026-07-21 14:59:00.000",
        "largeIn": 120.0,
        "largeOut": 70.0,
        "largeNet": 50.0,
        "mediumIn": 80.0,
        "mediumOut": 100.0,
        "mediumNet": -20.0,
        "smallIn": 60.0,
        "smallOut": 90.0,
        "smallNet": -30.0,
        "totalIn": 260.0,
        "totalOut": 260.0,
        "totalNet": 0.0,
        "largeNetRatio": 0.1923,
        "signal": "minute_inflow",
        "signalLabel": "分时流入",
    }


def _series() -> dict:
    return {
        "summary": {
            "label": "资金修复",
            "priceChange": 1.2,
            "totalNetChange": 33.0,
            "largeNetChange": 12.0,
            "reason": "价格与资金同步改善",
        },
        "interpretation": {
            "recentChange": {
                "timeRange": "14:45-15:00",
                "judgement": "价格和资金同步改善",
                "priceChange": 0.8,
                "totalNetChange": 15.0,
                "largeNetChange": 5.0,
            },
            "operationHint": "立即追涨",
        },
    }


def test_order_flow_context_keeps_directional_fields_and_drops_operation_advice() -> None:
    context = stock_analyzer._build_order_flow_context(_snapshot(), _series())

    assert context["as_of"] == "2026-07-21 14:59:00.000"
    assert context["amount_unit"] == "万（对应市场币种）"
    assert context["large"] == {"in": 120.0, "out": 70.0, "net": 50.0}
    assert context["medium"] == {"in": 80.0, "out": 100.0, "net": -20.0}
    assert context["small"] == {"in": 60.0, "out": 90.0, "net": -30.0}
    assert context["total_net"] == 0.0
    assert context["large_net_ratio"] == 0.1923
    assert context["recent_change"]["judgement"] == "价格和资金同步改善"
    assert "operationHint" not in json.dumps(context, ensure_ascii=False)
    assert "立即追涨" not in json.dumps(context, ensure_ascii=False)


def test_order_flow_prompt_states_amount_bucket_identity_boundary() -> None:
    context = stock_analyzer._build_order_flow_context(_snapshot(), _series())
    prompt = stock_analyzer._build_user_prompt(
        [], {}, {}, None, "600519.SH", "", "cn", {}, context
    )
    system = stock_analyzer._build_system_prompt("cn")

    assert '"large": {"in": 120.0, "out": 70.0, "net": 50.0}' in prompt
    assert "大中小单" in prompt
    assert "成交金额分档" in system
    assert "不得直接断言为机构" in system


def test_unavailable_order_flow_safely_degrades() -> None:
    assert stock_analyzer._build_order_flow_context({"available": False}, {}) == {}
