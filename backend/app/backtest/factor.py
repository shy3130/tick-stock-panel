"""因子回测服务 — IC/IR 分析 + 分层回测 + 多空组合。

纯 Polars 向量化实现，无 pandas 依赖。
"""
from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal

import numpy as np
import polars as pl

from app.backtest import stats_v2
from app.backtest.engine import BacktestEngine
from app.backtest.fundamentals import (
    FUNDAMENTAL_FACTOR_NAMES,
    attach_fundamental_factors,
    load_fundamental_snapshot,
)
from app.factors.registry import factor_columns_view as _factor_columns_view
from app.strategy.scoring import (
    VIRTUAL_SCORING_DEPENDENCIES as DERIVED_FACTOR_DEPENDENCIES,
)
from app.strategy.scoring import (
    materialize_scoring_columns,
)

logger = logging.getLogger(__name__)

# 可研究因子目录。保留历史 ID 兼容已有候选方案; 价格尺度相关指标优先提供归一化版本。
# P1 起目录元数据单一权威来源为 app/factors/registry.py, 本常量为兼容别名 (顺序与键不变)。
FACTOR_COLUMNS: list[dict] = _factor_columns_view()

FACTOR_WARMUP_DAYS = 120
FACTOR_METHODOLOGY_VERSION = "factor_v2"
_DAILY_FORWARD_HORIZONS = (1, 3, 5)


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
    commission_pct: float | None = None
    stamp_tax_pct: float | None = None


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
    # factor_v2 兼容扩展字段必须追加在旧字段之后, 保留位置参数语义。
    methodology_version: str = FACTOR_METHODOLOGY_VERSION
    coverage: float | None = None
    turnover: float | None = None
    long_short_sharpe: float | None = None
    yearly_ic: list[dict] = field(default_factory=list)
    ic_decay: list[dict] = field(default_factory=list)
    regime_stats: list[dict] = field(default_factory=list)


@dataclass
class FactorBatchConfig:
    factor_names: list[str]
    symbols: list[str] | None
    start: date
    end: date
    n_groups: int = 5
    rebalance: Literal["daily", "weekly", "monthly"] = "monthly"
    weight: Literal["equal", "factor_weight"] = "equal"
    fees_pct: float = 0.0002
    slippage_bps: float = 5.0
    asset_type: str = "stock"
    commission_pct: float | None = None
    stamp_tax_pct: float | None = None


@dataclass
class FactorBatchItem:
    factor_name: str
    label: str
    group: str
    ic_mean: float | None = None
    ir: float | None = None
    ic_win_rate: float | None = None
    long_short_return: float | None = None
    long_short_max_drawdown: float | None = None
    n_symbols: int = 0
    n_dates: int = 0
    elapsed_ms: float = 0.0
    error: str | None = None
    methodology_version: str = FACTOR_METHODOLOGY_VERSION
    coverage: float | None = None
    turnover: float | None = None
    long_short_sharpe: float | None = None
    yearly_ic: list[dict] = field(default_factory=list)
    ic_decay: list[dict] = field(default_factory=list)
    regime_stats: list[dict] = field(default_factory=list)
    # metrics_v2 (P3): NW HAC t 值 (滞后=1, 日频 1 日前瞻) 与 BH-FDR q 值; 样本不足为 None
    t_naive: float | None = None
    t_newey_west: float | None = None
    nw_lag: int | None = None
    p_value: float | None = None
    q_value: float | None = None


@dataclass
class FactorBatchResult:
    run_id: str
    config: dict
    results: list[FactorBatchItem] = field(default_factory=list)
    elapsed_ms: float = 0.0
    n_symbols: int = 0
    n_dates: int = 0
    error: str | None = None


class FactorBacktestService:
    def __init__(self, engine: BacktestEngine) -> None:
        self.engine = engine

    # 盘中实时刷新 (默认 ~6s 一轮) 每轮换新 generation, 快照式读取撞上发布属常态
    # (issue #517): 有界重读新世代整体重跑, 而不是 attempts=1 直接失败。
    _SNAPSHOT_MAX_ATTEMPTS = 3
    _SNAPSHOT_RETRY_SLEEP_S = 1.0

    def _with_snapshot_retry(self, body):
        from app.enriched_generation import EnrichedGenerationUnavailableError

        for attempt in range(self._SNAPSHOT_MAX_ATTEMPTS):
            if attempt:
                time.sleep(self._SNAPSHOT_RETRY_SLEEP_S)
            try:
                return body()
            except EnrichedGenerationUnavailableError:
                if attempt + 1 >= self._SNAPSHOT_MAX_ATTEMPTS:
                    raise

    def run(
        self,
        config: FactorConfig,
        *,
        regime_by_date: Mapping[object, Any] | None = None,
    ) -> FactorResult:
        from app.services.heavy_job_limiter import shared_heavy_job_limiter

        with shared_heavy_job_limiter.slot("exclusive"):
            return self._with_snapshot_retry(
                lambda: self._run(config, regime_by_date=regime_by_date)
            )

    def _run(
        self,
        config: FactorConfig,
        *,
        regime_by_date: Mapping[object, Any] | None = None,
    ) -> FactorResult:
        t0 = time.perf_counter()
        run_id = uuid.uuid4().hex[:10]
        generation = self._data_generation(config.asset_type)
        panel = self._load_factor_panel(
            config,
            [config.factor_name],
            expected_generation=generation,
        )
        if panel.is_empty():
            return self._error_result(config, run_id, t0, "无数据, 请检查日期范围或先运行盘后管道")

        if config.factor_name in FUNDAMENTAL_FACTOR_NAMES and self._fundamentals_missing():
            return self._error_result(
                config, run_id, t0,
                "本地没有财务数据: 请先在数据页同步财务数据后再使用财务因子",
            )

        trading_dates = self._global_trading_dates(config)
        self._assert_data_generation(config.asset_type, generation)
        panel = self._attach_shared_next_return(
            panel,
            config,
            trading_dates=trading_dates,
        )
        evaluate_kwargs = (
            {"regime_by_date": regime_by_date}
            if regime_by_date is not None
            else {}
        )
        return self._evaluate_panel(
            panel,
            config,
            run_id,
            t0,
            market_trading_dates=trading_dates,
            **evaluate_kwargs,
        )

    def run_batch(
        self,
        config: FactorBatchConfig,
        *,
        regime_by_date: Mapping[object, Any] | None = None,
    ) -> FactorBatchResult:
        from app.services.heavy_job_limiter import shared_heavy_job_limiter

        with shared_heavy_job_limiter.slot("exclusive"):
            return self._with_snapshot_retry(
                lambda: self._run_batch(config, regime_by_date=regime_by_date)
            )

    def _run_batch(
        self,
        config: FactorBatchConfig,
        *,
        regime_by_date: Mapping[object, Any] | None = None,
    ) -> FactorBatchResult:
        """在同一份 Panel 上依次评估多个因子, 避免重复读取和计算指标。"""
        t0 = time.perf_counter()
        run_id = uuid.uuid4().hex[:10]
        factor_names = list(dict.fromkeys(config.factor_names))
        result_config = self._batch_config_to_dict(config, factor_names)
        if not factor_names:
            return FactorBatchResult(
                run_id=run_id,
                config=result_config,
                error="至少选择一个因子",
            )

        generation = self._data_generation(config.asset_type)
        panel = self._load_factor_panel(
            config,
            factor_names,
            expected_generation=generation,
        )
        if panel.is_empty():
            return FactorBatchResult(
                run_id=run_id,
                config=result_config,
                error="无数据, 请检查日期范围或先运行盘后管道",
                elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
            )

        # P1: 预计算共享下期收益 (仅依赖 close/date/symbol), 避免每个因子重复 shift/调仓日 JOIN。
        trading_dates = self._global_trading_dates(config)
        self._assert_data_generation(config.asset_type, generation)
        panel = self._attach_shared_next_return(
            panel,
            config,
            trading_dates=trading_dates,
        )

        metadata = {item["id"]: item for item in FACTOR_COLUMNS}
        fundamentals_missing = (
            any(name in FUNDAMENTAL_FACTOR_NAMES for name in factor_names)
            and self._fundamentals_missing()
        )
        items: list[FactorBatchItem] = []
        for factor_name in factor_names:
            item_t0 = time.perf_counter()
            factor_config = FactorConfig(
                factor_name=factor_name,
                symbols=config.symbols,
                start=config.start,
                end=config.end,
                n_groups=config.n_groups,
                rebalance=config.rebalance,
                weight=config.weight,
                fees_pct=config.fees_pct,
                commission_pct=config.commission_pct,
                stamp_tax_pct=config.stamp_tax_pct,
                slippage_bps=config.slippage_bps,
                asset_type=config.asset_type,
            )
            meta = metadata.get(factor_name, {})
            if factor_name in FUNDAMENTAL_FACTOR_NAMES and fundamentals_missing:
                items.append(FactorBatchItem(
                    factor_name=factor_name,
                    label=str(meta.get("label", factor_name)),
                    group=str(meta.get("group", "")),
                    elapsed_ms=round((time.perf_counter() - item_t0) * 1000, 1),
                    error="本地没有财务数据: 请先在数据页同步财务数据后再使用财务因子",
                ))
                continue
            try:
                evaluate_kwargs = (
                    {"regime_by_date": regime_by_date}
                    if regime_by_date is not None
                    else {}
                )
                result = self._evaluate_panel(
                    panel,
                    factor_config,
                    f"{run_id}-{len(items) + 1}",
                    item_t0,
                    market_trading_dates=trading_dates,
                    **evaluate_kwargs,
                )
                long_short = result.long_short_stats
                # metrics_v2: 由 IC 序列推导 NW HAC t 值与 p 值 (日频 1 日前瞻 → 滞后 1)
                ic_values = [row.get("ic") for row in result.ic_series]
                t_naive = stats_v2.naive_t(ic_values)
                nw = stats_v2.newey_west_t(ic_values, lag=1)
                items.append(FactorBatchItem(
                    factor_name=factor_name,
                    label=str(meta.get("label", factor_name)),
                    group=str(meta.get("group", "")),
                    ic_mean=result.ic_mean,
                    ir=result.ir,
                    ic_win_rate=result.ic_win_rate,
                    long_short_return=long_short.get("total_return"),
                    long_short_max_drawdown=long_short.get("max_drawdown"),
                    methodology_version=result.methodology_version,
                    coverage=result.coverage,
                    turnover=result.turnover,
                    long_short_sharpe=result.long_short_sharpe,
                    yearly_ic=result.yearly_ic,
                    ic_decay=result.ic_decay,
                    regime_stats=result.regime_stats,
                    n_symbols=result.n_symbols,
                    n_dates=result.n_dates,
                    elapsed_ms=result.elapsed_ms,
                    error=result.error,
                    t_naive=t_naive,
                    t_newey_west=nw[0] if nw else None,
                    nw_lag=1 if nw else None,
                    p_value=(
                        stats_v2.normal_two_sided_p(nw[0]) if nw
                        else stats_v2.normal_two_sided_p(t_naive) if t_naive is not None
                        else None
                    ),
                ))
            except Exception as exc:  # 单因子失败不能中止整个筛选批次
                logger.exception("factor batch item failed: %s", factor_name)
                items.append(FactorBatchItem(
                    factor_name=factor_name,
                    label=str(meta.get("label", factor_name)),
                    group=str(meta.get("group", "")),
                    elapsed_ms=round((time.perf_counter() - item_t0) * 1000, 1),
                    error=str(exc),
                ))

        n_symbols = max((item.n_symbols for item in items), default=0)
        n_dates = max((item.n_dates for item in items), default=0)
        # metrics_v2: 批内 BH-FDR q 值 (m = 可检验因子数, 计算失败项不计)
        q_values = stats_v2.bh_fdr_qvalues([item.p_value for item in items])
        for item, q_value in zip(items, q_values, strict=True):
            item.q_value = q_value
        return FactorBatchResult(
            run_id=run_id,
            config=result_config,
            results=items,
            elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
            n_symbols=n_symbols,
            n_dates=n_dates,
        )

    def _data_generation(self, asset_type: str) -> str | None:
        loader = getattr(self.engine, "data_generation", None)
        return loader(asset_type) if callable(loader) else None

    def _fundamentals_missing(self) -> bool:
        return load_fundamental_snapshot(self._fundamentals_data_dir()) is None

    def _fundamentals_data_dir(self) -> Path | None:
        repo = getattr(self.engine, "repo", None)
        return getattr(getattr(repo, "store", None), "data_dir", None)

    def _assert_data_generation(
        self,
        asset_type: str,
        expected: str | None,
    ) -> None:
        verifier = getattr(self.engine, "assert_data_generation", None)
        if callable(verifier):
            verifier(asset_type, expected)

    def _load_factor_panel(
        self,
        config: FactorConfig | FactorBatchConfig,
        factor_names: list[str],
        *,
        expected_generation: str | None = None,
    ) -> pl.DataFrame:
        panel_columns = [
            "symbol", "date", "open", "high", "low", "close", "volume", "amount",
            "turnover_rate",
        ]
        if any(
            name in ("limit_up_count_20d", "limit_up_count_60d")
            for name in factor_names
        ):
            panel_columns.append("consecutive_limit_ups")
        load_start = config.start
        if any(name != "turnover_rate" for name in factor_names):
            load_start = config.start - timedelta(days=FACTOR_WARMUP_DAYS)

        load_kwargs = {
            "columns": panel_columns,
            "asset_type": config.asset_type,
        }
        if expected_generation is not None:
            load_kwargs["expected_generation"] = expected_generation
        panel = self.engine.load_panel(
            config.symbols,
            load_start,
            config.end,
            **load_kwargs,
        )
        if panel.is_empty():
            return panel

        missing = set(factor_names) - set(panel.columns)
        if missing:
            panel = self._compute_missing_factors(panel, missing)
        fundamental_names = [name for name in factor_names if name in FUNDAMENTAL_FACTOR_NAMES]
        if fundamental_names:
            # 点时财务因子: 公告日门控, 无数据标的保持 null (不参与该日截面)。
            panel = attach_fundamental_factors(
                panel,
                load_fundamental_snapshot(self._fundamentals_data_dir()),
                fundamental_names,
            )
        return panel

    def _global_trading_dates(
        self,
        config: FactorConfig | FactorBatchConfig,
    ) -> list[date] | None:
        """Read the market date axis from enriched partitions, independent of symbols."""
        repo = getattr(self.engine, "repo", None)
        data_dir = getattr(getattr(repo, "store", None), "data_dir", None)
        if data_dir is None:
            return None
        from app.tickflow.repository import enriched_dirname

        values: list[date] = []
        root = data_dir / enriched_dirname(config.asset_type)
        for partition in root.glob("date=*"):
            try:
                value = date.fromisoformat(partition.name.removeprefix("date="))
            except ValueError:
                continue
            if value <= config.end and (partition / "part.parquet").is_file():
                values.append(value)
        ordered = sorted(set(values))
        formal = [value for value in ordered if value >= config.start]
        predecessor = next(
            (value for value in reversed(ordered) if value < config.start),
            None,
        )
        if predecessor is not None:
            formal.insert(0, predecessor)
        return formal or None

    @staticmethod
    def _attach_shared_next_return(
        panel: pl.DataFrame,
        config: FactorConfig | FactorBatchConfig,
        *,
        trading_dates: list[date] | None = None,
    ) -> pl.DataFrame:
        """Prepare returns once on the complete price axis before factor filtering."""
        forward_columns = [f"_forward_return_{horizon}d" for horizon in _DAILY_FORWARD_HORIZONS]
        prepared_columns = ["_next_return", *forward_columns]
        if all(column in panel.columns for column in prepared_columns):
            return panel
        existing_columns = [column for column in prepared_columns if column in panel.columns]
        if existing_columns:
            panel = panel.drop(existing_columns)

        base = (
            panel.filter((pl.col("date") >= config.start) & (pl.col("date") <= config.end))
            .filter(pl.col("close").is_not_null() & (pl.col("close") > 0))
            .select(["symbol", "date", "close"])
            .unique(subset=["symbol", "date"], keep="last")
            .sort(["symbol", "date"])
        )
        if base.is_empty():
            return panel.with_columns(
                [pl.lit(None).cast(pl.Float64).alias(column) for column in forward_columns]
                + [pl.lit(None).cast(pl.Float64).alias("_next_return")]
            )

        all_dates = sorted(
            value
            for value in (
                trading_dates if trading_dates is not None else base["date"].unique().to_list()
            )
            if config.start <= value <= config.end
        )
        date_dtype = base.schema["date"]
        for horizon, return_column in zip(
            _DAILY_FORWARD_HORIZONS,
            forward_columns,
            strict=True,
        ):
            if len(all_dates) <= horizon:
                base = base.with_columns(
                    pl.lit(None).cast(pl.Float64).alias(return_column)
                )
                continue
            target_column = f"_target_date_{horizon}d"
            target_close_column = f"_target_close_{horizon}d"
            date_map = pl.DataFrame({
                "date": all_dates[:-horizon],
                target_column: all_dates[horizon:],
            }).with_columns(
                pl.col("date").cast(date_dtype),
                pl.col(target_column).cast(date_dtype),
            )
            price_lookup = base.select(
                "symbol",
                pl.col("date").alias(target_column),
                pl.col("close").alias(target_close_column),
            )
            base = (
                base.join(date_map, on="date", how="left")
                .join(price_lookup, on=["symbol", target_column], how="left")
                .with_columns(
                    pl.when(pl.col(target_close_column).is_not_null())
                    .then(pl.col(target_close_column) / pl.col("close") - 1.0)
                    .otherwise(None)
                    .cast(pl.Float64)
                    .alias(return_column)
                )
                .drop([target_column, target_close_column])
            )

        if config.rebalance == "daily":
            base = base.with_columns(
                pl.col("_forward_return_1d").alias("_next_return")
            )
        else:
            base = FactorBacktestService._calc_period_return(base, config.rebalance)

        return panel.join(
            base.select(["symbol", "date", "_next_return", *forward_columns]),
            on=["symbol", "date"],
            how="left",
        )

    def _evaluate_panel(
        self,
        source_panel: pl.DataFrame,
        config: FactorConfig,
        run_id: str,
        t0: float,
        *,
        regime_by_date: Mapping[object, Any] | None = None,
        market_trading_dates: list[date] | None = None,
    ) -> FactorResult:
        def _err(msg: str) -> FactorResult:
            return self._error_result(config, run_id, t0, msg)

        factor_col = config.factor_name
        if factor_col not in source_panel.columns:
            return _err(f"因子列 '{factor_col}' 不存在于 enriched 数据中, 且无法从基础行情计算")
        if "close" not in source_panel.columns:
            return _err("enriched 数据缺少收盘价 close")
        if "_next_return" not in source_panel.columns:
            source_panel = self._attach_shared_next_return(source_panel, config)

        return_columns = [
            column
            for column in (
                "_next_return",
                *(f"_forward_return_{horizon}d" for horizon in _DAILY_FORWARD_HORIZONS),
            )
            if column in source_panel.columns
        ]
        price_panel = (
            source_panel.select(["symbol", "date", "close", factor_col, *return_columns])
            .filter((pl.col("date") >= config.start) & (pl.col("date") <= config.end))
            .filter(pl.col("close").is_not_null() & (pl.col("close") > 0))
        )
        total_price_rows = price_panel.height
        panel = price_panel.filter(
            pl.col(factor_col).is_not_null() & pl.col(factor_col).is_finite()
        )
        if panel.is_empty():
            return _err("过滤后无有效数据")

        panel = panel.sort(["symbol", "date"])
        coverage = panel.height / total_price_rows if total_price_rows else None
        n_symbols = panel["symbol"].n_unique()
        n_dates = panel["date"].n_unique()

        # ── 1. IC 分析 ──
        ic_df = self._calc_ic(panel, factor_col)
        valid_ic_df = ic_df.filter(pl.col("ic").is_not_null() & pl.col("ic").is_finite())
        ic_rows = valid_ic_df.iter_rows(named=True)
        ic_series = [
            {"date": str(row["date"]), "ic": round(float(row["ic"]), 4)}
            for row in ic_rows
        ]
        ic_values = valid_ic_df["ic"].to_numpy() if not valid_ic_df.is_empty() else np.array([])
        ic_mean = float(np.mean(ic_values)) if ic_values.size else None
        ic_std = float(np.std(ic_values)) if ic_values.size else None
        ir = (ic_mean / ic_std) if (ic_mean is not None and ic_std and ic_std > 1e-8) else None
        ic_win_rate = float(np.mean(ic_values > 0)) if ic_values.size else None
        yearly_ic = self._calc_yearly_ic(valid_ic_df)
        ic_decay = self._calc_ic_decay(panel, factor_col)
        regime_stats = self._calc_regime_stats(
            valid_ic_df,
            price_panel,
            regime_by_date,
            config.start,
            config.end,
            market_trading_dates=market_trading_dates,
        )

        # ── 2. 分层回测 ──
        panel = self._add_groups(panel, factor_col, config.n_groups)
        group_nav = self._calc_group_nav(panel, config)
        group_stats = self._calc_group_stats(group_nav, config.start, config.end, config.rebalance)
        turnover = self._calc_turnover(panel, config)

        # ── 3. 理论因子多空组合 ──
        long_short_nav, long_short_stats = self._calc_long_short(group_nav, config)
        long_short_sharpe = long_short_stats.get("sharpe")

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
            group_nav=self._round_nav(group_nav),
            long_short_stats=long_short_stats,
            long_short_nav=long_short_nav,
            coverage=round(coverage, 4) if coverage is not None else None,
            turnover=round(turnover, 4) if turnover is not None else None,
            long_short_sharpe=long_short_sharpe,
            yearly_ic=yearly_ic,
            ic_decay=ic_decay,
            regime_stats=regime_stats,
            elapsed_ms=round(elapsed, 1),
            n_symbols=n_symbols,
            n_dates=n_dates,
        )

    @staticmethod
    def _compute_missing_factors(
        panel: pl.DataFrame,
        factor_cols: set[str],
        *,
        assume_sorted: bool = False,
    ) -> pl.DataFrame:
        required = {"symbol", "date", "open", "high", "low", "close", "volume"}
        if not required.issubset(panel.columns):
            missing = sorted(required - set(panel.columns))
            logger.warning("factors %s cannot be computed, missing columns: %s", factor_cols, missing)
            return panel

        # 扩展表因子 (ext_ base 条目) = 外部物化列, 指标补算管线不认识;
        # 请求的因子集合命中时在此按 (symbol, date) 时序对齐注入 (与
        # compute_signals 同一原语, 历史帧不含快照 → 无未来函数)。
        from app.factors import ext_factors

        if factor_cols & ext_factors.ext_factor_ids():
            panel = ext_factors.attach_ext_columns(panel, include_snapshot=False)

        from app.factors.registry import get_factor
        from app.indicators.pipeline import compute_indicators

        # custom/composite 因子走注册表→DSL/组合 的同一条物化路径 (P3, 与策略评分共用);
        # 其底层依赖 (如 change_pct/ma20) 先经内置补算路径物化, 再做 DSL/组合物化。
        registry_names = {
            name for name in factor_cols
            if (spec := get_factor(name)) is not None and spec.kind in ("custom", "composite")
        }
        if registry_names:
            base_deps: set[str] = set()
            for name in registry_names:
                spec = get_factor(name)
                if spec is not None:
                    base_deps.update(spec.dependencies)
            base_deps -= set(panel.columns) | registry_names
            if base_deps:
                panel = FactorBacktestService._compute_missing_factors(
                    panel, base_deps, assume_sorted=assume_sorted,
                )
            panel = materialize_scoring_columns(panel, registry_names)
            factor_cols = factor_cols - registry_names
            if not factor_cols:
                return panel

        derived = factor_cols & set(DERIVED_FACTOR_DEPENDENCIES)
        indicator_columns = factor_cols - derived
        for factor_name in derived:
            indicator_columns.update(DERIVED_FACTOR_DEPENDENCIES[factor_name])
        panel = compute_indicators(
            panel,
            needed=indicator_columns,
            assume_sorted=assume_sorted,
        )
        return FactorBacktestService._compute_derived_factors(panel, derived)

    @staticmethod
    def _compute_derived_factors(panel: pl.DataFrame, factor_cols: set[str]) -> pl.DataFrame:
        return materialize_scoring_columns(panel, factor_cols)

    @staticmethod
    def _error_result(
        config: FactorConfig,
        run_id: str,
        started_at: float,
        message: str,
    ) -> FactorResult:
        return FactorResult(
            run_id=run_id,
            config=FactorBacktestService._config_to_dict(config),
            error=message,
            elapsed_ms=round((time.perf_counter() - started_at) * 1000, 1),
        )

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

    @staticmethod
    def _calc_yearly_ic(ic_df: pl.DataFrame) -> list[dict]:
        if ic_df.is_empty():
            return []
        yearly = (
            ic_df.with_columns(pl.col("date").dt.year().alias("_year"))
            .group_by("_year")
            .agg(
                pl.col("ic").mean().alias("ic_mean"),
                pl.col("ic").std(ddof=0).alias("ic_std"),
                (pl.col("ic") > 0).mean().alias("win_rate"),
                pl.len().alias("n_dates"),
            )
            .sort("_year")
        )
        result: list[dict] = []
        for row in yearly.iter_rows(named=True):
            mean = float(row["ic_mean"])
            std = float(row["ic_std"] or 0.0)
            result.append({
                "year": int(row["_year"]),
                "ic_mean": round(mean, 4),
                "ir": round(mean / std, 4) if std > 1e-8 else None,
                "win_rate": round(float(row["win_rate"]), 4),
                "n_dates": int(row["n_dates"]),
            })
        return result

    @staticmethod
    def _calc_ic_decay(panel: pl.DataFrame, factor_col: str) -> list[dict]:
        columns = [
            (horizon, f"_forward_return_{horizon}d")
            for horizon in _DAILY_FORWARD_HORIZONS
            if f"_forward_return_{horizon}d" in panel.columns
        ]
        if not columns:
            return []
        decay_df = (
            panel.group_by("date")
            .agg([
                pl.corr(
                    pl.col(factor_col).rank(method="average"),
                    pl.col(return_column).rank(method="average"),
                ).alias(f"ic_{horizon}d")
                for horizon, return_column in columns
            ])
            .sort("date")
        )
        result: list[dict] = []
        for horizon, _ in columns:
            column = f"ic_{horizon}d"
            values = decay_df.filter(
                pl.col(column).is_not_null() & pl.col(column).is_finite()
            )[column]
            result.append({
                "horizon": horizon,
                "ic_mean": round(float(values.mean()), 4) if len(values) else None,
                "n_dates": len(values),
            })
        return result

    @staticmethod
    def _calc_regime_stats(
        ic_df: pl.DataFrame,
        price_panel: pl.DataFrame,
        regime_by_date: Mapping[object, Any] | None,
        required_start: date,
        required_end: date,
        *,
        market_trading_dates: list[date] | None = None,
    ) -> list[dict]:
        if not regime_by_date:
            return []

        from app.backtest.regime_alignment import (
            align_regime_t_minus_one,
            three_level_regime,
        )

        formal_labels = tuple(
            str(value)[:10] for value in sorted(price_panel["date"].unique().to_list())
        )
        if market_trading_dates:
            labels = tuple(
                str(value)[:10]
                for value in market_trading_dates
                if value <= required_end
            )
        else:
            predecessor = max(
                (
                    str(value)[:10]
                    for value in regime_by_date
                    if str(value)[:10] < str(required_start)
                ),
                default=None,
            )
            labels = (
                (predecessor, *formal_labels)
                if predecessor is not None
                else formal_labels
            )
        aligned = align_regime_t_minus_one(
            labels,
            regime_by_date,
            required_start,
            required_end,
            # 统计场景: 数据边界即正式首日 (如「全部」/「1年」范围起点=本地数据首日) 时,
            # 首日无 T-1 环境属正常, 跳过首日不参与环境分组即可, 不阻断回测;
            # 与策略回测 clamp_formal_start_for_regime 的「首日让渡」同口径。
            # 内部缺口 (次日 T-1 缺环境) 仍 fail-closed 报错。
            first_day_boundary_ok=True,
        )
        ic_by_date = {
            str(row["date"])[:10]: float(row["ic"])
            for row in ic_df.iter_rows(named=True)
        }
        grouped: dict[str, dict[str, list[float]]] = {}
        for label, point in zip(labels, aligned, strict=True):
            if label not in formal_labels or point is None:
                continue
            state, score = point
            bucket = grouped.setdefault(
                three_level_regime(state),
                {"scores": [], "ics": [], "dates": []},
            )
            bucket["scores"].append(score)
            bucket["dates"].append(label)
            if label in ic_by_date:
                bucket["ics"].append(ic_by_date[label])

        order = {"strong": 0, "range": 1, "weak": 2}
        result: list[dict] = []
        for state in sorted(grouped, key=lambda value: (order.get(value, 99), value)):
            values = np.asarray(grouped[state]["ics"], dtype=np.float64)
            scores = np.asarray(grouped[state]["scores"], dtype=np.float64)
            mean = float(np.mean(values)) if values.size else None
            std = float(np.std(values)) if values.size else None
            result.append({
                "state": state,
                "ic_mean": round(mean, 4) if mean is not None else None,
                "ir": (
                    round(mean / std, 4)
                    if mean is not None and std is not None and std > 1e-8
                    else None
                ),
                "win_rate": round(float(np.mean(values > 0)), 4) if values.size else None,
                "mean_score": round(float(np.mean(scores)), 2),
                "n_dates": len(grouped[state]["dates"]),
                "n_ic_dates": int(values.size),
            })
        return result

    # ── 调仓期收益 ──

    @staticmethod
    def _calc_period_return(panel: pl.DataFrame, rebalance: str) -> pl.DataFrame:
        """计算到下个调仓日的收益。

        weekly: 下个周调仓日 close / 今日 close - 1
        monthly: 下个月调仓日 close / 今日 close - 1
        只在调仓日标记行有效，其他行为 null。
        """
        all_dates = sorted(panel["date"].unique().to_list())
        if rebalance == "weekly":
            rebalance_dates = set()
            seen_weeks: set[tuple[int, int]] = set()
            for current_date in all_dates:
                normalized_date = (
                    current_date
                    if hasattr(current_date, "isocalendar")
                    else date.fromisoformat(str(current_date)[:10])
                )
                iso_year, iso_week, _ = normalized_date.isocalendar()
                week = (iso_year, iso_week)
                if week not in seen_weeks:
                    seen_weeks.add(week)
                    rebalance_dates.add(current_date)
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
        """Tie-aware cross-sectional buckets; equal factor values never split."""
        return (
            panel.with_columns(
                pl.col(factor_col).rank(method="average").over("date").alias("_factor_rank"),
                pl.len().over("date").alias("_factor_count"),
            )
            .with_columns(
                (
                    pl.lit("Q")
                    + (
                        (
                            (pl.col("_factor_rank") - 1.0)
                            * n_groups
                            / pl.col("_factor_count")
                        )
                        .floor()
                        .cast(pl.Int64)
                        + 1
                    )
                    .clip(1, n_groups)
                    .cast(pl.Utf8)
                )
                .alias("_group"),
                (
                    pl.col("_factor_rank")
                    - (pl.col("_factor_count") + 1) / 2.0
                )
                .abs()
                .add(0.5)
                .alias("_factor_strength"),
            )
            .drop(["_factor_rank", "_factor_count"])
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
    def _round_nav(group_nav: list[dict]) -> list[dict]:
        return [
            {
                key: value if key == "date" else round(float(value), 4)
                for key, value in row.items()
            }
            for row in group_nav
        ]

    @staticmethod
    def _round_trip_cost(config: FactorConfig) -> float:
        commission = (
            config.commission_pct
            if config.commission_pct is not None
            else config.fees_pct
        )
        stamp_tax = config.stamp_tax_pct or 0.0
        slippage = config.slippage_bps / 10_000.0
        return 2.0 * commission + stamp_tax + 2.0 * slippage

    @staticmethod
    def _calc_group_nav(panel: pl.DataFrame, config: FactorConfig) -> list[dict]:
        """Calculate group NAV with the configured cross-sectional weighting."""
        eligible = panel.filter(
            pl.col("_next_return").is_not_null() & pl.col("_group").is_not_null()
        )
        if config.weight == "factor_weight":
            group_ret = (
                eligible.with_columns(
                    pl.when(pl.col("_factor_strength") > 0)
                    .then(pl.col("_factor_strength"))
                    .otherwise(1.0)
                    .alias("_weight")
                )
                .group_by(["date", "_group"])
                .agg(
                    (
                        (pl.col("_next_return") * pl.col("_weight")).sum()
                        / pl.col("_weight").sum()
                    ).alias("group_return")
                )
            )
        else:
            group_ret = eligible.group_by(["date", "_group"]).agg(
                pl.col("_next_return").mean().alias("group_return")
            )
        group_ret = group_ret.with_columns(
            (pl.col("group_return") - FactorBacktestService._round_trip_cost(config))
            .alias("group_return")
        )

        pivot = group_ret.pivot(
            index="date",
            on="_group",
            values="group_return",
        ).sort("date")
        if pivot.is_empty():
            return []

        group_cols = sorted(
            [column for column in pivot.columns if column != "date"],
            key=FactorBacktestService._group_sort_key,
        )
        nav_df = pivot.with_columns(
            [(1.0 + pl.col(column).fill_null(0.0)).cum_prod().alias(column) for column in group_cols]
        )
        result: list[dict] = []
        for row in nav_df.iter_rows(named=True):
            entry: dict = {"date": str(row["date"])[:10]}
            for column in group_cols:
                entry[column] = float(row[column])
            result.append(entry)
        return result

    @staticmethod
    def _calc_turnover(panel: pl.DataFrame, config: FactorConfig) -> float | None:
        eligible = panel.filter(
            pl.col("_next_return").is_not_null() & pl.col("_group").is_not_null()
        )
        if eligible.is_empty():
            return None
        groups = sorted(
            eligible["_group"].unique().to_list(),
            key=FactorBacktestService._group_sort_key,
        )
        if not groups:
            return None
        top_group = groups[-1]
        weights = eligible.filter(pl.col("_group") == top_group)
        if config.weight == "factor_weight":
            weights = weights.with_columns(
                pl.when(pl.col("_factor_strength") > 0)
                .then(pl.col("_factor_strength"))
                .otherwise(1.0)
                .alias("_raw_weight")
            )
        else:
            weights = weights.with_columns(pl.lit(1.0).alias("_raw_weight"))
        weights = weights.with_columns(
            (pl.col("_raw_weight") / pl.col("_raw_weight").sum().over("date"))
            .alias("_weight")
        )

        by_date: dict[object, dict[str, float]] = {}
        for row in weights.select(["date", "symbol", "_weight"]).iter_rows(named=True):
            by_date.setdefault(row["date"], {})[str(row["symbol"])] = float(row["_weight"])
        ordered_dates = sorted(by_date)
        if len(ordered_dates) < 2:
            return 0.0
        turnovers: list[float] = []
        for previous_date, current_date in pairwise(ordered_dates):
            previous = by_date[previous_date]
            current = by_date[current_date]
            symbols = previous.keys() | current.keys()
            turnovers.append(
                0.5 * sum(abs(current.get(symbol, 0.0) - previous.get(symbol, 0.0)) for symbol in symbols)
            )
        return float(np.mean(turnovers))

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
        # 夏普 — 年化系数必须匹配 group_nav 的调仓频率 (每个净值点 = 一个调仓周期收益);
        # 周/月频收益若乘 √252 会把 Sharpe 高估 √(252/期数) 倍 (月频 ≈4.6x, 周频 ≈2.2x)。
        _ann = {"daily": 252, "weekly": 52, "monthly": 12}.get(rebalance, 252)

        stats = []
        for i, c in enumerate(group_cols):
            values = [r[c] for r in group_nav if r.get(c) is not None]
            if not values:
                continue
            arr = np.asarray(values, dtype=np.float64)
            last = float(arr[-1])
            total_return = last - 1.0
            annual_return = last ** (1 / max(years, 0.01)) - 1 if last > 0 else 0.0

            # 最大回撤 (向量化): 峰值 = max(1.0, 历史最高), 与原 peak 初值 1.0 的逐行 max 一致
            peak = np.maximum(np.maximum.accumulate(arr), 1.0)
            max_dd = float(np.min((arr - peak) / peak))

            # 周期收益序列 (向量化): nav[t]/nav[t-1] - 1, 仅保留 nav[t-1] > 0 的样本
            prev = arr[:-1]
            with np.errstate(divide="ignore", invalid="ignore"):
                rets = arr[1:] / prev - 1.0
            rets = rets[prev > 0]
            if rets.size:
                std = float(np.std(rets))
                sharpe = float(np.mean(rets) / std) * np.sqrt(_ann) if std > 0 else 0.0
                win_rate = float(np.mean(rets > 0))
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

        # 向量化: 各组净值 (null 视为 1.0), 前置 1.0 作为初值 prev_top/prev_bot,
        # 等价于原逐行 prev_top/prev_bot 初始 1.0 的累乘逻辑。
        top = np.array(
            [r[top_col] if r.get(top_col) is not None else 1.0 for r in group_nav],
            dtype=np.float64,
        )
        bot = np.array(
            [r[bottom_col] if r.get(bottom_col) is not None else 1.0 for r in group_nav],
            dtype=np.float64,
        )
        prev_top = np.concatenate(([1.0], top[:-1]))
        prev_bot = np.concatenate(([1.0], bot[:-1]))

        # 分组收益; prev <= 0 时按原逻辑置 0 (做多 top, 做空 bottom = 取反)
        with np.errstate(divide="ignore", invalid="ignore"):
            top_ret = np.where(prev_top > 0, top / prev_top - 1.0, 0.0)
            bot_ret = np.where(prev_bot > 0, bot / prev_bot - 1.0, 0.0)
        ls_ret = (
            (top_ret - bot_ret) / 2.0
            - FactorBacktestService._round_trip_cost(config)
        )  # 50/50 理论多空两腿
        ls_value = np.cumprod(1.0 + ls_ret)

        # 最大回撤: 峰值 = max(1.0, 历史最高), 与原 peak 初值 1.0 一致
        peak = np.maximum(np.maximum.accumulate(ls_value), 1.0)
        max_dd = float(np.min((ls_value - peak) / peak))

        ann_factor = {"daily": 252, "weekly": 52, "monthly": 12}.get(
            config.rebalance,
            252,
        )
        ls_std = float(np.std(ls_ret))
        sharpe = (
            float(np.mean(ls_ret) / ls_std) * np.sqrt(ann_factor)
            if ls_std > 1e-8
            else 0.0
        )
        ls_nav = [
            {"date": group_nav[k]["date"], "value": round(float(ls_value[k]), 4)}
            for k in range(len(group_nav))
        ]
        ls_stats = {
            "total_return": round(float(ls_value[-1]) - 1.0, 4),
            "max_drawdown": round(max_dd, 4),
            "sharpe": round(sharpe, 4),
            "top_group": top_col,
            "bottom_group": bottom_col,
            "portfolio_type": "theoretical_factor_spread",
            "executable_short": False,
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
            "commission_pct": c.commission_pct,
            "stamp_tax_pct": c.stamp_tax_pct,
            "slippage_bps": c.slippage_bps,
            "asset_type": c.asset_type,
        }

    @staticmethod
    def _batch_config_to_dict(c: FactorBatchConfig, factor_names: list[str]) -> dict:
        return {
            "factor_names": factor_names,
            "symbols": c.symbols,
            "start": str(c.start),
            "end": str(c.end),
            "n_groups": c.n_groups,
            "rebalance": c.rebalance,
            "weight": c.weight,
            "fees_pct": c.fees_pct,
            "commission_pct": c.commission_pct,
            "stamp_tax_pct": c.stamp_tax_pct,
            "slippage_bps": c.slippage_bps,
            "asset_type": c.asset_type,
        }
