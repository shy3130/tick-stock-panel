"""回测服务(§6.7)。

包 vectorbt — 全项目唯一一处出现 pandas。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

import numpy as np
import pandas as pd
import polars as pl

from app.config import settings
from app.parquet import scan_enriched_parquet
from app.tickflow.repository import KlineRepository

logger = logging.getLogger(__name__)

# vectorbt 是 optional extras(见 pyproject.toml).未装时只有 backtest 不可用,其他功能正常.
_vbt = None
_vbt_unavailable_reason: str | None = None


class VectorbtUnavailable(RuntimeError):
    """vectorbt 未安装 — 提示用户 `uv sync --extra backtest`."""


def _get_vbt():
    global _vbt, _vbt_unavailable_reason
    if _vbt is not None:
        return _vbt
    if _vbt_unavailable_reason is not None:
        raise VectorbtUnavailable(_vbt_unavailable_reason)
    try:
        import vectorbt as vbt
        _vbt = vbt
        return _vbt
    except ImportError as e:
        _vbt_unavailable_reason = (
            "vectorbt 未安装 — 它是回测的可选依赖.macOS Intel 用户先 `brew install cmake` "
            "然后 `uv sync --extra backtest`"
        )
        logger.warning("vectorbt unavailable: %s", e)
        raise VectorbtUnavailable(_vbt_unavailable_reason) from e


def is_available() -> bool:
    """供 API 层快速检测."""
    try:
        _get_vbt()
        return True
    except VectorbtUnavailable:
        return False


SignalKind = Literal[
    "macd_golden", "macd_dead",
    "ma_golden_5_20", "ma_dead_5_20",
    "ma_golden_20_60",
    "ma20_breakout", "ma20_breakdown",
    "n_day_high", "n_day_low",
    "boll_breakout_upper", "boll_breakdown_lower",
    "volume_surge",
    "rsi_oversold", "rsi_overbought",
    "stop_loss", "trailing_stop", "max_hold",
]


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
_SIGNAL_COLS: dict[SignalKind, str] = {
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
    ) -> pd.DataFrame:
        """加载 [date × symbol] 价格面板 — Polars scan_parquet + 即时计算指标。

        **全项目唯一从 Polars 转 pandas 的边界**(§7.4 / ADR-19)。
        asset_type='etf' 时读 ETF enriched。
        """
        try:
            from app.tickflow.repository import enriched_dirname
            enriched_glob = str(self.repo.store.data_dir / enriched_dirname(asset_type) / "**" / "*.parquet")
            df = (
                scan_enriched_parquet(enriched_glob)
                .filter(
                    (pl.col("symbol").is_in(symbols))
                    & (pl.col("date") >= start)
                    & (pl.col("date") <= end)
                )
                .sort(["date", "symbol"])
                .collect()
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("backtest load failed: %s", e)
            return pd.DataFrame()

        if df.is_empty():
            return pd.DataFrame()

        # 即时计算指标 + 信号
        from app.indicators.pipeline import compute_all
        df = compute_all(df)

        # 选择需要的列
        needed_cols = [
            "date", "symbol", "open", "high", "low", "close", "volume",
            "rsi_14", "signal_macd_golden", "signal_macd_dead",
            "signal_ma_golden_5_20", "signal_ma_dead_5_20",
            "signal_ma_golden_20_60",
            "signal_ma20_breakout", "signal_ma20_breakdown",
            "signal_n_day_high", "signal_n_day_low",
            "signal_boll_breakout_upper", "signal_boll_breakdown_lower",
            "signal_volume_surge",
        ]
        existing = [c for c in needed_cols if c in df.columns]
        df = df.select(existing)

        # to_pandas 边界
        return df.to_pandas(use_pyarrow_extension_array=False)

    def _build_signal_matrix(
        self,
        panel: pd.DataFrame,
        kinds: list[str],
        config: BacktestConfig,
    ) -> pd.DataFrame:
        """从面板构造 [date × symbol] 的布尔信号矩阵。"""
        if not kinds or panel.empty:
            return pd.DataFrame()

        # pivot 成 [date × symbol] 形式
        result = None
        for kind in kinds:
            mat = None
            if kind in _SIGNAL_COLS:
                col = _SIGNAL_COLS[kind]
                mat = panel.pivot(index="date", columns="symbol", values=col).fillna(False).astype(bool)
            elif kind == "rsi_oversold":
                mat = (panel.pivot(index="date", columns="symbol", values="rsi_14")
                       < config.rsi_oversold_threshold)
            elif kind == "rsi_overbought":
                mat = (panel.pivot(index="date", columns="symbol", values="rsi_14")
                       > config.rsi_overbought_threshold)
            # stop_loss / trailing / max_hold 通过 vectorbt 参数处理,不参与信号矩阵

            if mat is not None:
                result = mat if result is None else (result | mat)
        return result if result is not None else pd.DataFrame()

    def run(self, config: BacktestConfig) -> BacktestResult:
        vbt = _get_vbt()
        run_id = uuid.uuid4().hex[:10]

        panel = self._load_panel(config.symbols, config.start, config.end, config.asset_type)
        if panel.empty:
            return BacktestResult(
                run_id=run_id,
                config=_config_to_dict(config),
                stats={"error": "no data"},
                equity_curve=[],
                trades=[],
                per_symbol_stats=[],
            )

        # 价格面板
        close = panel.pivot(index="date", columns="symbol", values="close")

        # 信号矩阵
        entries = self._build_signal_matrix(panel, config.entries, config)
        exits = self._build_signal_matrix(panel, config.exits, config)

        # 对齐 index/columns
        if not entries.empty:
            entries = entries.reindex_like(close).fillna(False).astype(bool)
        else:
            entries = pd.DataFrame(False, index=close.index, columns=close.columns)
        if not exits.empty:
            exits = exits.reindex_like(close).fillna(False).astype(bool)
        else:
            exits = pd.DataFrame(False, index=close.index, columns=close.columns)

        if not entries.any().any():
            return BacktestResult(
                run_id=run_id,
                config=_config_to_dict(config),
                stats={"error": "no buy signals"},
                equity_curve=[],
                trades=[],
                per_symbol_stats=[],
            )

        # T+1 适配:vectorbt 默认信号当根 K 撮合
        # close_t 撮合:维持默认
        # open_t+1 撮合:shift 信号 1 根 + 用 open 作为价
        if config.matching == "open_t+1":
            entries = entries.shift(1).fillna(False).astype(bool)
            exits = exits.shift(1).fillna(False).astype(bool)
            price = panel.pivot(index="date", columns="symbol", values="open")
        else:
            price = close

        # 跑回测
        try:
            pf_kwargs = dict(
                close=close,
                entries=entries,
                exits=exits,
                price=price,
                fees=config.fees_pct,
                slippage=config.slippage_bps / 10000.0,
                freq="1D",
            )
            if config.stop_loss_pct is not None:
                pf_kwargs["sl_stop"] = abs(config.stop_loss_pct)
            if config.max_hold_days is not None:
                # vectorbt 没有内置 max-hold;用时间退出近似:
                # 在 max_hold_days 后强制 exit
                exits_idx = entries.copy()
                for col in entries.columns:
                    entry_rows = np.where(entries[col].values)[0]
                    for i in entry_rows:
                        end_i = min(i + config.max_hold_days, len(entries) - 1)
                        if end_i > i:
                            exits_idx.iloc[end_i][col] = True
                pf_kwargs["exits"] = (exits | exits_idx).astype(bool)

            pf = vbt.Portfolio.from_signals(**pf_kwargs)
        except Exception as e:  # noqa: BLE001
            logger.exception("vectorbt backtest failed")
            return BacktestResult(
                run_id=run_id,
                config=_config_to_dict(config),
                stats={"error": str(e)},
                equity_curve=[],
                trades=[],
                per_symbol_stats=[],
            )

        # 提取结果
        try:
            stats_series = pf.stats(silence_warnings=True)
            if isinstance(stats_series, pd.DataFrame):
                # 多列时取 agg
                stats_dict = stats_series.mean(numeric_only=True).to_dict()
            else:
                stats_dict = stats_series.to_dict()
        except Exception:  # noqa: BLE001
            stats_dict = {}

        # 净值曲线(组合平均)
        equity = pf.value().mean(axis=1) if isinstance(pf.value(), pd.DataFrame) else pf.value()
        equity_curve = [
            {"date": str(idx.date() if hasattr(idx, "date") else idx), "value": float(v)}
            for idx, v in equity.items() if pd.notna(v)
        ]

        # 交易记录
        try:
            trades_df = pf.trades.records_readable
            trades = trades_df.to_dict(orient="records") if not trades_df.empty else []
            # 字段名美化
            trades = [
                {
                    "symbol": t.get("Column", t.get("Symbol", "")),
                    "entry_date": str(t.get("Entry Timestamp", t.get("Entry Date", ""))),
                    "exit_date": str(t.get("Exit Timestamp", t.get("Exit Date", ""))),
                    "entry_price": float(t.get("Avg Entry Price", t.get("Avg. Entry Price", 0))),
                    "exit_price": float(t.get("Avg Exit Price", t.get("Avg. Exit Price", 0))),
                    "pnl_pct": float(t.get("Return", t.get("PnL %", 0))),
                    "duration": str(t.get("Duration", "")),
                }
                for t in trades
            ]
        except Exception:  # noqa: BLE001
            trades = []

        # 每标的统计
        per_symbol = []
        try:
            total_ret = pf.total_return()
            if isinstance(total_ret, pd.Series):
                for sym, ret in total_ret.items():
                    if pd.notna(ret):
                        per_symbol.append({"symbol": sym, "total_return": float(ret)})
        except Exception:  # noqa: BLE001
            pass

        result = BacktestResult(
            run_id=run_id,
            config=_config_to_dict(config),
            stats={k: _json_safe(v) for k, v in stats_dict.items()},
            equity_curve=equity_curve,
            trades=trades,
            per_symbol_stats=per_symbol,
        )

        # 落盘
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
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    if isinstance(v, (np.floating, np.integer)):
        return float(v) if not np.isnan(float(v)) else None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)
