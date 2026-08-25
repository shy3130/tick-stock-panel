"""Screener 服务(§6.3)。

性能优化:
  - enriched parquet 仅存 14 列基础数据, 指标和信号即时计算
  - 自定义 SQL 选股已下线: 结构化筛选走 screener_query (Polars 谓词)
  - 预设/自定义策略统一走 StrategyEngine (app/strategy/engine.py)
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, timedelta

import polars as pl

from app.storage.repository import KlineRepository

logger = logging.getLogger(__name__)

# ── 进程级历史数据缓存: 有界 OrderedDict 窗口缓存 ──
# 避免 run_all 每次重新扫描 parquet + compute_all。key 含 resolved data root 与
# repo.cache_generation: data root 隔离防止跨仓库/测试污染;generation 让每次管道
# 刷新立即逻辑失效,不必为命中率保留无界历史版本。
HistoryCacheKey = tuple[str, int, date, int]
_history_cache: OrderedDict[HistoryCacheKey, tuple[float, pl.DataFrame, int]] = OrderedDict()
_history_cache_lock = threading.Lock()
_HISTORY_CACHE_TTL = 120.0  # 秒
_HISTORY_CACHE_MAX_ENTRIES = 2
_HISTORY_CACHE_MAX_BYTES = 256 * 1024 * 1024


def _frame_estimated_size(df: pl.DataFrame) -> int:
    """估算 DataFrame 常驻字节数(用于缓存字节预算)。"""
    try:
        return int(df.estimated_size())
    except Exception:  # noqa: BLE001
        return _HISTORY_CACHE_MAX_BYTES + 1


def _history_cache_get(key: HistoryCacheKey, now: float) -> pl.DataFrame | None:
    """锁内查缓存: 先清全部过期项,命中则 move_to_end。昂贵计算应在锁外完成。"""
    with _history_cache_lock:
        if _history_cache:
            expired = [k for k, (ts, _, _) in _history_cache.items()
                       if now - ts >= _HISTORY_CACHE_TTL]
            for k in expired:
                del _history_cache[k]
        entry = _history_cache.get(key)
        if entry is None:
            return None
        ts, df, _size = entry
        if now - ts >= _HISTORY_CACHE_TTL:
            del _history_cache[key]
            return None
        _history_cache.move_to_end(key)
        return df


def _history_cache_put(key: HistoryCacheKey, now: float, df: pl.DataFrame) -> None:
    """锁内写缓存: 跳过空/超大帧,替换既有键,按条数与字节淘汰最旧。

    单帧超过字节预算时正常返回但不缓存(超大绕过),避免为命中率重新引入无界常驻。
    """
    if df.is_empty():
        return
    size = _frame_estimated_size(df)
    if size > _HISTORY_CACHE_MAX_BYTES:
        return
    with _history_cache_lock:
        if _history_cache:
            expired = [k for k, (ts, _, _) in _history_cache.items()
                       if now - ts >= _HISTORY_CACHE_TTL]
            for k in expired:
                del _history_cache[k]
        if key in _history_cache:
            del _history_cache[key]
        _history_cache[key] = (now, df, size)
        while (
            len(_history_cache) > _HISTORY_CACHE_MAX_ENTRIES
            or sum(_sz for _, _, _sz in _history_cache.values()) > _HISTORY_CACHE_MAX_BYTES
        ):
            if not _history_cache:
                break
            _history_cache.popitem(last=False)


def _history_cache_clear() -> None:
    with _history_cache_lock:
        _history_cache.clear()

def close_screener_sql_connection() -> None:
    """no-op: 自定义 SQL 选股 (POST /api/screener/run) 已下线, 进程级连接随之移除。

    保留签名供 main.py / mcp_server.py lifespan 关闭流程继续导入调用。
    """


@dataclass
class ScreenerResult:
    as_of: date
    strategy: str | None
    rows: list[dict] = field(default_factory=list)
    total: int = 0
    elapsed_ms: float = 0.0


class ScreenerService:
    def __init__(self, repo: KlineRepository) -> None:
        self.repo = repo

    @staticmethod
    def clear_history_cache() -> None:
        """清空进程级历史窗口缓存 (TTL/LRU/字节有界)。

        清除数据后调用, 避免内存里的旧历史窗口残留导致策略/看板仍命中旧数据。
        """
        _history_cache_clear()

    def _load_enriched_for_date(
        self,
        target_date: date,
        columns: list[str] | None = None,
    ) -> pl.DataFrame:
        """从 enriched 读取指定日期的指标+信号数据。

        ``columns`` 非空时只读取请求列, 但始终保留 repository 强制的
        ``symbol``/``date`` 语义; 省略时保持完整数据调用的原行为。
        """
        instrument_requested = columns is None or bool(
            set(columns) & {"name", "total_shares", "float_shares"}
        )
        # 最新日热缓存(已含完整指标)
        cache, cache_date = self.repo.get_enriched_latest()
        if cache is not None and not cache.is_empty() and cache_date == target_date:
            df = cache
            # JOIN instruments (name/total_shares/float_shares)
            df_i = self.repo.get_instruments() if instrument_requested else pl.DataFrame()
            if not df_i.is_empty():
                inst_cols = [c for c in ["symbol", "name", "total_shares", "float_shares"] if c in df_i.columns]
                if "name" not in df.columns:
                    df = df.join(df_i.select(inst_cols), on="symbol", how="left")
            if columns is not None:
                projected = list(dict.fromkeys(["symbol", "date", *columns]))
                df = df.select([c for c in projected if c in df.columns])
            return df

        # 精确分区存在性检查: 周末/假日或未落盘日期直接返回空,避免按需扫描
        enriched_dir = self.repo.store.data_dir / "kline_daily_enriched"
        ds = target_date.isoformat()
        target_parquet = enriched_dir / f"date={ds}" / "part.parquet"
        if not target_parquet.exists():
            return pl.DataFrame()

        # 历史日: 仓库按需扫描 + compute_all(返回完整指标/信号)
        df = self.repo.get_enriched_range(target_date, target_date, columns=columns)
        if df is None or df.is_empty():
            return pl.DataFrame()
        # JOIN instruments(name 等列) — compute_all 已传 instruments,此处幂等补全
        df_i = self.repo.get_instruments() if instrument_requested else pl.DataFrame()
        if not df_i.is_empty():
            inst_cols = [c for c in ["symbol", "name", "total_shares", "float_shares"] if c in df_i.columns]
            if "name" not in df.columns:
                df = df.join(df_i.select(inst_cols), on="symbol", how="left")
        if columns is not None:
            projected = list(dict.fromkeys(["symbol", "date", *columns]))
            df = df.select([c for c in projected if c in df.columns])
        return df

    def _load_enriched_history(self, target_date: date, lookback_days: int) -> pl.DataFrame:
        """读取目标日期之前的基础行情数据, 供历史窗口策略使用。

        命中进程级有界窗口缓存时 0ms;miss 时走 repo.get_enriched_range
        按需扫描 + compute_all 慢路径,计算在缓存锁外完成。
        """
        t0 = time.perf_counter()

        now = time.monotonic()
        cache_key: HistoryCacheKey = (
            str(self.repo.store.data_dir.resolve()),
            self.repo.cache_generation,
            target_date,
            lookback_days,
        )

        # 1. 进程级有界窗口缓存(锁内只做查找,命中即返回)
        cached = _history_cache_get(cache_key, now)
        if cached is not None:
            logger.debug("history cache hit: %s lookback=%d", target_date, lookback_days)
            return cached

        # 2. repo 按需扫描 + compute_all(慢路径,在锁外执行)
        logger.info("_load_enriched_history cache miss, computing (%s, %d)...",
                    target_date, lookback_days)
        start = target_date - timedelta(days=lookback_days)
        df = self.repo.get_enriched_range(start, target_date)
        if df is None or df.is_empty():
            return pl.DataFrame()

        # JOIN instruments(name 等列) 若缺失
        instruments = self.repo.get_instruments()
        if instruments is not None and not instruments.is_empty() and "name" not in df.columns:
            inst_cols = [c for c in ["symbol", "name", "total_shares", "float_shares"]
                         if c in instruments.columns]
            df = df.join(instruments.select(inst_cols), on="symbol", how="left")

        df = df.sort(["symbol", "date"])

        elapsed = (time.perf_counter() - t0) * 1000
        logger.info("_load_enriched_history(%s, %d): computed in %.1fms, %d rows",
                    target_date, lookback_days, elapsed, len(df))

        # 写入有界缓存(昂贵的扫描/计算已在锁外完成)
        _history_cache_put(cache_key, time.monotonic(), df)
        return df

    def latest_date(self) -> date | None:
        d = self.repo.enriched_latest_date()
        if d:
            return d
        # 回退 DuckDB (按 enriched 读取水位夹逼: 原始 max(date) 可领先水位,
        # 会把查询打到被隔离的未信任分区上)
        try:
            ceiling = self.repo.enriched_read_ceiling
            if ceiling is not None:
                res = self.repo.execute_one(
                    "SELECT max(date) FROM kline_enriched WHERE date <= ?",
                    [ceiling],
                )
            else:
                res = self.repo.execute_one(
                    "SELECT max(date) FROM kline_enriched",
                )
            if res and res[0]:
                d = res[0]
                return d if isinstance(d, date) else date.fromisoformat(str(d))
        except Exception:  # noqa: BLE001
            return None
        return None
