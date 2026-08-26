"""short_pool 定向单元测试：固定筛选、零写入与服务端确认草案。"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.services import market_concentration as market_concentration_module
from app.services import screener_query as screener_query_module
from app.services.agent_research_tools import screen_stock_pool
from app.services.market_concentration import (
    MarketStateCoverage,
    MarketStateGates,
    MarketStateMetrics,
    MarketStatePercentiles,
    MarketStateSnapshot,
)
from app.services.short_pool import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MIN_LIMIT,
    SHORT_POOL_CONDITIONS,
    SHORT_POOL_ORDER_BY,
    ShortPoolLimit,
    build_query_request,
    build_t_research_hypothesis,
    run_short_pool,
)

AS_OF = "2026-08-25"

# fake 行覆盖全部 12 个条件字段 + symbol/name。
ROW_TEMPLATE = {
    "symbol": "600000.SH",
    "name": "浦发银行",
    "exclude_st": True,
    "listing_days": 5000,
    "amount": 8.5e9,
    "turnover_rate": 5.2,
    "above_ma20": True,
    "momentum_20d": 0.12,
    "distance_to_60d_high": -3.5,
    "atr_pct_14": 4.1,
    "vol_ratio_5d": 1.6,
    "change_pct": 0.025,
    "limit_up": False,
    "broken_limit_up": False,
}


class _FakeRepo:
    def __init__(self, data_dir: Path):
        self.store = SimpleNamespace(data_dir=data_dir)
        self.cache_generation = 7
        self.enriched_read_ceiling = date(2026, 8, 24)


@pytest.fixture()
def state(tmp_path: Path):
    return SimpleNamespace(repo=_FakeRepo(tmp_path))


def _market_snapshot(state_name: str = "dispersed") -> MarketStateSnapshot:
    available = state_name != "unavailable"
    return MarketStateSnapshot(
        available=available,
        state=state_name,
        target_date=AS_OF,
        signal_date="2026-08-24",
        metrics=MarketStateMetrics(
            return_std=0.02,
            return_q90_q10=0.05,
            turnover_hhi=0.2,
            positive_return_hhi=0.3,
            top3_contribution=0.4,
            top5_contribution=0.6,
        ),
        percentiles=MarketStatePercentiles(
            return_std=0.7,
            turnover_hhi=0.3,
            positive_return_hhi=0.2,
            top3_contribution=0.3,
        ),
        coverage=MarketStateCoverage(
            stock_count=4200,
            industry_count=31,
            symbol_coverage=0.98,
            amount_symbol_coverage=0.99,
            turnover_coverage=0.99,
            calibration_days=252,
        ),
        gates=MarketStateGates(
            automatic_research_allowed=state_name == "dispersed",
            reasons=[]
            if state_name == "dispersed"
            else [f"market_state_not_dispersed:{state_name}"],
        ),
        reason=None if available else "insufficient_calibration",
    )


@pytest.fixture(autouse=True)
def market_state_fake(monkeypatch):
    monkeypatch.setattr(
        market_concentration_module,
        "market_state_for_date",
        lambda _repo, _target: _market_snapshot(),
    )


def _rows(n: int) -> list[dict]:
    rows = []
    for i in range(n):
        row = dict(ROW_TEMPLATE)
        row["symbol"] = f"6000{i:02d}.SH"
        row["name"] = f"股{i}"
        row["momentum_20d"] = 0.25 - i * 0.01  # 严格降序 → 校验排序透传
        rows.append(row)
    return rows


class _FakeQueryService:
    """typed 筛选边界替身: 记录请求, 返回合法 rows/total/as_of/applied。"""

    calls: list = []

    def __init__(self, repo):  # noqa: ANN001 — 与真实 QueryService 同签名
        self.repo = repo

    def query(self, req):  # noqa: ANN001
        _FakeQueryService.calls.append(req)
        n = getattr(_FakeQueryService, "row_count", 10)
        return {
            "rows": _rows(n),
            "total": n,
            "as_of": AS_OF,
            "applied": [
                {"field": c["field"], "op": c["op"], "value": c["value"]}
                for c in SHORT_POOL_CONDITIONS
            ],
            "elapsed_ms": 1.0,
        }


@pytest.fixture()
def screener_fake(monkeypatch):
    _FakeQueryService.calls = []
    _FakeQueryService.row_count = 10
    monkeypatch.setattr(screener_query_module, "QueryService", _FakeQueryService)
    return _FakeQueryService


# ── 严格入参边界 ─────────────────────────────────────────────
@pytest.mark.parametrize("bad", [4, 13, 0, -1, 100])
def test_limit_out_of_range_rejected(bad):
    with pytest.raises(ValidationError):
        ShortPoolLimit(limit=bad)


@pytest.mark.parametrize("ok", [MIN_LIMIT, 8, MAX_LIMIT])
def test_limit_bounds_accepted(ok):
    assert ShortPoolLimit(limit=ok).limit == ok


def test_limit_default_and_extra_forbidden():
    assert ShortPoolLimit().limit == DEFAULT_LIMIT == 8
    with pytest.raises(ValidationError):
        ShortPoolLimit.model_validate({"limit": 8, "extra": 1})


def test_limit_rejects_non_int():
    with pytest.raises(ValidationError):
        ShortPoolLimit.model_validate({"limit": "8"})
    with pytest.raises(ValidationError):
        ShortPoolLimit.model_validate({"limit": 8.0})


# ── 条件与排序逐字锁定 ───────────────────────────────────────
def test_build_query_request_locks_conditions_and_order():
    req = build_query_request(8)
    assert [c.model_dump(exclude={"group"}) for c in req.conditions] == [
        dict(c) for c in SHORT_POOL_CONDITIONS
    ]
    assert req.order_by.model_dump() == SHORT_POOL_ORDER_BY
    assert req.limit == 8
    assert req.as_of is None


@pytest.mark.parametrize("bad", [4, 13, "8", 8.0])
def test_build_query_request_rejects_invalid_limit(bad):
    with pytest.raises(ValidationError):
        build_query_request(bad)


def test_preset_conditions_literal():
    assert len(SHORT_POOL_CONDITIONS) == 12
    assert [c["field"] for c in SHORT_POOL_CONDITIONS] == [
        "exclude_st",
        "listing_days",
        "amount",
        "turnover_rate",
        "above_ma20",
        "momentum_20d",
        "distance_to_60d_high",
        "atr_pct_14",
        "vol_ratio_5d",
        "change_pct",
        "limit_up",
        "broken_limit_up",
    ]
    assert SHORT_POOL_ORDER_BY == {"field": "momentum_20d", "direction": "desc"}
    by_field = {c["field"]: c for c in SHORT_POOL_CONDITIONS}
    assert by_field["exclude_st"] == {"field": "exclude_st", "op": "=", "value": True}
    assert by_field["listing_days"] == {"field": "listing_days", "op": ">=", "value": 120}
    assert by_field["amount"] == {"field": "amount", "op": ">=", "value": 300000000}
    assert by_field["turnover_rate"] == {
        "field": "turnover_rate",
        "op": "between",
        "value": [2, 18],
    }
    assert by_field["above_ma20"] == {"field": "above_ma20", "op": "=", "value": True}
    assert by_field["momentum_20d"] == {
        "field": "momentum_20d",
        "op": "between",
        "value": [0.03, 0.25],
    }
    assert by_field["distance_to_60d_high"] == {
        "field": "distance_to_60d_high",
        "op": "between",
        "value": [-15, 0],
    }
    assert by_field["atr_pct_14"] == {"field": "atr_pct_14", "op": "between", "value": [2, 9]}
    assert by_field["vol_ratio_5d"] == {"field": "vol_ratio_5d", "op": ">=", "value": 1}
    assert by_field["change_pct"] == {
        "field": "change_pct",
        "op": "between",
        "value": [-0.03, 0.08],
    }
    assert by_field["limit_up"] == {"field": "limit_up", "op": "=", "value": False}
    assert by_field["broken_limit_up"] == {"field": "broken_limit_up", "op": "=", "value": False}


# ── QueryService 调用 + 候选/证据 ────────────────────────────
def test_run_short_pool_calls_query_service_with_preset_request(state, screener_fake):
    resp = run_short_pool(state, limit=6)
    assert len(screener_fake.calls) == 1
    req = screener_fake.calls[0]
    assert [c.model_dump(exclude={"group"}) for c in req.conditions] == [
        dict(c) for c in SHORT_POOL_CONDITIONS
    ]
    assert req.order_by.model_dump() == SHORT_POOL_ORDER_BY
    assert req.limit == 6
    # 封套完整键
    assert set(resp) == {
        "status",
        "summary",
        "pool_id",
        "as_of",
        "count",
        "total",
        "preset",
        "candidates",
        "disclaimer",
        "selection_basis",
        "ai_role",
        "market_state",
        "t_research",
        "next_actions",
    }
    assert resp["status"] == "success"
    assert resp["as_of"] == AS_OF
    assert resp["count"] == 6 and resp["total"] == 10
    assert resp["preset"] == {
        "preset_id": "short_momentum_quality_v1",
        "version": 1,
        "name": "短线动量质量观察",
        "description": "以流动性、趋势位置、温和动量、波动与涨停风险约束形成的固定研究观察池",
    }


def test_candidates_ranked_with_full_evidence_from_rows(state, screener_fake):
    resp = run_short_pool(state, limit=5)
    cands = resp["candidates"]
    assert [c["rank"] for c in cands] == [1, 2, 3, 4, 5]
    # 排序透传: fake rows 的 momentum_20d 降序 → symbol 顺序一致
    assert [c["symbol"] for c in cands] == [r["symbol"] for r in _rows(10)][:5]
    for cand in cands:
        assert set(cand) == {"rank", "symbol", "name", "evidence"}
        assert len(cand["evidence"]) == 12
        for entry in cand["evidence"]:
            assert set(entry) == {
                "field",
                "label",
                "actual",
                "display",
                "op",
                "target",
                "criterion",
                "unit",
            }
        by_field = {e["field"]: e for e in cand["evidence"]}
        row = _rows(10)[cand["rank"] - 1]
        # actual 全部来自 QueryService 返回行
        assert by_field["momentum_20d"]["actual"] == row["momentum_20d"]
        assert by_field["amount"]["actual"] == row["amount"]
        assert by_field["exclude_st"]["actual"] is True
        # op/target 来自 applied conditions
        assert by_field["momentum_20d"]["op"] == "between"
        assert by_field["momentum_20d"]["target"] == [0.03, 0.25]
        assert by_field["listing_days"]["target"] == 120
        # 展示字符串确定性
        assert by_field["momentum_20d"]["display"] == f"{row['momentum_20d'] * 100:.2f}%"
        assert by_field["amount"]["display"].endswith("亿元")
        assert all(isinstance(entry["unit"], str) for entry in cand["evidence"])


def test_limit_caps_candidates_at_12(state, screener_fake):
    screener_fake.row_count = 20
    resp = run_short_pool(state, limit=MAX_LIMIT)
    assert resp["count"] == MAX_LIMIT == 12
    assert len(resp["candidates"]) == 12


def test_next_actions_only_advertise_supported_ui_actions(state, screener_fake):
    resp = run_short_pool(state, limit=6)
    assert resp["next_actions"] == [
        "view_stock_detail",
        "add_to_watchlist",
        "stage_strategy_backtest",
    ]
    assert "start_pool_backtest" not in resp["next_actions"]


def test_disclaimer_and_ai_role(state, screener_fake):
    resp = run_short_pool(state, limit=6)
    assert resp["disclaimer"] == "研究观察池，非投资建议"
    assert "不得生成、删除或重排候选" in resp["ai_role"]
    assert "不提供买卖方向、价格或仓位建议" in resp["ai_role"]
    assert resp["selection_basis"]["deterministic"] is True
    assert resp["selection_basis"]["order_by"] == SHORT_POOL_ORDER_BY


def test_disperse_market_state_produces_confirmation_only_draft(state, screener_fake):
    response = run_short_pool(state, limit=6)

    assert response["market_state"]["state"] == "dispersed"
    assert response["market_state"]["signal_date"] == "2026-08-24"
    assert response["t_research"] == {
        "protocol_id": "bollinger_volatility_t_research_v1",
        "bar_precision": "5m",
        "lookback_sessions": 120,
        "min_events": 30,
        "signal_lag": "T-1",
        "validation": "strict_walk_forward",
        "baseline": "all_eligible_days",
        "filtered": "market_state=dispersed",
        "round_trip_cost_bps": 20,
        "cost_sensitivity_bps": [10, 20, 30],
        "automatic_run": False,
        "status": "ready_for_confirmation",
    }


@pytest.mark.parametrize("state_name", ["concentrated", "transition", "unavailable"])
def test_non_disperse_market_state_blocks_research_draft(state, screener_fake, state_name):
    response = run_short_pool(
        state,
        limit=6,
        market_state_provider=lambda: _market_snapshot(state_name),
    )

    assert response["market_state"]["state"] == state_name
    assert response["t_research"]["status"] == "blocked_by_market_state"
    assert response["t_research"]["automatic_run"] is False


def test_market_snapshot_is_part_of_content_address(state, screener_fake):
    dispersed = run_short_pool(
        state,
        limit=6,
        market_state_provider=lambda: _market_snapshot("dispersed"),
    )
    transition = run_short_pool(
        state,
        limit=6,
        market_state_provider=lambda: _market_snapshot("transition"),
    )

    assert dispersed["pool_id"] != transition["pool_id"]


def test_invalid_market_snapshot_fails_closed(state, screener_fake):
    invalid = _market_snapshot().model_dump(mode="json")
    invalid["gates"]["automatic_research_allowed"] = False

    with pytest.raises(ValueError, match="市场状态快照无效"):
        run_short_pool(state, limit=6, market_state_provider=lambda: invalid)


@pytest.mark.parametrize(
    ("field_path", "value"),
    [
        (("signal_date",), None),
        (("coverage", "stock_count"), 999),
        (("coverage", "industry_count"), 19),
        (("coverage", "amount_symbol_coverage"), 0.89),
        (("coverage", "turnover_coverage"), 0.94),
        (("coverage", "calibration_days"), 119),
    ],
)
def test_available_snapshot_rejects_incomplete_t1_or_coverage(field_path, value):
    payload = _market_snapshot().model_dump(mode="json")
    target = payload
    for key in field_path[:-1]:
        target = target[key]
    target[field_path[-1]] = value

    with pytest.raises(ValidationError):
        MarketStateSnapshot.model_validate(payload)


# ── 请求内结果 + 内容寻址确认 ────────────────────────────────
def test_empty_pool_succeeds_without_persistence(state, screener_fake):
    screener_fake.row_count = 0

    response = run_short_pool(state, limit=8)

    assert response["status"] == "success"
    assert response["count"] == 0
    assert response["candidates"] == []
    assert response["total"] == 0
    assert response["next_actions"] == []
    assert response["t_research"]["status"] == "blocked_by_market_state"
    assert not (Path(state.repo.store.data_dir) / "user_data" / "short_pools").exists()


def test_result_is_content_addressed_but_never_written(state, screener_fake):
    first = run_short_pool(state, limit=8)
    second = run_short_pool(state, limit=8)

    assert first["pool_id"] == second["pool_id"]
    assert len(first["pool_id"]) == 16
    assert all(character in "0123456789abcdef" for character in first["pool_id"])
    assert first["candidates"] == second["candidates"]
    assert not list(Path(state.repo.store.data_dir).rglob("*.json"))


def test_content_change_changes_hash(state, screener_fake):
    first = run_short_pool(state, limit=8)["pool_id"]
    screener_fake.row_count = 9
    second = run_short_pool(state, limit=8)["pool_id"]

    assert first != second


def test_server_builds_reserved_hypothesis_only_from_ready_pool(state, screener_fake):
    pool = run_short_pool(state, limit=8)

    hypothesis = build_t_research_hypothesis(pool)

    assert hypothesis["title"] == f"做T研究 · AI短线研究池 · {AS_OF}"
    assert hypothesis["status"] == "exploring"
    assert "不自动运行回测" in hypothesis["thesis"]
    assert hypothesis["tags"][:3] == [
        "做T研究",
        "AI短线研究池",
        "market_concentration_v1",
    ]
    assert hypothesis["tags"][3] == f"short_pool:{pool['pool_id']}"


def test_server_rejects_blocked_or_empty_hypothesis_draft(state, screener_fake):
    blocked = run_short_pool(
        state,
        limit=8,
        market_state_provider=lambda: _market_snapshot("transition"),
    )
    with pytest.raises(ValueError, match="不允许"):
        build_t_research_hypothesis(blocked)

    screener_fake.row_count = 0
    empty = run_short_pool(state, limit=8)
    with pytest.raises(ValueError, match="不允许|没有可研究候选"):
        build_t_research_hypothesis(empty)


# ── screen_stock_pool preset 分支分发 ─────────────────────────
def test_screen_stock_pool_preset_branch_delegates(state, screener_fake):
    resp = screen_stock_pool(state, {"preset_id": "short_momentum_quality_v1", "limit": 7})
    assert resp["status"] == "success"
    assert resp["count"] == 7
    assert resp["preset"]["preset_id"] == "short_momentum_quality_v1"
    assert len(screener_fake.calls) == 1


def test_screen_stock_pool_preset_default_limit(state, screener_fake):
    screen_stock_pool(state, {"preset_id": "short_momentum_quality_v1"})
    assert screener_fake.calls[0].limit == 8


@pytest.mark.parametrize(
    "bad",
    [
        {
            "preset_id": "short_momentum_quality_v1",
            "conditions": [{"field": "close", "op": ">", "value": 1}],
        },
        {"preset_id": "short_momentum_quality_v1", "as_of": "2026-08-25"},
        {
            "preset_id": "short_momentum_quality_v1",
            "order_by": {"field": "close", "direction": "desc"},
        },
        {"preset_id": "short_momentum_quality_v1", "limit": 4},
        {"preset_id": "short_momentum_quality_v1", "limit": 13},
        {"preset_id": "other_preset"},
        {},
        {"conditions": None, "preset_id": None},
    ],
)
def test_preset_branch_rejects_extras(state, bad):
    with pytest.raises((ValueError, ValidationError)):
        screen_stock_pool(state, bad)


def test_legacy_conditions_path_unchanged(state, screener_fake):
    """无 preset_id 的旧强类型 conditions 路径逐字兼容。"""
    resp = screen_stock_pool(
        state,
        {
            "conditions": [{"field": "change_pct", "op": ">", "value": 0.05}],
            "as_of": AS_OF,
            "limit": 500,
        },
    )
    assert resp["status"] == "success"
    assert "preview" in resp and "candidates" not in resp
    req = screener_fake.calls[0]
    assert [c.model_dump(exclude={"group"}) for c in req.conditions] == [
        {"field": "change_pct", "op": ">", "value": 0.05}
    ]
    assert req.limit == 500


# ── 工具注册 / read_only / 分发 (不新增第 14 个工具) ──────────────
def test_tools_registry_stays_at_13_and_screen_pool_read_only():
    from app.services.agent_tools import TOOLS

    assert len(TOOLS) == 13
    names = [t["name"] for t in TOOLS]
    assert len(set(names)) == 13
    assert "run_short_pool_strategy" not in names
    tool = next(t for t in TOOLS if t["name"] == "screen_stock_pool")
    assert tool["read_only"] is True
    # Pi worker 不支持 oneOf；公共 schema 只声明字段并关闭额外字段，
    # 两分支互斥和 preset 的 5..12 限制由 _ScreenArgs 运行时校验。
    schema = tool["parameters"]
    assert schema == tool["input_schema"]
    assert "oneOf" not in schema
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"preset_id", "conditions", "as_of", "order_by", "limit"}
    assert schema["properties"]["preset_id"]["enum"] == ["short_momentum_quality_v1"]
    assert schema["properties"]["conditions"]["minItems"] == 1
    assert schema["properties"]["conditions"]["maxItems"] == 20
    assert schema["properties"]["limit"]["minimum"] == 1
    assert schema["properties"]["limit"]["maximum"] == 500


def test_screen_tool_description_pins_determinism_and_ai_role():
    from app.services.agent_tools import TOOLS

    desc = next(t for t in TOOLS if t["name"] == "screen_stock_pool")["description"]
    assert "short_momentum_quality_v1" in desc
    assert "确定性" in desc
    assert "AI 只解释证据" in desc
    assert "不得增删重排候选" in desc
    assert "不保存 short_pool artifact" in desc


def test_pool_backtest_tool_excludes_short_pool_ids():
    from app.services.agent_tools import TOOLS

    desc = next(t for t in TOOLS if t["name"] == "start_pool_backtest")["description"]
    assert "不接受 short_momentum_quality_v1 返回的 pool_id" in desc
    assert "short_pool_id" not in desc


def test_call_tool_dispatches_preset_branch(state, screener_fake):
    from app.services.agent_tools import call_tool

    resp = call_tool(
        "screen_stock_pool", state, {"preset_id": "short_momentum_quality_v1", "limit": 6}
    )
    assert resp["status"] == "success"
    assert resp["count"] == 6
    assert resp["preset"]["preset_id"] == "short_momentum_quality_v1"
