"""回测引擎的共享数据加载及 Tivelo 撮合与统计适配。"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import date, timedelta
from typing import TYPE_CHECKING

import polars as pl
import pyarrow as pa
from tivelo_backtest.legacy.simulator import (
    LegacySimulator,
)
from tivelo_backtest.legacy.simulator import (
    MatcherConfig as MatcherConfig,
)
from tivelo_backtest.legacy.simulator import (
    SimResult as SimResult,
)
from tivelo_backtest.legacy.simulator import (
    SimulationOptions as SimulationOptions,
)
from tivelo_backtest.legacy.simulator import (
    TradeRecord as TradeRecord,
)
from tivelo_backtest.legacy.simulator import (
    _apply_position_policy as _apply_position_policy,
)
from tivelo_backtest.legacy.simulator import (
    _matrix_entry_score as _matrix_entry_score,
)
from tivelo_backtest.legacy.simulator import (
    _resolve_signal_id as _resolve_signal_id,
)

from app.backtest.matrix import (
    MarketDataMatrix,
    MarketMatrix,
    MatrixCacheProfile,
    load_market_data_matrix_from_daily_panel,
    load_market_data_matrix_from_parquet,
)
from app.backtest.matrix import (
    build_market_matrix as build_market_matrix,
)
from app.backtest.price_basis import BacktestPriceBasis, BackwardAdjustment
from app.config import settings
from app.enriched_generation import EnrichedGenerationUnavailableError
from app.parquet import scan_enriched_parquet
from app.tickflow.repository import KlineRepository

if TYPE_CHECKING:
    from app.backtest.snapshot import SnapshotDailySource

logger = logging.getLogger(__name__)


def history_load_start(engine, start: date, asset_type: str, bars: int, *, minimum_days: int = 120) -> date:
    """保留既有最小预取范围; 长假或分区稀疏时按真实交易日向前取足。"""
    fallback = start - timedelta(days=max(minimum_days, int(bars * 1.6)))
    data_dir = getattr(getattr(getattr(engine, "repo", None), "store", None), "data_dir", None)
    if bars <= 1 or data_dir is None:
        return fallback
    from app.services.mining_preflight import enriched_partition_dates

    dates = enriched_partition_dates(data_dir, asset_type, end=start - timedelta(days=1))
    return min(fallback, dates[max(0, len(dates) - bars)]) if dates else fallback






# ================================================================
# 数据结构
# ================================================================











# ================================================================
# PanelCache — 避免重复 scan_parquet + compute_all
# ================================================================

class _CacheEntry:
    __slots__ = ("df", "ts")

    def __init__(self, df: pl.DataFrame, ts: float):
        self.df = df
        self.ts = ts


class _InFlight:
    """同 key 正在计算的占位: leader 算完通过 done 唤醒所有跟随者复用结果。"""

    __slots__ = ("done", "df", "error")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.df: pl.DataFrame | None = None
        self.error: BaseException | None = None


class PanelCache:
    """LRU + TTL 数据面板缓存。"""

    def __init__(self, max_size: int = 2, ttl_seconds: int = 180):
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl_seconds
        # 跨请求单例, SSE 回测在各自 daemon 线程并发访问 OrderedDict。
        # 无锁的 move_to_end/del/popitem check-then-act 会抛 "OrderedDict mutated"。
        # 用实例锁守护所有 OrderedDict 变更; compute_fn (重扫盘) 放锁外避免串行化。
        self._lock = threading.Lock()
        # single-flight: 同 key 只让一个线程 compute, 其余等其结果复用。
        # 否则优化器/walk-forward 的 max_workers 个线程冷启动同时 miss, 会并行加载 N 份同一面板。
        self._inflight: dict[str, _InFlight] = {}
        # 轻量遥测: 累加真实扫盘耗时与命中/复用次数, 用于量化 IO 占比 (是否值得进一步优化)。
        # compute_seconds 只计 leader 的实际 compute_fn 耗时, follower 复用不计 —— 反映真实 IO。
        self._compute_seconds = 0.0
        self._compute_count = 0   # 实际扫盘次数
        self._hit_count = 0       # 缓存命中 (未扫盘) 次数
        self._reuse_count = 0     # single-flight 跟随者复用次数

    def get_or_compute(
        self,
        symbols: list[str] | None,
        start: date,
        end: date,
        columns: list[str] | None,
        compute_fn,
        asset_type: str = "stock",
        generation: str | None = None,
    ) -> pl.DataFrame:
        key = self._make_key(
            symbols, start, end, columns, asset_type, generation
        )
        now = time.monotonic()

        with self._lock:
            entry = self._cache.get(key)
            if entry is not None:
                if now - entry.ts < self._ttl:
                    self._cache.move_to_end(key)
                    self._hit_count += 1
                    return entry.df
                del self._cache[key]  # 过期, 丢弃后重算
            # single-flight: 同 key 若已有线程在算, 登记为跟随者; 否则本线程当 leader。
            flight = self._inflight.get(key)
            leader = flight is None
            if leader:
                flight = _InFlight()
                self._inflight[key] = flight

        if not leader:
            # 跟随者: 等 leader 算完直接复用, 不重复 compute (消除缓存踩踏)。
            flight.done.wait()
            with self._lock:
                self._reuse_count += 1
            if flight.error is not None:
                raise flight.error
            return flight.df

        # leader: compute 放锁外 (不同 key 仍可并发, 保留原设计优点)。
        t_compute = time.perf_counter()
        try:
            df = compute_fn(symbols, start, end, columns, asset_type)
        except BaseException as e:
            # 失败不缓存: 摘除 inflight 让后续线程重试, 并把异常透传给已在等的跟随者。
            with self._lock:
                self._compute_seconds += time.perf_counter() - t_compute  # 失败也花了 IO, 计入
                self._compute_count += 1
                self._inflight.pop(key, None)
            flight.error = e
            flight.done.set()
            raise
        with self._lock:
            self._compute_seconds += time.perf_counter() - t_compute
            self._compute_count += 1
            self._cache[key] = _CacheEntry(df=df, ts=now)
            if len(self._cache) > self._max_size:
                self._cache.popitem(last=False)
            self._inflight.pop(key, None)
        flight.df = df
        flight.done.set()
        return df

    def stats(self) -> dict:
        """遥测快照: 累计扫盘耗时/次数与命中/复用次数。首尾快照取差即区间内 IO 开销。"""
        with self._lock:
            return {
                "compute_seconds": round(self._compute_seconds, 4),
                "compute_count": self._compute_count,
                "hit_count": self._hit_count,
                "reuse_count": self._reuse_count,
            }

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    @staticmethod
    def _make_key(
        symbols: list[str] | None,
        start: date,
        end: date,
        columns: list[str] | None,
        asset_type: str = "stock",
        generation: str | None = None,
    ) -> str:
        if symbols is None:
            h = "all"
        else:
            h = hashlib.md5(",".join(sorted(symbols)).encode()).hexdigest()[:12]
        cols = "all" if columns is None else hashlib.md5(",".join(sorted(columns)).encode()).hexdigest()[:8]
        return f"{asset_type}:{generation or 'unmanaged'}:{h}:{start}:{end}:{cols}"


# 等待进行中 enriched 发布的上限与轮询间隔。孤儿标记由 get_enriched_generation
# 在读取时直接自愈, 因此这里等到的 EnrichedGenerationUnavailableError 意味着
# 发布方确实存活 —— 对回测/优化这类长任务, 有界等待优于立即失败。仅用于
# worker 任务路径 (矩阵加载), 实时热路径不得调用 data_generation_await。
_GENERATION_WAIT_TIMEOUT_S = 300.0
_GENERATION_POLL_S = 1.0


# ================================================================
# BacktestEngine
# ================================================================

class BacktestEngine:
    """回测引擎 — 数据加载 + 撮合模拟 + 统计计算。"""

    def __init__(self, repo: KlineRepository, *, snapshot: SnapshotDailySource | None = None) -> None:
        self.repo = repo
        self.snapshot = snapshot
        self._cache = PanelCache()
        self._evidence = None
        self._entry_data = None

    # ── 数据加载 ──────────────────────────────────────

    def data_generation(self, asset_type: str = "stock") -> str | None:
        from app.services.daily_health import assert_usable

        root = getattr(getattr(self.repo, "store", None), "data_dir", None)
        if root is not None:
            assert_usable(root, asset_type)
        loader = getattr(self.repo, "get_matrix_data_generation", None)
        return loader(asset_type) if callable(loader) else None

    def data_generation_await(
        self,
        asset_type: str = "stock",
        *,
        cancel_event: threading.Event | None = None,
        timeout_s: float = _GENERATION_WAIT_TIMEOUT_S,
    ) -> str | None:
        """获取 generation; 发布进行中时在超时窗口内轮询, 可被取消事件打断。"""
        deadline = time.monotonic() + timeout_s
        from app.services.daily_health import DailyIntegrityError

        while True:
            try:
                return self.data_generation(asset_type)
            except DailyIntegrityError:
                raise
            except EnrichedGenerationUnavailableError:
                if cancel_event is not None and cancel_event.is_set():
                    raise
                if time.monotonic() >= deadline:
                    raise
                time.sleep(_GENERATION_POLL_S)

    def assert_data_generation(
        self,
        asset_type: str,
        expected: str | None,
    ) -> None:
        if expected is None:
            return
        current = self.data_generation(asset_type)
        if current != expected:
            raise EnrichedGenerationUnavailableError(
                "enriched data changed while the snapshot was being read"
            )

    def _range_cache_generation(self, asset_type: str, end: date, expected: str | None) -> str | None:
        loader = getattr(self.repo, "get_enriched_generation_snapshot", None)
        if not callable(loader) or expected is None:
            return expected
        snapshot = loader(asset_type)
        if snapshot.generation != expected:
            raise EnrichedGenerationUnavailableError(
                "enriched data changed while the snapshot was being read"
            )
        return snapshot.through(end)

    def load_panel(
        self,
        symbols: list[str] | None,
        start: date,
        end: date,
        columns: list[str] | None = None,
        asset_type: str = "stock",
        *,
        expected_generation: str | None = None,
    ) -> pl.DataFrame:
        """加载 enriched 数据面板，带缓存。asset_type='etf' 时读 ETF enriched。"""
        # 快照世代路径也给一次重试 (issue #517): 盘中刷新每轮换新 generation,
        # 读后校验失败时重读「现役世代」而不是死等已作废的旧世代。
        attempts = 2
        expected = expected_generation
        for attempt in range(attempts):
            generation = (
                expected
                if expected is not None
                else self.data_generation(asset_type)
            )
            try:
                # Cache identity follows the consumed prefix. The global generation
                # still guards the read, including in-progress/partial publications.
                cache_generation = self._range_cache_generation(asset_type, end, generation)
                panel = self._cache.get_or_compute(
                    symbols,
                    start,
                    end,
                    columns,
                    self._load_panel_inner,
                    asset_type=asset_type,
                    generation=cache_generation,
                )
                self.assert_data_generation(asset_type, generation)
            except EnrichedGenerationUnavailableError:
                if attempt + 1 >= attempts:
                    raise
                expected = None
                continue
            return panel
        raise EnrichedGenerationUnavailableError(
            "unable to read a stable enriched data snapshot"
        )

    def load_panel_for_backtest(
        self,
        symbols: list[str] | None,
        start: date,
        end: date,
        feature_plan,
        asset_type: str = "stock",
        *,
        evidence=None,
        price_basis: BacktestPriceBasis = "qfq",
        raw_execution_quotes: pl.DataFrame | None = None,
    ) -> pl.DataFrame:
        """按解析后的依赖加载窄基础列并计算回测所需特征。"""
        columns = sorted(feature_plan.base_columns)
        generation = None
        if self.snapshot is not None:
            df = self.snapshot.read_panel(self.repo, symbols, start, end, columns)
        else:
            generation = self.data_generation_await(asset_type) if price_basis == "hfq" else None
            df = self.load_panel(
                symbols,
                start,
                end,
                columns=columns,
                asset_type=asset_type,
                **({"expected_generation": generation} if generation is not None else {}),
            )
        if df.is_empty():
            return df
        if raw_execution_quotes is not None:
            from app.backtest.dynamic_daily import attach_exchange_quotes, current_replay

            df = attach_exchange_quotes(df, raw_execution_quotes)
            if price_basis == "dynamic":
                df = current_replay().continuous_panel(df, self.repo.store.data_dir, asset_type)
        if self.snapshot is None and price_basis == "hfq":
            df = BackwardAdjustment(self.repo.store.data_dir, asset_type).panel(df)
            self.assert_data_generation(asset_type, generation)

        from app.backtest.fundamentals import (
            load_fundamental_snapshot,
        )

        fundamental_names = sorted(
            getattr(feature_plan, "fundamental_columns", frozenset())
            or frozenset()
        )
        financials = load_fundamental_snapshot(
            self.repo.store.data_dir if self.repo is not None else None,
        ) if fundamental_names else None
        instruments = (
            self.repo.get_instruments_asset(asset_type)
            if self.repo is not None
            else pl.DataFrame()
        )
        historical_shares = (
            self.repo.get_historical_shares()
            if asset_type == "stock" and self.repo is not None and not instruments.is_empty()
            else None
        )
        if evidence is not None:
            from app.factors.ext_factors import attach_ext_columns

            # 扩展时序值也要固定在本次原始快照内；检查期间不重新读取磁盘。
            inputs = self._entry_data
            locked_inputs = inputs is not None and (inputs.version is not None or inputs.configs())
            df = attach_ext_columns(
                df, include_snapshot=False, data_dir=self.repo.store.data_dir,
                **({"inputs": inputs} if locked_inputs else {}),
            )

        def prepare(raw):
            return self._prepare_panel_features(
                raw, feature_plan, instruments, historical_shares, financials,
                attach_extensions=evidence is None,
            )

        if evidence is not None:
            evidence.capture_panel(df, prepare, {
                "instruments": instruments, "historical_shares": historical_shares,
                "financial_announcements": financials,
            })
        return prepare(df)

    @staticmethod
    def _prepare_panel_features(
        df, feature_plan, instruments, historical_shares, financials, *, attach_extensions=True,
    ):
        from app.backtest.fundamentals import attach_fundamental_factors
        from app.indicators.pipeline import (
            compute_indicators,
            compute_limit_signals,
            compute_signals,
        )

        fundamental_names = sorted(getattr(feature_plan, "fundamental_columns", ()) or ())
        if fundamental_names:
            df = attach_fundamental_factors(df, financials, fundamental_names)
        matrix_native = feature_plan.execution_backend == "matrix_native"
        if not matrix_native:
            df = compute_indicators(df, needed=set(feature_plan.indicator_columns))
            df = compute_signals(
                df, needed=set(feature_plan.signal_columns),
                **({"attach_extensions": False} if not attach_extensions else {}),
            )
        if not instruments.is_empty():
            df = compute_limit_signals(
                df,
                instruments,
                needed={"signal_limit_up", "signal_limit_down"}
                if matrix_native
                else set(feature_plan.signal_columns),
                historical_shares=historical_shares,
            )
            join_cols = ["symbol"] if "symbol" in instruments.columns else []
            join_cols.extend(
                col for col in sorted(feature_plan.instrument_columns)
                if col in instruments.columns and col not in df.columns
            )
            if len(join_cols) > 1:
                df = df.join(
                    instruments.select(join_cols).unique(subset=["symbol"]),
                    on="symbol",
                    how="left",
                )

        required_columns = set(feature_plan.base_columns) | set(feature_plan.instrument_columns)
        required_columns |= set(feature_plan.signal_columns)
        if not matrix_native:
            required_columns |= set(feature_plan.indicator_columns)
        missing_columns = required_columns - set(df.columns)
        if missing_columns:
            raise ValueError(f"回测字段依赖未生成: {sorted(missing_columns)}")

        float_cols = [c for c in df.columns if df[c].dtype.is_float()]
        if float_cols:
            df = df.with_columns([
                pl.when(pl.col(c).is_nan() | pl.col(c).is_infinite())
                .then(None)
                .otherwise(pl.col(c))
                .alias(c)
                for c in float_cols
            ])
        return df

    def load_market_data_matrix_for_backtest(
        self,
        symbols: list[str] | None,
        start: date,
        end: date,
        feature_plan,
        asset_type: str = "stock",
        *,
        cache_profile: MatrixCacheProfile | None = None,
        coverage_start: date | None = None,
        coverage_end: date | None = None,
        expected_generation: str | None = None,
        cancel_event: threading.Event | None = None,
        price_basis: BacktestPriceBasis = "qfq",
    ) -> MarketDataMatrix:
        """Load a matrix-native backtest directly from projected parquet batches."""
        if feature_plan.execution_backend != "matrix_native":
            raise ValueError("direct market matrix loading requires matrix_native backend")
        from app.tickflow.repository import enriched_dirname

        parquet_root = self.repo.store.data_dir / enriched_dirname(asset_type)
        instruments = self.repo.get_instruments_asset(asset_type)
        if self._evidence is not None:
            self._evidence.record_frame("instruments", instruments)
        field_columns = (
            set(feature_plan.base_columns)
            | set(feature_plan.instrument_columns)
            | set(feature_plan.matrix_columns)
        )
        # 扩展时序数值字段不进 parquet 解析 (enriched 只落 14 存储列), 矩阵构建后
        # 按 (日期, 标的) 对齐现场注入 — 与财务因子 attach 同构。快照模式/字符串
        # 字段/未注册名在此拦截 (快照回看历史 = 未来函数)。
        from app.factors.ext_factors import numeric_timeseries_ext_columns

        ext_wanted = frozenset(c for c in field_columns if c.startswith("ext_"))
        if ext_wanted:
            invalid_ext = ext_wanted - numeric_timeseries_ext_columns(
                self.repo.store.data_dir, inputs=self._entry_data,
            )
            if invalid_ext:
                raise ValueError(
                    f"扩展字段不可用于矩阵回测 (字段已失效，或不是时序模式的数值字段): "
                    f"{sorted(invalid_ext)}。请在策略设置中移除或替换相关评分因子、叠加条件；"
                    "若策略源码直接引用该字段，请同步修改源码，或恢复对应的时序扩展数据。"
                )
            field_columns = field_columns - ext_wanted
        if self.snapshot is not None:
            panel = self.snapshot.read_panel(self.repo, symbols, start, end, field_columns)
            market = load_market_data_matrix_from_daily_panel(
                panel, start, end, field_columns=field_columns, instruments=instruments,
                cancel_event=cancel_event,
            )
            return self._attach_market_fields(market, feature_plan, ext_wanted)

        cache_root = (
            self.repo.store.data_dir / ".backtest_matrix_cache"
            if settings.backtest_matrix_disk_cache_enabled
            else None
        )
        cache_fields = (
            cache_profile.field_columns
            if cache_profile is not None
            else frozenset(field_columns)
        )
        # 缓存档包含其他策略的依赖; 所有扩展列均须留到当前策略的现场注入阶段,
        # 否则无关策略残留的旧扩展字段也会被当作 parquet 必需列, 拖垮整个共享缓存。
        cache_fields = frozenset(name for name in cache_fields if not name.startswith("ext_"))
        cache_max_bytes = (
            cache_profile.max_disk_bytes
            if cache_profile is not None
            else settings.backtest_matrix_cache_max_mb * 1024 * 1024
        )
        source_generation = (
            expected_generation
            if expected_generation is not None
            else self.data_generation_await(asset_type, cancel_event=cancel_event)
        )
        # 快照世代路径也给第二次机会 (issue #517): 循环内撞换代时下方 except
        # 已用 data_generation_await 刷新世代, 这里只需不再限定单次尝试。
        attempts = 2
        for attempt in range(attempts):
            try:
                cache_generation = self._range_cache_generation(
                    asset_type, coverage_end or end, source_generation,
                )
                market = load_market_data_matrix_from_parquet(
                    parquet_root,
                    start,
                    end,
                    field_columns=field_columns,
                    symbols=symbols,
                    instruments=instruments,
                    cache_root=cache_root,
                    coverage_start=coverage_start,
                    coverage_end=coverage_end,
                    cache_field_columns=cache_fields,
                    cache_max_bytes=cache_max_bytes,
                    profile_generation=(
                        cache_profile.generation if cache_profile is not None else "request"
                    ),
                    source_generation=cache_generation,
                    cancel_event=cancel_event,
                )
                if price_basis == "hfq":
                    market = BackwardAdjustment(self.repo.store.data_dir, asset_type).matrix(market)
                self.assert_data_generation(asset_type, source_generation)
                return self._attach_market_fields(market, feature_plan, ext_wanted)
            except EnrichedGenerationUnavailableError:
                if attempt + 1 >= attempts:
                    raise
                source_generation = self.data_generation_await(
                    asset_type, cancel_event=cancel_event
                )
            except pa.ArrowException as exc:
                raise ValueError(f"direct market matrix parquet scan failed: {exc}") from exc
        raise EnrichedGenerationUnavailableError(
            "unable to read a stable enriched matrix snapshot"
        )

    def _attach_market_fields(self, market, feature_plan, ext_wanted):
        from app.backtest.fundamentals import attach_matrix_fundamental_fields

        fundamental_names = sorted(
            getattr(feature_plan, "fundamental_columns", frozenset())
            or frozenset()
        )
        if fundamental_names:
            # 财务因子不落 enriched 存储: 矩阵加载后按公告日门控附加字段。
            market = attach_matrix_fundamental_fields(
                market,
                self.repo.store.data_dir if self.repo is not None else None,
                fundamental_names,
                evidence=self._evidence,
            )
        if ext_wanted:
            from app.factors.ext_factors import attach_matrix_ext_fields

            market = attach_matrix_ext_fields(
                market,
                self.repo.store.data_dir if self.repo is not None else None,
                ext_wanted,
                inputs=self._entry_data,
            )
        return market

    def cache_stats(self) -> dict:
        """暴露 PanelCache 遥测快照 (扫盘耗时/次数/命中/复用), 供上层量化 IO 占比。"""
        return self._cache.stats()

    def clear_panel_cache(self) -> None:
        self._cache.invalidate()

    def _load_panel_inner(
        self,
        symbols: list[str] | None,
        start: date,
        end: date,
        columns: list[str] | None = None,
        asset_type: str = "stock",
    ) -> pl.DataFrame:
        t0 = time.perf_counter()

        # 近期区间优先复用 repository 的预计算 enriched 历史缓存 (仅 stock: 该缓存为股票专用)。
        try:
            if columns is None and asset_type == "stock" and self.repo is not None and hasattr(self.repo, "get_enriched_range"):
                cached = self.repo.get_enriched_range(start, end, symbols=symbols, columns=columns)
                if cached is not None and not cached.is_empty():
                    elapsed = (time.perf_counter() - t0) * 1000
                    logger.info("load_panel(cache): %.0fms, %d rows, %d columns", elapsed, len(cached), len(cached.columns))
                    return cached
        except Exception as e:  # noqa: BLE001
            logger.debug("backtest load panel cache miss: %s", e)

        from app.tickflow.repository import enriched_dirname
        enriched_glob = str(self.repo.store.data_dir / enriched_dirname(asset_type) / "**" / "*.parquet")

        try:
            lf = scan_enriched_parquet(enriched_glob)
            if symbols is not None:
                lf = lf.filter(pl.col("symbol").is_in(symbols))
            if columns is not None:
                available = set(lf.collect_schema().names())
                selected = [c for c in columns if c in available]
                if "symbol" not in selected and "symbol" in available:
                    selected.insert(0, "symbol")
                if "date" not in selected and "date" in available:
                    selected.insert(1, "date")
                lf = lf.select(selected)
            df = (
                lf.filter(
                    (pl.col("date") >= start)
                    & (pl.col("date") <= end)
                )
                .sort(["symbol", "date"])
                .collect(streaming=True)
            )
        except Exception as e:
            logger.warning("backtest load panel failed: %s", e)
            return pl.DataFrame()

        if df.is_empty():
            return df

        if columns is not None:
            elapsed = (time.perf_counter() - t0) * 1000
            logger.info("load_panel: %.0fms, %d rows, %d columns", elapsed, len(df), len(df.columns))
            return df

        from app.indicators.pipeline import compute_all
        # 按 asset_type 取维表: ETF 回测须用 ETF 维表, 否则名称 JOIN 失败(全 null)、
        # 涨停信号算在错误的 instruments 上。
        instruments = self.repo.get_instruments_asset(asset_type)
        df = compute_all(df, instruments=instruments)
        if not instruments.is_empty() and "name" not in df.columns:
            inst_cols = [c for c in ["symbol", "name"] if c in instruments.columns]
            if len(inst_cols) == 2:
                df = df.join(
                    instruments.select(inst_cols).unique(subset=["symbol"]),
                    on="symbol",
                    how="left",
                )

        elapsed = (time.perf_counter() - t0) * 1000
        logger.info("load_panel: %.0fms, %d rows", elapsed, len(df))
        return df

    # ── 撮合模拟 ──────────────────────────────────────

    # 分钟K cache 存储的数值列及固定顺序 (_resolve_minute_fill 按此顺序整数索引)。
    _MINUTE_NUMERIC_COLS = ["open", "high", "low", "close", "volume", "amount"]

    @staticmethod
    def _load_minute_for_fills(
        repo,
        symbols: list[str],
        dates_needed: set,
        asset_type: str,
        *, evidence=None,
    ) -> dict:
        """按触发日加载分钟K, 返回 {(symbol, date_str): float64 2D ndarray}。

        dates_needed: 需要分钟数据的日期集合 (set of date strings "YYYY-MM-DD")

        按触发日分批读取对应分区文件 (get_minute_by_dates), 而非扫描整个区间
        (get_minute_range)。内存与回测区间长度解耦, 只随触发日数量增长 ——
        触发日稀疏时避免读取区间内大量无关日期导致爆内存。

        cache 值为 float64 紧凑 2D 数组 (列顺序见 _MINUTE_NUMERIC_COLS), 而非
        完整 DataFrame, 避免每条记录携带 polars 元数据开销导致内存膨胀。
        """
        if not symbols or not dates_needed:
            return {}
        from datetime import date as _date
        sorted_date_strs = sorted(dates_needed)
        date_objs = [_date.fromisoformat(s) for s in sorted_date_strs]

        cache: dict = {}
        # 分批读取: 每批 50 个交易日, 处理完拼进 cache, 避免单批过大。
        BATCH = 50
        numeric_cols = BacktestEngine._MINUTE_NUMERIC_COLS
        from app.services.minute_adjust import MinutePriceBasisError

        reader = getattr(repo, "get_minute_for_backtest", None) or repo.get_minute_by_dates
        for i in range(0, len(date_objs), BATCH):
            batch = date_objs[i:i + BATCH]
            try:
                df = reader(symbols, batch, asset_type=asset_type)
            except MinutePriceBasisError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("minute fill data load failed (batch %d-%d): %s", i, i + len(batch), e)
                continue
            if df.is_empty():
                continue
            if evidence is not None:
                evidence.record_frame(f"minute_fill/{asset_type}/{batch[0]}/{batch[-1]}", df)
            # 按 (symbol, 日期) 分组, 每组转紧凑 float64 数组存入 cache
            df = df.sort(["symbol", "datetime"]).with_columns(
                pl.col("datetime").dt.strftime("%Y-%m-%d").alias("_d_str")
            )
            for sub in df.partition_by(["symbol", "_d_str"], as_dict=False):
                if sub.is_empty():
                    continue
                sym = sub["symbol"][0]
                d_str = sub["_d_str"][0]
                cols = [c for c in numeric_cols if c in sub.columns]
                cache[(sym, d_str)] = sub.select(
                    [pl.col(c).cast(pl.Float64) for c in cols]
                ).to_numpy()
        return cache

    def _execution(self) -> LegacySimulator:
        # The host owns minute I/O and evidence; the simulator sees only arrays.
        return LegacySimulator(
            minute_loader=lambda symbols, dates, asset_type: self._load_minute_for_fills(
                self.repo,
                symbols,
                dates,
                asset_type,
                evidence=self._evidence,
            )
        )

    def simulate(
        self,
        panel: pl.DataFrame,
        entries: pl.Series | None,
        exits: pl.Series | None,
        config: MatcherConfig,
        entry_signal_ids: list[str] | None = None,
        exit_signal_ids: list[str] | None = None,
    ) -> SimResult:
        return self._execution().simulate(
            panel, entries, exits, config, entry_signal_ids, exit_signal_ids
        )

    def simulate_independent_candidates(
        self,
        panel: pl.DataFrame,
        entries: pl.Series | None,
        exits: pl.Series | None,
        config: MatcherConfig,
        progress_cb: Callable[[dict], None] | None = None,
        cancel_event: threading.Event | None = None,
        entry_signal_ids: list[str] | None = None,
        exit_signal_ids: list[str] | None = None,
    ) -> SimResult:
        return self._execution().simulate_independent_candidates(
            panel,
            entries,
            exits,
            config,
            progress_cb,
            cancel_event,
            entry_signal_ids,
            exit_signal_ids,
        )

    _resolve_entry_prices = staticmethod(LegacySimulator._resolve_entry_prices)

    def _simulate_independent_matrix(
        self,
        matrix: MarketMatrix,
        raw_candidates: int,
        config: MatcherConfig,
        progress_cb: Callable[[dict], None] | None,
        cancel_event: threading.Event | None,
        options: SimulationOptions | None = None,
    ) -> SimResult:
        return self._execution()._simulate_independent_matrix(
            matrix, raw_candidates, config, progress_cb, cancel_event, options
        )

    def simulate_independent_market_matrix(
        self,
        matrix: MarketMatrix,
        raw_candidates: int,
        config: MatcherConfig,
        progress_cb: Callable[[dict], None] | None = None,
        cancel_event: threading.Event | None = None,
        options: SimulationOptions | None = None,
    ) -> SimResult:
        return self._execution().simulate_independent_market_matrix(
            matrix, raw_candidates, config, progress_cb, cancel_event, options
        )

    def simulate_independent_candidates_legacy(
        self,
        panel: pl.DataFrame,
        entries: pl.Series | None,
        exits: pl.Series | None,
        config: MatcherConfig,
        progress_cb: Callable[[dict], None] | None = None,
        cancel_event: threading.Event | None = None,
        entry_signal_ids: list[str] | None = None,
        exit_signal_ids: list[str] | None = None,
    ) -> SimResult:
        return self._execution().simulate_independent_candidates_legacy(
            panel,
            entries,
            exits,
            config,
            progress_cb,
            cancel_event,
            entry_signal_ids,
            exit_signal_ids,
        )

    _resolve_minute_fill = staticmethod(LegacySimulator._resolve_minute_fill)

    _resolve_minute_exit_trigger = staticmethod(LegacySimulator._resolve_minute_exit_trigger)

    def simulate_portfolio(
        self,
        panel: pl.DataFrame,
        entries: pl.Series | None,
        exits: pl.Series | None,
        config: MatcherConfig,
        progress_cb: Callable[[dict], None] | None = None,
        cancel_event: threading.Event | None = None,
        entry_signal_ids: list[str] | None = None,
        exit_signal_ids: list[str] | None = None,
    ) -> SimResult:
        return self._execution().simulate_portfolio(
            panel,
            entries,
            exits,
            config,
            progress_cb,
            cancel_event,
            entry_signal_ids,
            exit_signal_ids,
        )

    def simulate_market_matrix(
        self,
        matrix: MarketMatrix,
        config: MatcherConfig,
        progress_cb: Callable[[dict], None] | None = None,
        cancel_event: threading.Event | None = None,
        options: SimulationOptions | None = None,
    ) -> SimResult:
        return self._execution().simulate_market_matrix(
            matrix, config, progress_cb, cancel_event, options
        )

    def _simulate_portfolio_matrix(
        self,
        matrix: MarketMatrix,
        config: MatcherConfig,
        progress_cb: Callable[[dict], None] | None,
        cancel_event: threading.Event | None,
        options: SimulationOptions | None = None,
    ) -> SimResult:
        return self._execution()._simulate_portfolio_matrix(
            matrix, config, progress_cb, cancel_event, options
        )

    def simulate_portfolio_legacy(
        self,
        panel: pl.DataFrame,
        entries: pl.Series | None,
        exits: pl.Series | None,
        config: MatcherConfig,
        progress_cb: Callable[[dict], None] | None = None,
        cancel_event: threading.Event | None = None,
        entry_signal_ids: list[str] | None = None,
        exit_signal_ids: list[str] | None = None,
    ) -> SimResult:
        return self._execution().simulate_portfolio_legacy(
            panel,
            entries,
            exits,
            config,
            progress_cb,
            cancel_event,
            entry_signal_ids,
            exit_signal_ids,
        )

    _reconcile_terminal_curve = staticmethod(LegacySimulator._reconcile_terminal_curve)

    _build_curves = staticmethod(LegacySimulator._build_curves)

    _sortino_ratio = staticmethod(LegacySimulator._sortino_ratio)

    _mc_drawdown_percentiles = staticmethod(LegacySimulator._mc_drawdown_percentiles)

    _per_trade_block = staticmethod(LegacySimulator._per_trade_block)

    _calc_stats = staticmethod(LegacySimulator._calc_stats)

    _calc_per_symbol = staticmethod(LegacySimulator._calc_per_symbol)

    _calc_independent_candidate_result = staticmethod(
        LegacySimulator._calc_independent_candidate_result
    )

    _calc_portfolio_stats = staticmethod(LegacySimulator._calc_portfolio_stats)

    _calc_portfolio_stats_from_values = staticmethod(
        LegacySimulator._calc_portfolio_stats_from_values
    )

    _date_str = staticmethod(LegacySimulator._date_str)

    _empty_result = staticmethod(LegacySimulator._empty_result)

    cross_section_rank = staticmethod(LegacySimulator.cross_section_rank)

    cross_section_qcut = staticmethod(LegacySimulator.cross_section_qcut)
