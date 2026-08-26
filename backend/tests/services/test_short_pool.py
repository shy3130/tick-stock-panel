"""short_pool 定向单元测试: 固定 preset 确定性筛选 + 不可变内容寻址 artifact。

确定性策略: 不读真实行情 — QueryService monkeypatch 为进程内 fake,
artifact 全部落 tmp_path, 绝不触碰真实 user_data/ 或 data/。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.services import short_pool as sp
from app.services import screener_query as screener_query_module
from app.services.agent_research_tools import screen_stock_pool
from app.services.short_pool import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MIN_LIMIT,
    SHORT_POOL_CONDITIONS,
    SHORT_POOL_ORDER_BY,
    ShortPoolLimit,
    build_query_request,
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
    assert [c.model_dump(exclude={"group"}) for c in req.conditions] == [dict(c) for c in SHORT_POOL_CONDITIONS]
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
        "exclude_st", "listing_days", "amount", "turnover_rate", "above_ma20",
        "momentum_20d", "distance_to_60d_high", "atr_pct_14", "vol_ratio_5d",
        "change_pct", "limit_up", "broken_limit_up",
    ]
    assert SHORT_POOL_ORDER_BY == {"field": "momentum_20d", "direction": "desc"}
    by_field = {c["field"]: c for c in SHORT_POOL_CONDITIONS}
    assert by_field["exclude_st"] == {"field": "exclude_st", "op": "=", "value": True}
    assert by_field["listing_days"] == {"field": "listing_days", "op": ">=", "value": 120}
    assert by_field["amount"] == {"field": "amount", "op": ">=", "value": 300000000}
    assert by_field["turnover_rate"] == {"field": "turnover_rate", "op": "between", "value": [2, 18]}
    assert by_field["above_ma20"] == {"field": "above_ma20", "op": "=", "value": True}
    assert by_field["momentum_20d"] == {"field": "momentum_20d", "op": "between", "value": [0.03, 0.25]}
    assert by_field["distance_to_60d_high"] == {"field": "distance_to_60d_high", "op": "between", "value": [-15, 0]}
    assert by_field["atr_pct_14"] == {"field": "atr_pct_14", "op": "between", "value": [2, 9]}
    assert by_field["vol_ratio_5d"] == {"field": "vol_ratio_5d", "op": ">=", "value": 1}
    assert by_field["change_pct"] == {"field": "change_pct", "op": "between", "value": [-0.03, 0.08]}
    assert by_field["limit_up"] == {"field": "limit_up", "op": "=", "value": False}
    assert by_field["broken_limit_up"] == {"field": "broken_limit_up", "op": "=", "value": False}


# ── QueryService 调用 + 候选/证据 ────────────────────────────
def test_run_short_pool_calls_query_service_with_preset_request(state, screener_fake):
    resp = run_short_pool(state, limit=6)
    assert len(screener_fake.calls) == 1
    req = screener_fake.calls[0]
    assert [c.model_dump(exclude={"group"}) for c in req.conditions] == [dict(c) for c in SHORT_POOL_CONDITIONS]
    assert req.order_by.model_dump() == SHORT_POOL_ORDER_BY
    assert req.limit == 6
    # 封套完整键
    assert set(resp) == {
        "status", "summary", "pool_id", "as_of", "count", "total", "preset",
        "candidates", "disclaimer", "selection_basis", "ai_role", "next_actions",
        "artifacts",
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
                "field", "label", "actual", "display", "op", "target", "criterion", "unit",
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




# ── 空池成功 + artifact ──────────────────────────────────────
def test_empty_pool_still_succeeds_and_persists_artifact(state, screener_fake):
    screener_fake.row_count = 0
    resp = run_short_pool(state, limit=8)
    assert resp["status"] == "success"
    assert resp["count"] == 0 and resp["candidates"] == [] and resp["total"] == 0
    assert resp["next_actions"] == []
    artifact = Path(state.repo.store.data_dir) / "user_data" / "short_pools" / f"{resp['pool_id']}.json"
    assert artifact.is_file()
    pool = json.loads(artifact.read_text(encoding="utf-8"))
    assert pool["count"] == 0 and pool["candidates"] == []

def test_artifact_root_can_be_isolated_for_real_data_smoke(state, screener_fake, tmp_path):
    isolated = tmp_path / "isolated-output"
    resp = run_short_pool(state, limit=5, artifact_root=isolated)
    artifact = isolated / "user_data" / "short_pools" / f"{resp['pool_id']}.json"
    assert artifact.is_file()
    default_artifact = Path(state.repo.store.data_dir) / "user_data" / "short_pools" / artifact.name
    assert not default_artifact.exists()


def test_artifact_content_addressed_and_idempotent(state, screener_fake, monkeypatch):
    first = run_short_pool(state, limit=8)
    pool_id = first["pool_id"]
    assert len(pool_id) == 16 and all(c in "0123456789abcdef" for c in pool_id)
    artifact = Path(state.repo.store.data_dir) / "user_data" / "short_pools" / f"{pool_id}.json"
    assert artifact.is_file()
    pool = json.loads(artifact.read_text(encoding="utf-8"))
    assert pool["pool_id"] == pool_id
    assert pool["schema_version"] == 1
    assert pool["data_watermark"]["cache_generation"] == 7
    assert pool["checksum"] and len(pool["checksum"]) == 64
    assert pool["candidates"] == first["candidates"]
    assert pool["order_by"] == SHORT_POOL_ORDER_BY

    # 相同内容幂等: 二次运行不覆写 artifact。
    # (run_short_pool 延迟从 agent_research_tools 导入 _atomic_write_json, 须 patch 源模块)
    from app.services import agent_research_tools as art

    def _no_write(*_a, **_k):  # pragma: no cover — 触发即失败
        raise AssertionError("short pool artifact must not be overwritten")

    monkeypatch.setattr(art, "_atomic_write_json", _no_write)
    again = run_short_pool(state, limit=8)
    assert again["pool_id"] == pool_id
    assert again["candidates"] == first["candidates"]


def test_content_change_changes_hash(state, screener_fake):
    a = run_short_pool(state, limit=8)["pool_id"]
    screener_fake.row_count = 9  # 内容变化 → hash 变化
    b = run_short_pool(state, limit=8)["pool_id"]
    assert a != b


# ── tamper / collision fail-closed ──────────────────────────
def test_tampered_artifact_fails_closed(state, screener_fake):
    first = run_short_pool(state, limit=8)
    pool_id = first["pool_id"]
    artifact = Path(state.repo.store.data_dir) / "user_data" / "short_pools" / f"{pool_id}.json"
    pool = json.loads(artifact.read_text(encoding="utf-8"))
    pool["candidates"][0]["name"] = "被篡改"
    artifact.write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="checksum mismatch") as excinfo:
        run_short_pool(state, limit=8)
    assert str(artifact) not in str(excinfo.value)  # 错误不泄露路径


def test_tampered_pool_id_fails_closed(state, screener_fake):
    first = run_short_pool(state, limit=8)
    pool_id = first["pool_id"]
    artifact = Path(state.repo.store.data_dir) / "user_data" / "short_pools" / f"{pool_id}.json"
    pool = json.loads(artifact.read_text(encoding="utf-8"))
    pool["pool_id"] = "0" * 16
    artifact.write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="pool_id mismatch"):
        run_short_pool(state, limit=8)

def test_tampered_data_watermark_fails_closed(state, screener_fake):
    first = run_short_pool(state, limit=8)
    pool_id = first["pool_id"]
    artifact = Path(state.repo.store.data_dir) / "user_data" / "short_pools" / f"{pool_id}.json"
    pool = json.loads(artifact.read_text(encoding="utf-8"))
    pool["data_watermark"] = {"cache_generation": 999999}
    artifact.write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="checksum mismatch"):
        run_short_pool(state, limit=8)


def test_hash_collision_fails_closed(state, screener_fake, monkeypatch):
    """pool_id 相同但内容不同(理论上不可达, 防御性) → fail-closed。"""
    first = run_short_pool(state, limit=8)
    pool_id = first["pool_id"]
    artifact = Path(state.repo.store.data_dir) / "user_data" / "short_pools" / f"{pool_id}.json"
    # 篡改后同时伪造自洽 checksum 与 pool_id, 制造"内容不同但校验通过"的碰撞态:
    # 直接删掉 artifact 中的一个候选并重算校验, 使 _load 通过但内容 != 新 content。
    pool = json.loads(artifact.read_text(encoding="utf-8"))
    pool["candidates"] = pool["candidates"][:-1]
    pool["count"] = len(pool["candidates"])
    content = {k: v for k, v in pool.items() if k not in ("pool_id", "data_watermark", "checksum")}
    pool["pool_id"] = sp._pool_id_hex(content)
    integrity_payload = {k: v for k, v in pool.items() if k != "checksum"}
    pool["checksum"] = sp._checksum_hex(integrity_payload)
    artifact.unlink()
    new_path = artifact.parent / f"{pool['pool_id']}.json"
    new_path.write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")

    # 构造与新 artifact 同 pool_id 的运行: 让 fake 返回与第一次相同内容 → 不同 content → 碰撞。
    with pytest.raises(ValueError, match="collision"):
        _force_collision(state, pool["pool_id"])


def _force_collision(state, target_pool_id):
    """绕过内容寻址直接注入目标 pool_id, 验证防御分支。"""
    # 用 monkeypatch 替换 _pool_id_hex 为固定值, 使新内容落同一文件名。
    import app.services.short_pool as mod

    original = mod._pool_id_hex
    mod._pool_id_hex = lambda content: target_pool_id
    try:
        return run_short_pool(state, limit=8)
    finally:
        mod._pool_id_hex = original


# ── screen_stock_pool preset 分支分发 ─────────────────────────
def test_screen_stock_pool_preset_branch_delegates(state, screener_fake):
    resp = screen_stock_pool(state, {"preset_id": "short_momentum_quality_v1", "limit": 7})
    assert resp["status"] == "success"
    assert resp["count"] == 7
    assert resp["preset"]["preset_id"] == "short_momentum_quality_v1"
    assert len(screener_fake.calls) == 1


def test_screen_stock_pool_preset_default_limit(state, screener_fake):
    resp = screen_stock_pool(state, {"preset_id": "short_momentum_quality_v1"})
    assert screener_fake.calls[0].limit == 8


@pytest.mark.parametrize(
    "bad",
    [
        {"preset_id": "short_momentum_quality_v1", "conditions": [{"field": "close", "op": ">", "value": 1}]},
        {"preset_id": "short_momentum_quality_v1", "as_of": "2026-08-25"},
        {"preset_id": "short_momentum_quality_v1", "order_by": {"field": "close", "direction": "desc"}},
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
        {"conditions": [{"field": "change_pct", "op": ">", "value": 0.05}], "as_of": AS_OF, "limit": 500},
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
    # oneOf: preset 分支只允许 preset_id + limit(5..12)
    schema = tool["parameters"]
    assert len(schema["oneOf"]) == 2
    preset_branch, legacy_branch = schema["oneOf"]
    assert preset_branch["required"] == ["preset_id"]
    assert set(preset_branch["properties"]) == {"preset_id", "limit"}
    assert preset_branch["properties"]["preset_id"]["enum"] == ["short_momentum_quality_v1"]
    assert preset_branch["properties"]["limit"]["minimum"] == 5
    assert preset_branch["properties"]["limit"]["maximum"] == 12
    assert legacy_branch["required"] == ["conditions"]


def test_screen_tool_description_pins_determinism_and_ai_role():
    from app.services.agent_tools import TOOLS

    desc = next(t for t in TOOLS if t["name"] == "screen_stock_pool")["description"]
    assert "short_momentum_quality_v1" in desc
    assert "确定性" in desc
    assert "AI 只解释证据" in desc
    assert "不得增删重排候选" in desc

def test_pool_backtest_tool_excludes_short_pool_ids():
    from app.services.agent_tools import TOOLS

    desc = next(t for t in TOOLS if t["name"] == "start_pool_backtest")["description"]
    assert "不接受 short_momentum_quality_v1 返回的 short_pool_id" in desc


def test_call_tool_dispatches_preset_branch(state, screener_fake):
    from app.services.agent_tools import call_tool

    resp = call_tool("screen_stock_pool", state, {"preset_id": "short_momentum_quality_v1", "limit": 6})
    assert resp["status"] == "success"
    assert resp["count"] == 6
    assert resp["preset"]["preset_id"] == "short_momentum_quality_v1"
