"""旧信号回测 API 的行情和结果适配。成交由 Tivelo 回测引擎执行。"""
from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

import numpy as np
import polars as pl
from tivelo_backtest.legacy.simulator import (
    LegacySimulator,
    MatcherConfig,
    SimulationOptions,
    TradeRecord,
)
from tivelo_backtest.research.matrix import build_market_matrix

from app.config import settings
from app.parquet import parquet_glob, scan_enriched_parquet
from app.tickflow.repository import KlineRepository

logger = logging.getLogger(__name__)

# 旧信号回测的指标 warmup 日历窗口 (#201): 与 backtest.factor.FACTOR_WARMUP_DAYS
# 同源 (120 交易日 → 保守取日历日), 覆盖 MA60/MACD/BOLL 等最长回看
_WARMUP_CALENDAR_DAYS = 120 * 1.6
_EQUITY_BASE = 100.0


@dataclass
class BacktestConfig:
    symbols: list[str]
    start: date
    end: date
    # 买入信号(任一触发即买)
    entries: list[str] = field(default_factory=list)
    # 卖出信号(任一触发即卖)
    exits: list[str] = field(default_factory=list)
    # 其他参数
    stop_loss_pct: float | None = None       # 例 -0.05 = -5%
    max_hold_days: int | None = None
    fees_pct: float = 0.0002                 # 万二佣金
    slippage_bps: float = 5                  # 5 bps
    # 撮合
    matching: Literal["close_t", "open_t+1"] = "close_t"
    rsi_oversold_threshold: float = 30
    rsi_overbought_threshold: float = 70
    asset_type: str = "stock"


@dataclass
class BacktestResult:
    run_id: str
    config: dict
    stats: dict
    equity_curve: list[dict]      # [{date, value}]
    trades: list[dict]            # [{symbol, entry_date, exit_date, pnl_pct, ...}]
    per_symbol_stats: list[dict]  # 每只股票的统计


# enriched 表里的信号列名映射
_SIGNAL_COLS = {
    "macd_golden": "signal_macd_golden",
    "macd_dead": "signal_macd_dead",
    "ma_golden_5_20": "signal_ma_golden_5_20",
    "ma_dead_5_20": "signal_ma_dead_5_20",
    "ma_golden_20_60": "signal_ma_golden_20_60",
    "ma20_breakout": "signal_ma20_breakout",
    "ma20_breakdown": "signal_ma20_breakdown",
    "n_day_high": "signal_n_day_high",
    "n_day_low": "signal_n_day_low",
    "boll_breakout_upper": "signal_boll_breakout_upper",
    "boll_breakdown_lower": "signal_boll_breakdown_lower",
    "volume_surge": "signal_volume_surge",
}


class BacktestService:
    def __init__(self, repo: KlineRepository) -> None:
        self.repo = repo

    def _load_panel(
        self,
        symbols: list[str],
        start: date,
        end: date,
        asset_type: str = "stock",
    ) -> pl.DataFrame:
        """读取指定资产的日 K，预热指标后裁回请求区间。"""
        try:
            from app.tickflow.repository import enriched_dirname
            enriched_glob = parquet_glob(self.repo.store.data_dir / enriched_dirname(asset_type))
            # 指标 warmup (#201): MA/MACD/RSI/BOLL 需要区间前的历史窗口,
            # 直接按 [start,end] 过滤后 compute_all 会让区间头部的指标失真。
            # 与挖掘侧同款公式 (mining_runtime: warmup = max(120, bars*1.6)),
            # 此处指标最长回看约 120 交易日, 取保守日历日窗口; 数据不足时
            # 自然退化 (有多少算多少)。计算完成后裁回 [start,end]。
            warmup_start = start - timedelta(days=_WARMUP_CALENDAR_DAYS)
            df = (
                scan_enriched_parquet(enriched_glob)
                .filter(
                    (pl.col("symbol").is_in(symbols))
                    & (pl.col("date") >= warmup_start)
                    & (pl.col("date") <= end)
                )
                .sort(["date", "symbol"])
                .collect()
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("backtest load failed: %s", e)
            return pl.DataFrame()

        if df.is_empty():
            return df

        # 即时计算指标 + 信号
        from app.indicators.pipeline import compute_all, compute_limit_signals

        df = compute_all(df)
        if asset_type in {"stock", "etf"}:
            if "raw_close" not in df.columns:
                df = df.with_columns(pl.col("close").alias("raw_close"))
            instruments = self.repo.get_instruments_asset(asset_type)
            if instruments.is_empty():
                instruments = df.select("symbol").unique()
            df = compute_limit_signals(
                df, instruments, needed={"limit_up_price", "limit_down_price"},
            ).with_columns(
                (pl.col("raw_close") >= pl.col("limit_up_price")).alias("signal_limit_up"),
                (pl.col("raw_close") <= pl.col("limit_down_price")).alias("signal_limit_down"),
            )
        df = df.filter(pl.col("date") >= start)

        needed_cols = (
            "date", "symbol", "open", "high", "low", "close", "volume",
            "signal_limit_up", "signal_limit_down", "rsi_14", *_SIGNAL_COLS.values(),
        )
        return df.select(c for c in needed_cols if c in df.columns).sort("symbol", "date")

    @staticmethod
    def _signal_expr(panel: pl.DataFrame, kinds: list[str], config: BacktestConfig) -> pl.Expr:
        conditions = [pl.lit(False)]
        for kind in kinds:
            column = _SIGNAL_COLS.get(kind)
            if column is not None and column in panel.columns:
                conditions.append(pl.col(column))
            elif kind == "rsi_oversold" and "rsi_14" in panel.columns:
                conditions.append(pl.col("rsi_14") < config.rsi_oversold_threshold)
            elif kind == "rsi_overbought" and "rsi_14" in panel.columns:
                conditions.append(pl.col("rsi_14") > config.rsi_overbought_threshold)
        return pl.any_horizontal(conditions).fill_null(False)

    def run(self, config: BacktestConfig) -> BacktestResult:
        from app.services.heavy_job_limiter import shared_heavy_job_limiter

        with shared_heavy_job_limiter.slot("exclusive"):
            return self._run(config)

    def _run(self, config: BacktestConfig) -> BacktestResult:
        result = BacktestResult(uuid.uuid4().hex[:10], _config_to_dict(config), {}, [], [], [])

        panel = self._load_panel(config.symbols, config.start, config.end, config.asset_type)
        if panel.is_empty():
            result.stats = {"error": "no data"}
            return result

        panel = panel.with_columns(
            self._signal_expr(panel, config.entries, config).alias("_entry"),
            self._signal_expr(panel, config.exits, config).alias("_exit"),
        )
        if not panel["_entry"].any():
            result.stats = {"error": "no buy signals"}
            return result

        # 同根 K 的两侧信号冲突时忽略，保留旧信号接口的处理方式。
        panel = panel.with_columns(
            (pl.col("_entry") & ~pl.col("_exit")).alias("_entry"),
            (pl.col("_exit") & ~pl.col("_entry")).alias("_exit"),
        )
        matcher = MatcherConfig(
            matching=config.matching,
            fees_pct=config.fees_pct,
            slippage_bps=config.slippage_bps,
            stop_loss_pct=config.stop_loss_pct,
            max_hold_days=config.max_hold_days,
            max_positions=1,
            initial_capital=1_000_000.0,
            asset_type=config.asset_type,
        )
        options = SimulationOptions(include_monte_carlo=False, include_per_symbol_stats=False)
        simulator = LegacySimulator()
        delay = int(config.matching == "open_t+1")
        days = panel["date"].unique().sort().cast(pl.String).to_list()
        day_indices = {day: index for index, day in enumerate(days)}
        equity_changes = np.zeros(len(days))
        trades: list[TradeRecord] = []
        per_symbol = []
        try:
            # 旧信号接口每个标的各自使用同额资金，汇总时取等权净值。
            for bars in panel.partition_by("symbol"):
                total_return = 0.0
                if bars["_entry"].any():
                    matrix = build_market_matrix(
                        bars, bars["_entry"], bars["_exit"],
                        entry_delay_bars=delay, exit_delay_bars=delay,
                    )
                    if matrix.entry.any():
                        sim = simulator.simulate_market_matrix(matrix, matcher, options=options)
                        trades.extend(sim.trades)
                        values = np.array([point["value"] for point in sim.equity_curve])
                        indices = [day_indices[point["date"]] for point in sim.equity_curve]
                        # 涨跌额落在真实行情日，缺失日自然沿用上一个净值。
                        equity_changes[indices] += np.diff(values, prepend=matcher.initial_capital)
                        total_return = float(values[-1] / matcher.initial_capital - 1)
                per_symbol.append({"symbol": bars["symbol"][0], "total_return": total_return})
        except Exception as exc:
            logger.exception("Tivelo signal backtest failed")
            result.stats = {"error": str(exc)}
            return result

        equity = _EQUITY_BASE * (1 + equity_changes.cumsum() / (matcher.initial_capital * len(per_symbol)))
        result.stats = _signal_stats(days, equity, trades)
        result.equity_curve = [
            {"date": day, "value": float(value)} for day, value in zip(days, equity, strict=True)
        ]
        result.trades = [
            {
                "symbol": trade.symbol,
                "entry_date": f"{trade.entry_date} 00:00:00",
                "exit_date": f"{trade.exit_date} 00:00:00",
                "entry_price": trade.entry_price,
                "exit_price": trade.exit_price,
                "pnl_pct": trade.pnl_pct,
                "duration": str(trade.duration),
            }
            for trade in trades
        ]
        result.per_symbol_stats = per_symbol
        self._persist(result)
        return result

    def _persist(self, result: BacktestResult) -> None:
        out_dir = settings.data_dir / "backtest_results"
        out_dir.mkdir(parents=True, exist_ok=True)
        # 用 polars 写一份汇总
        summary = pl.DataFrame({
            "run_id": [result.run_id],
            "stats_json": [str(result.stats)],
            "n_trades": [len(result.trades)],
        })
        summary.write_parquet(out_dir / f"run_id={result.run_id}.parquet")

    def get_result(self, run_id: str) -> BacktestResult | None:
        # Phase 1:只保留近似落盘,完整结果保存在内存的近期 cache 中
        # 简化:重新 run 比缓存复杂结果代价小,暂不实现 get_result
        return None


def _signal_stats(days: list[str], equity: np.ndarray, trades: list[TradeRecord]) -> dict:
    summary = LegacySimulator._calc_portfolio_stats_from_values(
        equity.tolist(), [], trades, _EQUITY_BASE, include_monte_carlo=False,
    )
    gross_profit = sum(trade.pnl_amount for trade in trades if trade.pnl_amount > 0)
    gross_loss = -sum(trade.pnl_amount for trade in trades if trade.pnl_amount < 0)
    stats = {
        "Start": f"{days[0]}T00:00:00",
        "End": f"{days[-1]}T00:00:00",
        "Period": f"P{len(days)}DT0H0M0S",
        "Start Value": _EQUITY_BASE,
        "End Value": equity[-1],
        "Total Return [%]": (equity[-1] / _EQUITY_BASE - 1) * 100,
        "Max Drawdown [%]": -summary["max_drawdown"] * 100,
        "Total Trades": summary["n_trades"],
        "Win Rate [%]": summary["win_rate"] * 100 if trades else None,
        "Best Trade [%]": summary["best"] * 100 if trades else None,
        "Worst Trade [%]": summary["worst"] * 100 if trades else None,
        "Profit Factor": gross_profit / gross_loss if gross_loss else None,
        "Sharpe Ratio": summary["sharpe"],
        "Calmar Ratio": summary["calmar"],
        "Sortino Ratio": summary["sortino"],
    }
    return {key: _json_safe(value) for key, value in stats.items()}


def _config_to_dict(c: BacktestConfig) -> dict:
    return {
        "symbols": c.symbols,
        "start": str(c.start),
        "end": str(c.end),
        "entries": c.entries,
        "exits": c.exits,
        "stop_loss_pct": c.stop_loss_pct,
        "max_hold_days": c.max_hold_days,
        "fees_pct": c.fees_pct,
        "slippage_bps": c.slippage_bps,
        "matching": c.matching,
    }


def _json_safe(v):
    # JSONResponse 不接受非有限浮点，原生 float 和 NumPy 标量都需要拦下。
    if isinstance(v, (float, np.floating)) and not math.isfinite(float(v)):
        return None
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    if isinstance(v, (np.floating, np.integer)):
        return float(v)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)
