"""扩展数据定时拉取引擎 — 从外部 API 拉取数据写入 Parquet。"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import date, datetime, timezone
from functools import reduce
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

def _apply_preset_flatten(config_id: str, rows: list[dict]) -> list[dict]:
    """对内置预设 (概念/行业) 应用结构转换, 与 fetch_preset 保持一致。

    延迟导入避免与 ext_presets 形成循环依赖。
    非预设 id 原样返回。
    """
    if config_id not in ("ext_gn_ths", "ext_hy_ths"):
        return rows
    from app.services.ext_presets import _flatten_concept_rows, _flatten_industry_rows
    flatten = _flatten_concept_rows if config_id == "ext_gn_ths" else _flatten_industry_rows
    return flatten(rows)


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

    async with httpx.AsyncClient(timeout=30) as client:
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

    # 内置预设 (概念/行业): 应用结构转换, 让产出 schema 与分析页一致。
    # 否则 raw 接口列 (concepts/industries 数组、name) 会直接覆盖正确的 part.parquet,
    # 导致分析页因找不到维度字段 (所属概念/所属同花顺行业) 而"数据消失"。
    # 见 ext_presets._flatten_* —— 手动拉取 / 定时拉取都必须走同一套转换。
    rows = _apply_preset_flatten(config.id, rows)

    # 字段映射
    rows = _apply_field_map(rows, pull.field_map)

    # 校验可关联标的的字段：直接 symbol/code，或配置里声明的映射源列。
    row_keys = set(rows[0]) if rows else set()
    mapped_cols = {
        m.get("col")
        for m in (config.symbol_map or {}, config.code_map or {})
        if m.get("type") == "mapped" and m.get("col")
    }
    if rows and not ({"symbol", "code"} & row_keys or mapped_cols & row_keys):
        raise ValueError("数据行中缺少 symbol/code 字段，请配置字段映射或标的映射")

    # 写入
    snap = date.today()
    n = rows_to_parquet(rows, config, data_dir, snapshot_date=snap)
    return n, snap.isoformat()


# ---------------------------------------------------------------------------
# 调度器
# ---------------------------------------------------------------------------

class PullScheduler:
    """后台调度器：为每个启用了 pull 的 ExtConfig 维护定时任务。

    线程安全说明:
      refresh()/stop() 可能从主事件循环 (lifespan startup) 或同步路由的
      worker 线程 (configure_pull 是 def 而非 async def, FastAPI 丢进线程池)
      调用。worker 线程里没有 running loop, 直接 asyncio.create_task 会抛
      "no running event loop"。因此对 task 的增删一律通过
      call_soon_threadsafe 提交到主循环执行 —— 同一套代码两种调用场景都安全。
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self, data_dir) -> None:
        """启动调度（在 lifespan startup 调用，主事件循环内）。"""
        self._running = True
        self._data_dir = data_dir
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        logger.info("PullScheduler started")

    def _submit(self, fn, *args) -> None:
        """把一个 callable 提交到主事件循环执行 (线程安全)。

        startup 在主循环内调用时 fn 立即排队; worker 线程调用时跨线程排队。
        两者都通过 call_soon_threadsafe, 保证 _tasks 字典的读写只在主循环里发生。
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            raise RuntimeError(
                "PullScheduler: 事件循环不可用 (start() 未在事件循环中调用?)"
            )
        loop.call_soon_threadsafe(fn, *args)

    def stop(self) -> None:
        """停止所有任务 (从 shutdown 调用)。"""
        self._running = False
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()
        logger.info("PullScheduler stopped")

    def refresh(self, data_dir) -> None:
        """重新加载配置，更新调度任务（增/删/改）。线程安全。"""
        self._data_dir = data_dir
        store = ExtConfigStore(data_dir)
        configs = store.load_all()

        active_ids: set[str] = set()
        new_configs: list[ExtConfig] = []

        for config in configs:
            if not config.pull or not config.pull.enabled or not config.pull.url:
                continue
            active_ids.add(config.id)
            if config.id not in self._tasks:
                new_configs.append(config)

        # 需要移除的 id (快照当前 task 字典的键, 避免遍历时改字典)
        remove_ids = [cid for cid in list(self._tasks) if cid not in active_ids]

        # 所有对 _tasks 的修改都提交到主循环里执行, 保证线程安全
        def _apply() -> None:
            for config in new_configs:
                if config.id not in self._tasks:  # 二次校验, 防重复
                    self._tasks[config.id] = self._loop.create_task(
                        self._run_loop(config)
                    )
                    logger.info(
                        "PullScheduler: scheduled %s (every %d min)",
                        config.id, config.pull.schedule_minutes,
                    )
            for cid in remove_ids:
                task = self._tasks.pop(cid, None)
                if task is not None:
                    task.cancel()
                    logger.info("PullScheduler: removed %s", cid)

        self._submit(_apply)

    async def _run_loop(self, config: ExtConfig) -> None:
        """单个配置的定时拉取循环。

        策略: 启用后立即执行一次, 之后按 interval 循环。
        每次循环重读最新配置 (fresh), interval 取自 fresh.pull.schedule_minutes,
        这样用户中途修改间隔也能立即生效 (无需重启)。
        """
        try:
            while self._running:
                # 每轮重读最新配置 — 用户可能修改了 url / interval / enabled
                store = ExtConfigStore(self._data_dir)
                fresh = store.get(config.id)
                if not fresh or not fresh.pull or not fresh.pull.enabled:
                    break
                pull = fresh.pull

                # 先执行一次 (启用即拉取, 让用户立刻看到生效)
                try:
                    n, d = await fetch_and_ingest(fresh, self._data_dir)
                    fresh.pull.last_run = datetime.now(timezone.utc).isoformat()
                    fresh.pull.last_status = "success"
                    fresh.pull.last_message = f"{n} rows @ {d}"
                    fresh.pull.last_rows = n
                    store.upsert(fresh)
                    logger.info("PullScheduler: %s success, %d rows", config.id, n)
                except Exception as e:
                    fresh2 = store.get(config.id)
                    if fresh2 and fresh2.pull:
                        fresh2.pull.last_run = datetime.now(timezone.utc).isoformat()
                        fresh2.pull.last_status = "error"
                        fresh2.pull.last_message = str(e)[:200]
                        store.upsert(fresh2)
                    logger.warning("PullScheduler: %s error: %s", config.id, e)

                # 间隔取自最新配置 (每次重新读取, 修复改间隔不生效)
                interval = max(pull.schedule_minutes * 60, 60)  # 至少 60s
                # 预告下次运行时间, 供前端展示
                next_dt = datetime.now(timezone.utc).timestamp() + interval
                latest = store.get(config.id)
                if latest and latest.pull:
                    latest.pull.next_run = datetime.fromtimestamp(
                        next_dt, tz=timezone.utc
                    ).isoformat()
                    store.upsert(latest)

                await asyncio.sleep(interval)
                if not self._running:
                    break
        except asyncio.CancelledError:
            pass


# 全局单例
pull_scheduler = PullScheduler()
