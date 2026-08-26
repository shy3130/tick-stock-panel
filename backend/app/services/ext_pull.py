"""扩展数据定时拉取引擎 — 从外部 API 拉取数据写入 Parquet。"""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import date, datetime, timezone
from typing import Any

import httpx

from app.services.ext_data import (
    ExtConfig,
    ExtConfigStore,
    rows_to_parquet,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------

def _extract_rows(data: Any, path: str) -> list[dict]:
    """按 dot-path 从 JSON 响应中提取行数组。

    例: path="data.list" → response["data"]["list"]
    如果 path 为空，直接将 data 视为数组。
    """
    if not path:
        if isinstance(data, list):
            return data
        raise ValueError("response_path 为空但响应不是数组")

    keys = path.split(".")
    current = data
    for key in keys:
        if isinstance(current, dict):
            if key not in current:
                raise ValueError(f"响应中不存在路径 '{path}'，缺失键 '{key}'")
            current = current[key]
        elif isinstance(current, list):
            try:
                current = current[int(key)]
            except (ValueError, IndexError) as e:
                raise ValueError(f"响应路径 '{path}' 解析失败: {e}") from e
        else:
            raise ValueError(f"响应路径 '{path}' 中间值不是 dict/list: {type(current)}")

    if not isinstance(current, list):
        raise ValueError(f"路径 '{path}' 指向的不是数组，而是 {type(current)}")

    return current


def _apply_field_map(rows: list[dict], field_map: dict[str, str]) -> list[dict]:
    """将外部字段名映射为内部配置字段名。field_map: {外部名: 内部名}。"""
    if not field_map:
        return rows
    mapped = []
    for row in rows:
        new_row: dict = {}
        for k, v in row.items():
            mapped_key = field_map.get(k, k)
            new_row[mapped_key] = v
        mapped.append(new_row)
    return mapped


# ---------------------------------------------------------------------------
# 拉取执行
# ---------------------------------------------------------------------------

async def fetch_and_ingest(
    config: ExtConfig,
    data_dir,
) -> tuple[int, str]:
    """执行一次拉取: 请求外部 API → 解析响应 → 写入 Parquet。

    Returns:
        (rows_written, date_str)
    """
    pull = config.pull
    if not pull or not pull.url:
        raise ValueError("拉取未配置或 URL 为空")

    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        headers = pull.headers or {}
        kwargs: dict[str, Any] = {"headers": headers}

        if pull.method.upper() == "POST" and pull.body:
            kwargs["content"] = pull.body
            if "content-type" not in {k.lower() for k in headers}:
                kwargs["headers"]["Content-Type"] = "application/json"

        resp = await client.request(pull.method.upper(), pull.url, **kwargs)
        resp.raise_for_status()

    # 解析 JSON
    try:
        data = resp.json()
    except Exception as e:
        raise ValueError(f"响应不是有效 JSON: {e}") from e

    # 提取行
    rows = _extract_rows(data, pull.response_path)
    if not rows:
        raise ValueError("提取到的行数为 0")

    # 字段映射
    rows = _apply_field_map(rows, pull.field_map)

    # 校验 symbol 列
    if rows and "symbol" not in rows[0]:
        raise ValueError("数据行中缺少 symbol 字段，请配置 field_map 映射")

    # 写入
    snap = date.today()
    n = rows_to_parquet(rows, config, data_dir, snapshot_date=snap)
    return n, snap.isoformat()


# ---------------------------------------------------------------------------
# 调度器
# ---------------------------------------------------------------------------

class PullScheduler:
    """后台调度器：为每个启用了 pull 的 ExtConfig 维护定时任务。"""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._running = False
        self._lock = threading.Lock()

    def start(self, data_dir) -> None:
        """启动调度（在 lifespan startup 调用）。"""
        self._running = True
        self._data_dir = data_dir
        logger.info("PullScheduler started")

    def stop(self) -> None:
        """停止所有任务。"""
        self._running = False
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()
        logger.info("PullScheduler stopped")

    def refresh(self, data_dir) -> None:
        """重新加载配置，更新调度任务（增/删/改）。"""
        self._data_dir = data_dir
        store = ExtConfigStore(data_dir)
        configs = store.load_all()

        active_ids: set[str] = set()

        for config in configs:
            if not config.pull or not config.pull.enabled or not config.pull.url:
                continue
            active_ids.add(config.id)
            if config.id not in self._tasks:
                # 新增调度
                task = asyncio.create_task(self._run_loop(config))
                self._tasks[config.id] = task
                logger.info("PullScheduler: scheduled %s (every %d min)", config.id, config.pull.schedule_minutes)

        # 移除不再活跃的
        for cid in list(self._tasks):
            if cid not in active_ids:
                self._tasks[cid].cancel()
                del self._tasks[cid]
                logger.info("PullScheduler: removed %s", cid)

    async def _run_loop(self, config: ExtConfig) -> None:
        """单个配置的定时拉取循环。"""
        try:
            while self._running:
                pull = config.pull
                if not pull:
                    break
                interval = max(pull.schedule_minutes * 60, 60)  # 至少 60s
                await asyncio.sleep(interval)
                if not self._running:
                    break
                try:
                    # 重新加载最新配置（用户可能中途修改）
                    store = ExtConfigStore(self._data_dir)
                    fresh = store.get(config.id)
                    if not fresh or not fresh.pull or not fresh.pull.enabled:
                        break
                    n, d = await fetch_and_ingest(fresh, self._data_dir)
                    fresh.pull.last_run = datetime.now(timezone.utc).isoformat()
                    fresh.pull.last_status = "success"
                    fresh.pull.last_message = f"{n} rows @ {d}"
                    fresh.pull.last_rows = n
                    store.upsert(fresh)
                    logger.info("PullScheduler: %s success, %d rows", config.id, n)
                except Exception as e:
                    store = ExtConfigStore(self._data_dir)
                    fresh = store.get(config.id)
                    if fresh and fresh.pull:
                        fresh.pull.last_run = datetime.now(timezone.utc).isoformat()
                        fresh.pull.last_status = "error"
                        fresh.pull.last_message = str(e)[:200]
                        store.upsert(fresh)
                    logger.warning("PullScheduler: %s error: %s", config.id, e)
        except asyncio.CancelledError:
            pass


# 全局单例
pull_scheduler = PullScheduler()
