"""Screener 服务(§6.3)。

性能优化:
  - enriched parquet 仅存 14 列基础数据, 指标和信号即时计算
  - preset 策略: 从内存缓存或即时计算获取完整指标, ~10-50ms
  - custom SQL: DuckDB (用户传 SQL WHERE 字符串), ~10-50ms
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

import polars as pl

from app.parquet import scan_enriched_parquet
from app.tickflow.repository import KlineRepository

logger = logging.getLogger(__name__)

# ── 进程级历史数据缓存 (避免 run_all 每次重新扫描 parquet + 计算指标) ──
_history_cache: dict[tuple[str, date, int], tuple[float, pl.DataFrame]] = {}
_HISTORY_CACHE_TTL = 120.0  # 秒


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
        "asset_types": ["stock", "etf"],
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
        "asset_types": ["stock", "etf"],
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
        "asset_types": ["stock", "etf"],
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
        "asset_types": ["stock", "etf"],
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
        "asset_types": ["stock", "etf"],
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
        "asset_types": ["stock"],
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
        "asset_types": ["stock", "etf"],
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
        "asset_types": ["stock", "etf"],
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
        "asset_types": ["stock", "etf"],
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
        "asset_types": ["stock"],
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
        "asset_types": ["stock", "etf"],
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
        "asset_types": ["stock", "etf"],
    },
}


def strategy_supports_asset(strat: dict, asset_type: str) -> bool:
    """策略是否支持该资产类型。默认仅 stock（未标注 asset_types 的自定义/AI 策略保守视为股票专用）。"""
    return asset_type in strat.get("asset_types", ["stock"])


@dataclass
class ScreenerResult:
    as_of: date
    strategy: str | None
    rows: list[dict] = field(default_factory=list)
    total: int = 0
    elapsed_ms: float = 0.0


class ScreenerService:
    def __init__(self, repo: KlineRepository, asset_type: str = "stock") -> None:
        self.repo = repo
        self.asset_type = asset_type
        from app.tickflow.repository import enriched_dirname
        self._enriched_dirname = enriched_dirname(asset_type)

    @staticmethod
    def clear_history_cache() -> None:
        """清空进程级 _history_cache (TTL 缓存)。

        清除数据后调用, 避免内存里的旧历史窗口残留导致策略/看板仍命中旧数据。
        """
        _history_cache.clear()

    def _load_enriched_for_date(self, target_date: date) -> pl.DataFrame:
        """从 enriched parquet 读取指定日期的基础数据并即时计算完整指标+信号。

        enriched parquet 仅存 14 列。读取后需要即时计算 ma/ema/macd/kdj/rsi/boll/momentum/signal 等列。
        对于最新日, 优先使用内存缓存 (已包含完整指标)。
        """
        # 优先使用 repo 最新日缓存
        cache, cache_date = self.repo.get_enriched_latest_asset(self.asset_type)
        if cache is not None and not cache.is_empty() and cache_date == target_date:
            df = cache
            # JOIN instruments
            df_i = self.repo.get_instruments_asset(self.asset_type)
            if not df_i.is_empty():
                inst_cols = [c for c in ["symbol", "name", "total_shares", "float_shares"] if c in df_i.columns]
                if "name" not in df.columns:
                    df = df.join(df_i.select(inst_cols), on="symbol", how="left")
            return df

        # 尝试从 repo 级预计算历史缓存中提取目标日期 (仅 stock: 该缓存为股票专用)
        if self.asset_type == "stock":
            cached_hist = self.repo.get_enriched_history(target_date, 1)
            if cached_hist is not None and not cached_hist.is_empty() and "date" in cached_hist.columns:
                df = cached_hist.filter(pl.col("date") == target_date)
                if not df.is_empty():
                    logger.debug("_load_enriched_for_date: repo history cache for %s", target_date)
                    # JOIN instruments
                    df_i = self.repo.get_instruments_asset(self.asset_type)
                    if not df_i.is_empty():
                        inst_cols = [c for c in ["symbol", "name", "total_shares", "float_shares"] if c in df_i.columns]
                        if "name" not in df.columns:
                            df = df.join(df_i.select(inst_cols), on="symbol", how="left")
                    return df

        # 历史日期: 从 parquet 读取 14 列, 即时计算指标 (慢路径)
        enriched_dir = self.repo.store.data_dir / self._enriched_dirname
        ds = target_date.isoformat()
        target_parquet = enriched_dir / f"date={ds}" / "part.parquet"

        if not target_parquet.exists():
            return pl.DataFrame()

        try:
            df = pl.read_parquet(target_parquet)
        except Exception as e:  # noqa: BLE001
            logger.warning("load_enriched_for_date failed: %s", e)
            return pl.DataFrame()

        if df.is_empty():
            return df

        # 即时计算指标: 需要加载历史窗口作 warmup
        df_full = self._compute_enriched_full(df, target_date)
        return df_full

    def load_prior_consecutive(self, as_of: date, consec_col: str) -> pl.DataFrame:
        """窄读: 仅取前一交易日的 [symbol, consec_col] 两列 (谓词下推到单日 parquet)。

        consecutive_limit_ups / consecutive_limit_downs 是 enriched 的存储列,
        可直接从 parquet 读取, 无需 _load_enriched_for_date 的全量指标重算
        (历史日期该慢路径最坏会触发 9 次全市场 compute_enriched_full)。

        选取逻辑与旧循环等价: 在 as_of 前 1~9 天内找到第一个存在的日分区
        (即前一交易日), 读取其 symbol + consec_col。存储列的值与重算值逐位一致
        (连板计数为 run-length, 150 天 warmup 完全覆盖 A 股最长连板, 二者相等)。

        返回列: symbol, prev_consec。找不到前一交易日时返回空 DataFrame。
        """
        enriched_dir = self.repo.store.data_dir / self._enriched_dirname
        for delta in range(1, 10):
            candidate = as_of - timedelta(days=delta)
            target_parquet = enriched_dir / f"date={candidate.isoformat()}" / "part.parquet"
            if not target_parquet.exists():
                continue
            try:
                lf = pl.scan_parquet(target_parquet)
                cols = lf.collect_schema().names()
            except Exception as e:  # noqa: BLE001
                logger.warning("load_prior_consecutive scan failed for %s: %s", candidate, e)
                return pl.DataFrame()
            # 存储列理论上必含 consec_col; 若该分区缺列则继续向前找 (与旧循环一致)
            if "symbol" not in cols or consec_col not in cols:
                continue
            try:
                return lf.select(
                    "symbol",
                    pl.col(consec_col).alias("prev_consec"),
                ).collect()
            except Exception as e:  # noqa: BLE001
                logger.warning("load_prior_consecutive read failed for %s: %s", candidate, e)
                return pl.DataFrame()
        return pl.DataFrame()

    def _compute_enriched_full(self, df_target: pl.DataFrame, target_date: date) -> pl.DataFrame:
        """从 14 列基础数据即时计算完整 enriched (含全部指标和信号)。

        读取历史数据作为指标计算的 warmup, 计算完成后只返回目标日期的行。
        """
        from app.indicators.pipeline import compute_indicators, compute_signals, compute_limit_signals

        # 加载 warmup 历史 (目标日期前 ~120 天)
        enriched_dir = self.repo.store.data_dir / self._enriched_dirname
        start = target_date - timedelta(days=150)
        read_cols = ["symbol", "date", "open", "high", "low", "close", "volume",
                     "amount", "raw_close", "raw_high", "raw_low"]

        try:
            lf = (
                scan_enriched_parquet(str(enriched_dir / "**" / "*.parquet"))
                .filter(
                    (pl.col("date") >= start)
                    & (pl.col("date") <= target_date)
                )
                .sort(["symbol", "date"])
            )
            available = [c for c in read_cols if c in lf.schema]
            df_hist = lf.select(available).collect()
        except Exception as e:  # noqa: BLE001
            logger.warning("warmup history load failed: %s", e)
            df_hist = df_target

        if df_hist.is_empty():
            df_hist = df_target

        # 计算指标
        df_full = compute_indicators(df_hist)
        df_full = compute_signals(df_full)

        # 计算涨跌停信号 (需要 instruments; 涨停为股票专有, ETF 跳过)
        instruments = self.repo.get_instruments_asset(self.asset_type)
        if self.asset_type == "stock" and instruments is not None and not instruments.is_empty():
            df_full = compute_limit_signals(df_full, instruments)

        # 只保留目标日期
        df_result = df_full.filter(pl.col("date") == target_date)

        # JOIN instruments (name, total_shares, float_shares)
        if not instruments.is_empty():
            inst_cols = [c for c in ["symbol", "name", "total_shares", "float_shares"] if c in instruments.columns]
            if "name" not in df_result.columns:
                df_result = df_result.join(instruments.select(inst_cols), on="symbol", how="left")

        return df_result

    def _load_enriched_history(self, target_date: date, lookback_days: int) -> pl.DataFrame:
        """读取目标日期之前的基础行情数据, 供历史窗口策略使用。

        优先从 repo 内存缓存获取 (启动时已预计算), 命中时 0ms。
        缓存 miss 时走 scan_parquet + compute_indicators 慢路径。
        """
        # 优先级 1: repo 级预计算缓存 (启动时 _refresh_enriched 已计算完整历史; 仅 stock)
        t0 = time.perf_counter()
        if self.asset_type == "stock":
            cached = self.repo.get_enriched_history(target_date, lookback_days)
            if cached is not None and not cached.is_empty():
                # JOIN instruments (repo 缓存不含 name 等列)
                instruments = self.repo.get_instruments_asset(self.asset_type)
                if instruments is not None and not instruments.is_empty() and "name" not in cached.columns:
                    inst_cols = [c for c in ["symbol", "name", "total_shares", "float_shares"]
                                 if c in instruments.columns]
                    cached = cached.join(instruments.select(inst_cols), on="symbol", how="left")
                elapsed = (time.perf_counter() - t0) * 1000
                logger.info("_load_enriched_history(%s, %d): repo cache hit, %.1fms, %d rows",
                            target_date, lookback_days, elapsed, len(cached))
                return cached

        # 优先级 2: 进程级 history_cache (之前的 TTL 缓存)
        cache_key = (self.asset_type, target_date, lookback_days)
        now = time.monotonic()
        ttl_cached = _history_cache.get(cache_key)
        if ttl_cached is not None:
            ts, cached_df = ttl_cached
            if now - ts < _HISTORY_CACHE_TTL:
                logger.debug("history TTL cache hit: %s lookback=%d", target_date, lookback_days)
                return cached_df
            del _history_cache[cache_key]

        # 优先级 3: scan_parquet + compute_indicators (慢路径, ~5s)
        logger.warning("_load_enriched_history cache miss, computing indicators (%s, %d)...",
                       target_date, lookback_days)
        from app.indicators.pipeline import compute_indicators, compute_signals, compute_limit_signals

        warmup = 60
        start = target_date - timedelta(days=min((lookback_days + warmup) * 2, 180))

        enriched_dir = self.repo.store.data_dir / self._enriched_dirname
        read_cols = ["symbol", "date", "open", "high", "low", "close", "volume",
                     "amount", "raw_close", "raw_high", "raw_low"]

        try:
            lf = (
                scan_enriched_parquet(str(enriched_dir / "**" / "*.parquet"))
                .filter((pl.col("date") >= start) & (pl.col("date") <= target_date))
                .sort(["symbol", "date"])
            )
            available = [c for c in read_cols if c in lf.collect_schema().names()]
            df_hist = lf.select(available).collect()
        except Exception as e:  # noqa: BLE001
            logger.warning("load_enriched_history failed: %s", e)
            return pl.DataFrame()

        if df_hist.is_empty():
            return pl.DataFrame()

        df_full = compute_indicators(df_hist)
        df_full = compute_signals(df_full)

        instruments = self.repo.get_instruments_asset(self.asset_type)
        if self.asset_type == "stock" and instruments is not None and not instruments.is_empty():
            df_full = compute_limit_signals(df_full, instruments)

        if instruments is not None and not instruments.is_empty():
            inst_cols = [c for c in ["symbol", "name", "total_shares", "float_shares"] if c in instruments.columns]
            if "name" not in df_full.columns:
                df_full = df_full.join(instruments.select(inst_cols), on="symbol", how="left")

        # 裁剪掉 warmup 部分, 只保留 lookback 范围 (减少 group_by 开销)。
        # 按交易日计数: 从数据里实际存在的交易日序列取最后 lookback_days 个交易日,
        # 不能用 timedelta(days=N) (自然日), 否则周末/节假日会让窗口偏少, 与回测不一致。
        if "date" in df_full.columns:
            trading_dates = df_full["date"].unique().sort()
            if len(trading_dates) > lookback_days:
                lookback_start = trading_dates[-(lookback_days + 1)]
            else:
                lookback_start = trading_dates[0]
            df_full = df_full.filter(pl.col("date") >= lookback_start)

        df_full = df_full.sort(["symbol", "date"])

        elapsed = (time.perf_counter() - t0) * 1000
        logger.info("_load_enriched_history(%s, %d): computed in %.1fms, %d rows",
                    target_date, lookback_days, elapsed, len(df_full))

        _history_cache[cache_key] = (now, df_full)
        if len(_history_cache) > 10:
            expired = [k for k, (ts, _) in _history_cache.items() if now - ts > _HISTORY_CACHE_TTL]
            for k in expired:
                del _history_cache[k]

        return df_full

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

        # 用 DuckDB 做 SQL 过滤 (注册临时视图)
        # 用独立的 :memory: 连接 (而非复用 repo 共享连接的 cursor): conditions 是用户
        # 传入的 SQL 片段, 隔离连接下注入至多能碰 read_csv/read_parquet 文件; 若复用共享
        # 连接则会把 app 已注册的真实业务表也暴露给注入, 扩大攻击面。隔离连接创建开销极低。
        con = None
        try:
            import duckdb
            con = duckdb.connect(database=":memory:")
            con.register("enriched", df.to_arrow())
            where = " AND ".join(f"({c})" for c in conditions)
            sql = f"SELECT * FROM enriched WHERE {where}"
            if order_by:
                sql += f" ORDER BY {order_by}"
            if limit:
                sql += f" LIMIT {limit}"
            df_result = con.execute(sql).pl()
        except Exception as e:  # noqa: BLE001
            logger.warning("screener SQL query failed: %s", e)
            df_result = pl.DataFrame()
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:  # noqa: BLE001
                    pass

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

        # 资产兼容拦截: 该策略不支持当前资产类型时直接返回空 (避免命中 ETF 不存在的列)
        if not strategy_supports_asset(strat, self.asset_type):
            return ScreenerResult(as_of=as_of, strategy=strategy_id)

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
        if self.asset_type != "stock":
            _, d = self.repo.get_enriched_latest_asset(self.asset_type)
            return d
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
