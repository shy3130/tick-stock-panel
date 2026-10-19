"""strategy_cache 的进程内内存镜像 (轮询瘦身)。

/cached-summary 前端每 2s 轮询一次, 无镜像时每次都读 13MB JSON 再解析 (50-100ms)。
镜像契约:
- read_cache 在 (st_mtime_ns, st_size) 指纹未变时返回共享对象 (is 同一对象), 调用方只读;
- 外部改写文件 → 指纹变 → 重读新内容, 不返回陈旧镜像;
- write_cache 写入成功直填镜像: 写后读不触发重解析 (read-modify-write 也走镜像);
- clear_cache / 文件消失 → 镜像条目一并清除。
"""
from __future__ import annotations

import json
import os

from app.services import strategy_cache


def _result(*symbols: str) -> dict:
    return {
        "total": len(symbols),
        "as_of": "2026-10-19",
        "rows": [{"symbol": s} for s in symbols],
    }


def _spy_json_loads(monkeypatch) -> dict:
    """统计 JSON 解析次数 — 镜像命中时不应发生解析。"""
    calls = {"n": 0}
    real = strategy_cache.json.loads

    def counting(text):
        calls["n"] += 1
        return real(text)

    monkeypatch.setattr(strategy_cache.json, "loads", counting)
    return calls


def test_read_returns_shared_mirror_object_without_reparse(tmp_path, monkeypatch):
    strategy_cache.write_cache(tmp_path, "2026-10-19", {"s1": _result("000001.SZ")})
    calls = _spy_json_loads(monkeypatch)

    first = strategy_cache.read_cache(tmp_path)
    second = strategy_cache.read_cache(tmp_path)

    assert first is second  # 镜像命中: 同一对象
    assert first["results"]["s1"]["rows"][0]["symbol"] == "000001.SZ"
    assert calls["n"] == 0  # write_cache 已直填镜像, 读零解析


def test_second_write_merges_via_mirror_then_reads_fresh(tmp_path, monkeypatch):
    strategy_cache.write_cache(tmp_path, "2026-10-19", {"s1": _result("000001.SZ")})
    calls = _spy_json_loads(monkeypatch)

    strategy_cache.write_cache(tmp_path, "2026-10-19", {"s2": _result("600000.SH")})
    cached = strategy_cache.read_cache(tmp_path)

    assert set(cached["results"]) == {"s1", "s2"}
    assert calls["n"] == 0  # read-modify-write 的读与随后的读全走镜像


def test_external_rewrite_invalidates_mirror(tmp_path):
    strategy_cache.write_cache(tmp_path, "2026-10-19", {"s1": _result("000001.SZ")})
    assert set(strategy_cache.read_cache(tmp_path)["results"]) == {"s1"}

    # 模拟外部进程改写缓存文件 (绕过 write_cache), 并显式改 mtime 规避
    # 文件系统时间戳分辨率导致的指纹碰撞
    path = tmp_path / "user_data" / "strategy_cache.json"
    external = {
        "as_of": "2026-10-19",
        "results": {"ext": _result("300750.SZ")},
        "today_ever_matched": {},
        "today_ever_rows": {},
        "updated_at": 1,
    }
    path.write_text(json.dumps(external), encoding="utf-8")
    os.utime(path, ns=(10**9, 10**9))

    cached = strategy_cache.read_cache(tmp_path)
    assert set(cached["results"]) == {"ext"}  # 读到磁盘新内容, 非陈旧镜像


def test_clear_cache_drops_mirror(tmp_path):
    strategy_cache.write_cache(tmp_path, "2026-10-19", {"s1": _result("000001.SZ")})
    assert strategy_cache.read_cache(tmp_path) is not None

    strategy_cache.clear_cache(tmp_path)
    assert strategy_cache.read_cache(tmp_path) is None


def test_file_deleted_externally_mirror_not_leaked(tmp_path):
    strategy_cache.write_cache(tmp_path, "2026-10-19", {"s1": _result("000001.SZ")})
    assert strategy_cache.read_cache(tmp_path) is not None

    # 外部直接删文件 (非 clear_cache): stat 失败路径须清镜像并返回 None
    (tmp_path / "user_data" / "strategy_cache.json").unlink()
    assert strategy_cache.read_cache(tmp_path) is None
