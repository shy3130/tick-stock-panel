"""策略池服务与 API 测试 — 池成员 / 库级分组与归档 / 迁移链 / 惰性登记。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import strategy_pool as strategy_pool_api
from app.services import preferences, strategy_pool


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    """settings.data_dir → tmp; preferences mtime 缓存随之失效 (路径已变)。"""
    from app import config as app_config

    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    preferences._invalidate_cache()
    return tmp_path


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(strategy_pool_api.router)
    return TestClient(app)


def _meta(state: dict, sid: str) -> dict:
    return state["meta"][sid]


# ── 服务层 ──────────────────────────────────────────────


def test_first_load_seeds_pool_from_legacy_ui_prefs():
    """旧版 ui_prefs.strategy_pool (JSON 文本数组) → 池一次性迁移写盘。"""
    preferences.set_ui_prefs({"strategy_pool": json.dumps(["custom_a", "custom_b"])})
    snapshot = strategy_pool.load_snapshot()
    assert snapshot["pool"] == ["custom_a", "custom_b"]
    # 迁移结果已落盘: 偏好清空也不回退
    preferences.set_ui_prefs({"strategy_pool": None})
    assert strategy_pool.load_snapshot()["pool"] == ["custom_a", "custom_b"]


def test_v1_file_migrates_pool_and_meta(tmp_path):
    """v1 文件 (entries/archived 各带属性) → 池取顺序, 属性折入 meta。"""
    strategy_pool.load_snapshot()  # 建立文件
    (tmp_path / "user_data" / "strategy_pool.json").write_text(json.dumps({
        "version": 1,
        "groups": [{"id": "g1", "name": "动量"}],
        "entries": [
            {"id": "a", "added_at": "2026-01-01T00:00:00", "group": "g1"},
            {"id": "b", "added_at": "2026-01-02T00:00:00", "group": None},
        ],
        "archived": [
            {"id": "c", "added_at": "2026-01-03T00:00:00", "group": "g1",
             "archived_at": "2026-02-01T00:00:00"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    snapshot = strategy_pool.load_snapshot()
    assert snapshot["pool"] == ["a", "b"]  # 归档项不进池
    assert snapshot["meta"]["a"] == {
        "group": "g1", "archived": False, "archived_at": None,
        "added_at": "2026-01-01T00:00:00",
    }
    assert snapshot["meta"]["c"]["archived"] is True
    assert snapshot["meta"]["c"]["archived_at"] == "2026-02-01T00:00:00"
    assert snapshot["meta"]["c"]["group"] == "g1"
    # 迁移写回 v2, 二次读取稳定
    assert strategy_pool.load_snapshot() == snapshot


def test_pool_set_add_remove_and_archived_guard():
    strategy_pool.set_pool(["s1", "s2"])
    strategy_pool.remove("s1")
    assert strategy_pool.load_snapshot()["pool"] == ["s2"]

    strategy_pool.add("s3")
    assert strategy_pool.load_snapshot()["pool"] == ["s2", "s3"]

    with pytest.raises(KeyError):
        strategy_pool.remove("ghost")

    with pytest.raises(ValueError, match="重复"):
        strategy_pool.set_pool(["s1", "s1"])


def test_archive_is_library_level_and_removes_from_pool():
    """归档是库级标记: 同时移出池; 恢复只回库, 不自动回池。"""
    strategy_pool.set_pool(["s1", "s2"])
    strategy_pool.archive("s1")
    snapshot = strategy_pool.load_snapshot()
    assert snapshot["pool"] == ["s2"]  # 自动移出池
    assert _meta(snapshot, "s1")["archived"] is True
    assert _meta(snapshot, "s1")["archived_at"]

    strategy_pool.unarchive("s1")
    snapshot = strategy_pool.load_snapshot()
    assert _meta(snapshot, "s1")["archived"] is False
    assert _meta(snapshot, "s1")["archived_at"] is None
    assert snapshot["pool"] == ["s2"]  # 恢复不回池

    with pytest.raises(KeyError):
        strategy_pool.unarchive("s1")  # 未归档
    # 已归档策略不能直接入池
    strategy_pool.archive("s1")
    with pytest.raises(ValueError, match="先在档案库恢复"):
        strategy_pool.add("s1")
    with pytest.raises(ValueError, match="先恢复"):
        strategy_pool.set_pool(["s1"])


def test_archive_without_pool_membership():
    """未入池的策略同样可以归档 (纯库级操作)。"""
    strategy_pool.archive("lonely")
    snapshot = strategy_pool.load_snapshot()
    assert snapshot["pool"] == []
    assert _meta(snapshot, "lonely")["archived"] is True
    assert _meta(snapshot, "lonely")["added_at"]  # 按需登记加入时间


def test_discard_record_removes_meta_and_pool_entry():
    strategy_pool.set_pool(["s1"])
    strategy_pool.archive("s1")
    strategy_pool.discard_record("s1")
    snapshot = strategy_pool.load_snapshot()
    assert "s1" not in snapshot["meta"]
    assert snapshot["pool"] == []
    with pytest.raises(KeyError):
        strategy_pool.discard_record("s1")


def test_strategy_group_meta_and_group_delete_keeps_strategies():
    _, g1 = strategy_pool.create_group("动量")
    _, g2 = strategy_pool.create_group("价值")
    strategy_pool.set_strategy_group("s1", g1["id"])
    strategy_pool.set_strategy_group("s2", g2["id"])

    with pytest.raises(ValueError, match="已存在"):
        strategy_pool.create_group("动量")  # casefold 查重
    with pytest.raises(ValueError, match="不存在"):
        strategy_pool.set_strategy_group("s1", "ghost")

    strategy_pool.reorder_groups([g2["id"], g1["id"]])
    assert [g["name"] for g in strategy_pool.load_snapshot()["groups"]] == ["价值", "动量"]
    with pytest.raises(ValueError, match="不一致"):
        strategy_pool.reorder_groups([g1["id"]])  # 非全量排列拒绝

    # 删除分组: 策略保留, 分组引用转为未分组
    strategy_pool.delete_group(g2["id"])
    snapshot = strategy_pool.load_snapshot()
    assert [g["id"] for g in snapshot["groups"]] == [g1["id"]]
    assert _meta(snapshot, "s1")["group"] == g1["id"]
    assert _meta(snapshot, "s2")["group"] is None


def test_sync_known_ids_registers_added_once():
    """惰性登记: 新见到的策略补 added_at, 已登记记录不漂移。"""
    strategy_pool.sync_known_ids(["a", "b"])
    first = _meta(strategy_pool.load_snapshot(), "a")["added_at"]
    assert first
    # 二次同步不动已有记录
    strategy_pool.sync_known_ids(["a", "b", "c"])
    snapshot = strategy_pool.load_snapshot()
    assert _meta(snapshot, "a")["added_at"] == first
    assert _meta(snapshot, "c")["added_at"]


def test_corrupt_store_fail_closed(tmp_path):
    strategy_pool.load_snapshot()  # 建立文件
    (tmp_path / "user_data" / "strategy_pool.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="损坏"):
        strategy_pool.load_snapshot()


def test_snapshot_revision_increases_on_changes_and_restore():
    first = strategy_pool.load_snapshot()
    second = strategy_pool.add("a")
    assert second["revision"] > first["revision"]
    restored = strategy_pool.restore_snapshot(first, allow_overwrite=True)
    assert restored["revision"] > second["revision"]
    assert restored["pool"] == []
    assert json.loads(preferences.get_ui_prefs()["strategy_pool"]) == []


def test_legacy_migration_does_not_replace_another_clients_pool():
    strategy_pool.set_pool(["other_window"])
    migrated = strategy_pool.set_pool(["old_local"], only_if_empty=True)
    assert migrated["pool"] == ["other_window"]


def test_stale_reorder_preserves_newer_other_window_changes():
    old = strategy_pool.set_pool(["a", "b"])
    strategy_pool.add("other_window")
    with pytest.raises(ValueError, match="其他窗口更新"):
        strategy_pool.set_pool(["b", "a"], expected_revision=old["revision"])
    assert strategy_pool.load_snapshot()["pool"] == ["a", "b", "other_window"]


def test_metadata_read_is_batch_only_and_does_not_initialize_store(tmp_path):
    assert strategy_pool.library_metadata(tmp_path) == {}
    assert not (tmp_path / "user_data/strategy_pool.json").exists()
    _, group = strategy_pool.create_group("动量")
    strategy_pool.set_strategy_group("a", group["id"])
    strategy_pool.archive("a")
    metadata = strategy_pool.library_metadata(tmp_path)
    assert metadata["a"]["archived"] is True
    assert metadata["a"]["group_name"] == "动量"


def test_future_version_cannot_be_overwritten(tmp_path):
    strategy_pool.load_snapshot()
    path = tmp_path / "user_data/strategy_pool.json"
    path.write_text(json.dumps({"version": 99, "groups": [], "pool": [], "meta": {}}))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="版本"):
        strategy_pool.add("a")
    assert path.read_bytes() == before


def test_strategy_lists_include_library_metadata_without_hiding_existing_references(tmp_path):
    from app.api import screener, strategy
    from app.strategy.engine import StrategyEngine

    directory = tmp_path / "strategies/custom"
    directory.mkdir(parents=True)
    (directory / "custom_a.py").write_text(
        'import polars as pl\nMETA = {"id":"custom_a", "name":"A"}\n'
        'def filter(df, params):\n    return pl.lit(True)\n', encoding="utf-8",
    )
    engine = StrategyEngine(strategy_dirs=[directory])
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), strategy_engine=engine,
    )))
    _, group = strategy_pool.create_group("观察组")
    strategy_pool.set_strategy_group("custom_a", group["id"])
    strategy_pool.archive("custom_a")
    listing = strategy.list_strategies(request)["strategies"]
    assert listing[0]["library"]["archived"] is True
    assert listing[0]["library"]["group_name"] == "观察组"
    assert strategy.get_strategy("custom_a", request)["library"] == listing[0]["library"]
    assert screener.strategies(request, "stock", "1d")["presets"][0]["library"] == listing[0]["library"]
    assert engine.has("custom_a")  # 归档不注销, 已保存的回测/监控/叠加仍可解析


# ── API 层 ──────────────────────────────────────────────


def test_api_roundtrip_pool_archive_groups():
    client = _client()
    assert client.get("/api/strategy-pool").json()["pool"] == []

    created = client.post("/api/strategy-pool/groups", json={"name": "动量"})
    assert created.status_code == 200
    group_id = created.json()["group"]["id"]

    put = client.put("/api/strategy-pool/entries", json={"pool": ["custom_a", "custom_b"]})
    assert put.status_code == 200
    assert put.json()["pool"] == ["custom_a", "custom_b"]
    assert put.json()["meta"].get("custom_a") is None  # 入池不登记 meta (GET 同步引擎时才登记)

    grouped = client.post("/api/strategy-pool/strategies/custom_a/group", json={"group": group_id})
    assert grouped.status_code == 200
    assert grouped.json()["meta"]["custom_a"]["group"] == group_id

    # 未知分组 fail-closed
    assert client.post(
        "/api/strategy-pool/strategies/custom_a/group", json={"group": "ghost"},
    ).status_code == 400

    # 归档: 移出池 + meta 标记
    archived = client.post("/api/strategy-pool/strategies/custom_a/archive")
    assert archived.status_code == 200
    body = archived.json()
    assert body["pool"] == ["custom_b"]
    assert body["meta"]["custom_a"]["archived"] is True
    assert body["meta"]["custom_a"]["group"] == group_id  # 分组保留

    # 已归档策略不能入池
    assert client.post("/api/strategy-pool/entries/custom_a").status_code == 400

    # 恢复: 回库不回池
    restored = client.post("/api/strategy-pool/strategies/custom_a/unarchive")
    assert restored.status_code == 200
    assert restored.json()["meta"]["custom_a"]["archived"] is False
    assert restored.json()["pool"] == ["custom_b"]
    assert client.post("/api/strategy-pool/strategies/custom_a/unarchive").status_code == 404

    # 删除库级记录
    client.post("/api/strategy-pool/strategies/custom_a/archive")
    discarded = client.delete("/api/strategy-pool/strategies/custom_a/record")
    assert discarded.status_code == 200
    assert "custom_a" not in discarded.json()["meta"]
    assert client.delete("/api/strategy-pool/strategies/custom_a/record").status_code == 404

    assert client.delete("/api/strategy-pool/entries/custom_b").status_code == 200
    assert client.delete("/api/strategy-pool/entries/custom_b").status_code == 404

    # 删除分组: 策略保留转未分组
    deleted = client.delete(f"/api/strategy-pool/groups/{group_id}")
    assert deleted.status_code == 200
    assert deleted.json()["meta"].get("custom_a") is None  # 记录已被彻底移除
    assert client.delete("/api/strategy-pool/groups/ghost").status_code == 404


def test_api_get_syncs_engine_strategy_ids(tmp_path):
    """GET 把引擎策略 ID 惰性登记进 meta (加入时间立即可排序)。"""
    app = FastAPI()
    app.include_router(strategy_pool_api.router)
    app.state.strategy_engine = type(
        "Engine", (), {"list_strategies": lambda self: [
            type("S", (), {"id": "engine_a"})(), type("S", (), {"id": "engine_b"})(),
        ]},
    )()
    client = TestClient(app)
    body = client.get("/api/strategy-pool").json()
    assert body["meta"]["engine_a"]["added_at"]
    assert body["meta"]["engine_b"]["added_at"]
