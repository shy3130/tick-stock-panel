"""Screener 服务(§6.3)。

性能优化:
  - enriched parquet 仅存 14 列基础数据, 指标和信号即时计算
  - preset 策略: 从内存缓存或即时计算获取完整指标, ~10-50ms
  - custom SQL: DuckDB (用户传 SQL WHERE 字符串), ~10-50ms
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

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

# ── 进程级复用的 DuckDB 连接,专供 ScreenerService.run() 的自定义 SQL 过滤用 ──
# 实测 duckdb.connect(":memory:") 每次新建约 78ms(主要是引擎初始化 + register
# 5500 行 arrow table),复用同一个连接后降到 ~0.5ms。ScreenerService 本身是
# 每次请求现建的(见 app/api/screener.py 的 ScreenerService(repo)),所以连接
# 必须放在模块级才能跨请求复用；用锁保证 register+query 这一对操作不会被并发
# 请求交错(两个请求都注册名为 "enriched" 的视图会互相覆盖)。
_screener_sql_conn: Any = None
_screener_sql_lock = threading.Lock()


def _get_screener_sql_conn():
    global _screener_sql_conn
    if _screener_sql_conn is None:
        from app.storage.duckdb_runtime import connect_duckdb
        _screener_sql_conn = connect_duckdb()
    return _screener_sql_conn


def close_screener_sql_connection() -> None:
    """关闭并清空进程级 Screener DuckDB 连接；幂等。"""
    global _screener_sql_conn
    with _screener_sql_lock:
        conn = _screener_sql_conn
        _screener_sql_conn = None
        if conn is not None:
            conn.close()


# 内置预设策略 — Polars 表达式方式
PRESET_STRATEGIES: dict[str, dict] = {
    "trend_breakout": {
        "name": "趋势突破",
        "description": "MA60 上方 + 60 日新高 + 量能 ≥ 2 倍均量",
        "filter": (
            (pl.col("close") > pl.col("ma60"))
            & pl.col("signal_n_day_high").fill_null(False)
            & (pl.col("vol_ratio_5d") >= 2.0)
        ),
        "order_by": "momentum_60d",
        "descending": True,
        "limit": 100,
    },
    "ma_golden_cross": {
        "name": "MA 金叉",
        "description": "MA5 上穿 MA20 当日触发,量能配合",
        "filter": (
            pl.col("signal_ma_golden_5_20").fill_null(False)
            & (pl.col("vol_ratio_5d") >= 1.2)
            & (pl.col("close") > pl.col("ma60"))
        ),
        "order_by": "momentum_20d",
        "descending": True,
        "limit": 100,
    },
    "macd_golden": {
        "name": "MACD 金叉放量",
        "description": "MACD 金叉当日 + 量能放大",
        "filter": (
            pl.col("signal_macd_golden").fill_null(False)
            & (pl.col("vol_ratio_5d") >= 1.5)
        ),
        "order_by": "momentum_60d",
        "descending": True,
        "limit": 100,
    },
    "volume_price_surge": {
        "name": "量价齐升",
        "description": "突破 MA20 + 放量 + 收阳",
        "filter": (
            pl.col("signal_ma20_breakout").fill_null(False)
            & (pl.col("vol_ratio_5d") >= 2.0)
            & (pl.col("close") > pl.col("open"))
        ),
        "order_by": "vol_ratio_5d",
        "descending": True,
        "limit": 100,
    },
    "low_volatility_leader": {
        "name": "低波动龙头",
        "description": "20 日动量为正 + 年化波动 < 30% + MA20 上方",
        "filter": (
            (pl.col("momentum_20d") > 0)
            & (pl.col("annual_vol_20d") < 0.30)
            & (pl.col("close") > pl.col("ma20"))
        ),
        "order_by": "momentum_60d",
        "descending": True,
        "limit": 100,
    },
    "broken_board_recovery": {
        "name": "断板反包",
        "description": "连板 ≥2 后断板 1-2 天，出现放量反包信号",
        "filter": (
            pl.col("signal_limit_up").fill_null(False)
            & (pl.col("vol_ratio_5d") >= 1.5)
            & (pl.col("change_pct") > 0.03)
        ),
        "order_by": "change_pct",
        "descending": True,
        "limit": 100,
    },
    "oversold_bounce": {
        "name": "超跌反弹",
        "description": "RSI14 < 30 超卖区 + 当日收阳 + 放量，抄底信号",
        "filter": (
            (pl.col("rsi_14") < 30)
            & (pl.col("close") > pl.col("open"))
            & (pl.col("vol_ratio_5d") >= 1.2)
        ),
        "order_by": "rsi_14",
        "descending": False,
        "limit": 100,
    },
    "boll_breakout": {
        "name": "布林突破",
        "description": "突破布林上轨 + 放量，强势加速信号",
        "filter": (
            pl.col("signal_boll_breakout_upper").fill_null(False)
            & (pl.col("vol_ratio_5d") >= 1.5)
        ),
        "order_by": "vol_ratio_5d",
        "descending": True,
        "limit": 100,
    },
    "bullish_alignment": {
        "name": "均线多头",
        "description": "MA5 > MA10 > MA20 > MA60 多头排列 + 短期动量为正",
        "filter": (
            (pl.col("ma5") > pl.col("ma10"))
            & (pl.col("ma10") > pl.col("ma20"))
            & (pl.col("ma20") > pl.col("ma60"))
            & (pl.col("momentum_20d") > 0)
        ),
        "order_by": "momentum_60d",
        "descending": True,
        "limit": 100,
    },
    "consecutive_limit_ups": {
        "name": "连板股",
        "description": "当日涨停且连续涨停 ≥ 2 天，强势追涨",
        "filter": (
            pl.col("signal_limit_up").fill_null(False)
            & (pl.col("consecutive_limit_ups") >= 2)
        ),
        "order_by": "consecutive_limit_ups",
        "descending": True,
        "limit": 100,
    },
    "pullback_to_support": {
        "name": "缩量回踩",
        "description": "回踩 MA20 附近 + 缩量 + 中期趋势向上",
        "filter": (
            (pl.col("close") > pl.col("ma20") * 0.98)
            & (pl.col("close") < pl.col("ma20") * 1.02)
            & (pl.col("vol_ratio_5d") < 0.8)
            & (pl.col("close") > pl.col("ma60"))
            & (pl.col("momentum_20d") > 0)
        ),
        "order_by": "momentum_60d",
        "descending": True,
        "limit": 100,
    },
    "n_day_low_reversal": {
        "name": "新低反转",
        "description": "触及 60 日新低后当日收阳放量，反转信号",
        "filter": (
            pl.col("signal_n_day_low").fill_null(False)
            & (pl.col("close") > pl.col("open"))
            & (pl.col("vol_ratio_5d") >= 1.5)
        ),
        "order_by": "change_pct",
        "descending": True,
        "limit": 100,
    },
}


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

    def run(
        self,
        as_of: date,
        conditions: list[str],
        order_by: str | None = None,
        limit: int = 30,
        pool: list[str] | None = None,
    ) -> ScreenerResult:
        """自定义 SQL 条件选股。

        先通过 Polars 即时计算完整指标, 再用 DuckDB 做 SQL WHERE 过滤。
        kline_enriched DuckDB 视图只有 14 列, 不能直接用于指标过滤。
        """
        t0 = time.perf_counter()

        if not conditions:
            return ScreenerResult(as_of=as_of, strategy=None)

        # 从即时计算获取完整 enriched 数据
        df = self._load_enriched_for_date(as_of)
        if df.is_empty():
            return ScreenerResult(as_of=as_of, strategy=None)

        # Pool 过滤
        if pool:
            df = df.filter(pl.col("symbol").is_in(pool))

        # 用 DuckDB 做 SQL 过滤 (注册临时视图, 复用进程级连接见 _get_screener_sql_conn)
        try:
            where = " AND ".join(f"({c})" for c in conditions)
            sql = f"SELECT * FROM enriched WHERE {where}"
            if order_by:
                sql += f" ORDER BY {order_by}"
            if limit:
                sql += f" LIMIT {limit}"
            with _screener_sql_lock:
                con = _get_screener_sql_conn()
                con.register("enriched", df.to_arrow())
                df_result = con.execute(sql).pl()
        except Exception as e:  # noqa: BLE001
            logger.warning("screener SQL query failed: %s", e)
            df_result = pl.DataFrame()

        rows = df_result.to_dicts() if not df_result.is_empty() else []
        elapsed = (time.perf_counter() - t0) * 1000

        return ScreenerResult(
            as_of=as_of,
            strategy=None,
            rows=rows,
            total=len(rows),
            elapsed_ms=elapsed,
        )

    def run_preset(
        self,
        strategy_id: str,
        as_of: date,
        pool: list[str] | None = None,
        precomputed: pl.DataFrame | None = None,
        basic_filter: dict | None = None,
        display_limit: int | None = None,
    ) -> ScreenerResult:
        """预设策略选股 — 从 enriched 读取预计算好的指标列后过滤。

        - precomputed 不为空: 直接复用（run_all 场景）
        - precomputed 为空: 从 enriched 读目标日期
        - basic_filter: 用户保存的基础参数过滤（boards、价格等）
        """
        t0 = time.perf_counter()

        strat = PRESET_STRATEGIES.get(strategy_id)
        if not strat:
            raise ValueError(f"unknown strategy: {strategy_id}")

        if precomputed is not None and not precomputed.is_empty():
            df = precomputed
        else:
            df = self._load_enriched_for_date(as_of)
            if df.is_empty():
                return ScreenerResult(as_of=as_of, strategy=strategy_id)

        # 应用用户基础参数过滤（boards、价格区间等）
        if basic_filter and basic_filter.get("enabled", True):
            df = self._apply_basic_filter(df, basic_filter)

        # 应用策略过滤
        df = df.filter(strat["filter"])

        # 应用 pool
        if pool:
            df = df.filter(pl.col("symbol").is_in(pool))

        # 排序 + 限制
        order_col = strat["order_by"]
        if order_col in df.columns:
            df = df.sort(order_col, descending=strat.get("descending", True))

        # display_limit: None=不限制, 0=全部, N=前N个
        if display_limit == 0:
            limit = None  # 不限制
        elif display_limit is not None:
            limit = display_limit
        else:
            limit = None  # 未配置时默认不限制
        if limit is not None and limit > 0:
            df = df.head(limit)

        # 基于排序列生成 0-100 评分 (与 StrategyEngine 统一)
        if order_col in df.columns and not df.is_empty():
            col_vals = df[order_col].cast(pl.Float64)
            col_min = col_vals.min()
            col_max = col_vals.max()
            col_range = col_max - col_min
            if col_range and col_range > 0:
                normalized = (col_vals - col_min) / col_range
            else:
                normalized = pl.Series("norm", [0.5] * len(df))
            if not strat.get("descending", True):
                normalized = 1.0 - normalized
            df = df.with_columns((normalized * 100).alias("score"))

        rows = df.to_dicts()
        elapsed = (time.perf_counter() - t0) * 1000

        # sanitize
        for r in rows:
            for k, v in list(r.items()):
                if isinstance(v, float) and (v != v or abs(v) == float("inf")):
                    r[k] = None

        return ScreenerResult(
            as_of=as_of,
            strategy=strategy_id,
            rows=rows,
            total=len(rows),
            elapsed_ms=elapsed,
        )

    @staticmethod
    def _apply_basic_filter(df: pl.DataFrame, bf: dict) -> pl.DataFrame:
        """应用用户基础参数过滤（boards、价格区间、市值等）"""
        exprs: list[pl.Expr] = []
        if bf.get("price_min") is not None:
            exprs.append(pl.col("close") >= bf["price_min"])
        if bf.get("price_max") is not None:
            exprs.append(pl.col("close") <= bf["price_max"])
        if bf.get("float_cap_min") is not None and "float_shares" in df.columns:
            exprs.append(pl.col("close") * pl.col("float_shares") >= bf["float_cap_min"])
        if bf.get("float_cap_max") is not None and "float_shares" in df.columns:
            exprs.append(pl.col("close") * pl.col("float_shares") <= bf["float_cap_max"])
        if bf.get("amount_min") is not None:
            exprs.append(pl.col("amount") >= bf["amount_min"])
        if bf.get("amount_max") is not None:
            exprs.append(pl.col("amount") <= bf["amount_max"])
        if bf.get("turnover_min") is not None and "turnover_rate" in df.columns:
            exprs.append(pl.col("turnover_rate") >= bf["turnover_min"])
        if bf.get("turnover_max") is not None and "turnover_rate" in df.columns:
            exprs.append(pl.col("turnover_rate") <= bf["turnover_max"])
        if bf.get("exclude_st") and "name" in df.columns:
            exprs.append(~pl.col("name").str.contains("(?i)ST|\\*ST|退"))
        # 板块过滤
        boards = bf.get("boards")
        if boards and isinstance(boards, list) and len(boards) > 0:
            board_exprs: list[pl.Expr] = []
            for b in boards:
                if b == "沪主板":
                    board_exprs.append(pl.col("symbol").str.starts_with("60"))
                elif b == "深主板":
                    board_exprs.append(
                        pl.col("symbol").str.starts_with("00")
                        | pl.col("symbol").str.starts_with("001")
                    )
                elif b == "创业板":
                    board_exprs.append(
                        pl.col("symbol").str.starts_with("300")
                        | pl.col("symbol").str.starts_with("301")
                    )
                elif b == "科创板":
                    board_exprs.append(pl.col("symbol").str.starts_with("688"))
                elif b == "北交所":
                    board_exprs.append(pl.col("symbol").str.contains(r"\.BJ$"))
            if board_exprs:
                exprs.append(pl.any_horizontal(board_exprs))
        if exprs:
            return df.filter(pl.all_horizontal(exprs))
        return df

    def latest_date(self) -> date | None:
        d = self.repo.enriched_latest_date()
        if d:
            return d
        # 回退 DuckDB
        try:
            res = self.repo.execute_one(
                "SELECT max(date) FROM kline_enriched",
            )
            if res and res[0]:
                d = res[0]
                return d if isinstance(d, date) else date.fromisoformat(str(d))
        except Exception:  # noqa: BLE001
            return None
        return None
