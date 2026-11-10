"""自选分组持久化与 API 契约。"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest
from fastapi import HTTPException

from app.api import watchlist as watchlist_api
from app.config import settings
from app.services import watchlist


def _request():
    repo = MagicMock()
    repo.get_name_map.return_value = {}
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))


def test_historical_watchlist_is_read_as_ungrouped(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    path = tmp_path / "user_data" / "watchlist.parquet"
    path.parent.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600000.SH"],
        "added_at": ["2026-08-08T10:00:00"],
        "note": [""],
    }).write_parquet(path)

    assert watchlist.list_symbols()[0]["group_ids"] == []


def test_group_lifecycle_preserves_watchlist_entries(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    groups, created = watchlist.create_group("  短线  ", "orange")
    assert groups == [{"id": created["id"], "name": "短线", "color": "orange"}]

    watchlist.add("600000.SH", group_id=created["id"])
    watchlist.add("000001.SZ")
    assert watchlist.list_symbols()[1]["group_ids"] == [created["id"]]

    renamed = watchlist.rename_group(created["id"], "观察", "fuchsia")
    assert renamed[0]["name"] == "观察"
    assert renamed[0]["color"] == "fuchsia"

    remaining, rows = watchlist.delete_group(created["id"])
    assert remaining == []
    assert {row["symbol"] for row in rows} == {"600000.SH", "000001.SZ"}
    assert all(row["group_ids"] == [] for row in rows)


def test_group_validation_and_assignment_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _, created = watchlist.create_group("核心")
    watchlist.add("600000.SH")

    with pytest.raises(ValueError, match="已存在"):
        watchlist.create_group("核心")
    with pytest.raises(ValueError, match="颜色"):
        watchlist.create_group("无效颜色", "black")
    with pytest.raises(ValueError, match="颜色"):
        watchlist.rename_group(created["id"], "核心", "black")
    with pytest.raises(ValueError, match="不存在"):
        watchlist.set_group("600000.SH", "missing")
    with pytest.raises(KeyError):
        watchlist.set_group("000001.SZ", created["id"])

    rows = watchlist.set_group("600000.SH", created["id"])
    assert rows[0]["group_ids"] == [created["id"]]
    rows = watchlist.set_group("600000.SH", None)
    assert rows[0]["group_ids"] == []


def test_reorder_groups(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _, first = watchlist.create_group("一")
    _, second = watchlist.create_group("二")
    _, third = watchlist.create_group("三")

    reordered = watchlist.reorder_groups([third["id"], first["id"], second["id"]])
    assert [group["name"] for group in reordered] == ["三", "一", "二"]
    assert [group["name"] for group in watchlist.list_groups()] == ["三", "一", "二"]

    # ids 与现有分组不一致 (缺失 / 多余 / 重复) 均拒绝
    with pytest.raises(ValueError, match="不一致"):
        watchlist.reorder_groups([first["id"], second["id"]])
    with pytest.raises(ValueError, match="不一致"):
        watchlist.reorder_groups([first["id"], second["id"], third["id"], "missing"])
    with pytest.raises(ValueError, match="不一致"):
        watchlist.reorder_groups([first["id"], first["id"], second["id"], third["id"]])
    # 失败请求不改变现有顺序
    assert [group["name"] for group in watchlist.list_groups()] == ["三", "一", "二"]


def test_reorder_groups_api(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _, first = watchlist.create_group("一")
    _, second = watchlist.create_group("二")

    result = watchlist_api.reorder_groups(
        watchlist_api.GroupReorderRequest(ordered_ids=[second["id"], first["id"]])
    )
    assert [group["name"] for group in result["groups"]] == ["二", "一"]

    with pytest.raises(HTTPException) as exc_info:
        watchlist_api.reorder_groups(
            watchlist_api.GroupReorderRequest(ordered_ids=["missing"])
        )
    assert exc_info.value.status_code == 400


def test_group_api_contract(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    request = _request()
    created = watchlist_api.create_group(
        watchlist_api.GroupNameRequest(name="中线", color="teal")
    )
    group_id = created["group"]["id"]
    assert created["group"]["color"] == "teal"

    added = watchlist_api.add_one(
        watchlist_api.AddRequest(symbol="600000.SH", group_id=group_id),
        request,
    )
    assert added["symbols"][0]["group_ids"] == [group_id]

    moved = watchlist_api.assign_group(
        "600000.SH",
        watchlist_api.GroupAssignRequest(group_id=None),
        request,
    )
    assert moved["symbols"][0]["group_ids"] == []

    with pytest.raises(HTTPException) as exc_info:
        watchlist_api.rename_group("missing", watchlist_api.GroupNameRequest(name="无效"))
    assert exc_info.value.status_code == 404


def test_historical_groups_default_to_sky(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    path = tmp_path / "user_data" / "watchlist_groups.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        '[{"id":"legacy","name":"旧分组"},'
        '{"id":"invalid","name":"未知颜色","color":"black"}]',
        encoding="utf-8",
    )

    assert watchlist.list_groups() == [
        {"id": "legacy", "name": "旧分组", "color": "sky"},
        {"id": "invalid", "name": "未知颜色", "color": "sky"},
    ]


def test_clear_group_moves_members_to_ungrouped(monkeypatch, tmp_path):
    """清空分组:成员变未分组,分组定义保留。"""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _, group = watchlist.create_group("芯片")
    watchlist.add("600000.SH", group_id=group["id"])
    watchlist.add("000001.SZ", group_id=group["id"])
    watchlist.add("300750.SZ")  # 不在任何分组

    rows = watchlist.clear_group(group["id"])
    # 3 只都还在,group_id 全部为 None
    assert len(rows) == 3
    assert all(r["group_ids"] == [] for r in rows)
    # 分组定义仍在
    assert any(g["id"] == group["id"] for g in watchlist.list_groups())

    # 清空不存在的分组 → KeyError
    with pytest.raises(KeyError):
        watchlist.clear_group("missing")


def test_clear_group_api(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    request = _request()
    created = watchlist_api.create_group(
        watchlist_api.GroupNameRequest(name="中线", color="teal")
    )
    group_id = created["group"]["id"]
    watchlist.add("600000.SH", group_id=group_id)
    watchlist.add("000001.SZ", group_id=group_id)

    result = watchlist_api.clear_group(group_id, request)
    assert all(s["group_ids"] == [] for s in result["symbols"])

    # 不存在的分组 → 404
    with pytest.raises(HTTPException) as exc:
        watchlist_api.clear_group("missing", request)
    assert exc.value.status_code == 404


# ── 多组成员关系 (M:N) ──────────────────────────────────────


def test_multi_group_membership(monkeypatch, tmp_path):
    """一股可同时属于多个分组; 移出一个不影响其他。"""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _, ga = watchlist.create_group("半导体")
    _, gb = watchlist.create_group("反包备选")
    watchlist.add("600118.SH", group_id=ga["id"])

    rows = watchlist.add_to_group("600118.SH", gb["id"])
    got = next(r for r in rows if r["symbol"] == "600118.SH")
    assert got["group_ids"] == [ga["id"], gb["id"]]      # 两组并存

    # 重复加入幂等
    rows = watchlist.add_to_group("600118.SH", gb["id"])
    got = next(r for r in rows if r["symbol"] == "600118.SH")
    assert got["group_ids"] == [ga["id"], gb["id"]]

    # 移出一个, 另一个保留
    rows = watchlist.remove_from_group("600118.SH", ga["id"])
    got = next(r for r in rows if r["symbol"] == "600118.SH")
    assert got["group_ids"] == [gb["id"]]

    # 移出最后一个 → 未分组(仍在自选)
    rows = watchlist.remove_from_group("600118.SH", gb["id"])
    got = next(r for r in rows if r["symbol"] == "600118.SH")
    assert got["group_ids"] == []
    assert any(r["symbol"] == "600118.SH" for r in rows)

    # 不存在的分组/标的
    with pytest.raises(ValueError, match="不存在"):
        watchlist.add_to_group("600118.SH", "missing")
    with pytest.raises(KeyError):
        watchlist.remove_from_group("000001.SZ", ga["id"])


def test_set_group_exclusive_keeps_only_one(monkeypatch, tmp_path):
    """互斥设定: 已在多组的标的被 set 后只保留指定组。"""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _, ga = watchlist.create_group("一")
    _, gb = watchlist.create_group("二")
    watchlist.add("600000.SH", group_id=ga["id"])
    watchlist.add_to_group("600000.SH", gb["id"])

    rows = watchlist.set_group("600000.SH", ga["id"])
    assert rows[0]["group_ids"] == [ga["id"]]


def test_clear_group_strips_only_that_group(monkeypatch, tmp_path):
    """清空分组只摘该组标签, 其他组成员关系保留。"""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _, ga = watchlist.create_group("一")
    _, gb = watchlist.create_group("二")
    watchlist.add("600000.SH", group_id=ga["id"])
    watchlist.add_to_group("600000.SH", gb["id"])
    watchlist.add("000001.SZ", group_id=ga["id"])

    rows = watchlist.clear_group(ga["id"])
    a = next(r for r in rows if r["symbol"] == "600000.SH")
    b = next(r for r in rows if r["symbol"] == "000001.SZ")
    assert a["group_ids"] == [gb["id"]]   # 二组保留
    assert b["group_ids"] == []


def test_legacy_single_group_id_migration(monkeypatch, tmp_path):
    """旧 schema(单值 group_id 列)读取迁移 + 首次写回前自动备份 .bak。"""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    path = tmp_path / "user_data" / "watchlist.parquet"
    path.parent.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600000.SH", "000001.SZ"],
        "added_at": ["2026-08-08T10:00:00"] * 2,
        "note": ["", ""],
        "group_id": ["g1", None],
    }).write_parquet(path)

    rows = watchlist.list_symbols()
    assert rows[0]["group_ids"] == ["g1"]
    assert rows[1]["group_ids"] == []
    assert not (tmp_path / "user_data" / "watchlist.parquet.bak").exists()  # 只读不备份

    # 触发写入 → 备份生成, 文件落新 schema
    watchlist.add("300750.SZ")
    assert (tmp_path / "user_data" / "watchlist.parquet.bak").exists()
    df = pl.read_parquet(path)
    assert "group_ids" in df.columns and "group_id" not in df.columns


def test_bulk_transfer_updates_membership_once_and_keeps_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    source, dest, other = [watchlist.create_group(n)[1]["id"] for n in ["关注", "观察", "持仓"]]
    watchlist.add_batch(["a", "b", "c"], note="保留备注", group_ids=[source, other])
    watchlist.add_to_group("b", dest)
    original = watchlist.list_symbols()
    rev = watchlist.revision()

    rows, changed = watchlist.transfer_members(source, dest, ["a", "b", "a"], "copy")
    assert changed == 1 and watchlist.revision() == rev + 1
    assert watchlist.transfer_members(source, dest, ["a", "b"], "copy") == (rows, 0)
    assert watchlist.revision() == rev + 1
    rows, changed = watchlist.transfer_members(source, dest, ["a", "b"], "move")
    assert changed == 2 and watchlist.revision() == rev + 2
    assert rows == [
        {**row, "group_ids": [other, dest]} if row["symbol"] in {"a", "b"} else row
        for row in original
    ]
    assert watchlist.list_symbols() == rows


def test_transfer_api_checks_whole_selection_and_virtual_groups(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    app = FastAPI()
    app.include_router(watchlist_api.router)
    app.state.repo = _request().app.state.repo
    src, dst = [watchlist.create_group(n)[1]["id"] for n in ["一", "二"]]
    watchlist.add("a", group_id=src)
    watchlist.add("b")
    with TestClient(app) as client:
        def post(group, target, symbols, action):
            return client.post(f"/api/watchlist/groups/{group}/transfer", json={
                "target_group_id": target, "symbols": symbols, "action": action,
            })

        before = watchlist.list_symbols()
        revision = watchlist.revision()
        for args in [
            (src, dst, ["a", "b"], "move"), (src, dst, ["a", "missing"], "copy"),
            (src, src, ["a"], "move"), (src, "missing", ["a"], "copy"),
            ("missing", dst, ["a"], "move"), ("all", dst, ["a"], "move"),
            ("ungrouped", dst, ["b", "a"], "move"), (src, dst, [], "copy"),
            (src, dst, ["a"], "invalid"),
        ]:
            assert 400 <= post(*args).status_code < 500
        assert watchlist.list_symbols() == before and watchlist.revision() == revision
        result = post("ungrouped", dst, ["b"], "move")
        assert result.status_code == 200 and result.json()["changed"] == 1
        result = post("all", dst, ["a", "b"], "copy")
        assert result.status_code == 200 and result.json()["changed"] == 1
        assert {r["symbol"]: r["group_ids"] for r in result.json()["symbols"]} == {"a": [src, dst], "b": [dst]}
