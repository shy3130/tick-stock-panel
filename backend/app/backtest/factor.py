"""因子回测服务 — IC/IR 分析 + 分层回测 + 多空组合。

纯 Polars 向量化实现，无 pandas 依赖。
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

import numpy as np
import polars as pl

from app.backtest.engine import BacktestEngine

logger = logging.getLogger(__name__)

# 可用因子列 (从 ENRICHED_COLUMNS 过滤出数值型指标)
FACTOR_COLUMNS: list[dict] = [
    {"id": "momentum_5d",  "label": "5日动量",     "group": "动量", "desc": "5日涨跌幅，正值表示上涨趋势"},
    {"id": "momentum_10d", "label": "10日动量",    "group": "动量", "desc": "10日涨跌幅，中短期趋势指标"},
    {"id": "momentum_20d", "label": "20日动量",    "group": "动量", "desc": "月度涨跌幅，常用因子"},
    {"id": "momentum_30d", "label": "30日动量",    "group": "动量", "desc": "30日涨跌幅"},
    {"id": "momentum_60d", "label": "60日动量",    "group": "动量", "desc": "季度涨跌幅，中期动量"},
    {"id": "rsi_6",        "label": "RSI(6)",      "group": "超买超卖", "desc": "6日相对强弱指标，敏感度高"},
    {"id": "rsi_14",       "label": "RSI(14)",     "group": "超买超卖", "desc": "14日相对强弱指标，经典周期"},
    {"id": "rsi_24",       "label": "RSI(24)",     "group": "超买超卖", "desc": "24日相对强弱指标"},
    {"id": "annual_vol_20d","label": "20日波动率", "group": "波动率",   "desc": "20日年化波动率"},
    {"id": "atr_14",       "label": "ATR(14)",     "group": "波动率",   "desc": "14日平均真实波幅"},
    {"id": "vol_ratio_5d", "label": "量比(5日)",   "group": "量价",     "desc": "当日成交量 / 5日均量"},
    {"id": "turnover_rate", "label": "换手率",     "group": "量价",     "desc": "当日换手率"},
    {"id": "macd_hist",    "label": "MACD柱",      "group": "趋势",     "desc": "MACD柱状图值"},
    {"id": "kdj_k",        "label": "KDJ-K",       "group": "趋势",     "desc": "KDJ指标K值"},
    {"id": "change_pct",   "label": "日涨跌幅",    "group": "基础",     "desc": "当日涨跌幅"},
    {"id": "amplitude",    "label": "日振幅",      "group": "基础",     "desc": "当日振幅 (最高-最低)/昨收"},
]

FACTOR_WARMUP_DAYS = 120


@dataclass
class FactorConfig:
    factor_name: str
    symbols: list[str] | None
    start: date
    end: date
    n_groups: int = 5
    rebalance: Literal["daily", "weekly", "monthly"] = "monthly"
    weight: Literal["equal", "factor_weight"] = "equal"
    fees_pct: float = 0.0002
    slippage_bps: float = 5.0
    asset_type: str = "stock"


@dataclass
class GroupStats:
    group: int
    label: str
    total_return: float
    annual_return: float
    max_drawdown: float
    sharpe: float
    win_rate: float


@dataclass
class FactorResult:
    run_id: str
    config: dict
    # IC 分析
    ic_mean: float | None = None
    ic_std: float | None = None
    ir: float | None = None
    ic_win_rate: float | None = None
    ic_series: list[dict] = field(default_factory=list)
    # 分层
    group_stats: list[dict] = field(default_factory=list)
    group_nav: list[dict] = field(default_factory=list)
    # 多空
    long_short_stats: dict = field(default_factory=dict)
    long_short_nav: list[dict] = field(default_factory=list)
    # 元信息
    elapsed_ms: float = 0.0
    n_symbols: int = 0
    n_dates: int = 0
    error: str | None = None


class FactorBacktestService:
    def __init__(self, engine: BacktestEngine) -> None:
        self.engine = engine

    def run(self, config: FactorConfig) -> FactorResult:
        t0 = time.perf_counter()
        run_id = uuid.uuid4().hex[:10]

        def _err(msg: str) -> FactorResult:
            return FactorResult(
                run_id=run_id,
                config=self._config_to_dict(config),
                error=msg,
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )

        # 加载基础面板: 当前 enriched parquet 只持久化基础列, 指标因子可能需要运行时计算。
        panel_columns = ["symbol", "date", "open", "high", "low", "close", "volume", "turnover_rate"]
        if config.factor_name not in panel_columns:
            panel_columns.append(config.factor_name)
        load_start = config.start
        if config.factor_name not in {"turnover_rate"}:
            load_start = config.start - timedelta(days=FACTOR_WARMUP_DAYS)

        panel = self.engine.load_panel(
            config.symbols,
            load_start,
            config.end,
            columns=panel_columns,
            asset_type=config.asset_type,
        )
        if panel.is_empty():
            return _err("无数据，请检查日期范围或先运行盘后管道")

        factor_col = config.factor_name
        if factor_col not in panel.columns:
            panel = self._compute_missing_factor(panel, factor_col)
        if factor_col not in panel.columns:
            return _err(f"因子列 '{factor_col}' 不存在于 enriched 数据中, 且无法从基础行情计算")
        if "close" not in panel.columns:
            return _err("enriched 数据缺少收盘价 close")
        panel = panel.select(["symbol", "date", "close", factor_col])
        panel = panel.filter((pl.col("date") >= config.start) & (pl.col("date") <= config.end))

        # 过滤有效行
        panel = panel.filter(
            pl.col(factor_col).is_not_null()
            & pl.col("close").is_not_null()
            & (pl.col("close") > 0)
        )
        if panel.is_empty():
            return _err("过滤后无有效数据")

        panel = panel.sort(["symbol", "date"])

        n_symbols = panel["symbol"].n_unique()
        n_dates = panel["date"].n_unique()

        # 计算下期收益
        # 根据调仓频率计算不同周期的 forward return
        if config.rebalance == "daily":
            panel = panel.with_columns(
                (pl.col("close").shift(-1).over("symbol") / pl.col("close") - 1)
                .alias("_next_return")
            )
        else:
            # weekly/monthly: 计算到下个调仓日的收益
            panel = self._calc_period_return(panel, config.rebalance)

        # ── 1. IC 分析 ──
        ic_df = self._calc_ic(panel, factor_col)
        ic_series = [
            {"date": str(row["date"]), "ic": round(float(row["ic"]), 4)}
            for row in ic_df.iter_rows(named=True)
            if row["ic"] is not None and not np.isnan(float(row["ic"]))
        ]
        ic_values = [r["ic"] for r in ic_series]
        ic_mean = float(np.mean(ic_values)) if ic_values else None
        ic_std = float(np.std(ic_values)) if ic_values else None
        ir = (ic_mean / ic_std) if (ic_mean is not None and ic_std and ic_std > 1e-8) else None
        ic_win_rate = (sum(1 for v in ic_values if v > 0) / len(ic_values)) if ic_values else None

        # ── 2. 分层回测 ──
        panel = self._add_groups(panel, factor_col, config.n_groups)
        group_nav = self._calc_group_nav(panel, config)
        group_stats = self._calc_group_stats(group_nav, config.start, config.end, config.rebalance)

        # ── 3. 多空组合 ──
        long_short_nav, long_short_stats = self._calc_long_short(group_nav, config)

        elapsed = (time.perf_counter() - t0) * 1000
        return FactorResult(
            run_id=run_id,
            config=self._config_to_dict(config),
            ic_mean=round(ic_mean, 4) if ic_mean is not None else None,
            ic_std=round(ic_std, 4) if ic_std is not None else None,
            ir=round(ir, 4) if ir is not None else None,
            ic_win_rate=round(ic_win_rate, 4) if ic_win_rate is not None else None,
            ic_series=ic_series,
            group_stats=group_stats,
            group_nav=group_nav,
            long_short_stats=long_short_stats,
            long_short_nav=long_short_nav,
            elapsed_ms=round(elapsed, 1),
            n_symbols=n_symbols,
            n_dates=n_dates,
        )

    @staticmethod
    def _compute_missing_factor(panel: pl.DataFrame, factor_col: str) -> pl.DataFrame:
        required = {"symbol", "date", "open", "high", "low", "close", "volume"}
        if not required.issubset(panel.columns):
            missing = sorted(required - set(panel.columns))
            logger.warning("factor %s cannot be computed, missing columns: %s", factor_col, missing)
            return panel

        from app.indicators.pipeline import compute_indicators

        # 只需要单个因子列 → 用 needed 裁剪, 跳过无关的 EMA/KDJ/RSI 等计算 pass
        computed = compute_indicators(panel, needed={factor_col})
        if factor_col not in computed.columns:
            return panel
        return computed.select(["symbol", "date", "close", factor_col])

    # ── IC 计算 ──

    @staticmethod
    def _calc_ic(panel: pl.DataFrame, factor_col: str) -> pl.DataFrame:
        """计算截面 Rank IC (因子值 rank vs 下期收益 rank 的相关系数)。"""
        return (
            panel.filter(pl.col("_next_return").is_not_null())
            .group_by("date")
            .agg(
                pl.corr(
                    pl.col(factor_col).rank(method="average"),
                    pl.col("_next_return").rank(method="average"),
                ).alias("ic")
            )
            .sort("date")
        )

    # ── 调仓期收益 ──

    @staticmethod
    def _calc_period_return(panel: pl.DataFrame, rebalance: str) -> pl.DataFrame:
        """计算到下个调仓日的收益。

        weekly: 下个周调仓日 close / 今日 close - 1
        monthly: 下个月调仓日 close / 今日 close - 1
        只在调仓日标记行有效，其他行为 null。
        """
        import datetime as _dt

        all_dates = sorted(panel["date"].unique().to_list())

        if rebalance == "weekly":
            # 调仓日 = 每周一
            rebalance_dates = set()
            for d in all_dates:
                if hasattr(d, "weekday"):
                    wd = d.weekday()
                else:
                    wd = _dt.date.fromisoformat(str(d)).weekday()
                if wd == 0:  # Monday
                    rebalance_dates.add(d)
        else:  # monthly
            # 调仓日 = 每月首个交易日
            seen_months: set[str] = set()
            rebalance_dates = set()
            for d in sorted(all_dates):
                m = str(d)[:7]  # "YYYY-MM"
                if m not in seen_months:
                    seen_months.add(m)
                    rebalance_dates.add(d)

        if not rebalance_dates:
            panel = panel.with_columns(pl.lit(None).cast(pl.Float64).alias("_next_return"))
            return panel

        # 对每个调仓日，找到下一个调仓日 (仅在 unique 日期上做, 成本极低)
        sorted_rebalance = sorted(rebalance_dates)
        reb_dates: list = []
        next_dates: list = []
        for i, d in enumerate(sorted_rebalance):
            if i + 1 < len(sorted_rebalance):
                reb_dates.append(d)
                next_dates.append(sorted_rebalance[i + 1])
            # 最后一个调仓日没有下一个，不计算收益

        if not reb_dates:
            panel = panel.with_columns(pl.lit(None).cast(pl.Float64).alias("_next_return"))
            return panel

        panel = panel.sort(["symbol", "date"])
        date_dtype = panel.schema["date"]

        # 调仓日 → 下一调仓日 的映射表 (向量化 JOIN, 替代 Python 逐行 price_map 循环)
        rebal_df = pl.DataFrame(
            {"date": reb_dates, "_next_reb_date": next_dates}
        ).with_columns(
            pl.col("date").cast(date_dtype),
            pl.col("_next_reb_date").cast(date_dtype),
        )

        # (symbol, 下一调仓日) → 该日 close 的查找表 (等价于原 price_map, 重复取 last)
        price_lookup = (
            panel.select(
                pl.col("symbol"),
                pl.col("date").alias("_next_reb_date"),
                pl.col("close").alias("_next_close"),
            )
            .unique(subset=["symbol", "_next_reb_date"], keep="last")
        )

        # 只在调仓日标记行有效: 下一调仓日该股 close / 当日 close - 1; 缺价或非调仓日为 null
        panel = (
            panel.join(rebal_df, on="date", how="left")
            .join(price_lookup, on=["symbol", "_next_reb_date"], how="left")
            .with_columns(
                pl.when(
                    pl.col("_next_reb_date").is_not_null()
                    & pl.col("_next_close").is_not_null()
                    & (pl.col("close") > 0)
                )
                .then(pl.col("_next_close") / pl.col("close") - 1.0)
                .otherwise(None)
                .cast(pl.Float64)
                .alias("_next_return")
            )
            .drop(["_next_reb_date", "_next_close"])
            .sort(["symbol", "date"])
        )
        return panel

    # ── 分组 ──

    @staticmethod
    def _add_groups(panel: pl.DataFrame, factor_col: str, n_groups: int) -> pl.DataFrame:
        """截面序号分桶，避免 qcut 在重复因子值截面上抛错。"""
        return (
            panel.sort(["date", factor_col, "symbol"])
            .with_columns(
                (pl.cum_count("symbol").over("date") - 1).alias("_factor_ord"),
                pl.len().over("date").alias("_factor_count"),
            )
            .with_columns(
                (
                    pl.lit("Q")
                    + (
                        ((pl.col("_factor_ord") * n_groups) / pl.col("_factor_count"))
                        .floor()
                        .cast(pl.Int64)
                        + 1
                    )
                    .clip(1, n_groups)
                    .cast(pl.Utf8)
                )
                .alias("_group")
            )
            .drop(["_factor_ord", "_factor_count"])
        )

    @staticmethod
    def _group_sort_key(group: str) -> int:
        if group.startswith("Q"):
            try:
                return int(group[1:])
            except ValueError:
                pass
        return 0

    # ── 分组净值 ──

    @staticmethod
    def _calc_group_nav(panel: pl.DataFrame, config: FactorConfig) -> list[dict]:
        """计算分组净值曲线 — 只在调仓日更新净值。"""
        # 只保留有下期收益的行 (= 调仓日)
        group_ret = (
            panel.filter(pl.col("_next_return").is_not_null() & pl.col("_group").is_not_null())
            .group_by(["date", "_group"])
            .agg(pl.col("_next_return").mean().alias("group_return"))
        )

        # pivot: date × group
        pivot = group_ret.pivot(index="date", columns="_group", values="group_return").sort("date")

        if pivot.is_empty():
            return []

        group_cols = sorted([c for c in pivot.columns if c != "date"], key=FactorBacktestService._group_sort_key)

        # 累乘净值曲线
        result: list[dict] = []
        nav_values: dict[str, float] = {c: 1.0 for c in group_cols}

        for row in pivot.iter_rows(named=True):
            entry: dict = {"date": str(row["date"])[:10]}
            for c in group_cols:
                ret = float(row[c]) if row[c] is not None else 0.0
                nav_values[c] *= (1 + ret)
                entry[c] = round(nav_values[c], 4)
            result.append(entry)

        return result

    # ── 分组统计 ──

    @staticmethod
    def _calc_group_stats(
        group_nav: list[dict], start: date, end: date,
        rebalance: str = "monthly",
    ) -> list[dict]:
        if not group_nav:
            return []

        group_cols = sorted(
            [k for k in group_nav[0] if k != "date"],
            key=FactorBacktestService._group_sort_key,
        )
        n_days = max((end - start).days, 1)
        years = n_days / 365.25

        stats = []
        for i, c in enumerate(group_cols):
            values = [r[c] for r in group_nav if r.get(c) is not None]
            if not values:
                continue
            total_return = values[-1] - 1.0
            annual_return = (values[-1]) ** (1 / max(years, 0.01)) - 1 if values[-1] > 0 else 0.0

            # 最大回撤
            peak = 1.0
            max_dd = 0.0
            for v in values:
                peak = max(peak, v)
                dd = (v - peak) / peak
                max_dd = min(max_dd, dd)

            # 日收益序列
            daily_rets = []
            for j in range(1, len(values)):
                if values[j - 1] > 0:
                    daily_rets.append(values[j] / values[j - 1] - 1)

            # 夏普 — 年化系数必须匹配 group_nav 的调仓频率 (每个净值点 = 一个调仓周期收益);
            # 周/月频收益若乘 √252 会把 Sharpe 高估 √(252/期数) 倍 (月频 ≈4.6x, 周频 ≈2.2x)。
            if daily_rets:
                arr = np.array(daily_rets)
                _ann = {"daily": 252, "weekly": 52, "monthly": 12}.get(rebalance, 252)
                sharpe = float(np.mean(arr) / np.std(arr)) * np.sqrt(_ann) if np.std(arr) > 0 else 0.0
                win_rate = float(np.mean(arr > 0))
            else:
                sharpe = 0.0
                win_rate = 0.0

            stats.append({
                "group": i + 1,
                "label": c,
                "total_return": round(total_return, 4),
                "annual_return": round(annual_return, 4),
                "max_drawdown": round(max_dd, 4),
                "sharpe": round(sharpe, 2),
                "win_rate": round(win_rate, 4),
            })

        return stats

    # ── 多空组合 ──

    @staticmethod
    def _calc_long_short(
        group_nav: list[dict], config: FactorConfig,
    ) -> tuple[list[dict], dict]:
        """多空组合: 做多最高组 + 做空最低组。"""
        if not group_nav:
            return [], {}

        group_cols = sorted(
            [k for k in group_nav[0] if k != "date"],
            key=FactorBacktestService._group_sort_key,
        )
        if len(group_cols) < 2:
            return [], {}

        top_col = group_cols[-1]  # Q5 (最高)
        bottom_col = group_cols[0]  # Q1 (最低)

        # 独立计算 top 和 bottom 的日收益，然后合成
        ls_value = 1.0
        prev_top = 1.0
        prev_bot = 1.0
        peak = 1.0
        max_dd = 0.0
        ls_nav: list[dict] = []

        for row in group_nav:
            top_nav = float(row.get(top_col, 1.0)) if row.get(top_col) is not None else 1.0
            bot_nav = float(row.get(bottom_col, 1.0)) if row.get(bottom_col) is not None else 1.0

            # top 组收益 (做多)
            top_ret = (top_nav / prev_top - 1) if prev_top > 0 else 0.0
            # bottom 组收益 (做空 = 取反)
            bot_ret = -(bot_nav / prev_bot - 1) if prev_bot > 0 else 0.0
            # 多空组合收益
            ls_ret = (top_ret + bot_ret) / 2  # 各分配 50% 资金
            ls_value *= (1 + ls_ret)

            prev_top = top_nav
            prev_bot = bot_nav

            peak = max(peak, ls_value)
            dd = (ls_value - peak) / peak if peak > 0 else 0.0
            max_dd = min(max_dd, dd)

            ls_nav.append({"date": row["date"], "value": round(ls_value, 4)})

        total_ret = ls_value - 1.0
        ls_stats = {
            "total_return": round(total_ret, 4),
            "max_drawdown": round(max_dd, 4),
            "top_group": top_col,
            "bottom_group": bottom_col,
        }

        return ls_nav, ls_stats

    @staticmethod
    def _config_to_dict(c: FactorConfig) -> dict:
        return {
            "factor_name": c.factor_name,
            "symbols": c.symbols,
            "start": str(c.start),
            "end": str(c.end),
            "n_groups": c.n_groups,
            "rebalance": c.rebalance,
            "weight": c.weight,
            "fees_pct": c.fees_pct,
            "slippage_bps": c.slippage_bps,
        }
