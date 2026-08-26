"""自选分组 (M:N) 持久化、迁移兼容与并发安全测试。

覆盖:
- 旧无组列文件只读映射为空 (不产生写盘);
- legacy 单值 group_id 列只读映射为 group_ids;
- 第一次实际写入前备份一次 watchlist.parquet.bak, clean-cutover 写 4 列 schema;
- M:N membership (加入/移出幂等, 一只标的同时属多组);
- 删除/清空分组只摘标签, 绝不删除自选标的;
- 组 CRUD 校验 (名称 trim 1..24、同名 409 语义、颜色白名单、id 规范);
- 并发 RMW (多线程 add/移组不丢更新);
- API 路由契约 (创建/重命名冲突/404/成员端点/清空)。
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import watchlist as watchlist_api
from app.services import watchlist


def _legacy_df(with_group_id: bool) -> pl.DataFrame:
    cols = {
        "symbol": ["600000.SH", "000001.SZ"],
        "added_at": ["2026-08-08T10:00:00", "2026-08-09T11:00:00"],
        "note": ["", ""],
    }
    if with_group_id:
        cols["group_id"] = ["g_legacy", None]
    return pl.DataFrame(cols)


def _write_legacy(tmp_path: Path, with_group_id: bool) -> Path:
    p = tmp_path / "user_data" / "watchlist.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    _legacy_df(with_group_id).write_parquet(p)
    return p


# ── 读兼容: 旧 schema ──────────────────────────────────
def test_historical_no_group_column_reads_as_empty(tmp_path: Path):
    p = _write_legacy(tmp_path, with_group_id=False)
    rows = watchlist.list_symbols(tmp_path)
    assert [r["symbol"] for r in rows] == ["600000.SH", "000001.SZ"]
    assert all(r["group_ids"] == [] for r in rows)
    # 只读映射不产生写盘 (mtime 不变)
    assert "group_ids" not in pl.read_parquet_schema(p).names()


def test_legacy_group_id_maps_to_group_ids(tmp_path: Path):
    p = _write_legacy(tmp_path, with_group_id=True)
    rows = watchlist.list_symbols(tmp_path)
    by_symbol = {r["symbol"]: r["group_ids"] for r in rows}
    assert by_symbol["600000.SH"] == ["g_legacy"]
    assert by_symbol["000001.SZ"] == []
    # 只读映射不产生写盘
    assert "group_ids" not in pl.read_parquet_schema(p).names()


# ── 首次写迁移: 备份 + clean-cutover ───────────────────
def test_first_write_backs_up_and_cuts_over(tmp_path: Path):
    p = _write_legacy(tmp_path, with_group_id=True)
    bak = p.with_suffix(p.suffix + ".bak")
    assert not bak.exists()

    rows = watchlist.add("300750.SZ", data_dir=tmp_path)

    # 备份文件 = 旧 schema 原文
    assert bak.exists()
    assert "group_ids" not in pl.read_parquet_schema(bak).names()
    # 新文件为 canonical 4 列 schema, legacy group_id 列被删, 值迁移
    names = pl.read_parquet_schema(p).names()
    assert "group_ids" in names and "group_id" not in names
    by_symbol = {r["symbol"]: r["group_ids"] for r in rows}
    assert by_symbol["600000.SH"] == ["g_legacy"]
    assert "300750.SZ" in by_symbol


def test_backup_only_once(tmp_path: Path):
    p = _write_legacy(tmp_path, with_group_id=False)
    bak = p.with_suffix(p.suffix + ".bak")
    watchlist.add("600000.SH", data_dir=tmp_path)
    first_bak_mtime = bak.stat().st_mtime_ns
    watchlist.add("000001.SZ", data_dir=tmp_path)
    # 第二次写不再覆盖备份
    assert bak.stat().st_mtime_ns == first_bak_mtime


def test_backup_failure_aborts_migration_without_partial_backup(
    tmp_path: Path,
    monkeypatch,
):
    path = _write_legacy(tmp_path, with_group_id=True)
    original = path.read_bytes()
    backup = path.with_suffix(path.suffix + ".bak")
    pending = backup.with_suffix(backup.suffix + ".tmp")

    def fail_copy(_source, target):
        Path(target).write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(watchlist.shutil, "copy2", fail_copy)

    with pytest.raises(OSError, match="disk full"):
        watchlist.add("300750.SZ", data_dir=tmp_path)

    assert path.read_bytes() == original
    assert not backup.exists()
    assert not pending.exists()
    assert "group_ids" not in pl.read_parquet_schema(path).names()


# ── M:N membership ────────────────────────────────────
def test_many_to_many_membership_and_idempotency(tmp_path: Path):
    _, g1 = watchlist.create_group("短线", data_dir=tmp_path)
    _, g2 = watchlist.create_group("中线", data_dir=tmp_path)
    watchlist.add("600000.SH", data_dir=tmp_path)

    rows = watchlist.add_to_group("600000.SH", g1["id"], data_dir=tmp_path)
    rows = watchlist.add_to_group("600000.SH", g2["id"], data_dir=tmp_path)
    assert rows[0]["group_ids"] == [g1["id"], g2["id"]]

    # 重复加入幂等
    rows = watchlist.add_to_group("600000.SH", g1["id"], data_dir=tmp_path)
    assert rows[0]["group_ids"] == [g1["id"], g2["id"]]

    # 移出一组后另一组保留; 重复移出幂等
    rows = watchlist.remove_from_group("600000.SH", g1["id"], data_dir=tmp_path)
    assert rows[0]["group_ids"] == [g2["id"]]
    rows = watchlist.remove_from_group("600000.SH", g1["id"], data_dir=tmp_path)
    assert rows[0]["group_ids"] == [g2["id"]]

    # 不存在的组 / 不存在的标的
    with pytest.raises(watchlist.WatchlistGroupError):
        watchlist.add_to_group("600000.SH", "ghost", data_dir=tmp_path)
    with pytest.raises(KeyError):
        watchlist.add_to_group("999999.SZ", g1["id"], data_dir=tmp_path)


def test_delete_and_clear_group_keep_symbols(tmp_path: Path):
    _, g = watchlist.create_group("核心", data_dir=tmp_path)
    watchlist.add("600000.SH", group_id=g["id"], data_dir=tmp_path)
    watchlist.add("000001.SZ", data_dir=tmp_path)

    # 清空组: 摘标签保留定义
    rows = watchlist.clear_group(g["id"], data_dir=tmp_path)
    assert {r["symbol"] for r in rows} == {"600000.SH", "000001.SZ"}
    assert all(r["group_ids"] == [] for r in rows)
    assert len(watchlist.list_groups(tmp_path)) == 1

    # 重新入组后删除组: 摘标签且删定义, 标的保留
    watchlist.add_to_group("600000.SH", g["id"], data_dir=tmp_path)
    remaining, rows = watchlist.delete_group(g["id"], data_dir=tmp_path)
    assert remaining == []
    assert {r["symbol"] for r in rows} == {"600000.SH", "000001.SZ"}
    assert all(r["group_ids"] == [] for r in rows)


# ── 组 CRUD 校验 ──────────────────────────────────────
def test_group_crud_validation(tmp_path: Path):
    # 名称 trim + 长度
    groups, g = watchlist.create_group("  短线  ", data_dir=tmp_path)
    assert g["name"] == "短线"
    assert re.fullmatch(r"[a-z0-9_]+", g["id"])
    with pytest.raises(watchlist.WatchlistGroupError):
        watchlist.create_group("   ", data_dir=tmp_path)
    with pytest.raises(watchlist.WatchlistGroupError):
        watchlist.create_group("x" * 25, data_dir=tmp_path)

    # 同名拒绝 (大小写不敏感)
    with pytest.raises(watchlist.DuplicateGroupNameError):
        watchlist.create_group("短线", data_dir=tmp_path)
    # 大小写不敏感的同名拒绝
    _, _g_en = watchlist.create_group("CORE", data_dir=tmp_path)
    with pytest.raises(watchlist.DuplicateGroupNameError):
        watchlist.create_group("core", data_dir=tmp_path)

    # 颜色白名单
    with pytest.raises(watchlist.WatchlistGroupError):
        watchlist.create_group("观察", "black", data_dir=tmp_path)
    _, g2 = watchlist.create_group("观察", "teal", data_dir=tmp_path)
    assert g2["color"] == "teal"

    # rename + color
    renamed = watchlist.rename_group(g["id"], "打板", "rose", data_dir=tmp_path)
    assert renamed[0]["name"] == "打板" and renamed[0]["color"] == "rose"
    with pytest.raises(KeyError):
        watchlist.rename_group("ghost", "x", data_dir=tmp_path)

    # reorder (当前有 打板/CORE/观察 三组)
    reordered = watchlist.reorder_groups([g2["id"], g["id"], _g_en["id"]], data_dir=tmp_path)
    assert [x["name"] for x in reordered] == ["观察", "打板", "CORE"]
    with pytest.raises(watchlist.WatchlistGroupError):
        watchlist.reorder_groups([g["id"]], data_dir=tmp_path)


def test_add_with_group_id_keeps_existing_groups(tmp_path: Path):
    _, g1 = watchlist.create_group("A", data_dir=tmp_path)
    _, g2 = watchlist.create_group("B", data_dir=tmp_path)
    watchlist.add("600000.SH", group_id=g1["id"], data_dir=tmp_path)
    # 重复添加带新 group_id: 保留旧组并入新组
    rows = watchlist.add("600000.SH", group_id=g2["id"], data_dir=tmp_path)
    assert rows[0]["group_ids"] == [g1["id"], g2["id"]]
    # 不存在的初始组拒绝
    with pytest.raises(watchlist.WatchlistGroupError):
        watchlist.add("000001.SZ", group_id="ghost", data_dir=tmp_path)


# ── 并发 RMW ─────────────────────────────────────────
def test_concurrent_adds_and_group_ops_no_lost_update(tmp_path: Path):
    _, g = watchlist.create_group("并发", data_dir=tmp_path)
    n = 24
    symbols = [f"600{i:03d}.SH" for i in range(n)]

    errors: list[Exception] = []

    def worker(sym: str) -> None:
        try:
            watchlist.add(sym, group_id=g["id"], data_dir=tmp_path)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(s,)) for s in symbols]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    rows = watchlist.list_symbols(tmp_path)
    assert {r["symbol"] for r in rows} == set(symbols)
    # 每只都在组里 (无丢更新)
    assert all(r["group_ids"] == [g["id"]] for r in rows)


def test_concurrent_membership_flip_flops(tmp_path: Path):
    _, g1 = watchlist.create_group("一", data_dir=tmp_path)
    _, g2 = watchlist.create_group("二", data_dir=tmp_path)
    watchlist.add("600000.SH", data_dir=tmp_path)
    barrier = threading.Barrier(2)

    def flip(gid: str) -> None:
        barrier.wait()
        for _ in range(20):
            watchlist.add_to_group("600000.SH", gid, data_dir=tmp_path)
            watchlist.remove_from_group("600000.SH", gid, data_dir=tmp_path)

    threads = [
        threading.Thread(target=flip, args=(g1["id"],)),
        threading.Thread(target=flip, args=(g2["id"],)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows = watchlist.list_symbols(tmp_path)
    assert len(rows) == 1
    # 最终态: 两个组都不含 (flip 结束于 remove); 文件可正常读取无损坏
    assert rows[0]["group_ids"] == []


# ── API 路由契约 ──────────────────────────────────────
def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.state.repo = MagicMock()
    app.state.repo.store = SimpleNamespace(data_dir=tmp_path)
    app.include_router(watchlist_api.router)
    return TestClient(app)


def test_group_api_crud_contract(tmp_path: Path):
    client = _client(tmp_path)

    # 创建
    resp = client.post("/api/watchlist/groups", json={"name": "短线", "color": "orange"})
    assert resp.status_code == 200
    group = resp.json()["group"]
    assert group["color"] == "orange"

    # 同名 → 409
    resp = client.post("/api/watchlist/groups", json={"name": "短线"})
    assert resp.status_code == 409

    # 非法颜色/名称 → 400
    assert (
        client.post("/api/watchlist/groups", json={"name": "x", "color": "black"}).status_code
        == 400
    )
    assert client.post("/api/watchlist/groups", json={"name": "   "}).status_code == 400

    # 改名 + 换色
    resp = client.put(
        f"/api/watchlist/groups/{group['id']}", json={"name": "超短", "color": "rose"}
    )
    assert resp.status_code == 200
    assert resp.json()["groups"][0]["name"] == "超短"

    # 404
    assert client.put("/api/watchlist/groups/ghost", json={"name": "x"}).status_code == 404
    assert client.delete("/api/watchlist/groups/ghost").status_code == 404
    assert client.post("/api/watchlist/groups/ghost/clear").status_code == 404

    # 成员端点: 加自选 → 入组 → 幂等 → 移出
    assert client.post("/api/watchlist", json={"symbol": "600000.SH"}).status_code == 200
    resp = client.post(f"/api/watchlist/groups/{group['id']}/members/600000.SH")
    assert resp.status_code == 200
    assert resp.json()["symbols"][0]["group_ids"] == [group["id"]]
    # 幂等重复
    resp = client.post(f"/api/watchlist/groups/{group['id']}/members/600000.SH")
    assert resp.status_code == 200
    assert resp.json()["symbols"][0]["group_ids"] == [group["id"]]
    # 不存在的标的 → 404
    assert client.post(f"/api/watchlist/groups/{group['id']}/members/999999.SZ").status_code == 404
    # 移出
    resp = client.delete(f"/api/watchlist/groups/{group['id']}/members/600000.SH")
    assert resp.status_code == 200
    assert resp.json()["symbols"][0]["group_ids"] == []
    # 标的仍在自选
    assert any(r["symbol"] == "600000.SH" for r in client.get("/api/watchlist").json()["symbols"])

    # 清空组: 标的保留
    client.post(f"/api/watchlist/groups/{group['id']}/members/600000.SH")
    resp = client.post(f"/api/watchlist/groups/{group['id']}/clear")
    assert resp.status_code == 200
    assert resp.json()["symbols"][0]["group_ids"] == []

    # 删除组: 标的保留
    client.post(f"/api/watchlist/groups/{group['id']}/members/600000.SH")
    resp = client.delete(f"/api/watchlist/groups/{group['id']}")
    assert resp.status_code == 200
    assert any(r["symbol"] == "600000.SH" for r in resp.json()["symbols"])


def test_group_api_reorder(tmp_path: Path):
    client = _client(tmp_path)
    ids = []
    for name in ("一", "二", "三"):
        ids.append(client.post("/api/watchlist/groups", json={"name": name}).json()["group"]["id"])

    resp = client.put(
        "/api/watchlist/groups/reorder", json={"ordered_ids": [ids[2], ids[0], ids[1]]}
    )
    assert resp.status_code == 200
    assert [g["name"] for g in resp.json()["groups"]] == ["三", "一", "二"]
    assert (
        client.put("/api/watchlist/groups/reorder", json={"ordered_ids": [ids[0]]}).status_code
        == 400
    )


def test_watchlist_add_with_group_id_via_api(tmp_path: Path):
    client = _client(tmp_path)
    gid = client.post("/api/watchlist/groups", json={"name": "核心"}).json()["group"]["id"]
    # 搜索新增 + 指定组 (自选页选中组标签时自动入组)
    resp = client.post("/api/watchlist", json={"symbol": "600000.SH", "group_id": gid})
    assert resp.status_code == 200
    assert resp.json()["symbols"][0]["group_ids"] == [gid]
    # 不存在的组 → 400
    assert (
        client.post("/api/watchlist", json={"symbol": "000001.SZ", "group_id": "ghost"}).status_code
        == 400
    )
