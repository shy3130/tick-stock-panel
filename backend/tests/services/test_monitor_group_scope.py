"""监控规则 scope=watchlist_group — 自选分组动态作用域。

在现有 test_monitor_scope.py (sector fail-closed) 基础上补充分组作用域:
- 规则校验/normalize (group_id 必填, symbols 清空, 其他 scope 清 group_id);
- 引擎按分组当前成员过滤 (分组增删标的无需改规则, 下一轮评估自动生效);
- 分组删除/空组/data_dir 缺失 → fail-closed, 绝不退化为全市场;
- API 保存时分组存在性校验, 列表 runtime_warning, options 暴露 scope+groups;
- 现有 all/symbols 行为不变。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import monitor_rules as monitor_rules_api
from app.services import watchlist
from app.strategy import monitor_rules
from app.strategy.monitor import MonitorRuleEngine


def _group_rule(rid="r_grp", group_id="g1", **overrides) -> dict:
    rule = {
        "id": rid,
        "name": rid,
        "type": "signal",
        "scope": "watchlist_group",
        "group_id": group_id,
        "symbols": [],
        "logic": "or",
        "conditions": [{"field": "rsi_14", "op": "<", "value": 100}],
        "cooldown_seconds": 0,
        "enabled": True,
    }
    rule.update(overrides)
    return rule


def _stock_df() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["600000.SH", "000001.SZ", "300750.SZ"],
            "name": ["浦发银行", "平安银行", "宁德时代"],
            "close": [10.0, 12.0, 200.0],
            "change_pct": [1.0, 2.0, 3.0],
            "rsi_14": [40.0, 50.0, 60.0],
        }
    )


# ── 校验与 normalize ─────────────────────────────────────
def test_group_scope_validation():
    with pytest.raises(ValueError, match="自选分组"):
        monitor_rules.validate(_group_rule(group_id=None))
    with pytest.raises(ValueError, match="自选分组"):
        monitor_rules.validate(_group_rule(group_id="  "))
    monitor_rules.validate(_group_rule())  # 合法


def test_normalize_group_scope_fields():
    # 分组作用域: 保留 group_id, 清掉 symbols (成员动态来自分组)
    r = monitor_rules.normalize(_group_rule(symbols=["600000.SH"]))
    assert r["group_id"] == "g1"
    assert r["symbols"] == []
    # 非分组作用域: 清掉残留 group_id
    r = monitor_rules.normalize(_group_rule(scope="all"))
    assert r["group_id"] is None


# ── 引擎: 动态成员过滤 ───────────────────────────────────
def test_engine_group_scope_dynamic_members(tmp_path: Path):
    """分组内后续加入的标的, 无需修改规则即自动进入监控范围。"""
    _, group = watchlist.create_group("核心池", data_dir=tmp_path)
    gid = group["id"]
    watchlist.add("600000.SH", group_id=gid, data_dir=tmp_path)
    watchlist.add("000001.SZ", group_id=gid, data_dir=tmp_path)

    eng = MonitorRuleEngine()
    eng.set_data_dir(tmp_path)
    eng.set_rules([_group_rule(group_id=gid)])
    df = _stock_df()

    events = eng.evaluate(df)
    assert {e["symbol"] for e in events} == {"600000.SH", "000001.SZ"}

    # 分组新增宁德时代 → 同一条规则下一轮自动覆盖 (版本号缓存立即失效)
    watchlist.add("300750.SZ", group_id=gid, data_dir=tmp_path)
    events = eng.evaluate(df)
    assert "300750.SZ" in {e["symbol"] for e in events}

    # 移出分组 → 自动退出监控范围
    watchlist.remove_from_group("300750.SZ", gid, data_dir=tmp_path)
    events = eng.evaluate(df)
    assert "300750.SZ" not in {e["symbol"] for e in events}


def test_engine_group_scope_missing_group_fail_closed(tmp_path: Path):
    """分组已删除: 不崩、不触发、绝不退化为全市场。"""
    watchlist.create_group("核心池", data_dir=tmp_path)  # 分组文件存在, 但规则绑定的 id 不在其中

    eng = MonitorRuleEngine()
    eng.set_data_dir(tmp_path)
    eng.set_rules([_group_rule(group_id="ghost")])
    assert eng.evaluate(_stock_df()) == []


def test_engine_group_scope_deleted_group_fail_closed(tmp_path: Path):
    """规则绑定后分组被删除: 下一轮评估 fail-closed (规则暂停)。"""
    _, group = watchlist.create_group("待删", data_dir=tmp_path)
    gid = group["id"]
    watchlist.add("600000.SH", group_id=gid, data_dir=tmp_path)

    eng = MonitorRuleEngine()
    eng.set_data_dir(tmp_path)
    eng.set_rules([_group_rule(group_id=gid)])
    assert {e["symbol"] for e in eng.evaluate(_stock_df())} == {"600000.SH"}

    watchlist.delete_group(gid, data_dir=tmp_path)
    # 删除后标的仍在自选 (未分组), 但规则绝不再对它触发
    assert eng.evaluate(_stock_df()) == []
    assert any(r["symbol"] == "600000.SH" for r in watchlist.list_symbols(tmp_path))


def test_engine_group_scope_empty_group(tmp_path: Path):
    _, group = watchlist.create_group("空组", data_dir=tmp_path)

    eng = MonitorRuleEngine()
    eng.set_data_dir(tmp_path)
    eng.set_rules([_group_rule(group_id=group["id"])])
    assert eng.evaluate(_stock_df()) == []


def test_engine_group_scope_no_data_dir(tmp_path: Path, monkeypatch):
    """即使默认目录存在同 id 分组，未注入 data_dir 仍须 fail-closed。"""
    monkeypatch.setattr(watchlist.settings, "data_dir", tmp_path)
    _, group = watchlist.create_group("默认目录组")
    watchlist.add("600000.SH", group_id=group["id"])

    eng = MonitorRuleEngine()
    eng.set_rules([_group_rule(group_id=group["id"])])
    assert eng.evaluate(_stock_df()) == []


def test_engine_group_scope_corrupt_store_fail_closed(tmp_path: Path, monkeypatch):
    """分组数据读取异常: 本轮跳过, 不崩、不退化为全市场。"""
    from app.services import watchlist as wl

    def boom(*a, **k):
        raise RuntimeError("disk error")

    monkeypatch.setattr(wl, "list_groups", boom)
    monkeypatch.setattr(wl, "revision", lambda *a, **k: 0)

    eng = MonitorRuleEngine()
    eng.set_data_dir(tmp_path)
    eng.set_rules([_group_rule(group_id="g1")])
    assert eng.evaluate(_stock_df()) == []


def test_engine_existing_scopes_unchanged(tmp_path: Path):
    """现有 all/symbols 行为不变 (对照组)。"""
    eng = MonitorRuleEngine()
    all_rule = {
        "id": "r_all",
        "name": "全市场",
        "type": "signal",
        "scope": "all",
        "conditions": [{"field": "rsi_14", "op": "<", "value": 100}],
        "logic": "and",
        "cooldown_seconds": 0,
    }
    sym_rule = {
        "id": "r_sym",
        "name": "指定",
        "type": "signal",
        "scope": "symbols",
        "symbols": ["600000.SH"],
        "conditions": [{"field": "rsi_14", "op": "<", "value": 100}],
        "logic": "and",
        "cooldown_seconds": 0,
    }
    eng.set_rules([all_rule, sym_rule])
    events = eng.evaluate(_stock_df())
    assert {e["rule_id"] for e in events} == {"r_all", "r_sym"}
    assert all(e["symbol"] != "000001.SZ" or e["rule_id"] == "r_all" for e in events)


def test_group_rule_not_expanded_into_symbols(tmp_path: Path):
    """保存时不把分组展开固化到 symbols (normalize 清空, 持久化只存 group_id)。"""
    r = monitor_rules.normalize(_group_rule(symbols=["600000.SH", "000001.SZ"]))
    assert r["symbols"] == []
    assert r["group_id"] == "g1"


# ── API: save 校验 / list warning / options ─────────────
def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.state.repo = MagicMock()
    app.state.repo.store = SimpleNamespace(data_dir=tmp_path)
    app.include_router(monitor_rules_api.router)
    return TestClient(app)


def _group_payload(rid="r_grp", group_id="g1", **overrides) -> dict:
    return _group_rule(rid=rid, group_id=group_id, **overrides)


def test_api_save_rejects_missing_group(tmp_path: Path):
    client = _client(tmp_path)
    resp = client.post("/api/monitor-rules", json=_group_payload(group_id="ghost"))
    assert resp.status_code == 400
    assert "自选分组" in resp.json()["detail"]


def test_api_save_accepts_existing_group(tmp_path: Path):
    client = _client(tmp_path)
    _, group = watchlist.create_group("核心", data_dir=tmp_path)
    resp = client.post("/api/monitor-rules", json=_group_payload(group_id=group["id"]))
    assert resp.status_code == 200
    rule = resp.json()["rule"]
    assert rule["scope"] == "watchlist_group"
    assert rule["group_id"] == group["id"]
    assert rule["symbols"] == []

    # 落盘文件同样只存 group_id (不展开 symbols)
    saved = monitor_rules.load_one(tmp_path, "r_grp")
    assert saved["group_id"] == group["id"]
    assert saved["symbols"] == []


def test_api_list_runtime_warning_for_missing_group(tmp_path: Path):
    client = _client(tmp_path)
    _, group = watchlist.create_group("核心", data_dir=tmp_path)
    watchlist.add("600000.SH", group_id=group["id"], data_dir=tmp_path)
    client.post("/api/monitor-rules", json=_group_payload(group_id=group["id"]))

    # 组存在且有成员: 无警告
    rules = client.get("/api/monitor-rules").json()["rules"]
    assert rules[0].get("runtime_warning") is None

    # 组被删除: 列表返回 runtime_warning, 且不回写持久化
    watchlist.delete_group(group["id"], data_dir=tmp_path)
    rules = client.get("/api/monitor-rules").json()["rules"]
    assert "已删除" in rules[0]["runtime_warning"]
    assert "runtime_warning" not in monitor_rules.load_one(tmp_path, "r_grp")


def test_api_list_runtime_warning_for_empty_group(tmp_path: Path):
    client = _client(tmp_path)
    _, group = watchlist.create_group("空组", data_dir=tmp_path)
    client.post("/api/monitor-rules", json=_group_payload(group_id=group["id"]))

    rules = client.get("/api/monitor-rules").json()["rules"]
    assert rules[0].get("runtime_warning") is not None
    assert "为空" in rules[0]["runtime_warning"]


def test_api_list_runtime_warning_when_group_store_is_corrupt(tmp_path: Path):
    client = _client(tmp_path)
    _, group = watchlist.create_group("损坏前", data_dir=tmp_path)
    client.post("/api/monitor-rules", json=_group_payload(group_id=group["id"]))
    (tmp_path / "user_data" / "watchlist_groups.json").write_text(
        "{broken",
        encoding="utf-8",
    )

    rules = client.get("/api/monitor-rules").json()["rules"]
    assert "读取失败" in rules[0]["runtime_warning"]
    assert "runtime_warning" not in monitor_rules.load_one(tmp_path, "r_grp")


def test_api_options_expose_scope_and_groups(tmp_path: Path):
    client = _client(tmp_path)
    _, g1 = watchlist.create_group("一", data_dir=tmp_path)

    resp = client.get("/api/monitor-rules/options")
    assert resp.status_code == 200
    data = resp.json()
    scopes = [s["key"] for s in data["scopes"]]
    assert "watchlist_group" in scopes
    assert "symbols" in scopes and "all" in scopes
    assert {g["id"] for g in data["watchlist_groups"]} == {g1["id"]}
