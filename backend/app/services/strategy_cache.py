"""策略结果缓存 — 写入本地文件，供策略页面秒加载。

缓存结构:
  {
    "as_of": "2024-01-15",
    "results": { strategy_id: { total, as_of, rows } },
    "today_ever_matched": { strategy_id: [symbol, ...] },    // 今日曾命中 symbol 并集
    "today_ever_rows": { strategy_id: { symbol: row_data } },// 今日曾命中的完整行数据
    "updated_at": 1705324800000  # Unix ms
  }

文件路径: data/user_data/strategy_cache.json
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any


def _json_default(obj: Any) -> Any:
    """处理 date/datetime 等 JSON 不认识的类型。"""
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


logger = logging.getLogger(__name__)

_CACHE_FILENAME = "strategy_cache.json"

# 读写同一 JSON 文件的进程内锁: write_cache 的 read-modify-write 与并发 read_cache
# 无锁会丢更新/读到半写文件。read_cache 与 write_cache 共用此锁; write 内部复用
# _read_cache_unlocked 避免自死锁。写入用临时文件 + os.replace 做到原子替换。
_file_lock = threading.Lock()


def _cache_path(data_dir: Path) -> Path:
    return data_dir / "user_data" / _CACHE_FILENAME


def _enriched_parquet_path(data_dir: Path, as_of: str) -> Path:
    """返回 enriched parquet 文件路径。"""
    return data_dir / "kline_daily_enriched" / f"date={as_of}" / "part.parquet"


def _get_enriched_mtime(data_dir: Path, as_of: str) -> float | None:
    """返回 enriched parquet 文件的 mtime (秒)。文件不存在返回 None。"""
    p = _enriched_parquet_path(data_dir, as_of)
    try:
        return p.stat().st_mtime
    except FileNotFoundError:
        return None


def read_cache(data_dir: Path) -> dict | None:
    """读取策略缓存文件。返回 None 表示无缓存或读取失败。

    说明: 原先有 enriched mtime 过期校验 (数据文件变化 → 判过期返回 None),
    但在有实时行情的系统里, enriched parquet 每轮被刷新 → mtime 必然变化 →
    缓存被永久判死, 策略页读不到数据。且判过期后不触发重算, 只能让用户手动重跑,
    保护价值有限。故移除: 盘后缓存总能读出, 实时新鲜度由 /api/screener/cached
    端点叠加监控引擎的内存实时结果 (latest_strategy_results) 来保证。
    """
    with _file_lock:
        return _read_cache_unlocked(data_dir)


def _read_cache_unlocked(data_dir: Path) -> dict | None:
    """实际读取逻辑 (不持锁)。供 read_cache 与 write_cache 复用, 避免重入死锁。"""
    path = _cache_path(data_dir)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return None
        cached = json.loads(text)
    except Exception as e:  # noqa: BLE001
        logger.warning("读取策略缓存失败: %s", e)
        return None

    return cached


def _rows_to_symbol_map(rows: list[dict]) -> dict[str, dict]:
    """将 rows 列表转为 {symbol: row_data} 映射。"""
    result: dict[str, dict] = {}
    for row in rows:
        sym = row.get("symbol")
        if sym:
            result[sym] = row
    return result


def write_cache(
    data_dir: Path,
    as_of: str,
    results: dict[str, Any],
) -> None:
    """将策略结果写入缓存文件，同时更新今日曾命中集合。

    - 日期变更时重置 today_ever_matched 和 today_ever_rows
    - 同一天内合并 (并集) 之前曾命中的 symbol，并用最新行数据更新
    """
    path = _cache_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    # 整个 read-modify-write 持锁: 避免并发 write 丢更新, 也避免与 read_cache 撕裂
    with _file_lock:
        _write_cache_locked(path, data_dir, as_of, results)


def _write_cache_locked(
    path: Path,
    data_dir: Path,
    as_of: str,
    results: dict[str, Any],
) -> None:
    """持 _file_lock 后的实际写入逻辑 (read-merge-write + 原子替换)。"""
    # 读取旧缓存 (已持锁, 走不重入的 _read_cache_unlocked)
    old = _read_cache_unlocked(data_dir)
    old_as_of = old.get("as_of") if old else None
    old_ever_rows: dict[str, dict[str, dict]] = old.get("today_ever_rows", {}) if old else {}

    # 当前命中的行数据 → symbol 映射
    current_row_maps: dict[str, dict[str, dict]] = {}
    for sid, r in results.items():
        current_row_maps[sid] = _rows_to_symbol_map(r.get("rows", []))

    if old_as_of and old_as_of == as_of and old_ever_rows:
        # 同一天: 合并 — 用当前行数据更新旧数据 (保持最新价格等)
        merged_rows: dict[str, dict[str, dict]] = {}
        all_keys = set(old_ever_rows.keys()) | set(current_row_maps.keys())
        for sid in all_keys:
            old_map = old_ever_rows.get(sid, {})
            cur_map = current_row_maps.get(sid, {})
            # 以旧数据为基础，用当前数据覆盖 (当前数据更新鲜)
            combined = {**old_map, **cur_map}
            merged_rows[sid] = combined
        today_ever_rows = merged_rows
    else:
        # 新的一天或首次写入
        today_ever_rows = current_row_maps

    # 从 ever_rows 提取 symbol 列表 (用于快速计数)
    today_ever_matched = {sid: sorted(maps.keys()) for sid, maps in today_ever_rows.items()}

    # enriched_mtime: 盘后缓存写入时记录 (向后兼容旧字段)。read_cache 已不再用它
    # 做过期校验, 实时新鲜度改由 /cached 端点叠加监控引擎内存结果保证。
    enriched_mtime = _get_enriched_mtime(data_dir, as_of)

    payload = {
        "as_of": as_of,
        "results": results,
        "today_ever_matched": today_ever_matched,
        "today_ever_rows": today_ever_rows,
        "enriched_mtime": enriched_mtime,
        "updated_at": int(time.time() * 1000),
    }
    try:
        # 原子写: 先写临时文件再 os.replace, 避免读侧读到半写的 JSON
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, default=_json_default), encoding="utf-8")
        os.replace(tmp, path)
        total_rows = sum(len(r.get("rows", [])) for r in results.values())
        total_ever = sum(len(v) for v in today_ever_matched.values())
        logger.info("策略缓存已写入: %s, %d 策略, %d 命中, %d 曾命中", as_of, len(results), total_rows, total_ever)
    except Exception as e:  # noqa: BLE001
        logger.warning("写入策略缓存失败: %s", e)
