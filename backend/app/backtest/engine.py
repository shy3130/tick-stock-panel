"""回测引擎 — 共享数据加载 + 撮合 + 统计计算。

纯 Polars/NumPy 实现，不依赖 pandas/vectorbt。
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
from typing import Callable

logger = logging.getLogger(__name__)
from typing import Literal

import numpy as np
import polars as pl

from app.parquet import scan_enriched_parquet
from app.tickflow.repository import KlineRepository

logger = logging.getLogger(__name__)


# ================================================================
# 数据结构
# ================================================================

@dataclass
class MatcherConfig:
    # matching 为向后兼容入口: 仅传 matching 时, entry_fill/exit_fill 都取 matching 的值。
    # 显式传入 entry_fill/exit_fill 时以二者为准 (允许建仓/清仓口径不同)。
    matching: Literal["close_t", "open_t+1"] = "close_t"
    entry_fill: Literal["close_t", "open_t+1"] | None = None
    exit_fill: Literal["close_t", "open_t+1"] | None = None
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


@dataclass
class SimResult:
    equity_curve: list[dict]       # [{date, value}]
    drawdown_curve: list[dict]     # [{date, value}]
    trades: list[TradeRecord]
    per_symbol_stats: list[dict]
    stats: dict


# ================================================================
# PanelCache — 避免重复 scan_parquet + compute_all
# ================================================================

class _CacheEntry:
    __slots__ = ("df", "ts")

    def __init__(self, df: pl.DataFrame, ts: float):
        self.df = df
        self.ts = ts


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
            if key in self._cache:
                entry = self._cache[key]
                if now - entry.ts < self._ttl:
                    self._cache.move_to_end(key)
                    return entry.df
                del self._cache[key]

        # 计算在锁外 (可能重扫 parquet, 耗时); 并发相同 key 至多重复算一次, 不会崩
        df = compute_fn(symbols, start, end, columns, asset_type)
        with self._lock:
            self._cache[key] = _CacheEntry(df=df, ts=now)
            if len(self._cache) > self._max_size:
                self._cache.popitem(last=False)
        return df

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
            if asset_type == "stock" and self.repo is not None and hasattr(self.repo, "get_enriched_range"):
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
                    # 回撤止盈触发线: 相对峰值价回撤 drawdown 个点。
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

            exit_price = float(exit_price_override) if exit_price_override is not None else float(exit_prices[idx])
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

            entry_price = float(entry_prices[entry_idx])
            pos = {
                "symbol": sym,
                "name": str(names[entry_idx] or ""),
                "entry_idx": entry_idx,
                "entry_date": self._date_str(panel_dates[entry_idx]),
                "entry_signal_date": entry_signal_dates[entry_idx] or self._date_str(panel_dates[entry_idx]),
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

    def simulate_portfolio(
        self,
        panel: pl.DataFrame,
        entries: pl.Series | None,
        exits: pl.Series | None,
        config: MatcherConfig,
        progress_cb: "Callable[[dict], None] | None" = None,
        cancel_event: "threading.Event | None" = None,
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
            exit_price = float(exit_price_override) if exit_price_override is not None else float(exit_prices[idx])
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
                        # 回撤止盈触发线: 相对峰值价回撤 drawdown 个点。
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
                entry_price = float(entry_prices[idx])
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
        return {
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
            **BacktestEngine._mc_drawdown_percentiles(pnls),
        }

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
    ) -> SimResult:
        """全量独立候选统计：按每个候选样本的实际执行收益聚合。"""
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
            equity_curve.append({
                "date": d_str,
                "value": round(float(equity), 4),
                "positions": len(values),
            })
            drawdown_curve.append({"date": d_str, "value": round(float(dd), 4)})

        values = np.array([r["value"] for r in equity_curve], dtype=float)
        total_return = float(values[-1] - 1.0) if len(values) else 0.0
        peaks = np.maximum.accumulate(values) if len(values) else np.array([])
        drawdowns = values / peaks - 1 if len(values) else np.array([])
        max_drawdown = float(drawdowns.min()) if len(drawdowns) else 0.0
        daily = np.array(daily_avg, dtype=float)
        sharpe = float(np.mean(daily) / np.std(daily) * np.sqrt(252)) if len(daily) > 1 and np.std(daily) > 0 else 0.0
        sortino = BacktestEngine._sortino_ratio(daily)

        lo, hi, nbins = -0.20, 0.20, 20
        clipped = np.clip(pnls, lo, hi)
        counts, edges = np.histogram(clipped, bins=nbins, range=(lo, hi))
        dist = [
            {
                "range": f"{(edges[i]*100):+.0f}~{(edges[i+1]*100):+.0f}%",
                "count": int(counts[i]),
                "ratio": round(float(counts[i] / pnls.size), 4) if pnls.size else 0.0,
            }
            for i in range(nbins)
        ]

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
            "return_distribution": dist,
            "execution": execution_stats,
            **BacktestEngine._mc_drawdown_percentiles(pnls),
        }

        return SimResult(
            equity_curve=equity_curve,
            drawdown_curve=drawdown_curve,
            trades=trades,
            per_symbol_stats=BacktestEngine._calc_per_symbol(trades),
            stats=stats,
        )

    @staticmethod
    def _calc_portfolio_stats(
        equity_curve: list[dict],
        trades: list[TradeRecord],
        initial_capital: float,
    ) -> dict:
        if not equity_curve:
            return {"total_return": 0, "n_trades": 0}
        final_equity = float(equity_curve[-1]["value"])
        total_return = final_equity / initial_capital - 1 if initial_capital > 0 else 0.0
        values = np.array([float(r["value"]) for r in equity_curve], dtype=float)
        daily = values[1:] / values[:-1] - 1 if len(values) > 1 else np.array([])
        annual_return = (1 + total_return) ** (252 / max(len(equity_curve), 1)) - 1 if total_return > -1 else total_return
        peaks = np.maximum.accumulate(values)
        drawdowns = values / peaks - 1
        max_drawdown = float(drawdowns.min()) if len(drawdowns) else 0.0
        sharpe = float(np.mean(daily) / np.std(daily) * np.sqrt(252)) if len(daily) and np.std(daily) > 0 else 0.0
        sortino = BacktestEngine._sortino_ratio(daily)
        pnls = np.array([t.pnl_pct for t in trades], dtype=float) if trades else np.array([])
        durations = np.array([t.duration for t in trades], dtype=float) if trades else np.array([])
        exposures = np.array([float(r.get("exposure", 0.0)) for r in equity_curve], dtype=float)
        wins = pnls[pnls > 0]
        losses = pnls[pnls <= 0]
        avg_win = float(np.mean(wins)) if len(wins) else 0.0
        avg_loss = abs(float(np.mean(losses))) if len(losses) else 0.0
        return {
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
            **BacktestEngine._mc_drawdown_percentiles(pnls),
            "final_equity": round(final_equity, 2),
            "initial_capital": round(float(initial_capital), 2),
            "avg_exposure": round(float(np.mean(exposures)), 4) if len(exposures) else 0.0,
            "max_exposure": round(float(np.max(exposures)), 4) if len(exposures) else 0.0,
        }

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
