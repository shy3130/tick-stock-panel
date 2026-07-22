"""回测引擎 — 共享数据加载 + 撮合 + 统计计算。

纯 Polars/NumPy 实现，不依赖 pandas/vectorbt。
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Literal

import numpy as np
import polars as pl
import pyarrow as pa

from app.backtest.matrix import (
    MarketDataMatrix,
    MarketMatrix,
    MatrixCacheProfile,
    build_market_matrix,
    load_market_data_matrix_from_parquet,
)
from app.config import settings
from app.parquet import scan_enriched_parquet
from app.tickflow.repository import KlineRepository

logger = logging.getLogger(__name__)


def _matrix_entry_score(matrix: MarketMatrix, time_id: int, asset_id: int) -> float:
    source_time = int(matrix.entry_signal_time[time_id, asset_id])
    if source_time < 0:
        return 0.0
    return float(matrix.score[source_time, asset_id])


# ================================================================
# 数据结构
# ================================================================

@dataclass
class MatcherConfig:
    # matching 为向后兼容入口: 仅传 matching 时, entry_fill/exit_fill 都取 matching 的值。
    # 显式传入 entry_fill/exit_fill 时以二者为准 (允许建仓/清仓口径不同)。
    matching: Literal["close_t", "open_t+1"] = "close_t"
    entry_fill: Literal["close_t", "open_t+1"] | None = None
    exit_fill: Literal["close_t", "open_t+1", "signal_next_minute"] | None = None
    # 成本模型: 优先使用拆分口径 (佣金双边 + 印花税仅卖出 + 滑点双边)。
    # 未设 commission_pct 时回退到 fees_pct 作为双边佣金 (向后兼容, 无印花税)。
    fees_pct: float = 0.0002
    commission_pct: float | None = None
    stamp_tax_pct: float | None = None
    slippage_bps: float = 5.0
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    trailing_stop_pct: float | None = None
    trailing_take_profit_activate_pct: float | None = None
    trailing_take_profit_drawdown_pct: float | None = None
    max_hold_days: int | None = None
    max_positions: int = 10
    max_exposure_pct: float = 1.0
    score_min: float | None = None
    score_max: float | None = None
    initial_capital: float = 1_000_000.0
    position_sizing: Literal["equal", "score_weight"] = "equal"
    # 分钟K精确成交: 开启后, 信号触发日的成交价用当日分钟K优化
    # (有参考线→穿越价, 无参考线→VWAP)。数据缺失时降级为日K口径。
    minute_fill: bool = False
    # regime 软叠加: 与矩阵 time 轴对齐的牛市标记 (True=牛/满仓, False=熊/缩放)。
    # 非 None 时, 熊市日 max_exposure_pct 乘以 regime_bear_weight (软减仓而非禁止开仓)。
    regime_allow: list[bool] | None = None
    regime_bear_weight: float = 0.3

    def __post_init__(self) -> None:
        # 解析最终口径: 优先 entry_fill/exit_fill, 否则回退到 matching (向后兼容)。
        if self.entry_fill is None:
            self.entry_fill = self.matching
        if self.exit_fill is None:
            self.exit_fill = self.matching

    def _commission_pct(self) -> float:
        # commission_pct 显式给出时优先, 否则回退 fees_pct (向后兼容双边佣金)。
        return self.commission_pct if self.commission_pct is not None else self.fees_pct

    def buy_cost_pct(self) -> float:
        # 买入腿: 佣金 + 滑点。
        return self._commission_pct() + self.slippage_bps / 10000.0

    def sell_cost_pct(self) -> float:
        # 卖出腿: 佣金 + 印花税 + 滑点。印花税未设时为 0 (向后兼容)。
        stamp = self.stamp_tax_pct if self.stamp_tax_pct is not None else 0.0
        return self._commission_pct() + stamp + self.slippage_bps / 10000.0


@dataclass
class TradeRecord:
    symbol: str
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    pnl_pct: float
    duration: int
    exit_reason: str  # "signal" | "stop_loss" | "take_profit" | "trailing_stop" | "trailing_take_profit" | "max_hold" | "end"
    # 退出优先级 (高→低): pending_exit(历史挂单) > 风控(止损/移动止损/移动止盈) > signal(卖点) > max_hold(到期) > end
    name: str = ""
    shares: float = 0.0
    lots: float = 0.0
    position_pct: float = 0.0
    entry_value: float = 0.0
    exit_value: float = 0.0
    pnl_amount: float = 0.0
    entry_score: float | None = None
    entry_signal_date: date | str | None = None
    exit_signal_date: date | str | None = None
    blocked_exit_days: int = 0
    # 触发买入/卖出的具体信号列名 (如 signal_ma_golden_5_20 / csg_xxx);
    # 仅当该腿由信号触发时填充, 止损/止盈/到期等非信号退出时 exit_signal_id 为 None。
    entry_signal_id: str | None = None
    exit_signal_id: str | None = None


@dataclass
class SimResult:
    equity_curve: list[dict]       # [{date, value}]
    drawdown_curve: list[dict]     # [{date, value}]
    trades: list[TradeRecord]
    per_symbol_stats: list[dict]
    stats: dict


@dataclass(frozen=True)
class SimulationOptions:
    """Controls expensive result materialization without changing matching semantics."""

    include_monte_carlo: bool = True
    include_curves: bool = True
    include_trades: bool = True
    include_per_symbol_stats: bool = True
    include_return_distribution: bool = True


def _resolve_signal_id(panel: pl.DataFrame, idx: int, signal_ids: list[str] | None) -> str | None:
    """在触发行 idx 上, 从候选信号里找出 panel 列为 True 的那个, 返回其列名。

    多个信号同时为 True 时返回第一个匹配的 (信号 OR 关系, 回测只记录其一即可)。
    signal_ids 元素可能带 signal_/csg_ 前缀, 也可能是裸名 (如 "ma_golden_5_20")。
    """
    if not signal_ids:
        return None
    for sid in signal_ids:
        col = sid if (sid.startswith("signal_") or sid.startswith("csg_")) else f"signal_{sid}"
        if col not in panel.columns:
            continue
        try:
            if bool(panel[col][idx]):
                return col
        except (IndexError, TypeError):
            continue
    return None


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
    ) -> pl.DataFrame:
        key = self._make_key(symbols, start, end, columns, asset_type)
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
    def _make_key(symbols: list[str] | None, start: date, end: date, columns: list[str] | None, asset_type: str = "stock") -> str:
        if symbols is None:
            h = "all"
        else:
            h = hashlib.md5(",".join(sorted(symbols)).encode()).hexdigest()[:12]
        cols = "all" if columns is None else hashlib.md5(",".join(sorted(columns)).encode()).hexdigest()[:8]
        return f"{asset_type}:{h}:{start}:{end}:{cols}"


# ================================================================
# BacktestEngine
# ================================================================

class BacktestEngine:
    """回测引擎 — 数据加载 + 撮合模拟 + 统计计算。"""

    def __init__(self, repo: KlineRepository) -> None:
        self.repo = repo
        self._cache = PanelCache()

    # ── 数据加载 ──────────────────────────────────────

    def load_panel(
        self,
        symbols: list[str] | None,
        start: date,
        end: date,
        columns: list[str] | None = None,
        asset_type: str = "stock",
    ) -> pl.DataFrame:
        """加载 enriched 数据面板，带缓存。asset_type='etf' 时读 ETF enriched。"""
        return self._cache.get_or_compute(symbols, start, end, columns, self._load_panel_inner, asset_type=asset_type)

    def load_panel_for_backtest(
        self,
        symbols: list[str] | None,
        start: date,
        end: date,
        feature_plan,
        asset_type: str = "stock",
    ) -> pl.DataFrame:
        """按解析后的依赖加载窄基础列并计算回测所需特征。"""
        from app.indicators.pipeline import (
            compute_indicators,
            compute_limit_signals,
            compute_signals,
        )

        df = self.load_panel(
            symbols,
            start,
            end,
            columns=sorted(feature_plan.base_columns),
            asset_type=asset_type,
        )
        if df.is_empty():
            return df

        instruments = (
            self.repo.get_instruments_asset(asset_type)
            if self.repo is not None
            else pl.DataFrame()
        )
        matrix_native = feature_plan.execution_backend == "matrix_native"
        if not matrix_native:
            df = compute_indicators(df, needed=set(feature_plan.indicator_columns))
            df = compute_signals(df, needed=set(feature_plan.signal_columns))
        if not instruments.is_empty():
            df = compute_limit_signals(
                df,
                instruments,
                needed={"signal_limit_up", "signal_limit_down"}
                if matrix_native
                else set(feature_plan.signal_columns),
                historical_shares=(
                    self.repo.get_historical_shares()
                    if asset_type == "stock" and self.repo is not None
                    else None
                ),
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
    ) -> MarketDataMatrix:
        """Load a matrix-native backtest directly from projected parquet batches."""
        if feature_plan.execution_backend != "matrix_native":
            raise ValueError("direct market matrix loading requires matrix_native backend")
        from app.tickflow.repository import enriched_dirname

        parquet_root = self.repo.store.data_dir / enriched_dirname(asset_type)
        instruments = self.repo.get_instruments_asset(asset_type)
        field_columns = (
            set(feature_plan.base_columns)
            | set(feature_plan.instrument_columns)
            | set(feature_plan.matrix_columns)
        )
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
        cache_max_bytes = (
            cache_profile.max_disk_bytes
            if cache_profile is not None
            else settings.backtest_matrix_cache_max_mb * 1024 * 1024
        )
        generation_loader = getattr(self.repo, "get_matrix_data_generation", None)
        source_generation = (
            generation_loader(asset_type)
            if cache_root is not None and callable(generation_loader)
            else None
        )
        try:
            return load_market_data_matrix_from_parquet(
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
                source_generation=source_generation,
            )
        except pa.ArrowException as exc:
            raise ValueError(f"direct market matrix parquet scan failed: {exc}") from exc

    def cache_stats(self) -> dict:
        """暴露 PanelCache 遥测快照 (扫盘耗时/次数/命中/复用), 供上层量化 IO 占比。"""
        return self._cache.stats()

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

    def simulate(
        self,
        panel: pl.DataFrame,
        entries: pl.Series | None,
        exits: pl.Series | None,
        config: MatcherConfig,
        entry_signal_ids: list[str] | None = None,
        exit_signal_ids: list[str] | None = None,
    ) -> SimResult:
        """纯 NumPy 撮合模拟 — 逐 symbol 状态机。"""
        if panel.is_empty():
            return self._empty_result()

        n = len(panel)
        panel_dates = panel["date"].to_numpy()
        panel_symbols = panel["symbol"].to_numpy()

        # 构建信号数组
        ent = np.zeros(n, dtype=bool)
        ext = np.zeros(n, dtype=bool)
        if entries is not None and len(entries) == n:
            ent = entries.to_numpy().astype(bool)
        if exits is not None and len(exits) == n:
            ext = exits.to_numpy().astype(bool)

        if not ent.any():
            return self._empty_result()

        # 成交口径: entry/exit 可分别配置 close_t (信号当日收盘) 或 open_t+1 (次日开盘)。
        # open_t+1 时信号右移 1 天 (用前一根的信号 + 当根的 open 成交)。
        open_prices = panel["open"].to_numpy()
        close_prices = panel["close"].to_numpy()

        # 同一 symbol 内相邻行掩码, 跨 symbol 边界不允许 shift (避免错配)。
        same_prev_symbol = np.zeros(n, dtype=bool)
        same_prev_symbol[1:] = panel_symbols[1:] == panel_symbols[:-1]

        entry_prices = open_prices if config.entry_fill == "open_t+1" else close_prices
        exit_prices = open_prices if config.exit_fill == "open_t+1" else close_prices

        if config.entry_fill == "open_t+1":
            ent_s = np.zeros(n, dtype=bool)
            ent_s[1:] = ent[:-1] & same_prev_symbol
            ent = ent_s
        if config.exit_fill == "open_t+1":
            ext_s = np.zeros(n, dtype=bool)
            ext_s[1:] = ext[:-1] & same_prev_symbol
            ext = ext_s

        # 逐 symbol 撮合
        trades: list[TradeRecord] = []
        unique_symbols = np.unique(panel_symbols)

        for sym in unique_symbols:
            mask = panel_symbols == sym
            sym_ent = ent[mask]
            sym_ext = ext[mask]
            sym_entry_prices = entry_prices[mask]
            sym_exit_prices = exit_prices[mask]
            sym_close = close_prices[mask]
            sym_dates = panel_dates[mask]

            holding = False
            entry_idx = -1
            entry_price = 0.0
            hold_days = 0

            for i in range(len(sym_ent)):
                if not holding:
                    if sym_ent[i]:
                        holding = True
                        entry_idx = i
                        entry_price = float(sym_entry_prices[i])
                        hold_days = 0
                else:
                    hold_days += 1
                    exit_triggered = False
                    exit_reason = ""

                    # 止损 — 用当日 close 检测 (优先级最高)
                    if config.stop_loss_pct is not None:
                        pnl = (float(sym_close[i]) - entry_price) / entry_price
                        if pnl <= -abs(config.stop_loss_pct):
                            exit_triggered = True
                            exit_reason = "stop_loss"

                    # 信号退出 (优先于 max_hold: 卖点信号是策略主动离场)
                    if not exit_triggered and sym_ext[i]:
                        exit_triggered = True
                        exit_reason = "signal"

                    # 最大持仓天数 (兜底: 无信号/未止损时强制平仓)
                    if not exit_triggered and config.max_hold_days is not None:
                        if hold_days >= config.max_hold_days:
                            exit_triggered = True
                            exit_reason = "max_hold"

                    if exit_triggered:
                        exit_price = float(sym_exit_prices[i])
                        pnl_pct = (exit_price - entry_price) / entry_price if entry_price > 0 else 0.0
                        fee_cost = config.buy_cost_pct() + config.sell_cost_pct()
                        pnl_pct -= fee_cost

                        e_date = sym_dates[entry_idx]
                        x_date = sym_dates[i]
                        trades.append(TradeRecord(
                            symbol=str(sym),
                            entry_date=e_date.item() if hasattr(e_date, "item") else e_date,
                            exit_date=x_date.item() if hasattr(x_date, "item") else x_date,
                            entry_price=round(entry_price, 4),
                            exit_price=round(exit_price, 4),
                            pnl_pct=round(pnl_pct, 6),
                            duration=int(hold_days),
                            exit_reason=exit_reason,
                        ))
                        holding = False

        # 净值曲线: 按出场日期归集收益
        all_dates_sorted = np.sort(np.unique(panel_dates))
        equity_curve, drawdown_curve = self._build_curves(trades, all_dates_sorted, config.initial_capital)

        # 统计
        date_min = panel_dates.min()
        date_max = panel_dates.max()
        d_min = date_min.item() if hasattr(date_min, "item") else date_min
        d_max = date_max.item() if hasattr(date_max, "item") else date_max
        stats = self._calc_stats(trades, config.initial_capital, d_min, d_max)
        per_symbol = self._calc_per_symbol(trades)

        return SimResult(
            equity_curve=equity_curve,
            drawdown_curve=drawdown_curve,
            trades=trades,
            per_symbol_stats=per_symbol,
            stats=stats,
        )

    def simulate_independent_candidates(
        self,
        panel: pl.DataFrame,
        entries: pl.Series | None,
        exits: pl.Series | None,
        config: MatcherConfig,
        progress_cb: "Callable[[dict], None] | None" = None,
        cancel_event: "threading.Event | None" = None,
        entry_signal_ids: list[str] | None = None,
        exit_signal_ids: list[str] | None = None,
    ) -> SimResult:
        """Execute every candidate independently on MarketMatrix asset columns."""
        if panel.is_empty():
            return self._empty_result()
        raw_candidates = int(entries.fill_null(False).sum()) if entries is not None and len(entries) == len(panel) else 0
        if raw_candidates <= 0:
            return self._empty_result()
        matrix = build_market_matrix(
            panel,
            entries,
            exits,
            entry_delay_bars=1 if config.entry_fill == "open_t+1" else 0,
            exit_delay_bars=1 if config.exit_fill == "open_t+1" else 0,
            entry_signal_ids=entry_signal_ids,
            exit_signal_ids=exit_signal_ids,
            minute_exit_trigger=config.exit_fill == "signal_next_minute",
        )
        return self._simulate_independent_matrix(
            matrix, raw_candidates, config, progress_cb, cancel_event,
        )

    def _simulate_independent_matrix(
        self,
        matrix: MarketMatrix,
        raw_candidates: int,
        config: MatcherConfig,
        progress_cb: "Callable[[dict], None] | None",
        cancel_event: "threading.Event | None",
        options: SimulationOptions | None = None,
    ) -> SimResult:
        options = options or SimulationOptions()
        entry_prices = matrix.open if config.entry_fill == "open_t+1" else matrix.close
        exit_prices = matrix.open if config.exit_fill == "open_t+1" else matrix.close
        buy_cost_pct = config.buy_cost_pct()
        sell_cost_pct = config.sell_cost_pct()
        trades: list[TradeRecord] = []
        execution_stats = {
            "buy_invalid_price": 0,
            "buy_suspended": 0,
            "buy_limit_up": 0,
            "buy_score_filter": 0,
            "buy_no_next_bar": max(raw_candidates - int(matrix.entry.sum()), 0),
            "sell_invalid_price": 0,
            "sell_suspended": 0,
            "sell_limit_down": 0,
            "sell_no_future": 0,
            "pending_exit": 0,
        }

        minute_cache: dict = {}
        if config.minute_fill:
            trigger_times, trigger_assets = np.nonzero(matrix.entry | matrix.exit)
            dates = {matrix.timestamp_labels[int(t)][:10] for t in trigger_times}
            symbols = {matrix.symbols[int(a)] for a in trigger_assets}
            if dates and symbols:
                loaded = self._load_minute_for_fills(self.repo, list(symbols), dates, "stock")
                minute_cache = {key: value for key, value in loaded.items() if value is not None and len(value) > 0}

        def _count(key: str) -> None:
            execution_stats[key] = execution_stats.get(key, 0) + 1

        def _valid_price(value) -> bool:
            return bool(np.isfinite(value) and value > 0)

        def _present(time_id: int, asset_id: int) -> bool:
            return bool(np.isfinite([
                matrix.open[time_id, asset_id], matrix.high[time_id, asset_id],
                matrix.low[time_id, asset_id], matrix.close[time_id, asset_id],
            ]).any())

        def _signal_id(code: int, ids: tuple[str, ...]) -> str | None:
            return ids[code] if 0 <= code < len(ids) else None

        def _signal_date(signal_time: int, fallback: str) -> str:
            return matrix.timestamp_labels[signal_time][:10] if signal_time >= 0 else fallback

        def _refill(time_id: int, asset_id: int, side: str, daily_price: float) -> float:
            if not config.minute_fill or not minute_cache:
                return daily_price
            rows = minute_cache.get((matrix.symbols[asset_id], matrix.timestamp_labels[time_id][:10]))
            if rows is None:
                return daily_price
            reference = float(matrix.reference_price[time_id, asset_id])
            precise = self._resolve_minute_fill(
                rows, reference if _valid_price(reference) else None, side,
            )
            return precise if precise is not None else daily_price

        def _minute_trigger_price(time_id: int, asset_id: int) -> float | None:
            if not config.minute_fill or not minute_cache:
                return None
            rows = minute_cache.get((matrix.symbols[asset_id], matrix.timestamp_labels[time_id][:10]))
            if rows is None:
                return None
            reference = float(matrix.reference_price[time_id, asset_id])
            return self._resolve_minute_exit_trigger(
                rows,
                reference if _valid_price(reference) else None,
            )

        def _one_price_limit(time_id: int, asset_id: int, direction: str) -> bool:
            if not matrix.tradable[time_id, asset_id]:
                return False
            prices = [
                float(matrix.open[time_id, asset_id]), float(matrix.high[time_id, asset_id]),
                float(matrix.low[time_id, asset_id]), float(matrix.close[time_id, asset_id]),
            ]
            if not all(_valid_price(value) for value in prices):
                return False
            same = max(prices) - min(prices) <= max(abs(prices[3]) * 1e-4, 0.01)
            flags = matrix.limit_up_locked if direction == "up" else matrix.limit_down_locked
            return bool(flags[time_id, asset_id]) and same

        def _can_buy(time_id: int, asset_id: int) -> tuple[bool, str]:
            if not matrix.tradable[time_id, asset_id]:
                return False, "buy_suspended"
            if not _valid_price(entry_prices[time_id, asset_id]):
                return False, "buy_invalid_price"
            if _one_price_limit(time_id, asset_id, "up"):
                return False, "buy_limit_up"
            return True, ""

        def _can_sell(time_id: int, asset_id: int, override: float | None) -> tuple[bool, str]:
            if not matrix.tradable[time_id, asset_id]:
                return False, "sell_suspended"
            price = override if override is not None else exit_prices[time_id, asset_id]
            if not _valid_price(price):
                return False, "sell_invalid_price"
            if _one_price_limit(time_id, asset_id, "down"):
                return False, "sell_limit_down"
            return True, ""

        def _risk_exit(pos: dict, time_id: int, asset_id: int) -> tuple[str | None, float | None]:
            if pos.get("pending_exit_reason") or pos["entry_time"] == time_id:
                return None, None
            entry_price = float(pos["entry_price"])
            open_price = float(matrix.open[time_id, asset_id])
            low_price = float(matrix.low[time_id, asset_id])
            high_price = float(matrix.high[time_id, asset_id])
            peak_price = float(pos["max_high"])
            lines: list[tuple[float, str]] = []
            if config.stop_loss_pct is not None:
                lines.append((entry_price * (1 - abs(config.stop_loss_pct)), "stop_loss"))
            if config.trailing_stop_pct is not None:
                lines.append((peak_price * (1 - abs(config.trailing_stop_pct)), "trailing_stop"))
            activate = config.trailing_take_profit_activate_pct
            drawdown = config.trailing_take_profit_drawdown_pct
            if activate is not None and drawdown is not None and peak_price > entry_price:
                if peak_price / entry_price - 1 >= abs(float(activate)):
                    lines.append((peak_price * (1 - abs(float(drawdown))), "trailing_take_profit"))
            valid_lines = [(line, reason) for line, reason in lines if _valid_price(line)]
            if valid_lines:
                stop_price, reason = max(valid_lines, key=lambda item: item[0])
                if _valid_price(open_price) and open_price <= stop_price:
                    return reason, open_price
                if _valid_price(low_price) and low_price <= stop_price:
                    return reason, stop_price
            if config.take_profit_pct is not None:
                take_profit = entry_price * (1 + abs(float(config.take_profit_pct)))
                if _valid_price(open_price) and open_price >= take_profit:
                    return "take_profit", open_price
                if _valid_price(high_price) and high_price >= take_profit:
                    return "take_profit", take_profit
            return None, None

        def _try_close(
            pos: dict,
            time_id: int,
            asset_id: int,
            reason: str,
            signal_date: str,
            override: float | None = None,
        ) -> bool:
            signal_id = (
                _signal_id(int(matrix.exit_signal_code[time_id, asset_id]), matrix.exit_signal_ids)
                if reason == "signal" else None
            )
            minute_trigger = config.exit_fill == "signal_next_minute" and reason == "signal"
            if minute_trigger and override is None:
                if pos.get("pending_exit_next_open"):
                    open_price = float(matrix.open[time_id, asset_id])
                    override = open_price if _valid_price(open_price) else None
                else:
                    override = _minute_trigger_price(time_id, asset_id)
                    if override is None:
                        if not pos.get("pending_exit_reason"):
                            pos["pending_exit_reason"] = reason
                            pos["pending_exit_signal_date"] = signal_date
                            pos["pending_exit_signal_id"] = signal_id
                            pos["pending_exit_next_open"] = True
                            _count("pending_exit")
                        pos["blocked_exit_days"] += 1
                        _count("sell_minute_trigger_fallback")
                        return False
            ok, blocked = _can_sell(time_id, asset_id, override)
            if not ok:
                if not pos.get("pending_exit_reason"):
                    pos["pending_exit_reason"] = reason
                    pos["pending_exit_signal_date"] = signal_date
                    pos["pending_exit_signal_id"] = signal_id
                    _count("pending_exit")
                if minute_trigger:
                    pos["pending_exit_next_open"] = True
                pos["blocked_exit_days"] += 1
                _count(blocked)
                return False
            exit_price = float(override) if override is not None else _refill(
                time_id, asset_id, "sell", float(exit_prices[time_id, asset_id])
            )
            shares = 100.0
            entry_value = shares * pos["entry_price"] * (1 + buy_cost_pct)
            exit_value = shares * exit_price * (1 - sell_cost_pct)
            pnl_amount = exit_value - entry_value
            trades.append(TradeRecord(
                symbol=matrix.symbols[asset_id],
                name=matrix.names[asset_id],
                entry_date=pos["entry_date"],
                exit_date=matrix.timestamp_labels[time_id][:10],
                entry_price=round(float(pos["entry_price"]), 4),
                exit_price=round(exit_price, 4),
                pnl_pct=round(float(pnl_amount / entry_value), 6) if entry_value > 0 else 0.0,
                duration=int(pos["hold_days"]),
                exit_reason=reason,
                shares=shares,
                lots=1.0,
                entry_value=round(float(entry_value), 2),
                exit_value=round(float(exit_value), 2),
                pnl_amount=round(float(pnl_amount), 2),
                entry_score=round(float(pos["entry_score"]), 2),
                entry_signal_date=pos["entry_signal_date"],
                exit_signal_date=signal_date,
                blocked_exit_days=int(pos["blocked_exit_days"]),
                entry_signal_id=pos["entry_signal_id"],
                exit_signal_id=(pos.get("pending_exit_signal_id") or signal_id) if reason == "signal" else None,
            ))
            return True

        entry_times, entry_assets = np.nonzero(matrix.entry)
        order = np.lexsort((entry_times, entry_assets))
        for seq, order_pos in enumerate(order, start=1):
            time_id = int(entry_times[order_pos])
            asset_id = int(entry_assets[order_pos])
            if cancel_event is not None and cancel_event.is_set():
                break
            if progress_cb is not None and (seq == 1 or seq % 500 == 0):
                try:
                    progress_cb({
                        "day": seq,
                        "total": len(order),
                        "date": matrix.timestamp_labels[time_id][:10],
                        "equity": 0,
                    })
                except Exception:
                    pass
            ok, blocked = _can_buy(time_id, asset_id)
            if not ok:
                _count(blocked)
                continue
            score = _matrix_entry_score(matrix, time_id, asset_id)
            if config.score_min is not None and score < config.score_min:
                _count("buy_score_filter")
                continue
            if config.score_max is not None and score > config.score_max:
                _count("buy_score_filter")
                continue
            future_times = [
                future for future in range(time_id + 1, matrix.shape[0])
                if _present(future, asset_id)
            ]
            if not future_times:
                _count("sell_no_future")
                continue
            entry_price = _refill(time_id, asset_id, "buy", float(entry_prices[time_id, asset_id]))
            entry_date = matrix.timestamp_labels[time_id][:10]
            pos = {
                "entry_time": time_id,
                "entry_date": entry_date,
                "entry_signal_date": _signal_date(
                    int(matrix.entry_signal_time[time_id, asset_id]), entry_date
                ),
                "entry_signal_id": _signal_id(
                    int(matrix.entry_signal_code[time_id, asset_id]), matrix.entry_signal_ids
                ),
                "entry_price": entry_price,
                "entry_score": score,
                "hold_days": 0,
                "max_high": max(entry_price, float(matrix.high[time_id, asset_id])),
                "pending_exit_reason": None,
                "pending_exit_signal_date": None,
                "pending_exit_signal_id": None,
                "pending_exit_next_open": False,
                "blocked_exit_days": 0,
            }
            closed = False
            for future in future_times:
                pos["hold_days"] += 1
                date_text = matrix.timestamp_labels[future][:10]
                reason, override = _risk_exit(pos, future, asset_id)
                if reason and _try_close(pos, future, asset_id, reason, date_text, override):
                    closed = True
                    break
                reason = None
                signal_date = date_text
                if pos.get("pending_exit_reason"):
                    reason = str(pos["pending_exit_reason"])
                    signal_date = str(pos.get("pending_exit_signal_date") or date_text)
                elif matrix.exit[future, asset_id]:
                    reason = "signal"
                    signal_date = _signal_date(int(matrix.exit_signal_time[future, asset_id]), date_text)
                elif config.max_hold_days is not None and pos["hold_days"] >= config.max_hold_days:
                    reason = "max_hold"
                elif future == future_times[-1]:
                    reason = "end"
                if reason and _try_close(pos, future, asset_id, reason, signal_date):
                    closed = True
                    break
                high_price = float(matrix.high[future, asset_id])
                if _valid_price(high_price):
                    pos["max_high"] = max(float(pos["max_high"]), high_price)
            if not closed and not pos.get("pending_exit_reason"):
                last_time = future_times[-1]
                _try_close(
                    pos, last_time, asset_id, "end", matrix.timestamp_labels[last_time][:10]
                )

        result = self._calc_independent_candidate_result(
            trades,
            raw_candidates,
            execution_stats,
            options=options,
        )
        result.stats["market_matrix_shape"] = list(matrix.shape)
        result.stats["market_matrix_bytes"] = matrix.nbytes
        return result

    def simulate_independent_market_matrix(
        self,
        matrix: MarketMatrix,
        raw_candidates: int,
        config: MatcherConfig,
        progress_cb: "Callable[[dict], None] | None" = None,
        cancel_event: "threading.Event | None" = None,
        options: SimulationOptions | None = None,
    ) -> SimResult:
        """Run independent-candidate simulation on a prebuilt MarketMatrix."""
        return self._simulate_independent_matrix(
            matrix, raw_candidates, config, progress_cb, cancel_event, options,
        )

    def simulate_independent_candidates_legacy(
        self,
        panel: pl.DataFrame,
        entries: pl.Series | None,
        exits: pl.Series | None,
        config: MatcherConfig,
        progress_cb: "Callable[[dict], None] | None" = None,
        cancel_event: "threading.Event | None" = None,
        entry_signal_ids: list[str] | None = None,
        exit_signal_ids: list[str] | None = None,
    ) -> SimResult:
        """全量候选独立执行：每个买入信号都是独立样本, 不受资金/仓位限制。"""
        if panel.is_empty():
            return self._empty_result()

        n = len(panel)
        panel_dates = panel["date"].to_numpy()
        panel_symbols = panel["symbol"].to_numpy()

        ent_raw = np.zeros(n, dtype=bool)
        ext_raw = np.zeros(n, dtype=bool)
        if entries is not None and len(entries) == n:
            ent_raw = entries.to_numpy().astype(bool)
        if exits is not None and len(exits) == n:
            ext_raw = exits.to_numpy().astype(bool)
        n_candidates = int(ent_raw.sum())
        if n_candidates <= 0:
            return self._empty_result()

        entry_signal_dates = np.array([None] * n, dtype=object)
        exit_signal_dates = np.array([None] * n, dtype=object)
        same_prev_symbol = panel_symbols[1:] == panel_symbols[:-1]

        # 建仓口径: close_t 用信号日收盘, open_t+1 右移到次日 open 成交。
        ent = np.zeros(n, dtype=bool)
        if config.entry_fill == "open_t+1":
            ent[1:] = ent_raw[:-1] & same_prev_symbol
            for idx in np.flatnonzero(ent):
                entry_signal_dates[idx] = self._date_str(panel_dates[idx - 1])
        else:
            ent = ent_raw
            for idx in np.flatnonzero(ent):
                entry_signal_dates[idx] = self._date_str(panel_dates[idx])

        # 清仓口径: 独立于建仓, close_t 用信号日收盘, open_t+1 右移到次日 open。
        ext = np.zeros(n, dtype=bool)
        if config.exit_fill == "open_t+1":
            ext[1:] = ext_raw[:-1] & same_prev_symbol
            for idx in np.flatnonzero(ext):
                exit_signal_dates[idx] = self._date_str(panel_dates[idx - 1])
        else:
            ext = ext_raw
            for idx in np.flatnonzero(ext):
                exit_signal_dates[idx] = self._date_str(panel_dates[idx])

        open_prices = panel["open"].to_numpy()
        high_prices = panel["high"].to_numpy() if "high" in panel.columns else open_prices
        low_prices = panel["low"].to_numpy()
        close_prices = panel["close"].to_numpy()
        # 撮合价: 建仓/清仓各自独立选列。
        entry_prices = open_prices if config.entry_fill == "open_t+1" else close_prices
        exit_prices = open_prices if config.exit_fill == "open_t+1" else close_prices

        # ── 分钟K精确成交预加载 (同 simulate_portfolio) ──
        minute_cache: dict = {}
        if config.minute_fill:
            _trigger_dates: set[str] = set()
            _trigger_symbols: set[str] = set()
            for _idx in range(n):
                if ent[_idx] or ext[_idx]:
                    _trigger_dates.add(self._date_str(panel_dates[_idx]))
                    _trigger_symbols.add(str(panel_symbols[_idx]))
            if _trigger_dates and _trigger_symbols:
                _loaded = self._load_minute_for_fills(
                    self.repo, list(_trigger_symbols), _trigger_dates, "stock",
                )
                for _key, _marr in _loaded.items():
                    if _marr is not None and len(_marr) > 0:
                        minute_cache[_key] = _marr

        def _refill_price(idx: int, side: str, daily_price: float) -> float:
            if not config.minute_fill or not minute_cache:
                return daily_price
            _sym = str(panel_symbols[idx])
            _d = self._date_str(panel_dates[idx])
            _marr = minute_cache.get((_sym, _d))
            if _marr is None:
                return daily_price
            _ref = None
            for _col in ("ma5", "ma10", "ma20"):
                if _col in panel.columns:
                    try:
                        _fv = float(panel[_col][idx])
                        if _fv > 0 and np.isfinite(_fv):
                            _ref = _fv
                            break
                    except (TypeError, ValueError):
                        pass
            _precise = self._resolve_minute_fill(_marr, _ref, side)
            return _precise if _precise is not None else daily_price

        has_volume = "volume" in panel.columns
        volumes = panel["volume"].fill_null(0).to_numpy() if has_volume else np.ones(n, dtype=float)
        names = panel["name"].fill_null("").to_numpy() if "name" in panel.columns else np.array([""] * n)
        scores = panel["score"].fill_null(0).to_numpy() if "score" in panel.columns else np.zeros(n, dtype=float)
        trade_scores = scores.copy()
        # 评分跟随建仓口径 shift (评分在买入日生效)。
        if config.entry_fill == "open_t+1":
            trade_scores[1:] = np.where(panel_symbols[1:] == panel_symbols[:-1], scores[:-1], trade_scores[1:])
        limit_up_flags = (
            panel["signal_limit_up"].fill_null(False).to_numpy().astype(bool)
            if "signal_limit_up" in panel.columns else np.zeros(n, dtype=bool)
        )
        limit_down_flags = (
            panel["signal_limit_down"].fill_null(False).to_numpy().astype(bool)
            if "signal_limit_down" in panel.columns else np.zeros(n, dtype=bool)
        )

        symbol_rows: dict[str, list[int]] = {}
        row_pos_in_symbol = np.zeros(n, dtype=int)
        for i, sym_value in enumerate(panel_symbols):
            sym = str(sym_value)
            rows = symbol_rows.setdefault(sym, [])
            row_pos_in_symbol[i] = len(rows)
            rows.append(i)

        buy_cost_pct = config.buy_cost_pct()
        sell_cost_pct = config.sell_cost_pct()
        score_min = getattr(config, "score_min", None)
        score_max = getattr(config, "score_max", None)
        trades: list[TradeRecord] = []
        execution_stats: dict[str, int] = {
            "buy_invalid_price": 0,
            "buy_suspended": 0,
            "buy_limit_up": 0,
            "buy_score_filter": 0,
            "buy_no_next_bar": max(n_candidates - int(ent.sum()), 0),
            "sell_invalid_price": 0,
            "sell_suspended": 0,
            "sell_limit_down": 0,
            "sell_no_future": 0,
            "pending_exit": 0,
        }

        def _count(key: str) -> None:
            execution_stats[key] = execution_stats.get(key, 0) + 1

        def _valid_price(value) -> bool:
            try:
                v = float(value)
            except (TypeError, ValueError):
                return False
            return v > 0 and np.isfinite(v)

        def _is_suspended(idx: int) -> bool:
            o = float(open_prices[idx])
            h = float(high_prices[idx])
            l = float(low_prices[idx])
            c = float(close_prices[idx])
            valid_bar = any(_valid_price(x) for x in (o, h, l, c))
            if not valid_bar:
                return True
            if has_volume and float(volumes[idx] or 0) <= 0:
                same_price = max(o, h, l, c) - min(o, h, l, c) <= max(abs(c) * 1e-4, 0.01)
                if same_price:
                    return True
            return False

        def _is_one_price_limit(idx: int, direction: str) -> bool:
            if _is_suspended(idx):
                return False
            o = float(open_prices[idx])
            h = float(high_prices[idx])
            l = float(low_prices[idx])
            c = float(close_prices[idx])
            if not all(_valid_price(x) for x in (o, h, l, c)):
                return False
            same_price = max(o, h, l, c) - min(o, h, l, c) <= max(abs(c) * 1e-4, 0.01)
            if direction == "up":
                return bool(limit_up_flags[idx]) and same_price
            return bool(limit_down_flags[idx]) and same_price

        def _can_buy(idx: int) -> tuple[bool, str]:
            if _is_suspended(idx):
                return False, "buy_suspended"
            if not _valid_price(entry_prices[idx]):
                return False, "buy_invalid_price"
            if _is_one_price_limit(idx, "up"):
                return False, "buy_limit_up"
            return True, ""

        def _can_sell(idx: int, exit_price_override: float | None = None) -> tuple[bool, str]:
            if _is_suspended(idx):
                return False, "sell_suspended"
            exit_price = exit_price_override if exit_price_override is not None else exit_prices[idx]
            if not _valid_price(exit_price):
                return False, "sell_invalid_price"
            if _is_one_price_limit(idx, "down"):
                return False, "sell_limit_down"
            return True, ""

        def _risk_exit(pos: dict, idx: int) -> tuple[str | None, float | None]:
            if pos.get("pending_exit_reason") or pos.get("entry_idx") == idx:
                return None, None
            entry_price = float(pos["entry_price"])
            if entry_price <= 0:
                return None, None
            open_price = float(open_prices[idx])
            low_price = float(low_prices[idx])
            high_price = float(high_prices[idx])
            peak_price = float(pos.get("max_high", entry_price))
            risk_lines: list[tuple[float, str]] = []

            if config.stop_loss_pct is not None:
                risk_lines.append((entry_price * (1 - abs(config.stop_loss_pct)), "stop_loss"))
            if config.trailing_stop_pct is not None and peak_price > 0:
                risk_lines.append((peak_price * (1 - abs(config.trailing_stop_pct)), "trailing_stop"))

            activate_pct = getattr(config, "trailing_take_profit_activate_pct", None)
            drawdown_pct = getattr(config, "trailing_take_profit_drawdown_pct", None)
            if activate_pct is not None and drawdown_pct is not None and peak_price > entry_price:
                peak_profit = peak_price / entry_price - 1
                if peak_profit >= abs(float(activate_pct)):
                    # 回撤止盈触发线: 相对峰值价回撤 drawdown 个点 (纯峰值口径)
                    risk_lines.append((peak_price * (1 - abs(float(drawdown_pct))), "trailing_take_profit"))

            risk_lines = [(line, reason) for line, reason in risk_lines if _valid_price(line)]
            # 止损/移损/回撤止盈: 价格跌破风控线触发 (取最高优先级线)
            if risk_lines:
                stop_price, reason = max(risk_lines, key=lambda item: item[0])
                if _valid_price(open_price) and open_price <= stop_price:
                    return reason, open_price
                if _valid_price(low_price) and low_price <= stop_price:
                    return reason, stop_price

            # 固定止盈: 价格涨破止盈线触发
            tp_pct = getattr(config, "take_profit_pct", None)
            if tp_pct is not None:
                tp_line = entry_price * (1 + abs(float(tp_pct)))
                if _valid_price(tp_line):
                    # 开盘即超过止盈线 → 以开盘价成交; 否则当日触及高点止盈
                    if _valid_price(open_price) and open_price >= tp_line:
                        return "take_profit", open_price
                    if _valid_price(high_price) and high_price >= tp_line:
                        return "take_profit", tp_line
            return None, None

        def _try_close(pos: dict, idx: int, reason: str, signal_date: str, exit_price_override: float | None = None) -> bool:
            ok, block_reason = _can_sell(idx, exit_price_override)
            if not ok:
                if not pos.get("pending_exit_reason"):
                    pos["pending_exit_reason"] = reason
                    pos["pending_exit_signal_date"] = signal_date
                    _count("pending_exit")
                pos["blocked_exit_days"] = int(pos.get("blocked_exit_days", 0)) + 1
                _count(block_reason)
                return False

            if exit_price_override is not None:
                exit_price = float(exit_price_override)
            else:
                exit_price = _refill_price(idx, "sell", float(exit_prices[idx]))
            shares = 100.0
            entry_value = shares * float(pos["entry_price"]) * (1 + buy_cost_pct)
            exit_value = shares * exit_price * (1 - sell_cost_pct)
            pnl_amount = exit_value - entry_value
            pnl_pct = pnl_amount / entry_value if entry_value > 0 else 0.0
            trades.append(TradeRecord(
                symbol=str(pos["symbol"]),
                name=str(pos.get("name", "")),
                entry_date=pos["entry_date"],
                exit_date=self._date_str(panel_dates[idx]),
                entry_price=round(float(pos["entry_price"]), 4),
                exit_price=round(exit_price, 4),
                pnl_pct=round(float(pnl_pct), 6),
                duration=int(pos["hold_days"]),
                exit_reason=reason,
                shares=shares,
                lots=1.0,
                position_pct=0.0,
                entry_value=round(float(entry_value), 2),
                exit_value=round(float(exit_value), 2),
                pnl_amount=round(float(pnl_amount), 2),
                entry_score=round(float(pos["entry_score"]), 2) if pos.get("entry_score") is not None else None,
                entry_signal_date=pos.get("entry_signal_date"),
                exit_signal_date=signal_date,
                blocked_exit_days=int(pos.get("blocked_exit_days", 0)),
                entry_signal_id=pos.get("entry_signal_id"),
                exit_signal_id=_resolve_signal_id(panel, idx, exit_signal_ids) if reason == "signal" else None,
            ))
            return True

        candidate_indices = np.flatnonzero(ent)
        for seq, entry_idx in enumerate(candidate_indices, start=1):
            if cancel_event is not None and cancel_event.is_set():
                logger.info("全量模拟被用户取消 (第 %d/%d 个候选)", seq, len(candidate_indices))
                break
            if progress_cb is not None and (seq == 1 or seq % 500 == 0):
                try:
                    progress_cb({
                        "day": seq,
                        "total": len(candidate_indices),
                        "date": self._date_str(panel_dates[entry_idx]),
                        "equity": 0,
                    })
                except Exception:
                    pass

            ok, block_reason = _can_buy(entry_idx)
            if not ok:
                _count(block_reason)
                continue
            score = float(trade_scores[entry_idx] or 0.0)
            if score_min is not None and score < score_min:
                _count("buy_score_filter")
                continue
            if score_max is not None and score > score_max:
                _count("buy_score_filter")
                continue

            sym = str(panel_symbols[entry_idx])
            rows = symbol_rows.get(sym, [])
            start_pos = int(row_pos_in_symbol[entry_idx])
            if start_pos >= len(rows):
                _count("sell_no_future")
                continue

            entry_price = _refill_price(entry_idx, "buy", float(entry_prices[entry_idx]))
            pos = {
                "symbol": sym,
                "name": str(names[entry_idx] or ""),
                "entry_idx": entry_idx,
                "entry_date": self._date_str(panel_dates[entry_idx]),
                "entry_signal_date": entry_signal_dates[entry_idx] or self._date_str(panel_dates[entry_idx]),
                "entry_signal_id": _resolve_signal_id(panel, entry_idx, entry_signal_ids),
                "entry_price": entry_price,
                "entry_score": score,
                "hold_days": 0,
                "max_high": entry_price,
                "pending_exit_reason": None,
                "pending_exit_signal_date": None,
                "blocked_exit_days": 0,
            }
            hi = float(high_prices[entry_idx])
            if _valid_price(hi):
                pos["max_high"] = max(float(pos["max_high"]), hi)

            closed = False
            last_idx = entry_idx
            for idx in rows[start_pos + 1:]:
                last_idx = idx
                pos["hold_days"] = int(pos["hold_days"]) + 1
                d_str = self._date_str(panel_dates[idx])

                def _scheduled_reason() -> tuple[str | None, str]:
                    if pos.get("pending_exit_reason"):
                        return str(pos["pending_exit_reason"]), str(pos.get("pending_exit_signal_date") or d_str)
                    # 卖点信号优先于到期: 策略主动离场先于 max_hold 兜底。
                    if ext[idx]:
                        return "signal", str(exit_signal_dates[idx] or d_str)
                    if config.max_hold_days is not None and pos["hold_days"] >= config.max_hold_days:
                        return "max_hold", d_str
                    if idx == rows[-1]:
                        return "end", d_str
                    return None, d_str

                # 统一退出顺序: 风控(止损/移动止损/止盈)先于计划出场 (signal/max_hold/end)。
                # 无论 entry/exit 口径如何, 风控都是保护性离场, 必须最高优先级。
                reason, override_price = _risk_exit(pos, idx)
                if reason and _try_close(pos, idx, reason, d_str, override_price):
                    closed = True
                    break
                reason, signal_date = _scheduled_reason()
                if reason and _try_close(pos, idx, reason, signal_date):
                    closed = True
                    break

                hi = float(high_prices[idx])
                if _valid_price(hi):
                    pos["max_high"] = max(float(pos.get("max_high", entry_price)), hi)

            if not closed:
                if last_idx == entry_idx:
                    _count("sell_no_future")
                elif not pos.get("pending_exit_reason"):
                    _try_close(pos, last_idx, "end", self._date_str(panel_dates[last_idx]))

        return self._calc_independent_candidate_result(trades, n_candidates, execution_stats)

    # ── 分钟K精确成交 ──────────────────────────────────

    @staticmethod
    def _resolve_minute_fill(
        minute_arr: np.ndarray,
        ref_price: float | None,
        side: str,
    ) -> float | None:
        """用当日分钟K确定精确成交价。

        Args:
            minute_arr: float64 2D 数组, 列顺序 = _MINUTE_NUMERIC_COLS
                        [open(0), high(1), low(2), close(3), volume(4), amount(5)]
                        (缺失的尾部列直接不存在, 用 shape 判断)
            ref_price: 信号参考线价格 (如 MA5 值); None 表示无参考线
            side: "buy" 或 "sell", 决定穿越方向

        Returns:
            精确成交价, 或 None (降级到日K口径)
        """
        if minute_arr is None or len(minute_arr) == 0:
            return None

        ncols = minute_arr.shape[1] if minute_arr.ndim == 2 else 1
        opens = minute_arr[:, 0]
        highs = minute_arr[:, 1] if ncols > 1 else opens
        lows = minute_arr[:, 2] if ncols > 2 else opens
        closes = minute_arr[:, 3] if ncols > 3 else opens
        volumes = minute_arr[:, 4] if ncols > 4 else None
        amounts = minute_arr[:, 5] if ncols > 5 else None

        # 有参考线 → 穿越价成交 (逻辑同止损: 找价格穿越参考线的时刻)
        if ref_price is not None and ref_price > 0 and np.isfinite(ref_price):
            if side == "sell":
                # 卖出: 价格跌破参考线 → 开盘已低于则按开盘; 否则按参考线 (低点触及)
                if np.isfinite(opens[0]) and opens[0] <= ref_price:
                    return float(opens[0])
                if np.any(np.isfinite(lows) & (lows <= ref_price)):
                    return float(ref_price)
            else:
                # 买入: 价格涨破参考线 → 开盘已高于则按开盘; 否则按参考线 (高点触及)
                if np.isfinite(opens[0]) and opens[0] >= ref_price:
                    return float(opens[0])
                if np.any(np.isfinite(highs) & (highs >= ref_price)):
                    return float(ref_price)
            # 参考线存在但当日分钟K未穿越 → 用收盘 (信号确认)
            return float(closes[-1]) if np.isfinite(closes[-1]) else None

        # 无参考线 → VWAP (成交额/成交量), 退化到收盘价
        if volumes is not None and amounts is not None:
            total_vol = float(np.nansum(volumes))
            total_amt = float(np.nansum(amounts))
            if total_vol > 0 and total_amt > 0:
                return total_amt / total_vol

        return float(closes[-1]) if np.isfinite(closes[-1]) else None

    @staticmethod
    def _resolve_minute_exit_trigger(
        minute_arr: np.ndarray,
        ref_price: float | None,
    ) -> float | None:
        """分钟收盘确认向下穿越后，返回下一分钟开盘价。"""
        if minute_arr is None or len(minute_arr) < 2:
            return None
        if ref_price is None or not np.isfinite(ref_price) or ref_price <= 0:
            return None

        ncols = minute_arr.shape[1] if minute_arr.ndim == 2 else 1
        if ncols < 4:
            return None
        opens = minute_arr[:, 0]
        closes = minute_arr[:, 3]
        below = np.isfinite(closes) & (closes < ref_price)
        previous_above = np.empty(len(closes), dtype=bool)
        previous_above[0] = True
        previous_above[1:] = np.isfinite(closes[:-1]) & (closes[:-1] >= ref_price)
        crossings = np.flatnonzero(below & previous_above)
        if crossings.size == 0:
            return None
        next_idx = int(crossings[0]) + 1
        if next_idx >= len(opens) or not np.isfinite(opens[next_idx]) or opens[next_idx] <= 0:
            return None
        return float(opens[next_idx])

    # 分钟K cache 存储的数值列及固定顺序 (_resolve_minute_fill 按此顺序整数索引)。
    _MINUTE_NUMERIC_COLS = ["open", "high", "low", "close", "volume", "amount"]

    @staticmethod
    def _load_minute_for_fills(
        repo,
        symbols: list[str],
        dates_needed: set,
        asset_type: str,
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
        for i in range(0, len(date_objs), BATCH):
            batch = date_objs[i:i + BATCH]
            try:
                df = repo.get_minute_by_dates(symbols, batch, asset_type=asset_type)
            except Exception as e:  # noqa: BLE001
                logger.warning("minute fill data load failed (batch %d-%d): %s", i, i + len(batch), e)
                continue
            if df.is_empty():
                continue
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

    def simulate_portfolio(
        self,
        panel: pl.DataFrame,
        entries: pl.Series | None,
        exits: pl.Series | None,
        config: MatcherConfig,
        progress_cb: "Callable[[dict], None] | None" = None,
        cancel_event: "threading.Event | None" = None,
        entry_signal_ids: list[str] | None = None,
        exit_signal_ids: list[str] | None = None,
    ) -> SimResult:
        """Account-level matcher backed by immutable ``time x asset`` arrays."""
        if panel.is_empty():
            return self._empty_result()

        matrix = build_market_matrix(
            panel,
            entries,
            exits,
            entry_delay_bars=1 if config.entry_fill == "open_t+1" else 0,
            exit_delay_bars=1 if config.exit_fill == "open_t+1" else 0,
            entry_signal_ids=entry_signal_ids,
            exit_signal_ids=exit_signal_ids,
            minute_exit_trigger=config.exit_fill == "signal_next_minute",
        )
        if not matrix.entry.any():
            return self._empty_result()
        return self._simulate_portfolio_matrix(matrix, config, progress_cb, cancel_event)

    def simulate_market_matrix(
        self,
        matrix: MarketMatrix,
        config: MatcherConfig,
        progress_cb: "Callable[[dict], None] | None" = None,
        cancel_event: "threading.Event | None" = None,
        options: SimulationOptions | None = None,
    ) -> SimResult:
        """Run the production Python matcher on a prebuilt MarketMatrix."""
        if not matrix.entry.any():
            return self._empty_result()
        return self._simulate_portfolio_matrix(matrix, config, progress_cb, cancel_event, options)

    def _simulate_portfolio_matrix(
        self,
        matrix: MarketMatrix,
        config: MatcherConfig,
        progress_cb: "Callable[[dict], None] | None",
        cancel_event: "threading.Event | None",
        options: SimulationOptions | None = None,
    ) -> SimResult:
        options = options or SimulationOptions()
        time_count, asset_count = matrix.shape
        entry_prices = matrix.open if config.entry_fill == "open_t+1" else matrix.close
        exit_prices = matrix.open if config.exit_fill == "open_t+1" else matrix.close
        buy_cost_pct = config.buy_cost_pct()
        sell_cost_pct = config.sell_cost_pct()
        cash = float(config.initial_capital)
        peak = cash
        max_positions = max(int(config.max_positions), 0)
        max_exposure_pct = min(max(float(config.max_exposure_pct), 0.0), 1.0)
        positions: dict[int, dict] = {}
        last_close = np.full(asset_count, np.nan, dtype=np.float64)
        trades: list[TradeRecord] = []
        equity_curve: list[dict] = []
        drawdown_curve: list[dict] = []
        equity_values: list[float] = []
        exposure_values: list[float] = []
        execution_stats = {
            "buy_invalid_price": 0,
            "buy_suspended": 0,
            "buy_limit_up": 0,
            "buy_no_slot": 0,
            "buy_cash": 0,
            "buy_lot_size": 0,
            "buy_same_day_reentry": 0,
            "buy_exposure": 0,
            "buy_score_filter": 0,
            "sell_invalid_price": 0,
            "sell_suspended": 0,
            "sell_limit_down": 0,
            "pending_exit": 0,
        }

        minute_cache: dict = {}
        if config.minute_fill:
            trigger_times, trigger_assets = np.nonzero(matrix.entry | matrix.exit)
            trigger_dates = {matrix.timestamp_labels[int(t)][:10] for t in trigger_times}
            trigger_symbols = {matrix.symbols[int(a)] for a in trigger_assets}
            if trigger_dates and trigger_symbols:
                asset_type = "etf" if all(
                    symbol.endswith(".SH") and symbol.startswith("5")
                    for symbol in list(trigger_symbols)[:5]
                ) else "stock"
                loaded = self._load_minute_for_fills(
                    self.repo, list(trigger_symbols), trigger_dates, asset_type,
                )
                minute_cache = {key: value for key, value in loaded.items() if value is not None and len(value) > 0}

        def _count(key: str) -> None:
            execution_stats[key] = execution_stats.get(key, 0) + 1

        def _valid_price(value) -> bool:
            return bool(np.isfinite(value) and value > 0)

        def _signal_id(code: int, signal_ids: tuple[str, ...]) -> str | None:
            return signal_ids[code] if 0 <= code < len(signal_ids) else None

        def _signal_date(signal_time: int, fallback: str) -> str:
            return matrix.timestamp_labels[signal_time][:10] if signal_time >= 0 else fallback

        def _market_value() -> float:
            total = 0.0
            for asset, pos in positions.items():
                mark = last_close[asset]
                if not _valid_price(mark):
                    mark = pos["entry_price"]
                total += pos["shares"] * mark
            return total

        def _refill_price(time_id: int, asset_id: int, side: str, daily_price: float) -> float:
            if not config.minute_fill or not minute_cache:
                return daily_price
            key = (matrix.symbols[asset_id], matrix.timestamp_labels[time_id][:10])
            minute_rows = minute_cache.get(key)
            if minute_rows is None:
                return daily_price
            reference = float(matrix.reference_price[time_id, asset_id])
            precise = self._resolve_minute_fill(
                minute_rows,
                reference if _valid_price(reference) else None,
                side,
            )
            return precise if precise is not None else daily_price

        def _minute_trigger_price(time_id: int, asset_id: int) -> float | None:
            if not config.minute_fill or not minute_cache:
                return None
            key = (matrix.symbols[asset_id], matrix.timestamp_labels[time_id][:10])
            minute_rows = minute_cache.get(key)
            if minute_rows is None:
                return None
            reference = float(matrix.reference_price[time_id, asset_id])
            return self._resolve_minute_exit_trigger(
                minute_rows,
                reference if _valid_price(reference) else None,
            )

        def _one_price_limit(time_id: int, asset_id: int, direction: str) -> bool:
            if not matrix.tradable[time_id, asset_id]:
                return False
            prices = (
                float(matrix.open[time_id, asset_id]),
                float(matrix.high[time_id, asset_id]),
                float(matrix.low[time_id, asset_id]),
                float(matrix.close[time_id, asset_id]),
            )
            if not all(_valid_price(value) for value in prices):
                return False
            same_price = max(prices) - min(prices) <= max(abs(prices[3]) * 1e-4, 0.01)
            flag = matrix.limit_up_locked if direction == "up" else matrix.limit_down_locked
            return bool(flag[time_id, asset_id]) and same_price

        def _can_buy(time_id: int, asset_id: int) -> tuple[bool, str]:
            if not matrix.tradable[time_id, asset_id]:
                return False, "buy_suspended"
            if not _valid_price(entry_prices[time_id, asset_id]):
                return False, "buy_invalid_price"
            if _one_price_limit(time_id, asset_id, "up"):
                return False, "buy_limit_up"
            return True, ""

        def _can_sell(time_id: int, asset_id: int, override: float | None = None) -> tuple[bool, str]:
            if not matrix.tradable[time_id, asset_id]:
                return False, "sell_suspended"
            price = override if override is not None else exit_prices[time_id, asset_id]
            if not _valid_price(price):
                return False, "sell_invalid_price"
            if _one_price_limit(time_id, asset_id, "down"):
                return False, "sell_limit_down"
            return True, ""

        def _mark_pending(
            asset_id: int,
            reason: str,
            signal_date: str,
            signal_id: str | None = None,
            next_open: bool = False,
        ) -> None:
            pos = positions[asset_id]
            if not pos.get("pending_exit_reason"):
                pos["pending_exit_reason"] = reason
                pos["pending_exit_signal_date"] = signal_date
                pos["pending_exit_signal_id"] = signal_id
                _count("pending_exit")
            if next_open:
                pos["pending_exit_next_open"] = True
            pos["blocked_exit_days"] += 1

        def _sell(
            time_id: int,
            asset_id: int,
            reason: str,
            signal_date: str,
            sold_today: set[int],
            override: float | None = None,
        ) -> None:
            nonlocal cash
            pos = positions.pop(asset_id)
            exit_price = float(override) if override is not None else _refill_price(
                time_id, asset_id, "sell", float(exit_prices[time_id, asset_id])
            )
            exit_value = pos["shares"] * exit_price * (1 - sell_cost_pct)
            cash += exit_value
            pnl_amount = exit_value - pos["entry_value"]
            pnl_pct = pnl_amount / pos["entry_value"] if pos["entry_value"] > 0 else 0.0
            sold_today.add(asset_id)
            trades.append(TradeRecord(
                symbol=matrix.symbols[asset_id],
                name=matrix.names[asset_id],
                entry_date=pos["entry_date"],
                exit_date=matrix.timestamp_labels[time_id][:10],
                entry_price=round(float(pos["entry_price"]), 4),
                exit_price=round(exit_price, 4),
                pnl_pct=round(float(pnl_pct), 6),
                duration=int(pos["hold_days"]),
                exit_reason=reason,
                shares=round(float(pos["shares"]), 4),
                lots=round(float(pos["lots"]), 2),
                position_pct=round(float(pos["position_pct"]), 6),
                entry_value=round(float(pos["entry_value"]), 2),
                exit_value=round(float(exit_value), 2),
                pnl_amount=round(float(pnl_amount), 2),
                entry_score=round(float(pos["entry_score"]), 2),
                entry_signal_date=pos["entry_signal_date"],
                exit_signal_date=signal_date,
                blocked_exit_days=int(pos["blocked_exit_days"]),
                entry_signal_id=pos["entry_signal_id"],
                exit_signal_id=(
                    pos.get("pending_exit_signal_id")
                    or _signal_id(int(matrix.exit_signal_code[time_id, asset_id]), matrix.exit_signal_ids)
                ) if reason == "signal" else None,
            ))

        def _try_sell(
            time_id: int,
            asset_id: int,
            reason: str,
            signal_date: str,
            sold_today: set[int],
            override: float | None = None,
        ) -> bool:
            signal_id = (
                _signal_id(int(matrix.exit_signal_code[time_id, asset_id]), matrix.exit_signal_ids)
                if reason == "signal" else None
            )
            minute_trigger = config.exit_fill == "signal_next_minute" and reason == "signal"
            if minute_trigger and override is None:
                pos = positions[asset_id]
                if pos.get("pending_exit_next_open"):
                    override = float(matrix.open[time_id, asset_id])
                else:
                    override = _minute_trigger_price(time_id, asset_id)
                    if override is None:
                        _mark_pending(asset_id, reason, signal_date, signal_id, next_open=True)
                        _count("sell_minute_trigger_fallback")
                        return False
            ok, blocked = _can_sell(time_id, asset_id, override)
            if not ok:
                _mark_pending(
                    asset_id,
                    reason,
                    signal_date,
                    signal_id,
                    next_open=minute_trigger,
                )
                _count(blocked)
                return False
            _sell(time_id, asset_id, reason, signal_date, sold_today, override)
            return True

        for time_id, date_label in enumerate(matrix.timestamp_labels):
            date_text = date_label[:10]
            if time_id % 20 == 0:
                if cancel_event is not None and cancel_event.is_set():
                    logger.info("回测被用户取消 (第 %d/%d 天)", time_id, time_count)
                    break
                if progress_cb is not None:
                    try:
                        progress_cb({
                            "day": time_id + 1,
                            "total": time_count,
                            "date": date_text,
                            "equity": round(cash + _market_value(), 2),
                        })
                    except Exception:
                        pass

            sold_today: set[int] = set()
            for pos in positions.values():
                pos["hold_days"] += 1

            for asset_id in list(positions):
                pos = positions.get(asset_id)
                if pos is None or pos.get("pending_exit_reason") or pos["entry_date"] == date_text:
                    continue
                if not matrix.tradable[time_id, asset_id] or pos["entry_price"] <= 0:
                    continue
                open_price = float(matrix.open[time_id, asset_id])
                low_price = float(matrix.low[time_id, asset_id])
                high_price = float(matrix.high[time_id, asset_id])
                entry_price = float(pos["entry_price"])
                peak_price = float(pos["max_high"])
                risk_lines: list[tuple[float, str]] = []
                if config.stop_loss_pct is not None:
                    risk_lines.append((entry_price * (1 - abs(config.stop_loss_pct)), "stop_loss"))
                if config.trailing_stop_pct is not None:
                    risk_lines.append((peak_price * (1 - abs(config.trailing_stop_pct)), "trailing_stop"))
                activate = config.trailing_take_profit_activate_pct
                drawdown = config.trailing_take_profit_drawdown_pct
                if activate is not None and drawdown is not None and peak_price > entry_price:
                    if peak_price / entry_price - 1 >= abs(float(activate)):
                        risk_lines.append((peak_price * (1 - abs(float(drawdown))), "trailing_take_profit"))
                valid_lines = [(line, reason) for line, reason in risk_lines if _valid_price(line)]
                if valid_lines:
                    stop_price, reason = max(valid_lines, key=lambda item: item[0])
                    override = None
                    if _valid_price(open_price) and open_price <= stop_price:
                        override = open_price
                    elif _valid_price(low_price) and low_price <= stop_price:
                        override = stop_price
                    if override is not None:
                        _try_sell(time_id, asset_id, reason, date_text, sold_today, override)
                        continue
                if config.take_profit_pct is not None:
                    take_profit = entry_price * (1 + abs(float(config.take_profit_pct)))
                    if _valid_price(open_price) and open_price >= take_profit:
                        _try_sell(time_id, asset_id, "take_profit", date_text, sold_today, open_price)
                    elif _valid_price(high_price) and high_price >= take_profit:
                        _try_sell(time_id, asset_id, "take_profit", date_text, sold_today, take_profit)

            for asset_id in list(positions):
                pos = positions.get(asset_id)
                if pos is None:
                    continue
                reason = ""
                signal_date = date_text
                if pos.get("pending_exit_reason"):
                    reason = str(pos["pending_exit_reason"])
                    signal_date = str(pos.get("pending_exit_signal_date") or date_text)
                elif matrix.exit[time_id, asset_id]:
                    reason = "signal"
                    signal_date = _signal_date(int(matrix.exit_signal_time[time_id, asset_id]), date_text)
                elif config.max_hold_days is not None and pos["hold_days"] >= config.max_hold_days:
                    reason = "max_hold"
                elif time_id == time_count - 1:
                    reason = "end"
                if reason:
                    _try_sell(time_id, asset_id, reason, signal_date, sold_today)

            if time_id < time_count - 1 and max_positions > 0:
                # regime 软叠加: 熊市日缩放当日可分配敞口 (硬门控则已在上游清零 entry, 此处不缩放)
                _day_exposure = max_exposure_pct
                if config.regime_allow is not None and time_id < len(config.regime_allow):
                    if not config.regime_allow[time_id]:
                        _day_exposure = max_exposure_pct * config.regime_bear_weight
                candidates: list[tuple[int, float]] = []
                for asset_id in np.flatnonzero(matrix.entry[time_id]):
                    asset = int(asset_id)
                    if asset in positions:
                        continue
                    if asset in sold_today:
                        _count("buy_same_day_reentry")
                        continue
                    ok, blocked = _can_buy(time_id, asset)
                    if not ok:
                        _count(blocked)
                        continue
                    score = _matrix_entry_score(matrix, time_id, asset)
                    if config.score_min is not None and score < config.score_min:
                        _count("buy_score_filter")
                        continue
                    if config.score_max is not None and score > config.score_max:
                        _count("buy_score_filter")
                        continue
                    candidates.append((asset, score))
                candidates.sort(key=lambda item: item[1], reverse=True)
                slots = max_positions - len(positions)
                if slots <= 0:
                    execution_stats["buy_no_slot"] += len(candidates)
                elif candidates:
                    selected = candidates[:slots]
                    market_value_before = _market_value()
                    equity_before = cash + market_value_before
                    target_value = equity_before * _day_exposure / max_positions
                    exposure_capacity = equity_before * _day_exposure - market_value_before
                    if equity_before <= 0 or exposure_capacity <= 0 or _day_exposure <= 0:
                        execution_stats["buy_exposure"] += len(selected)
                    else:
                        weights = np.repeat(1 / len(selected), len(selected))
                        if config.position_sizing == "score_weight":
                            raw_weights = np.array([max(item[1], 0.0) for item in selected])
                            if raw_weights.sum() > 0:
                                weights = raw_weights / raw_weights.sum()
                        total_budget = min(cash, exposure_capacity, target_value * len(selected))
                        for (asset_id, entry_score), weight in zip(selected, weights):
                            if len(positions) >= max_positions:
                                _count("buy_no_slot")
                                break
                            market_value = _market_value()
                            equity = cash + market_value
                            capacity = equity * _day_exposure - market_value
                            allocation = min(total_budget * float(weight), target_value, cash, capacity)
                            if allocation <= 0:
                                _count("buy_exposure")
                                continue
                            entry_price = _refill_price(
                                time_id, asset_id, "buy", float(entry_prices[time_id, asset_id])
                            )
                            shares = np.floor(allocation / (entry_price * (1 + buy_cost_pct)) / 100) * 100
                            entry_value = shares * entry_price * (1 + buy_cost_pct)
                            if shares <= 0:
                                _count("buy_lot_size")
                                continue
                            if entry_value > cash + 1e-6:
                                _count("buy_cash")
                                continue
                            if entry_value > capacity + 1e-6:
                                _count("buy_exposure")
                                continue
                            cash -= entry_value
                            positions[asset_id] = {
                                "entry_date": date_text,
                                "entry_signal_date": _signal_date(
                                    int(matrix.entry_signal_time[time_id, asset_id]), date_text
                                ),
                                "entry_signal_id": _signal_id(
                                    int(matrix.entry_signal_code[time_id, asset_id]), matrix.entry_signal_ids
                                ),
                                "entry_price": entry_price,
                                "entry_value": entry_value,
                                "shares": shares,
                                "lots": shares / 100,
                                "position_pct": entry_value / equity_before if equity_before > 0 else 0.0,
                                "entry_score": entry_score,
                                "max_high": entry_price,
                                "hold_days": 0,
                                "pending_exit_reason": None,
                                "pending_exit_signal_date": None,
                                "pending_exit_signal_id": None,
                                "pending_exit_next_open": False,
                                "blocked_exit_days": 0,
                            }

            for asset_id, pos in positions.items():
                high_price = float(matrix.high[time_id, asset_id])
                if _valid_price(high_price):
                    pos["max_high"] = max(float(pos["max_high"]), high_price)
            valid_closes = np.isfinite(matrix.close[time_id]) & (matrix.close[time_id] > 0)
            last_close[valid_closes] = matrix.close[time_id, valid_closes]

            market_value = _market_value()
            equity = cash + market_value
            peak = max(peak, equity)
            drawdown = (equity - peak) / peak if peak > 0 else 0.0
            exposure = market_value / equity if equity > 0 else 0.0
            equity_value = round(float(equity), 2)
            exposure_value = round(float(exposure), 4)
            equity_values.append(equity_value)
            exposure_values.append(exposure_value)
            if options.include_curves:
                equity_curve.append({
                    "date": date_text,
                    "value": equity_value,
                    "cash": round(float(cash), 2),
                    "positions": len(positions),
                    "exposure": exposure_value,
                })
                drawdown_curve.append({
                    "date": date_text,
                    "value": round(float(drawdown), 4),
                })

        statistics_started = time.perf_counter()
        stats = self._calc_portfolio_stats_from_values(
            equity_values,
            exposure_values,
            trades,
            config.initial_capital,
            include_monte_carlo=options.include_monte_carlo,
        )
        stats["statistics_ms"] = round(
            (time.perf_counter() - statistics_started) * 1000,
            1,
        )
        stats["execution"] = execution_stats
        stats["pending_exit_positions"] = sum(1 for pos in positions.values() if pos.get("pending_exit_reason"))
        stats["market_matrix_shape"] = [time_count, asset_count]
        stats["market_matrix_bytes"] = matrix.nbytes
        return SimResult(
            equity_curve=equity_curve if options.include_curves else [],
            drawdown_curve=drawdown_curve if options.include_curves else [],
            trades=trades if options.include_trades else [],
            per_symbol_stats=(
                self._calc_per_symbol(trades)
                if options.include_per_symbol_stats
                else []
            ),
            stats=stats,
        )

    def simulate_portfolio_legacy(
        self,
        panel: pl.DataFrame,
        entries: pl.Series | None,
        exits: pl.Series | None,
        config: MatcherConfig,
        progress_cb: "Callable[[dict], None] | None" = None,
        cancel_event: "threading.Event | None" = None,
        entry_signal_ids: list[str] | None = None,
        exit_signal_ids: list[str] | None = None,
    ) -> SimResult:
        """账户级组合回测：日线信号 → 成交约束 → 仓位/现金撮合。"""
        if panel.is_empty():
            return self._empty_result()

        n = len(panel)
        panel_dates = panel["date"].to_numpy()
        panel_symbols = panel["symbol"].to_numpy()

        ent_raw = np.zeros(n, dtype=bool)
        ext_raw = np.zeros(n, dtype=bool)
        if entries is not None and len(entries) == n:
            ent_raw = entries.to_numpy().astype(bool)
        if exits is not None and len(exits) == n:
            ext_raw = exits.to_numpy().astype(bool)
        if not ent_raw.any():
            return self._empty_result()

        entry_signal_dates = np.array([None] * n, dtype=object)
        exit_signal_dates = np.array([None] * n, dtype=object)
        same_prev_symbol = panel_symbols[1:] == panel_symbols[:-1]

        # 建仓口径: close_t 用信号日收盘, open_t+1 右移到次日 open 成交。
        ent = np.zeros(n, dtype=bool)
        if config.entry_fill == "open_t+1":
            ent[1:] = ent_raw[:-1] & same_prev_symbol
            for idx in np.flatnonzero(ent):
                entry_signal_dates[idx] = self._date_str(panel_dates[idx - 1])
        else:
            ent = ent_raw
            for idx in np.flatnonzero(ent):
                entry_signal_dates[idx] = self._date_str(panel_dates[idx])

        # 清仓口径: 独立于建仓。
        ext = np.zeros(n, dtype=bool)
        if config.exit_fill == "open_t+1":
            ext[1:] = ext_raw[:-1] & same_prev_symbol
            for idx in np.flatnonzero(ext):
                exit_signal_dates[idx] = self._date_str(panel_dates[idx - 1])
        else:
            ext = ext_raw
            for idx in np.flatnonzero(ext):
                exit_signal_dates[idx] = self._date_str(panel_dates[idx])

        open_prices = panel["open"].to_numpy()
        high_prices = panel["high"].to_numpy() if "high" in panel.columns else open_prices
        low_prices = panel["low"].to_numpy()
        close_prices = panel["close"].to_numpy()
        # 撮合价: 建仓/清仓各自独立选列。
        entry_prices = open_prices if config.entry_fill == "open_t+1" else close_prices
        exit_prices = open_prices if config.exit_fill == "open_t+1" else close_prices
        has_volume = "volume" in panel.columns
        volumes = panel["volume"].fill_null(0).to_numpy() if has_volume else np.ones(n, dtype=float)
        names = (
            panel["name"].fill_null("").to_numpy()
            if "name" in panel.columns else np.array([""] * n)
        )
        scores = (
            panel["score"].fill_null(0).to_numpy()
            if "score" in panel.columns else np.zeros(n, dtype=float)
        )
        trade_scores = scores.copy()
        # 评分跟随建仓口径 shift (评分在买入日生效)。
        if config.entry_fill == "open_t+1":
            trade_scores[1:] = np.where(panel_symbols[1:] == panel_symbols[:-1], scores[:-1], trade_scores[1:])
        limit_up_flags = (
            panel["signal_limit_up"].fill_null(False).to_numpy().astype(bool)
            if "signal_limit_up" in panel.columns else np.zeros(n, dtype=bool)
        )
        limit_down_flags = (
            panel["signal_limit_down"].fill_null(False).to_numpy().astype(bool)
            if "signal_limit_down" in panel.columns else np.zeros(n, dtype=bool)
        )

        date_to_indices: dict[str, list[int]] = {}
        for i, d in enumerate(panel_dates):
            d_str = self._date_str(d)
            date_to_indices.setdefault(d_str, []).append(i)
        all_dates = sorted(date_to_indices.keys())
        if not all_dates:
            return self._empty_result()

        buy_cost_pct = config.buy_cost_pct()
        sell_cost_pct = config.sell_cost_pct()
        cash = float(config.initial_capital)
        peak = cash
        max_positions = max(int(config.max_positions), 0)
        max_exposure_pct = min(max(float(getattr(config, "max_exposure_pct", 1.0)), 0.0), 1.0)
        score_min = getattr(config, "score_min", None)
        score_max = getattr(config, "score_max", None)
        positions: dict[str, dict] = {}
        last_close: dict[str, float] = {}
        trades: list[TradeRecord] = []

        # ── 分钟K精确成交预加载 ──
        # 信号触发日加载分钟K, 成交时用穿越价/VWAP替代收盘价
        minute_cache: dict = {}  # {(symbol, date_str): structured ndarray}
        if config.minute_fill:
            trigger_dates: set[str] = set()
            trigger_symbols: set[str] = set()
            for idx in range(n):
                if ent[idx] or ext[idx]:
                    trigger_dates.add(self._date_str(panel_dates[idx]))
                    trigger_symbols.add(str(panel_symbols[idx]))
            if trigger_dates and trigger_symbols:
                asset_type = "etf" if all(
                    str(s).endswith(".SH") and str(s).startswith("5") for s in list(trigger_symbols)[:5]
                ) else "stock"
                loaded = self._load_minute_for_fills(
                    self.repo, list(trigger_symbols), trigger_dates, asset_type,
                )
                for key, marr in loaded.items():
                    if marr is not None and len(marr) > 0:
                        minute_cache[key] = marr

        def _refill_price(idx: int, side: str, daily_price: float) -> float:
            """分钟K精确成交价; 无数据则降级为 daily_price。"""
            if not config.minute_fill or not minute_cache:
                return daily_price
            sym = str(panel_symbols[idx])
            d_str = self._date_str(panel_dates[idx])
            marr = minute_cache.get((sym, d_str))
            if marr is None:
                return daily_price
            # 参考线: 从 panel 取 ma5/ma10/ma20 作为近似 (均线类信号)
            ref = None
            for col in ("ma5", "ma10", "ma20"):
                if col in panel.columns:
                    val = panel[col][idx]
                    try:
                        fv = float(val)
                        if fv > 0 and np.isfinite(fv):
                            ref = fv
                            break
                    except (TypeError, ValueError):
                        pass
            precise = self._resolve_minute_fill(marr, ref, side)
            return precise if precise is not None else daily_price

        equity_curve: list[dict] = []
        drawdown_curve: list[dict] = []
        execution_stats: dict[str, int] = {
            "buy_invalid_price": 0,
            "buy_suspended": 0,
            "buy_limit_up": 0,
            "buy_no_slot": 0,
            "buy_cash": 0,
            "buy_lot_size": 0,
            "buy_same_day_reentry": 0,
            "buy_exposure": 0,
            "buy_score_filter": 0,
            "sell_invalid_price": 0,
            "sell_suspended": 0,
            "sell_limit_down": 0,
            "pending_exit": 0,
        }

        def _count(key: str) -> None:
            execution_stats[key] = execution_stats.get(key, 0) + 1

        def _valid_price(value) -> bool:
            try:
                v = float(value)
            except (TypeError, ValueError):
                return False
            return v > 0 and np.isfinite(v)

        def _market_value() -> float:
            value = 0.0
            for pos in positions.values():
                mark = last_close.get(pos["symbol"], pos["entry_price"])
                value += pos["shares"] * mark
            return value

        def _is_suspended(idx: int) -> bool:
            o = float(open_prices[idx])
            h = float(high_prices[idx])
            l = float(low_prices[idx])
            c = float(close_prices[idx])
            valid_bar = any(_valid_price(x) for x in (o, h, l, c))
            if not valid_bar:
                return True
            if has_volume and float(volumes[idx] or 0) <= 0:
                same_price = max(o, h, l, c) - min(o, h, l, c) <= max(abs(c) * 1e-4, 0.01)
                if same_price:
                    return True
            return False

        def _is_one_price_limit(idx: int, direction: str) -> bool:
            if _is_suspended(idx):
                return False
            o = float(open_prices[idx])
            h = float(high_prices[idx])
            l = float(low_prices[idx])
            c = float(close_prices[idx])
            if not all(_valid_price(x) for x in (o, h, l, c)):
                return False
            same_price = max(o, h, l, c) - min(o, h, l, c) <= max(abs(c) * 1e-4, 0.01)
            if direction == "up":
                return bool(limit_up_flags[idx]) and same_price
            return bool(limit_down_flags[idx]) and same_price

        def _can_buy(idx: int) -> tuple[bool, str]:
            if _is_suspended(idx):
                return False, "buy_suspended"
            if not _valid_price(entry_prices[idx]):
                return False, "buy_invalid_price"
            if _is_one_price_limit(idx, "up"):
                return False, "buy_limit_up"
            return True, ""

        def _can_sell(idx: int, exit_price_override: float | None = None) -> tuple[bool, str]:
            if _is_suspended(idx):
                return False, "sell_suspended"
            exit_price = exit_price_override if exit_price_override is not None else exit_prices[idx]
            if not _valid_price(exit_price):
                return False, "sell_invalid_price"
            if _is_one_price_limit(idx, "down"):
                return False, "sell_limit_down"
            return True, ""

        def _mark_pending(sym: str, reason: str, signal_date: str) -> None:
            pos = positions[sym]
            if not pos.get("pending_exit_reason"):
                pos["pending_exit_reason"] = reason
                pos["pending_exit_signal_date"] = signal_date
                _count("pending_exit")
            pos["blocked_exit_days"] = int(pos.get("blocked_exit_days", 0)) + 1

        def _sell(
            sym: str,
            idx: int,
            reason: str,
            signal_date: str,
            sold_today: set[str],
            exit_price_override: float | None = None,
        ) -> None:
            nonlocal cash
            pos = positions.pop(sym)
            if exit_price_override is not None:
                exit_price = float(exit_price_override)
            else:
                exit_price = _refill_price(idx, "sell", float(exit_prices[idx]))
            exit_value = pos["shares"] * exit_price * (1 - sell_cost_pct)
            cash += exit_value
            pnl_amount = exit_value - pos["entry_value"]
            pnl_pct = (exit_value - pos["entry_value"]) / pos["entry_value"] if pos["entry_value"] > 0 else 0.0
            sold_today.add(sym)
            trades.append(TradeRecord(
                symbol=sym,
                name=pos.get("name", ""),
                entry_date=pos["entry_date"],
                exit_date=self._date_str(panel_dates[idx]),
                entry_price=round(float(pos["entry_price"]), 4),
                exit_price=round(exit_price, 4),
                pnl_pct=round(float(pnl_pct), 6),
                duration=int(pos["hold_days"]),
                exit_reason=reason,
                shares=round(float(pos["shares"]), 4),
                lots=round(float(pos["lots"]), 2),
                position_pct=round(float(pos.get("position_pct", 0.0)), 6),
                entry_value=round(float(pos["entry_value"]), 2),
                exit_value=round(float(exit_value), 2),
                pnl_amount=round(float(pnl_amount), 2),
                entry_score=round(float(pos["entry_score"]), 2) if pos.get("entry_score") is not None else None,
                entry_signal_date=pos.get("entry_signal_date"),
                exit_signal_date=signal_date,
                blocked_exit_days=int(pos.get("blocked_exit_days", 0)),
                entry_signal_id=pos.get("entry_signal_id"),
                exit_signal_id=_resolve_signal_id(panel, idx, exit_signal_ids) if reason == "signal" else None,
            ))

        def _try_sell(
            sym: str,
            idx: int | None,
            reason: str,
            signal_date: str,
            sold_today: set[str],
            exit_price_override: float | None = None,
        ) -> bool:
            if idx is None:
                _mark_pending(sym, reason, signal_date)
                _count("sell_suspended")
                return False
            ok, block_reason = _can_sell(idx, exit_price_override)
            if not ok:
                _mark_pending(sym, reason, signal_date)
                _count(block_reason)
                return False
            _sell(sym, idx, reason, signal_date, sold_today, exit_price_override)
            return True

        def _process_scheduled_exits(
            d_idx: int,
            d_str: str,
            row_by_symbol: dict[str, int],
            sold_today: set[str],
        ) -> None:
            for sym in list(positions.keys()):
                pos = positions.get(sym)
                if pos is None:
                    continue
                idx = row_by_symbol.get(sym)
                reason = ""
                signal_date = d_str
                if pos.get("pending_exit_reason"):
                    reason = str(pos["pending_exit_reason"])
                    signal_date = str(pos.get("pending_exit_signal_date") or d_str)
                # 卖点信号优先于到期: 策略主动离场先于 max_hold 兜底。
                elif idx is not None and ext[idx]:
                    reason = "signal"
                    signal_date = str(exit_signal_dates[idx] or d_str)
                elif config.max_hold_days is not None and pos["hold_days"] >= config.max_hold_days:
                    reason = "max_hold"
                elif d_idx == len(all_dates) - 1:
                    reason = "end"
                if reason:
                    _try_sell(sym, idx, reason, signal_date, sold_today)

        def _process_risk_exits(d_str: str, row_by_symbol: dict[str, int], sold_today: set[str]) -> None:
            for sym in list(positions.keys()):
                pos = positions.get(sym)
                if pos is None or pos.get("pending_exit_reason"):
                    continue
                if pos.get("entry_date") == d_str:
                    continue
                idx = row_by_symbol.get(sym)
                if idx is None or pos["entry_price"] <= 0:
                    continue
                open_price = float(open_prices[idx])
                low_price = float(low_prices[idx])
                high_price = float(high_prices[idx])
                entry_price = float(pos["entry_price"])
                peak_price = float(pos.get("max_high", entry_price))
                risk_lines: list[tuple[float, str]] = []

                if config.stop_loss_pct is not None:
                    risk_lines.append((entry_price * (1 - abs(config.stop_loss_pct)), "stop_loss"))

                if config.trailing_stop_pct is not None and peak_price > 0:
                    risk_lines.append((peak_price * (1 - abs(config.trailing_stop_pct)), "trailing_stop"))

                activate_pct = getattr(config, "trailing_take_profit_activate_pct", None)
                drawdown_pct = getattr(config, "trailing_take_profit_drawdown_pct", None)
                if activate_pct is not None and drawdown_pct is not None and peak_price > entry_price:
                    peak_profit = peak_price / entry_price - 1
                    if peak_profit >= abs(float(activate_pct)):
                        # 回撤止盈触发线: 相对峰值价回撤 drawdown 个点 (纯峰值口径)
                        # 启动门槛用成本基准的浮盈率, 触发线用峰值基准, 与 trailing_stop 同口径
                        take_profit_line = peak_price * (1 - abs(float(drawdown_pct)))
                        risk_lines.append((take_profit_line, "trailing_take_profit"))

                # 止损/移损/回撤止盈: 价格跌破风控线触发
                risk_lines = [(line, reason) for line, reason in risk_lines if _valid_price(line)]
                if risk_lines:
                    stop_price, reason = max(risk_lines, key=lambda item: item[0])
                    exit_price_override = None
                    if _valid_price(open_price) and open_price <= stop_price:
                        exit_price_override = open_price
                    elif _valid_price(low_price) and low_price <= stop_price:
                        exit_price_override = stop_price
                    if exit_price_override is not None:
                        _try_sell(sym, idx, reason, d_str, sold_today, exit_price_override)
                        continue

                # 固定止盈: 价格涨破止盈线触发
                tp_pct = getattr(config, "take_profit_pct", None)
                if tp_pct is not None:
                    tp_line = entry_price * (1 + abs(float(tp_pct)))
                    if _valid_price(tp_line):
                        if _valid_price(open_price) and open_price >= tp_line:
                            _try_sell(sym, idx, "take_profit", d_str, sold_today, open_price)
                        elif _valid_price(high_price) and high_price >= tp_line:
                            _try_sell(sym, idx, "take_profit", d_str, sold_today, tp_line)

        def _process_entries(
            d_str: str,
            idxs: list[int],
            sold_today: set[str],
        ) -> None:
            nonlocal cash
            if max_positions <= 0:
                return
            candidates: list[tuple[int, str, float]] = []
            for idx in idxs:
                if not ent[idx]:
                    continue
                sym = str(panel_symbols[idx])
                if sym in positions:
                    continue
                if sym in sold_today:
                    _count("buy_same_day_reentry")
                    continue
                ok, block_reason = _can_buy(idx)
                if not ok:
                    _count(block_reason)
                    continue
                score = float(trade_scores[idx] or 0.0)
                if score_min is not None and score < score_min:
                    _count("buy_score_filter")
                    continue
                if score_max is not None and score > score_max:
                    _count("buy_score_filter")
                    continue
                candidates.append((idx, sym, score))
            if not candidates:
                return
            candidates.sort(key=lambda x: x[2], reverse=True)

            slots = max_positions - len(positions)
            if slots <= 0:
                execution_stats["buy_no_slot"] += len(candidates)
                return

            selected = candidates[:slots]
            market_value_before = _market_value()
            account_equity_before_buy = cash + market_value_before
            if account_equity_before_buy <= 0 or max_exposure_pct <= 0:
                execution_stats["buy_exposure"] += len(selected)
                return
            target_position_value = account_equity_before_buy * max_exposure_pct / max_positions
            max_exposure_value = account_equity_before_buy * max_exposure_pct
            exposure_capacity = max_exposure_value - market_value_before
            if exposure_capacity <= 0:
                execution_stats["buy_exposure"] += len(selected)
                return

            weights = np.repeat(1 / len(selected), len(selected))
            if config.position_sizing == "score_weight":
                raw = np.array([max(x[2], 0.0) for x in selected], dtype=float)
                if raw.sum() > 0:
                    weights = raw / raw.sum()
            total_budget = min(cash, exposure_capacity, target_position_value * len(selected))

            for (idx, sym, _score), weight in zip(selected, weights):
                if len(positions) >= max_positions:
                    _count("buy_no_slot")
                    break
                current_market_value = _market_value()
                current_equity = cash + current_market_value
                current_exposure_capacity = current_equity * max_exposure_pct - current_market_value
                allocation = min(total_budget * float(weight), target_position_value, cash, current_exposure_capacity)
                if allocation <= 0:
                    _count("buy_exposure")
                    continue
                entry_price = _refill_price(idx, "buy", float(entry_prices[idx]))
                shares = np.floor(allocation / (entry_price * (1 + buy_cost_pct)) / 100) * 100
                entry_value = shares * entry_price * (1 + buy_cost_pct)
                if shares <= 0:
                    _count("buy_lot_size")
                    continue
                if entry_value > cash + 1e-6:
                    _count("buy_cash")
                    continue
                if entry_value > current_exposure_capacity + 1e-6:
                    _count("buy_exposure")
                    continue
                cash -= entry_value
                positions[sym] = {
                    "symbol": sym,
                    "name": str(names[idx] or ""),
                    "entry_date": self._date_str(panel_dates[idx]),
                    "entry_signal_date": entry_signal_dates[idx] or self._date_str(panel_dates[idx]),
                    "entry_signal_id": _resolve_signal_id(panel, idx, entry_signal_ids),
                    "entry_price": entry_price,
                    "entry_value": entry_value,
                    "shares": shares,
                    "lots": shares / 100,
                    "position_pct": entry_value / account_equity_before_buy if account_equity_before_buy > 0 else 0.0,
                    "entry_score": _score,
                    "max_high": entry_price,
                    "hold_days": 0,
                    "pending_exit_reason": None,
                    "pending_exit_signal_date": None,
                    "blocked_exit_days": 0,
                }

        for d_idx, d_str in enumerate(all_dates):
            if d_idx % 20 == 0:
                if cancel_event is not None and cancel_event.is_set():
                    logger.info("回测被用户取消 (第 %d/%d 天)", d_idx, len(all_dates))
                    break
                if progress_cb is not None:
                    try:
                        progress_cb({
                            "day": d_idx + 1,
                            "total": len(all_dates),
                            "date": str(d_str)[:10],
                            "equity": round(cash + _market_value(), 2),
                        })
                    except Exception:
                        pass

            idxs = date_to_indices[d_str]
            row_by_symbol = {str(panel_symbols[i]): i for i in idxs}
            sold_today: set[str] = set()

            for pos in positions.values():
                pos["hold_days"] += 1

            # 统一执行顺序 (不分口径): 风控(止损/移动止损/止盈) → 计划出场(signal/max_hold/end) → 建仓。
            # 风控是保护性离场, 必须最先; 计划出场次之; 建仓最后 (卖出释放的现金/仓位先用于满足新买)。
            # 当天新建仓不会被风控误杀 (_process_risk_exits 跳过 entry_date == d_str 的仓位)。
            _process_risk_exits(d_str, row_by_symbol, sold_today)
            _process_scheduled_exits(d_idx, d_str, row_by_symbol, sold_today)
            if d_idx < len(all_dates) - 1:
                _process_entries(d_str, idxs, sold_today)

            for sym, pos in positions.items():
                idx = row_by_symbol.get(sym)
                if idx is not None:
                    hi = float(high_prices[idx])
                    if _valid_price(hi):
                        pos["max_high"] = max(float(pos.get("max_high", pos["entry_price"])), hi)

            for i in idxs:
                c = float(close_prices[i])
                if c > 0 and np.isfinite(c):
                    last_close[str(panel_symbols[i])] = c

            market_value = _market_value()
            equity = cash + market_value
            peak = max(peak, equity)
            dd = (equity - peak) / peak if peak > 0 else 0.0
            exposure = market_value / equity if equity > 0 else 0.0
            equity_curve.append({
                "date": d_str[:10],
                "value": round(float(equity), 2),
                "cash": round(float(cash), 2),
                "positions": len(positions),
                "exposure": round(float(exposure), 4),
            })
            drawdown_curve.append({"date": d_str[:10], "value": round(float(dd), 4)})

        stats = self._calc_portfolio_stats(equity_curve, trades, config.initial_capital)
        stats["execution"] = execution_stats
        stats["pending_exit_positions"] = sum(1 for p in positions.values() if p.get("pending_exit_reason"))
        per_symbol = self._calc_per_symbol(trades)
        return SimResult(
            equity_curve=equity_curve,
            drawdown_curve=drawdown_curve,
            trades=trades,
            per_symbol_stats=per_symbol,
            stats=stats,
        )

    # ── 净值曲线 ──────────────────────────────────────

    @staticmethod
    def _build_curves(
        trades: list[TradeRecord],
        all_dates: np.ndarray,
        initial_capital: float,
    ) -> tuple[list[dict], list[dict]]:
        """从交易记录构建日频净值曲线和回撤曲线。

        资金模型: 每笔交易等权分配 (1/N_capital)，N_capital = 同时持仓数上限。
        简化版: 按出场日归集所有已平仓交易的平均收益作为当日组合收益。
        """
        if not trades or len(all_dates) == 0:
            return [], []

        # 按出场日归集 pnl
        exit_pnl: dict[str, list[float]] = {}
        for t in trades:
            d_str = str(t.exit_date)
            exit_pnl.setdefault(d_str, []).append(t.pnl_pct)

        equity = initial_capital
        peak = initial_capital
        curve: list[dict] = []
        dd_curve: list[dict] = []

        for d in all_dates:
            d_str = str(d.item() if hasattr(d, "item") else d)
            pnls = exit_pnl.get(d_str, [])
            # 当日组合收益 = 该日所有出场交易的平均收益
            daily_ret = float(np.mean(pnls)) if pnls else 0.0
            equity *= (1 + daily_ret)
            peak = max(peak, equity)
            dd = (equity - peak) / peak if peak > 0 else 0.0
            curve.append({"date": d_str[:10], "value": round(equity, 2)})
            dd_curve.append({"date": d_str[:10], "value": round(dd, 4)})

        return curve, dd_curve

    # ── 统计计算 ──────────────────────────────────────

    @staticmethod
    def _sortino_ratio(returns: np.ndarray, periods_per_year: int = 252) -> float | None:
        """Sortino 比率: 用下行偏差 (仅惩罚负收益) 替代总标准差, 年化。

        下行偏差 = sqrt(mean(min(r, 0)^2)), MAR=0 的目标半方差 (对全部样本求均, 非仅负样本)。
        无下行波动 (无亏损) 时 Sortino 未定义, 返回 None (与 profit_factor 的 None 约定一致,
        不虚报 0 或 inf)。样本不足 (<2) 返回 0.0 (与 sharpe 的退化约定一致)。
        """
        returns = returns[np.isfinite(returns)]  # 剔除 inf/nan, 防止污染均值/序列化出非法 JSON
        if len(returns) < 2:
            return 0.0
        mean = float(np.mean(returns))
        downside = np.minimum(returns, 0.0)
        downside_dev = float(np.sqrt(np.mean(downside ** 2)))
        if downside_dev <= 0:
            return None
        return mean / downside_dev * float(np.sqrt(periods_per_year))

    @staticmethod
    def _mc_drawdown_percentiles(pnls: np.ndarray, n_sims: int = 1000) -> dict:
        """自助重抽样交易序列, 估计最大回撤的分布 — 回答"仅因成交顺序运气, 回撤能有多坏"。

        对每笔收益有放回重抽样 n_sims 次, 各自算最大回撤, 取分位:
        - mc_maxdd_p50: 中位场景最大回撤
        - mc_maxdd_p95: 95% 置信最坏场景 (= 分布 5 分位, 更负)

        固定种子保证可复现/可测。样本 <3 无统计意义, 返回 None。
        大样本 (如 full 模式数千笔) 时按 2M 单元上限压降模拟次数, 防止瞬时数组 OOM。
        """
        pnls = pnls[np.isfinite(pnls)]  # 剔除 inf/nan, 否则 cumprod 传播 nan 导致分位为 nan
        # 防御: 单笔 pnl <= -100% 时 (1+pnl) <= 0 会让 cumprod 符号翻转/得非正净值, 回撤失真。
        # 回测有止损, 实际不会发生; 兜底 clip 到 -99.99% 保证 (1+pnl) 恒正。
        pnls = np.clip(pnls, -0.9999, None)
        n = len(pnls)
        if n < 3:
            return {"mc_maxdd_p50": None, "mc_maxdd_p95": None}
        # 内存护栏: samples/equity/peak/dd 各占 eff_sims*n*8B, 控总单元 <= 2M (~64MB 峰值)
        eff_sims = min(n_sims, max(200, 2_000_000 // n))
        rng = np.random.default_rng(42)
        samples = rng.choice(pnls, size=(eff_sims, n), replace=True)
        equity = np.cumprod(1.0 + samples, axis=1)
        peak = np.maximum.accumulate(equity, axis=1)
        dd = (equity - peak) / peak
        maxdds = dd.min(axis=1)
        return {
            "mc_maxdd_p50": round(float(np.percentile(maxdds, 50)), 4),
            "mc_maxdd_p95": round(float(np.percentile(maxdds, 5)), 4),
        }

    @staticmethod
    def _per_trade_block(pnls: np.ndarray, durations: np.ndarray) -> dict:
        """per-trade 明细字段: best/worst/median_pnl/avg_holding_days。"""
        pnls = pnls[np.isfinite(pnls)]  # 剔除 inf/nan, 防 best/worst 出非法值
        durations = durations[np.isfinite(durations)] if len(durations) else durations
        if not len(pnls):
            return {"best": 0.0, "worst": 0.0, "median_pnl": 0.0, "avg_holding_days": 0.0}
        return {
            "best": round(float(np.max(pnls)), 4),
            "worst": round(float(np.min(pnls)), 4),
            "median_pnl": round(float(np.median(pnls)), 4),
            "avg_holding_days": round(float(np.mean(durations)), 1) if len(durations) else 0.0,
        }

    @staticmethod
    def _calc_stats(
        trades: list[TradeRecord],
        initial_capital: float,
        start: date,
        end: date,
        *,
        include_monte_carlo: bool = True,
    ) -> dict:
        if not trades:
            return {"total_return": 0, "n_trades": 0}

        pnls = np.array([t.pnl_pct for t in trades])
        n_trades = len(trades)

        # 从净值曲线推算总收益 (等权组合)
        cumulative = 1.0
        for p in pnls:
            cumulative *= (1 + p)
        # 修正: 等权组合的总收益不等于各笔复乘，用曲线终点更准
        # 但这里作为简化，用各笔复乘作为近似
        total_return = cumulative - 1.0

        # 年化
        n_days = max((end - start).days, 1)
        years = n_days / 365.25
        if total_return > -1.0 and years > 0:
            annual_return = (1 + total_return) ** (1 / years) - 1
        else:
            annual_return = total_return

        # 胜率
        wins = pnls[pnls > 0]
        losses = pnls[pnls <= 0]
        win_rate = len(wins) / n_trades

        # 盈亏比
        avg_win = float(np.mean(wins)) if len(wins) > 0 else 0.0
        avg_loss = abs(float(np.mean(losses))) if len(losses) > 0 else 0.0
        profit_factor = avg_win / avg_loss if avg_loss > 0 else (float("inf") if avg_win > 0 else 0.0)

        # 最大回撤 — 用交易序列近似
        equity = initial_capital
        peak = initial_capital
        max_dd = 0.0
        for p in pnls:
            equity *= (1 + p)
            peak = max(peak, equity)
            dd = (equity - peak) / peak
            max_dd = min(max_dd, dd)

        # 夏普 — 用交易收益标准差近似
        sharpe = float(np.mean(pnls) / np.std(pnls)) * np.sqrt(252) if np.std(pnls) > 0 else 0.0

        # Sortino: 刻意沿用本函数 sharpe 的逐笔收益 x sqrt(252) 基准。逐笔年化非严格正确,
        # 但保证同一函数内 sharpe/sortino 口径一致可比 (内部一致 > 局部绝对)。仅惩罚下行波动。
        sortino = BacktestEngine._sortino_ratio(pnls)

        # Calmar
        calmar = annual_return / abs(max_dd) if abs(max_dd) > 0.001 else 0.0

        durations = np.array([t.duration for t in trades], dtype=float)
        stats = {
            "total_return": round(float(total_return), 4),
            "annual_return": round(float(annual_return), 4),
            "max_drawdown": round(float(max_dd), 4),
            "sharpe": round(float(sharpe), 2),
            "sortino": round(float(sortino), 2) if sortino is not None else None,
            "calmar": round(float(calmar), 2),
            "win_rate": round(float(win_rate), 4),
            "profit_factor": round(float(profit_factor), 2) if np.isfinite(profit_factor) else None,
            "n_trades": n_trades,
            "avg_pnl": round(float(np.mean(pnls)), 4),
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            **BacktestEngine._per_trade_block(pnls, durations),
        }
        if include_monte_carlo:
            stats.update(BacktestEngine._mc_drawdown_percentiles(pnls))
        return stats

    @staticmethod
    def _calc_per_symbol(trades: list[TradeRecord]) -> list[dict]:
        if not trades:
            return []
        by_sym: dict[str, dict] = {}
        for t in trades:
            s = t.symbol
            d = by_sym.setdefault(s, {
                "symbol": s, "n_trades": 0, "total_return": 1.0,
                "best": -999.0, "worst": 999.0, "wins": 0, "pnls": [],
            })
            d["n_trades"] += 1
            d["pnls"].append(t.pnl_pct)
            d["total_return"] *= (1 + t.pnl_pct)
            d["best"] = max(d["best"], t.pnl_pct)
            d["worst"] = min(d["worst"], t.pnl_pct)
            if t.pnl_pct > 0:
                d["wins"] += 1

        result = []
        for d in by_sym.values():
            result.append({
                "symbol": d["symbol"],
                "n_trades": d["n_trades"],
                "total_return": round(d["total_return"] - 1.0, 4),
                "win_rate": round(d["wins"] / d["n_trades"], 4) if d["n_trades"] > 0 else 0.0,
                "best": round(d["best"], 4),
                "worst": round(d["worst"], 4),
            })
        return sorted(result, key=lambda x: x["total_return"], reverse=True)

    @staticmethod
    def _calc_independent_candidate_result(
        trades: list[TradeRecord],
        n_candidates: int,
        execution_stats: dict[str, int],
        *,
        options: SimulationOptions | None = None,
    ) -> SimResult:
        """全量独立候选统计：按每个候选样本的实际执行收益聚合。"""
        options = options or SimulationOptions()
        if not trades:
            return SimResult(
                equity_curve=[],
                drawdown_curve=[],
                trades=[],
                per_symbol_stats=[],
                stats={
                    "mode": "full",
                    "full_kind": "candidate_execution",
                    "error": "no executable trades",
                    "n_candidates": int(n_candidates),
                    "n_trades": 0,
                    "execution": execution_stats,
                },
            )

        pnls = np.array([t.pnl_pct for t in trades], dtype=float)
        durations = np.array([t.duration for t in trades], dtype=float)
        wins = pnls[pnls > 0]
        losses = pnls[pnls <= 0]
        avg_win = float(np.mean(wins)) if len(wins) else 0.0
        avg_loss = abs(float(np.mean(losses))) if len(losses) else 0.0

        # 按退出日聚合已实现样本收益, 构造“样本收益曲线”。它不是账户净值。
        daily_returns: dict[str, list[float]] = {}
        for t in trades:
            daily_returns.setdefault(str(t.exit_date)[:10], []).append(float(t.pnl_pct))

        equity_curve: list[dict] = []
        drawdown_curve: list[dict] = []
        equity_values: list[float] = []
        equity = 1.0
        peak = 1.0
        daily_avg: list[float] = []
        for d_str in sorted(daily_returns.keys()):
            values = daily_returns[d_str]
            avg_ret = float(np.mean(values)) if values else 0.0
            daily_avg.append(avg_ret)
            equity *= (1 + avg_ret)
            peak = max(peak, equity)
            dd = (equity - peak) / peak if peak > 0 else 0.0
            equity_value = round(float(equity), 4)
            equity_values.append(equity_value)
            if options.include_curves:
                equity_curve.append({
                    "date": d_str,
                    "value": equity_value,
                    "positions": len(values),
                })
                drawdown_curve.append({
                    "date": d_str,
                    "value": round(float(dd), 4),
                })

        statistics_started = time.perf_counter()
        values = np.array(equity_values, dtype=float)
        total_return = float(values[-1] - 1.0) if len(values) else 0.0
        peaks = np.maximum.accumulate(values) if len(values) else np.array([])
        drawdowns = values / peaks - 1 if len(values) else np.array([])
        max_drawdown = float(drawdowns.min()) if len(drawdowns) else 0.0
        daily = np.array(daily_avg, dtype=float)
        sharpe = float(np.mean(daily) / np.std(daily) * np.sqrt(252)) if len(daily) > 1 and np.std(daily) > 0 else 0.0
        sortino = BacktestEngine._sortino_ratio(daily)

        stats = {
            "mode": "full",
            "full_kind": "candidate_execution",
            "n_candidates": int(n_candidates),
            "n_trades": int(len(trades)),
            "n_days": int(len(daily_returns)),
            "avg_daily_candidates": round(float(len(trades) / max(len(daily_returns), 1)), 1),
            "avg_return": round(float(np.mean(pnls)), 4),
            "median_return": round(float(np.median(pnls)), 4),
            "win_rate": round(float(len(wins) / len(pnls)), 4) if len(pnls) else 0.0,
            "profit_factor": round(float(avg_win / avg_loss), 2) if avg_loss > 0 else None,
            "best": round(float(np.max(pnls)), 4),
            "worst": round(float(np.min(pnls)), 4),
            "avg_duration": round(float(np.mean(durations)), 1) if len(durations) else 0.0,
            "total_return": round(float(total_return), 4),
            "max_drawdown": round(float(max_drawdown), 4),
            "sharpe": round(float(sharpe), 2),
            "sortino": round(float(sortino), 2) if sortino is not None else None,
            "execution": execution_stats,
        }
        if options.include_return_distribution:
            lo, hi, nbins = -0.20, 0.20, 20
            clipped = np.clip(pnls, lo, hi)
            counts, edges = np.histogram(clipped, bins=nbins, range=(lo, hi))
            stats["return_distribution"] = [
                {
                    "range": f"{(edges[i]*100):+.0f}~{(edges[i+1]*100):+.0f}%",
                    "count": int(counts[i]),
                    "ratio": round(float(counts[i] / pnls.size), 4) if pnls.size else 0.0,
                }
                for i in range(nbins)
            ]
        if options.include_monte_carlo:
            stats.update(BacktestEngine._mc_drawdown_percentiles(pnls))
        stats["statistics_ms"] = round(
            (time.perf_counter() - statistics_started) * 1000,
            1,
        )

        return SimResult(
            equity_curve=equity_curve if options.include_curves else [],
            drawdown_curve=drawdown_curve if options.include_curves else [],
            trades=trades if options.include_trades else [],
            per_symbol_stats=(
                BacktestEngine._calc_per_symbol(trades)
                if options.include_per_symbol_stats
                else []
            ),
            stats=stats,
        )

    @staticmethod
    def _calc_portfolio_stats(
        equity_curve: list[dict],
        trades: list[TradeRecord],
        initial_capital: float,
        *,
        include_monte_carlo: bool = True,
    ) -> dict:
        equity_values = [float(row["value"]) for row in equity_curve]
        exposure_values = [float(row.get("exposure", 0.0)) for row in equity_curve]
        return BacktestEngine._calc_portfolio_stats_from_values(
            equity_values,
            exposure_values,
            trades,
            initial_capital,
            include_monte_carlo=include_monte_carlo,
        )

    @staticmethod
    def _calc_portfolio_stats_from_values(
        equity_values: list[float],
        exposure_values: list[float],
        trades: list[TradeRecord],
        initial_capital: float,
        *,
        include_monte_carlo: bool = True,
    ) -> dict:
        if not equity_values:
            return {"total_return": 0, "n_trades": 0}
        final_equity = float(equity_values[-1])
        total_return = final_equity / initial_capital - 1 if initial_capital > 0 else 0.0
        values = np.array(equity_values, dtype=float)
        daily = values[1:] / values[:-1] - 1 if len(values) > 1 else np.array([])
        annual_return = (1 + total_return) ** (252 / max(len(equity_values), 1)) - 1 if total_return > -1 else total_return
        peaks = np.maximum.accumulate(values)
        drawdowns = values / peaks - 1
        max_drawdown = float(drawdowns.min()) if len(drawdowns) else 0.0
        sharpe = float(np.mean(daily) / np.std(daily) * np.sqrt(252)) if len(daily) and np.std(daily) > 0 else 0.0
        sortino = BacktestEngine._sortino_ratio(daily)
        pnls = np.array([t.pnl_pct for t in trades], dtype=float) if trades else np.array([])
        durations = np.array([t.duration for t in trades], dtype=float) if trades else np.array([])
        exposures = np.array(exposure_values, dtype=float)
        wins = pnls[pnls > 0]
        losses = pnls[pnls <= 0]
        avg_win = float(np.mean(wins)) if len(wins) else 0.0
        avg_loss = abs(float(np.mean(losses))) if len(losses) else 0.0
        stats = {
            "total_return": round(float(total_return), 4),
            "annual_return": round(float(annual_return), 4),
            "max_drawdown": round(float(max_drawdown), 4),
            "sharpe": round(float(sharpe), 2),
            "sortino": round(float(sortino), 2) if sortino is not None else None,
            "calmar": round(float(annual_return / abs(max_drawdown)), 2) if abs(max_drawdown) > 0.001 else 0.0,
            "win_rate": round(float(len(wins) / len(pnls)), 4) if len(pnls) else 0.0,
            "profit_factor": round(float(avg_win / avg_loss), 2) if avg_loss > 0 else None,
            "n_trades": len(trades),
            "avg_pnl": round(float(np.mean(pnls)), 4) if len(pnls) else 0.0,
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            **BacktestEngine._per_trade_block(pnls, durations),
            "final_equity": round(final_equity, 2),
            "initial_capital": round(float(initial_capital), 2),
            "avg_exposure": round(float(np.mean(exposures)), 4) if len(exposures) else 0.0,
            "max_exposure": round(float(np.max(exposures)), 4) if len(exposures) else 0.0,
        }
        if include_monte_carlo:
            stats.update(BacktestEngine._mc_drawdown_percentiles(pnls))
        return stats

    @staticmethod
    def _date_str(value) -> str:
        value = value.item() if hasattr(value, "item") else value
        return str(value)[:10]

    @staticmethod
    def _empty_result() -> SimResult:
        return SimResult(
            equity_curve=[], drawdown_curve=[], trades=[],
            per_symbol_stats=[], stats={"error": "no data or no signals"},
        )

    # ── 截面工具 (因子回测用) ─────────────────────────

    @staticmethod
    def cross_section_rank(panel: pl.DataFrame, col: str) -> pl.DataFrame:
        return panel.with_columns(
            pl.col(col).rank(method="random").over("date").alias(f"{col}_rank")
        )

    @staticmethod
    def cross_section_qcut(panel: pl.DataFrame, col: str, n_groups: int) -> pl.DataFrame:
        return panel.with_columns(
            pl.col(col).qcut(n_groups, labels=[f"Q{i+1}" for i in range(n_groups)])
            .over("date").alias("_group")
        )
