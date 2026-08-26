"""Production mining runtime executed only inside a spawned worker."""
from __future__ import annotations

import json
import math
import os
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal

import numpy as np
import polars as pl

from app.backtest.factor import (
    FACTOR_COLUMNS,
    FACTOR_METHODOLOGY_VERSION,
    FACTOR_WARMUP_DAYS,
    FactorBacktestService,
    FactorBatchConfig,
)
from app.backtest.fundamentals import (
    FUNDAMENTAL_FACTOR_NAMES,
    attach_fundamental_factors,
    load_fundamental_snapshot,
)
from app.backtest.mining import (
    MAX_FINALISTS,
    CandidateEvaluation,
    FactorMetric,
    MiningBudget,
    MiningCandidate,
    MiningRequest,
    MiningResult,
    MiningService,
    benchmark_candidate,
    compute_rank_correlation,
    generate_nested_folds,
    required_outer_folds,
    required_trading_bars,
    validation_config_for_profile,
)
from app.backtest.strategy import (
    BacktestResultPolicy,
    ResolvedFeaturePlan,
    StrategyBacktestConfig,
    StrategyBacktestService,
    StrategyDependencyResolver,
    _merge_resolved_feature_plans,
    build_matrix_cache_profile,
)
from app.enriched_generation import (
    EnrichedGenerationUnavailableError,
    enriched_publication_incomplete,
)
from app.services.mining_jobs import MiningRunStore
from app.services.mining_preflight import enriched_partition_dates
from app.services.mining_schedule import MINING_ALGORITHM_VERSION
from app.strategy import config as strategy_config
from app.strategy.engine import StrategyEngine

ProgressCallback = Callable[[dict[str, Any]], None]
CancelCheck = Callable[[], bool] | Any
_PROFILE_NAMES = frozenset({"exploratory", "balanced", "strict"})
_FACTOR_IDS = frozenset(str(item["id"]) for item in FACTOR_COLUMNS)
_MINING_MATRIX_CACHE_BYTES = 32 * 1024 * 1024
_RESULT_POLICY = BacktestResultPolicy(
    required_stats=frozenset({"total_return", "sharpe", "max_drawdown", "n_trades"}),
    include_monte_carlo=False,
    include_curves=False,
    include_trades=False,
    include_per_symbol_stats=False,
    include_return_distribution=False,
    include_benchmark=False,
    include_strategy_info=False,
)
_REGIME_FILTERS: dict[str, dict[str, list[str]]] = {
    "strong": {"states": ["strong", "lean_strong"]},
    "range": {"states": ["range"]},
    "weak": {"states": ["lean_weak", "weak"]},
}

# 面板/撮合矩阵读取期间 enriched 世代被并发发布打断时, 允许用新世代整体重读的次数
# 与发布未完成时的等待秒数; 超过后以带指引的错误终止运行。
_SNAPSHOT_MAX_ATTEMPTS = 3
_SNAPSHOT_PUBLISH_WAIT_SECONDS = 5.0


class MiningRuntimeCancelledError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeRequest:
    run_id: str
    factor_names: tuple[str, ...]
    strategy_ids: tuple[str, ...]
    symbols: list[str] | None
    asset_type: Literal["stock", "etf"]
    start: date
    end: date
    profile: Literal["exploratory", "balanced", "strict"]
    forward_horizon: int
    commission_pct: float
    stamp_tax_pct: float
    slippage_bps: float
    correlation_threshold: float
    max_finalists: int
    require_regime: bool
    mining_request: MiningRequest


class TrainingMetricProvider:
    """Compute fold-local metrics and retain only compact call telemetry."""

    def __init__(self, target_column: str) -> None:
        self.target_column = target_column
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        train: pl.DataFrame,
        factor_names: Sequence[str],
    ) -> tuple[FactorMetric, ...]:
        metrics = tuple(
            _factor_metric(train, factor_name, self.target_column)
            for factor_name in factor_names
        )
        labels = _date_labels(train)
        self.calls.append({
            "start": labels[0] if labels else None,
            "end": labels[-1] if labels else None,
            "rows": train.height,
            "metrics": metrics,
        })
        return metrics


class MatcherCandidateEvaluator:
    """Evaluate one fixed definition with the production matrix matcher."""

    def __init__(
        self,
        service: StrategyBacktestService,
        strategy_engine: StrategyEngine,
        data_dir: Path,
        request: RuntimeRequest,
        base_market,
        cancel_check: CancelCheck | None,
    ) -> None:
        self.service = service
        self.strategy_engine = strategy_engine
        self.data_dir = data_dir
        self.request = request
        self.base_market = base_market
        self.cancel_check = cancel_check
        self.backtest_count = 0
        self.peak_compute_cache_bytes = 0

    def evaluate_candidate(
        self,
        train: pl.DataFrame,
        test: pl.DataFrame,
        definition: Mapping[str, Any],
    ) -> CandidateEvaluation:
        del train
        return self.evaluate_test(test, definition)

    def evaluate_candidate_labels(
        self,
        train_labels: Sequence[str],
        test_labels: Sequence[str],
        definition: Mapping[str, Any],
    ) -> CandidateEvaluation:
        del train_labels
        return self._evaluate_labels(test_labels, definition)

    def evaluate_test(
        self,
        test: pl.DataFrame,
        definition: Mapping[str, Any],
        *,
        regime_state: str = "overall",
    ) -> CandidateEvaluation:
        return self._evaluate_labels(
            _date_labels(test),
            definition,
            regime_state=regime_state,
        )

    def _evaluate_labels(
        self,
        labels: Sequence[str],
        definition: Mapping[str, Any],
        *,
        regime_state: str = "overall",
    ) -> CandidateEvaluation:
        _raise_if_cancelled(self.cancel_check)
        if not labels:
            return CandidateEvaluation(score=None, error="test fold contains no dates")
        try:
            config = self._backtest_config(
                definition,
                date.fromisoformat(labels[0]),
                date.fromisoformat(labels[-1]),
                regime_state,
            )
            prepared = self.service.prepare_matrix_optimization(
                [config],
                matrix_cache_max_bytes=_MINING_MATRIX_CACHE_BYTES,
                market_data_override=self.base_market,
            )
            try:
                result = self.service.run(
                    config,
                    cancel_event=self.cancel_check,
                    prepared=prepared,
                    result_policy=_RESULT_POLICY,
                )
                self.peak_compute_cache_bytes = max(
                    self.peak_compute_cache_bytes,
                    prepared.compute_cache.snapshot()["peak_bytes"],
                )
            finally:
                prepared.compute_cache.close()
            self.backtest_count += 1
            if result.error:
                return CandidateEvaluation(score=None, error=result.error)
            metrics = {
                key: _finite_or_none(result.stats.get(key))
                for key in ("total_return", "sharpe", "max_drawdown", "n_trades")
            }
            score = _finite_or_none(metrics.get("sharpe"))
            if score is None:
                return CandidateEvaluation(
                    score=None,
                    metrics=metrics,
                    error="backtest did not return a finite sharpe",
                )
            return CandidateEvaluation(score=score, metrics=metrics)
        except (OSError, ValueError, TypeError) as exc:
            return CandidateEvaluation(score=None, error=str(exc))

    def _backtest_config(
        self,
        definition: Mapping[str, Any],
        start: date,
        end: date,
        regime_state: str,
    ) -> StrategyBacktestConfig:
        kind = str(definition.get("kind") or "")
        if kind == "existing_strategy":
            strategy_id = str(definition.get("strategy_id") or "")
            strategy = self.strategy_engine.get(strategy_id)
            overrides = strategy_config.load_override(self.data_dir, strategy_id)
            params = self.strategy_engine.resolve_params(strategy, overrides=overrides)
        elif kind == "factor_rank":
            strategy_id = "factor_rank_research"
            scoring = definition.get("scoring")
            directions = definition.get("directions")
            if not isinstance(scoring, Mapping) or not scoring:
                raise ValueError("factor candidate has no scoring definition")
            if not isinstance(directions, Mapping):
                raise ValueError("factor candidate has no direction definition")
            params = {
                "scoring": {str(key): float(value) for key, value in scoring.items()},
                "directions": {str(key): str(value) for key, value in directions.items()},
                "entry_score": 70.0,
                "exit_score": 40.0,
                "top_rank": 20,
            }
            overrides = {}
        else:
            raise ValueError(f"unsupported mining candidate kind: {kind!r}")

        regime_filter = None
        if regime_state != "overall":
            regime_filter = _REGIME_FILTERS.get(regime_state)
            if regime_filter is None:
                raise ValueError(f"unsupported regime state: {regime_state}")
        return StrategyBacktestConfig(
            strategy_id=strategy_id,
            symbols=self.request.symbols,
            start=start,
            end=end,
            params=params,
            overrides=overrides,
            matching="open_t+1",
            entry_fill="open_t+1",
            exit_fill="open_t+1",
            fees_pct=self.request.commission_pct,
            commission_pct=self.request.commission_pct,
            stamp_tax_pct=self.request.stamp_tax_pct,
            slippage_bps=self.request.slippage_bps,
            max_positions=10,
            max_exposure_pct=1.0,
            initial_capital=1_000_000.0,
            position_sizing="equal",
            mode="position",
            asset_type=self.request.asset_type,
            holding_days=self.request.forward_horizon,
            minute_fill=False,
            regime_filter=regime_filter,
        )


_SYMBOL_BATCH_SIZE = 512


def _load_compact_factor_panel(
    factor_service: FactorBacktestService,
    config: FactorBatchConfig,
    factor_names: Sequence[str],
    *,
    expected_generation: str,
    cancel_check: CancelCheck | None,
) -> pl.DataFrame:
    panel_columns = [
        "symbol",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "turnover_rate",
    ]
    if any(
        name in ("limit_up_count_20d", "limit_up_count_60d")
        for name in factor_names
    ):
        panel_columns.append("consecutive_limit_ups")
    load_start = (
        config.start
        if all(name == "turnover_rate" for name in factor_names)
        else config.start - timedelta(days=FACTOR_WARMUP_DAYS)
    )
    raw = factor_service.engine.load_panel(
        config.symbols,
        load_start,
        config.end,
        columns=panel_columns,
        asset_type=config.asset_type,
        expected_generation=expected_generation,
    )
    if raw.is_empty():
        return raw

    fundamental_names = [
        str(name)
        for name in factor_names
        if str(name) in FUNDAMENTAL_FACTOR_NAMES
    ]
    if fundamental_names:
        data_dir = getattr(
            getattr(getattr(factor_service.engine, "repo", None), "store", None),
            "data_dir",
            None,
        )
        raw = attach_fundamental_factors(
            raw,
            load_fundamental_snapshot(data_dir),
            fundamental_names,
        )

    symbol = pl.col("symbol")
    day = pl.col("date")
    previous_symbol = symbol.shift(1)
    previous_day = day.shift(1)
    invalid_key = raw.select(
        (
            symbol.is_null()
            | day.is_null()
            | (symbol < previous_symbol).fill_null(False)
            | (
                (symbol == previous_symbol)
                & (day <= previous_day)
            ).fill_null(False)
        ).any()
    ).item()
    if invalid_key:
        raise ValueError(
            "mining factor panel requires non-null, unique symbol/date keys "
            "sorted by symbol and strictly increasing date"
        )
    output_by_date: dict[date, list[pl.DataFrame]] = {}
    group_sizes = (
        raw.group_by("symbol", maintain_order=True)
        .len()
        .get_column("len")
        .to_list()
    )
    row_offset = 0
    for offset in range(0, len(group_sizes), _SYMBOL_BATCH_SIZE):
        _raise_if_cancelled(cancel_check)
        row_count = sum(group_sizes[offset:offset + _SYMBOL_BATCH_SIZE])
        batch = raw.slice(row_offset, row_count)
        row_offset += row_count
        missing = set(factor_names) - set(batch.columns)
        if missing:
            batch = factor_service._compute_missing_factors(
                batch,
                missing,
                assume_sorted=True,
            )
        projected = batch.filter(
            (pl.col("date") >= config.start)
            & (pl.col("date") <= config.end)
            & pl.col("close").is_not_null()
            & (pl.col("close") > 0)
        ).select([
            "symbol",
            "date",
            "close",
            *(
                pl.col(name).cast(pl.Float32, strict=False).alias(name)
                for name in factor_names
            ),
        ])
        for daily in projected.partition_by("date", maintain_order=True):
            output_by_date.setdefault(daily.item(0, "date"), []).append(daily)
        del batch, projected
    return pl.concat(
        [
            pl.concat(output_by_date[label], how="vertical", rechunk=False)
            for label in sorted(output_by_date)
        ],
        how="vertical",
        rechunk=False,
    )


def run_mining_runtime(
    payload: Mapping[str, Any],
    *,
    data_dir: Path,
    service: StrategyBacktestService,
    strategy_engine: StrategyEngine,
    progress_cb: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    rss_sampler: Any | None = None,
) -> dict[str, Any]:
    """Run one persistent mining job and return only a compact IPC summary."""
    started = time.perf_counter()
    emit = progress_cb or (lambda _message: None)
    request = _decode_runtime_request(payload, data_dir, strategy_engine)
    fingerprint = payload.get("data_fingerprint")
    expected_generation = (
        fingerprint.get("generation")
        if isinstance(fingerprint, Mapping)
        else None
    )
    if not isinstance(expected_generation, str) or not expected_generation:
        raise ValueError("mining worker payload is missing its data generation")
    store = MiningRunStore(data_dir)
    phase_peak_rss_bytes: dict[str, int] = {}

    def start_phase() -> None:
        if rss_sampler is not None:
            rss_sampler.reset_phase()

    def finish_phase(name: str) -> None:
        if rss_sampler is not None:
            phase_peak_rss_bytes[name] = rss_sampler.phase_peak_rss_bytes()

    emit({"phase": "panel", "label": "加载因子面板", "done": 0, "total": 1})
    start_phase()
    _raise_if_cancelled(cancel_check)
    factor_service = FactorBacktestService(service.engine)
    factor_config = FactorBatchConfig(
        factor_names=list(request.factor_names),
        symbols=request.symbols,
        start=request.start,
        end=request.end,
        asset_type=request.asset_type,
        commission_pct=request.commission_pct,
        stamp_tax_pct=request.stamp_tax_pct,
        slippage_bps=request.slippage_bps,
    )
    phase_ms: dict[str, float] = {}
    panel: pl.DataFrame | None = None
    base_market: np.ndarray | None = None
    generation: str | None = None
    for attempt in range(_SNAPSHOT_MAX_ATTEMPTS):
        if attempt:
            # 读取期间 enriched 发布了新世代, 面板可能新旧混合: 丢弃本轮,
            # 用新世代整体重读。排队指纹只在首轮校验; 数据在运行中前进,
            # 重试跟随新世代属于预期 (等价于"更新后立刻重跑")。
            emit({"phase": "panel", "label": "数据已更新, 重新读取快照", "done": 0, "total": 1})
            if enriched_publication_incomplete(data_dir):
                time.sleep(_SNAPSHOT_PUBLISH_WAIT_SECONDS)
            service.engine.clear_panel_cache()
        panel_started = time.perf_counter()
        try:
            generation = factor_service._data_generation(request.asset_type)
            if attempt == 0 and generation != expected_generation:
                raise ValueError(
                    "mining data generation changed after the run was queued"
                )
            source_panel = _load_compact_factor_panel(
                factor_service,
                factor_config,
                request.factor_names,
                expected_generation=generation,
                cancel_check=cancel_check,
            )
            if source_panel.is_empty():
                raise ValueError("mining date range contains no enriched data")
            service.engine.clear_panel_cache()
            trading_dates = enriched_partition_dates(
                data_dir,
                request.asset_type,
                request.start,
                request.end,
            )
            factor_service._assert_data_generation(request.asset_type, generation)
            panel = attach_single_forward_return(
                source_panel,
                start=request.start,
                end=request.end,
                horizon=request.forward_horizon,
                trading_dates=trading_dates,
                factor_names=request.factor_names,
                target_column=request.mining_request.target_column,
                assume_unique_symbol_date=True,
            )
            del source_panel
            if panel.is_empty():
                raise ValueError("mining panel contains no valid price rows")
            phase_ms["panel"] = round(
                (time.perf_counter() - panel_started) * 1000.0, 3
            )
            finish_phase("panel")
            emit({
                "phase": "panel",
                "label": "因子面板已准备",
                "done": 1,
                "total": 1,
                "rows": panel.height,
                "factors": len(request.factor_names),
            })

            if request.require_regime:
                emit({"phase": "panel", "label": "校验市场环境数据", "done": 0, "total": 1})
                _validate_regime_availability(panel, request, data_dir)

            _raise_if_cancelled(cancel_check)
            start_phase()
            matrix_started = time.perf_counter()
            emit({"phase": "matrix", "label": "准备共享撮合矩阵", "done": 0, "total": 1})
            base_market = _prepare_base_market(
                service,
                strategy_engine,
                data_dir,
                request,
                expected_generation=generation,
                cancel_check=cancel_check,
            )
            factor_service._assert_data_generation(request.asset_type, generation)
            phase_ms["matrix"] = round(
                (time.perf_counter() - matrix_started) * 1000.0, 3
            )
            finish_phase("matrix")
            emit({
                "phase": "matrix",
                "label": "共享撮合矩阵已准备",
                "done": 1,
                "total": 1,
                "matrix_bytes": base_market.nbytes,
            })
            break
        except EnrichedGenerationUnavailableError as exc:
            if attempt + 1 >= _SNAPSHOT_MAX_ATTEMPTS:
                raise ValueError(
                    "行情数据正在更新: 挖掘读取期间 enriched 数据世代反复变化, "
                    "重试后仍拿不到稳定快照; 请等数据更新完成后再开始挖掘"
                ) from exc
    if panel is None or base_market is None or generation is None:
        raise RuntimeError("mining data snapshot did not settle")

    metric_provider = TrainingMetricProvider(request.mining_request.target_column)
    evaluator = MatcherCandidateEvaluator(
        service,
        strategy_engine,
        data_dir,
        request,
        base_market,
        cancel_check,
    )
    emit({"phase": "search", "label": "嵌套样本外搜索", "done": 0, "total": 1})
    start_phase()
    search_started = time.perf_counter()
    result = MiningService().run(
        panel,
        request.mining_request,
        metric_provider=metric_provider,
        evaluator=evaluator,
        cancel_check=cancel_check,
    )
    phase_ms["search"] = round((time.perf_counter() - search_started) * 1000.0, 3)
    finish_phase("search")
    if result.cancelled:
        raise MiningRuntimeCancelledError("mining cancelled")
    emit({
        "phase": "search",
        "label": "候选搜索完成",
        "done": 1,
        "total": 1,
        "proxy_trials": result.proxy_trials_used,
        "real_trials": result.trials_used,
    })

    _raise_if_cancelled(cancel_check)
    start_phase()
    artifact_started = time.perf_counter()
    emit({"phase": "artifacts", "label": "写入研究结果", "done": 0, "total": 4})
    artifact_frames = _build_artifacts(
        panel,
        request,
        result,
        metric_provider,
        evaluator,
        cancel_check,
    )
    for done, (name, frame) in enumerate(artifact_frames.items(), start=1):
        _raise_if_cancelled(cancel_check)
        path = store.artifact_path(request.run_id, name)  # type: ignore[arg-type]
        _atomic_write_parquet(frame, path)
        store.register_artifact(request.run_id, name)  # type: ignore[arg-type]
        emit({
            "phase": "artifacts",
            "label": f"已写入 {name}",
            "done": done,
            "total": 4,
        })
    phase_ms["artifacts"] = round((time.perf_counter() - artifact_started) * 1000.0, 3)
    finish_phase("artifacts")

    folds = artifact_frames["folds"]
    candidates = artifact_frames["candidates"]
    factors = artifact_frames["factors"]
    selected_overall = folds.filter(
        (pl.col("regime_state") == "overall")
        & (
            (pl.col("evaluation_kind") == "selected")
            | pl.col("candidate_signature").is_null()
        )
    )
    valid_folds = selected_overall.filter(~pl.col("skipped")).height
    skipped_folds = selected_overall.filter(pl.col("skipped")).height
    budget_exhausted = _budget_exhausted(result)
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
    phase_ms["total"] = elapsed_ms
    confidence = _confidence(request.profile)
    return {
        "status": (
            "succeeded_with_budget_exhausted" if budget_exhausted else "succeeded"
        ),
        "factor_count": len(request.factor_names),
        "selected_factor_count": int(factors.filter(pl.col("selected")).height),
        "candidate_count": candidates.height,
        "valid_fold_count": valid_folds,
        "skipped_fold_count": skipped_folds,
        "confidence": confidence,
        "budget_exhausted": budget_exhausted,
        "elapsed_ms": elapsed_ms,
        "data_as_of": request.end.isoformat(),
        "algorithm_version": MINING_ALGORITHM_VERSION,
        "methodology_version": FACTOR_METHODOLOGY_VERSION,
        "proxy_trials_used": result.proxy_trials_used,
        "trials_used": result.trials_used + max(0, evaluator.backtest_count - result.trials_used),
        "panel_rows": panel.height,
        "panel_scans": 1,
        "matrix_bytes": base_market.nbytes,
        "matrix_compute_cache_peak_bytes": evaluator.peak_compute_cache_bytes,
        "phase_ms": phase_ms,
        "phase_peak_rss_bytes": phase_peak_rss_bytes,
        "artifacts": list(artifact_frames),
    }


def attach_single_forward_return(
    panel: pl.DataFrame,
    *,
    start: date,
    end: date,
    horizon: int,
    trading_dates: Sequence[date],
    factor_names: Sequence[str],
    target_column: str = "_next_return",
    assume_unique_symbol_date: bool = False,
) -> pl.DataFrame:
    """Materialize exactly one global-axis forward label and its endpoint date."""
    if horizon <= 0:
        raise ValueError("forward horizon must be positive")
    names = tuple(str(name) for name in factor_names)
    required = {"symbol", "date", "close", *names}
    missing = sorted(required - set(panel.columns))
    if missing:
        raise ValueError(f"factor panel is missing required columns: {missing}")
    dates = tuple(sorted(dict.fromkeys(
        value for value in trading_dates if start <= value <= end
    )))
    if not dates:
        raise ValueError("mining date range has no trading dates")

    valid_rows = (
        (pl.col("date") >= start)
        & (pl.col("date") <= end)
        & pl.col("close").is_not_null()
        & (pl.col("close") > 0)
    )
    if assume_unique_symbol_date:
        invalid_count = panel.select((~valid_rows).sum()).item()
        if invalid_count:
            raise ValueError("prevalidated mining panel contains invalid price rows")
        scoped = panel.select([
            "symbol",
            "date",
            "close",
            *(
                pl.col(name).cast(pl.Float32, strict=False).alias(name)
                for name in names
            ),
        ])
    else:
        scoped = (
            panel.filter(valid_rows)
            .select([
                "symbol",
                "date",
                "close",
                *(
                    pl.col(name).cast(pl.Float32, strict=False).alias(name)
                    for name in names
                ),
            ])
            .unique(subset=["symbol", "date"], keep="last")
        )
    date_dtype = scoped.schema["date"]
    target_date_column = "_target_date"
    if len(dates) > horizon:
        date_map = pl.DataFrame({
            "date": dates[:-horizon],
            target_date_column: dates[horizon:],
        }).with_columns(
            pl.col("date").cast(date_dtype),
            pl.col(target_date_column).cast(date_dtype),
        )
    else:
        date_map = pl.DataFrame(
            schema={"date": date_dtype, target_date_column: date_dtype}
        )
    prices = scoped.select("symbol", "date", "close")
    lookup = prices.select(
        "symbol",
        pl.col("date").alias(target_date_column),
        pl.col("close").alias("_target_close"),
    )
    labels = (
        prices.join(date_map, on="date", how="left")
        .join(lookup, on=["symbol", target_date_column], how="left")
        .select(
            "symbol",
            "date",
            pl.when(pl.col("_target_close").is_not_null())
            .then(pl.col("_target_close") / pl.col("close") - 1.0)
            .otherwise(None)
            .cast(pl.Float32)
            .alias(target_column),
            target_date_column,
        )
    )
    if assume_unique_symbol_date:
        labels = labels.sort(["date", "symbol"])
        if (
            not labels.get_column("symbol").equals(scoped.get_column("symbol"))
            or not labels.get_column("date").equals(scoped.get_column("date"))
        ):
            raise ValueError("prevalidated mining panel is not sorted by date and symbol")
        return scoped.select(["symbol", "date", *names]).hstack([
            labels.get_column(target_column),
            labels.get_column(target_date_column),
        ])
    return (
        scoped.select(["symbol", "date", *names])
        .join(labels, on=["symbol", "date"], how="left")
        .sort(["date", "symbol"])
    )


def _decode_runtime_request(
    payload: Mapping[str, Any],
    data_dir: Path,
    strategy_engine: StrategyEngine,
) -> RuntimeRequest:
    run_id = str(payload.get("run_id") or "")
    request = payload.get("request")
    if not run_id or not isinstance(request, Mapping):
        raise ValueError("mining worker payload is missing run_id or request")

    factor_names = tuple(str(value) for value in request.get("factor_names") or ())
    if not factor_names or len(set(factor_names)) != len(factor_names):
        raise ValueError("factor_names must be non-empty and unique")
    unknown_factors = sorted(set(factor_names) - _FACTOR_IDS)
    if unknown_factors:
        raise ValueError(f"unknown mining factors: {unknown_factors}")
    if len(factor_names) > 48:
        raise ValueError("mining supports at most 48 factors")

    strategy_ids = tuple(str(value) for value in request.get("strategy_ids") or ())
    if len(set(strategy_ids)) != len(strategy_ids) or len(strategy_ids) > 8:
        raise ValueError("strategy_ids must be unique and contain at most 8 strategies")
    asset_type = str(request.get("asset_type") or "stock")
    if asset_type not in {"stock", "etf"}:
        raise ValueError("mining asset_type must be stock or etf")
    for strategy_id in strategy_ids:
        strategy = strategy_engine.get(strategy_id)
        if strategy.meta.get("research_only"):
            raise ValueError(f"research template cannot be selected as existing strategy: {strategy_id}")
        if strategy.execution_backend != "matrix_native":
            raise ValueError(f"mining strategy is not matrix-native: {strategy_id}")
        if asset_type not in strategy.meta.get("asset_types", ["stock"]):
            raise ValueError(f"mining strategy does not support {asset_type}: {strategy_id}")

    all_dates = enriched_partition_dates(data_dir, asset_type)
    if not all_dates:
        raise ValueError(f"no enriched {asset_type} trading dates are available")
    requested_start = _optional_date(request.get("start"))
    requested_end = _optional_date(request.get("end"))
    if (
        requested_start is not None
        and requested_end is not None
        and requested_start > requested_end
    ):
        raise ValueError("mining start must not be after end")
    start = max(requested_start or all_dates[0], all_dates[0])
    end = min(requested_end or all_dates[-1], all_dates[-1])
    if start > end:
        raise ValueError("mining date range contains no enriched data")

    profile = str(request.get("budget_profile") or "balanced")
    if profile not in _PROFILE_NAMES:
        raise ValueError(f"unsupported mining profile: {profile}")
    validation = validation_config_for_profile(profile)
    forward_horizon = int(request.get("forward_horizon") or 5)
    if forward_horizon not in {1, 3, 5}:
        raise ValueError("forward_horizon must be 1, 3, or 5 trading days")
    if validation.purge_bars < forward_horizon:
        raise ValueError("validation purge must cover the forward horizon")

    budget = getattr(MiningBudget, profile)()
    max_combination = int(
        request.get("max_combination_factors") or budget.max_combination_size
    )
    beam_width = int(request.get("beam_width") or budget.beam_width)
    max_finalists = int(request.get("max_finalists") or MAX_FINALISTS)
    if not 1 <= max_finalists <= MAX_FINALISTS:
        raise ValueError(f"max_finalists must be between 1 and {MAX_FINALISTS}")
    scoped_dates = [value for value in all_dates if start <= value <= end]
    required_folds = required_outer_folds(profile)
    required_bars = required_trading_bars(validation, required_folds)
    if len(scoped_dates) < required_bars:
        fold_label = "outer fold" if required_folds == 1 else "outer folds"
        raise ValueError(
            f"{profile} mining requires at least {required_bars} enriched trading "
            f"bars for {required_folds} {fold_label}; effective range "
            f"{start.isoformat()} to {end.isoformat()} has {len(scoped_dates)}"
        )
    nested = generate_nested_folds(
        [value.isoformat() for value in scoped_dates],
        validation,
    )
    reserved_regime_trials = 3 * len(nested) if request.get("require_regime", True) else 0
    real_trials = max(len(nested) + 1, budget.max_trials - reserved_regime_trials)
    real_trials = min(real_trials, budget.max_trials)
    budget = replace(
        budget,
        max_combination_size=max_combination,
        beam_width=beam_width,
        max_trials=real_trials,
    )
    correlation_threshold = _bounded_float(
        request.get("correlation_threshold", 0.75),
        "correlation_threshold",
        0.0,
        1.0,
        exclusive_min=True,
    )
    commission_pct = _bounded_float(
        request.get("commission_pct", 0.0002), "commission_pct", 0.0, 0.05
    )
    stamp_tax_pct = _bounded_float(
        request.get("stamp_tax_pct", 0.0005), "stamp_tax_pct", 0.0, 0.05
    )
    slippage_bps = _bounded_float(
        request.get("slippage_bps", 5.0), "slippage_bps", 0.0, 1000.0
    )
    symbols_value = request.get("symbols")
    symbols = None
    if symbols_value is not None:
        if not isinstance(symbols_value, list):
            raise ValueError("symbols must be a list or null")
        symbols = list(dict.fromkeys(str(value) for value in symbols_value if value))
        if not symbols:
            symbols = None

    mining_request = MiningRequest(
        factor_names=factor_names,
        existing_strategy_ids=strategy_ids,
        correlation_threshold=correlation_threshold,
        target_column="_next_return",
        budget=budget,
        validation=validation,
        profile=profile,  # type: ignore[arg-type]
    )
    return RuntimeRequest(
        run_id=run_id,
        factor_names=factor_names,
        strategy_ids=strategy_ids,
        symbols=symbols,
        asset_type=asset_type,  # type: ignore[arg-type]
        start=start,
        end=end,
        profile=profile,  # type: ignore[arg-type]
        forward_horizon=forward_horizon,
        commission_pct=commission_pct,
        stamp_tax_pct=stamp_tax_pct,
        slippage_bps=slippage_bps,
        correlation_threshold=correlation_threshold,
        max_finalists=max_finalists,
        require_regime=bool(request.get("require_regime", True)),
        mining_request=mining_request,
    )


def _prepare_base_market(
    service: StrategyBacktestService,
    strategy_engine: StrategyEngine,
    data_dir: Path,
    request: RuntimeRequest,
    *,
    expected_generation: str | None = None,
    cancel_check: CancelCheck | None = None,
):
    resolver = StrategyDependencyResolver()
    plans: list[ResolvedFeaturePlan] = []
    research = strategy_engine.get("factor_rank_research")
    for offset in range(0, len(request.factor_names), 4):
        factor_chunk = request.factor_names[offset:offset + 4]
        research_params = {
            "scoring": {factor_name: 1.0 for factor_name in factor_chunk},
            "directions": {factor_name: "high" for factor_name in factor_chunk},
        }
        plans.append(resolver.resolve(
            research,
            params=research_params,
            basic_filter=service._effective_basic_filter(research, {}),
            entry_signals=research.entry_signals,
            exit_signals=research.exit_signals,
            overrides={},
            asset_type=request.asset_type,
        ))
    for strategy_id in request.strategy_ids:
        strategy = strategy_engine.get(strategy_id)
        overrides = strategy_config.load_override(data_dir, strategy_id)
        params = strategy_engine.resolve_params(strategy, overrides=overrides)
        plans.append(resolver.resolve(
            strategy,
            params=params,
            basic_filter=service._effective_basic_filter(strategy, overrides),
            entry_signals=service._effective_signals(
                overrides, "entry_signals", strategy.entry_signals
            ),
            exit_signals=service._effective_signals(
                overrides, "exit_signals", strategy.exit_signals
            ),
            overrides=overrides,
            asset_type=request.asset_type,
        ))
    merged = _merge_resolved_feature_plans(plans)
    profile = build_matrix_cache_profile(
        strategy_engine,
        request.asset_type,
        requested_plan=merged,
        requested_forward_bars=request.forward_horizon,
    )
    warmup_days = max(120, int(max(merged.warmup_bars, 1) * 1.6))
    load_start = request.start - timedelta(days=warmup_days)
    return service.engine.load_market_data_matrix_for_backtest(
        request.symbols,
        load_start,
        request.end,
        merged,
        asset_type=request.asset_type,
        cache_profile=profile,
        coverage_start=load_start,
        coverage_end=request.end,
        expected_generation=expected_generation,
        cancel_event=cancel_check,
    )


def _build_artifacts(
    panel: pl.DataFrame,
    request: RuntimeRequest,
    result: MiningResult,
    metric_provider: TrainingMetricProvider,
    evaluator: MatcherCandidateEvaluator,
    cancel_check: CancelCheck | None,
) -> dict[str, pl.DataFrame]:
    nested = generate_nested_folds(_date_labels(panel), request.mining_request.validation)
    last_train = _panel_for_dates(panel, nested[-1].outer.train_labels)
    if "_target_date" in last_train.columns:
        last_train = last_train.filter(
            pl.col("_target_date").is_not_null()
            & (pl.col("_target_date") <= date.fromisoformat(nested[-1].outer.train_end))
        )
    latest_metrics = {
        metric.factor_id: metric
        for metric in metric_provider(last_train, request.factor_names)
    }
    selected_factors = {
        factor_name
        for fold in result.folds
        for factor_name in fold.selected_factors
    }
    direction_by_factor: dict[str, int] = {}
    for fold in result.folds:
        for candidate in fold.candidates:
            for factor_name, direction in zip(
                candidate.factor_names, candidate.directions, strict=True
            ):
                direction_by_factor.setdefault(factor_name, int(direction))
    metadata = {str(item["id"]): item for item in FACTOR_COLUMNS}
    factor_rows = []
    for factor_name in request.factor_names:
        metric = latest_metrics[factor_name]
        factor_rows.append({
            "factor_name": factor_name,
            "label": str(metadata.get(factor_name, {}).get("label", factor_name)),
            "direction": direction_by_factor.get(
                factor_name, 1 if metric.rank_ic >= 0 else -1
            ),
            "score": _finite_or_none(metric.composite_score),
            "ic_mean": _finite_or_none(metric.rank_ic),
            "ir": _finite_or_none(metric.ir),
            "coverage": _finite_or_none(metric.coverage),
            "turnover": _finite_or_none(metric.turnover),
            "spread_return": None,
            "spread_sharpe": None,
            "selected": factor_name in selected_factors,
            "excluded_reason": None if factor_name in selected_factors else "not_selected",
        })
    factors = pl.DataFrame(factor_rows)

    correlation = compute_rank_correlation(last_train, request.factor_names)
    correlation_rows = []
    for row_id, left in enumerate(correlation.factor_names):
        for column_id, right in enumerate(correlation.factor_names):
            count = int(correlation.pair_counts[row_id][column_id])
            correlation_rows.append({
                "factor_x": left,
                "factor_y": right,
                "rho": (
                    float(correlation.matrix[row_id][column_id]) if count > 0 else None
                ),
                "pair_count": count,
            })
    correlation_frame = pl.DataFrame(correlation_rows)

    benchmark_by_id = {
        benchmark_candidate(strategy_id).candidate_id: benchmark_candidate(strategy_id)
        for strategy_id in request.strategy_ids
    }
    candidate_by_id: dict[str, MiningCandidate] = {}
    for fold in result.folds:
        for candidate in fold.candidates:
            candidate_by_id.setdefault(candidate.candidate_id, candidate)
    fold_rows: list[dict[str, Any]] = []
    for fold, nested_fold in zip(result.folds, nested, strict=False):
        selected = (
            candidate_by_id.get(fold.selected_candidate_id)
            if fold.selected_candidate_id is not None
            else None
        )
        evaluation = fold.outer_evaluation
        fold_rows.append(_fold_row(
            fold.outer_index,
            selected,
            nested_fold.outer,
            evaluation,
            regime_state="overall",
            n_dates=len(nested_fold.outer.test_labels),
            reason=fold.error,
        ))
        for candidate_id, cross_evaluation in fold.cross_evaluations:
            fold_rows.append(_fold_row(
                fold.outer_index,
                candidate_by_id.get(candidate_id),
                nested_fold.outer,
                cross_evaluation,
                regime_state="overall",
                n_dates=len(nested_fold.outer.test_labels),
                reason=None,
                evaluation_kind="cross",
            ))
        for candidate_id, benchmark_evaluation in fold.benchmark_evaluations:
            fold_rows.append(_fold_row(
                fold.outer_index,
                benchmark_by_id.get(candidate_id),
                nested_fold.outer,
                benchmark_evaluation,
                regime_state="overall",
                n_dates=len(nested_fold.outer.test_labels),
                reason=None,
                evaluation_kind="benchmark",
            ))
        if (
            selected is None
            or evaluation is None
            or evaluation.error is not None
            or not request.require_regime
        ):
            continue
        test = _panel_for_dates(panel, nested_fold.outer.test_labels)
        for state in ("strong", "range", "weak"):
            _raise_if_cancelled(cancel_check)
            regime_evaluation = evaluator.evaluate_test(
                test,
                selected.definition(),
                regime_state=state,
            )
            n_dates = _regime_date_count(
                panel,
                nested_fold.outer,
                state,
                evaluator.data_dir,
            )
            fold_rows.append(_fold_row(
                fold.outer_index,
                selected,
                nested_fold.outer,
                regime_evaluation,
                regime_state=state,
                n_dates=n_dates,
                reason=None,
            ))
    folds = pl.DataFrame(fold_rows, schema_overrides={
        "total_return": pl.Float64,
        "sharpe": pl.Float64,
        "max_drawdown": pl.Float64,
        "n_trades": pl.Int64,
    })

    candidate_rows = []
    overall_rows = [row for row in fold_rows if row["regime_state"] == "overall"]
    winner_by_id = {
        fold.selected_candidate_id: candidate_by_id[fold.selected_candidate_id]
        for fold in result.folds
        if fold.selected_candidate_id is not None
        and fold.selected_candidate_id in candidate_by_id
    }
    ranked_candidates = _rank_artifact_candidates(
        winner_by_id.values(),
        overall_rows,
        limit=request.max_finalists,
    ) + [benchmark_by_id[cid] for cid in sorted(benchmark_by_id)]
    for candidate in ranked_candidates:
        rows = [row for row in overall_rows if row["candidate_signature"] == candidate.candidate_id]
        successful = [row for row in rows if not row["skipped"]]
        returns = [row["total_return"] for row in successful if row["total_return"] is not None]
        sharpes = [row["sharpe"] for row in successful if row["sharpe"] is not None]
        drawdowns = [row["max_drawdown"] for row in successful if row["max_drawdown"] is not None]
        trades = [row["n_trades"] for row in successful if row["n_trades"] is not None]
        definition = candidate.definition()
        candidate_rows.append({
            "signature": candidate.candidate_id,
            "name": _candidate_name(candidate),
            "kind": (
                "existing_strategy"
                if candidate.kind == "existing_strategy"
                else "factor_combination"
            ),
            "factor_names_json": json.dumps(
                list(candidate.factor_names), ensure_ascii=False, separators=(",", ":")
            ),
            "strategy_id": candidate.strategy_id,
            "definition_json": json.dumps(
                definition,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "regime_state": "overall",
            "score": _mean_or_none(sharpes),
            "oos_return": _mean_or_none(returns),
            "oos_sharpe": _mean_or_none(sharpes),
            "oos_max_drawdown": min(drawdowns) if drawdowns else None,
            "oos_positive_fold_ratio": (
                sum(value > 0 for value in returns) / len(returns) if returns else None
            ),
            "oos_n_trades": sum(int(value) for value in trades) if trades else None,
            "confidence": _confidence(request.profile),
            "valid_folds": len(successful),
            "skipped_folds": len(rows) - len(successful),
            "promoted_candidate_id": None,
            "published_strategy_id": None,
        })
    candidates = pl.DataFrame(candidate_rows, schema_overrides={
        "score": pl.Float64,
        "oos_return": pl.Float64,
        "oos_sharpe": pl.Float64,
        "oos_max_drawdown": pl.Float64,
        "oos_positive_fold_ratio": pl.Float64,
        "oos_n_trades": pl.Int64,
        "strategy_id": pl.Utf8,
        "promoted_candidate_id": pl.Utf8,
        "published_strategy_id": pl.Utf8,
    }) if candidate_rows else _empty_candidates_frame()
    return {
        "factors": factors,
        "correlation": correlation_frame,
        "candidates": candidates,
        "folds": folds,
    }


def _factor_metric(
    train: pl.DataFrame,
    factor_name: str,
    target_column: str,
) -> FactorMetric:
    scoped = train.select("date", "symbol", factor_name, target_column)
    finite_factor = pl.col(factor_name).is_not_null() & pl.col(factor_name).is_finite()
    eligible = scoped.filter(
        finite_factor
        & pl.col(target_column).is_not_null()
        & pl.col(target_column).is_finite()
    )
    coverage = (
        scoped.select(finite_factor.mean()).item() if scoped.height else 0.0
    )
    daily = (
        eligible.group_by("date")
        .agg(
            pl.corr(
                pl.col(factor_name).rank(method="average"),
                pl.col(target_column).rank(method="average"),
            ).alias("ic")
        )
        .filter(pl.col("ic").is_not_null() & pl.col("ic").is_finite())
        .sort("date")
    )
    values = daily["ic"].to_numpy() if not daily.is_empty() else np.array([])
    mean = float(np.mean(values)) if values.size else 0.0
    std = float(np.std(values)) if values.size else 0.0
    ir = mean / std if std > 1e-12 else 0.0
    turnover = _top_quintile_turnover(eligible, factor_name, direction=1 if mean >= 0 else -1)
    score = abs(ir) * float(coverage or 0.0) / (1.0 + turnover)
    return FactorMetric(
        factor_id=factor_name,
        composite_score=round(score, 8),
        ir=round(abs(ir), 8),
        coverage=round(float(coverage or 0.0), 8),
        turnover=round(turnover, 8),
        rank_ic=round(mean, 8),
    )


def _top_quintile_turnover(
    panel: pl.DataFrame,
    factor_name: str,
    *,
    direction: int,
) -> float:
    if panel.is_empty():
        return 1.0
    ranked = (
        panel.select("date", "symbol", factor_name)
        .sort(["date", factor_name, "symbol"], descending=[False, direction < 0, False])
        .with_columns(
            pl.col(factor_name).rank(method="average").over("date").alias("_rank"),
            pl.len().over("date").alias("_count"),
        )
        .filter(
            pl.col("_rank")
            > pl.col("_count") * (0.8 if direction > 0 else 0.0)
        )
    )
    if direction < 0:
        ranked = ranked.filter(pl.col("_rank") <= pl.col("_count") * 0.2)
    holdings = [
        set(str(value) for value in daily["symbol"].to_list())
        for daily in ranked.partition_by("date", maintain_order=True)
    ]
    if len(holdings) < 2:
        return 0.0
    values = []
    for previous, current in pairwise(holdings):
        denominator = max(len(previous), len(current), 1)
        values.append(1.0 - len(previous & current) / denominator)
    return float(np.mean(values)) if values else 0.0


def _fold_row(
    fold_index: int,
    candidate: MiningCandidate | None,
    validation_fold,
    evaluation: CandidateEvaluation | None,
    *,
    regime_state: str,
    n_dates: int,
    reason: str | None,
    evaluation_kind: str = "selected",
) -> dict[str, Any]:
    error = reason or (evaluation.error if evaluation is not None else None)
    metrics = evaluation.metrics if evaluation is not None else {}
    return {
        "candidate_signature": candidate.candidate_id if candidate is not None else None,
        "evaluation_kind": evaluation_kind,
        "fold": fold_index,
        "label": f"OOS {fold_index + 1}",
        "regime_state": regime_state,
        "n_dates": n_dates,
        "train_start": validation_fold.train_start,
        "train_end": validation_fold.train_end,
        "test_start": validation_fold.test_start,
        "test_end": validation_fold.test_end,
        "selected_factors_json": json.dumps(
            list(candidate.factor_names) if candidate is not None else [],
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "total_return": _finite_or_none(metrics.get("total_return")),
        "sharpe": _finite_or_none(metrics.get("sharpe")),
        "max_drawdown": _finite_or_none(metrics.get("max_drawdown")),
        "n_trades": _int_or_none(metrics.get("n_trades")),
        "skipped": evaluation is None or error is not None,
        "reason": error,
    }


def _validate_regime_availability(
    panel: pl.DataFrame,
    request: RuntimeRequest,
    data_dir: Path,
) -> None:
    """搜索开始前 fail-fast 校验市场环境数据。

    环境分组评估 (strong/range/weak) 依赖 T-1 市场环境序列; 数据为空或覆盖不足时,
    若拖到 artifacts 阶段才在 _regime_date_count 中抛错, 整轮嵌套搜索的计算全部浪费。
    这里用与 artifacts 相同的 fold 口径 (panel 标签) 预先构建一次 mask:
    required 区间取所有外层 fold 测试窗的并集, 覆盖后续每个 fold 单独调用的要求,
    任何 ValueError (数据为空 / 覆盖不完整) 立即带指引消息终止运行。
    """
    labels = _date_labels(panel)
    nested = generate_nested_folds(labels, request.mining_request.validation)
    if not nested:
        return
    StrategyBacktestService._build_regime_mask(
        labels,
        _REGIME_FILTERS["strong"],
        data_dir,
        required_start=date.fromisoformat(nested[0].outer.test_start),
        required_end=date.fromisoformat(nested[-1].outer.test_end),
    )


def _regime_date_count(
    panel: pl.DataFrame,
    validation_fold,
    regime_state: str,
    data_dir: Path,
) -> int:
    labels = tuple(
        label
        for label in _date_labels(panel)
        if label <= validation_fold.test_end
    )
    mask = StrategyBacktestService._build_regime_mask(
        labels,
        _REGIME_FILTERS[regime_state],
        data_dir,
        required_start=date.fromisoformat(validation_fold.test_start),
        required_end=date.fromisoformat(validation_fold.test_end),
    )
    if mask is None:
        return 0
    return sum(
        bool(allowed)
        for label, allowed in zip(labels, mask, strict=True)
        if validation_fold.test_start <= label <= validation_fold.test_end
    )


def _rank_artifact_candidates(
    candidates: Sequence[MiningCandidate],
    overall_rows: Sequence[Mapping[str, Any]],
    *,
    limit: int,
) -> list[MiningCandidate]:
    return sorted(
        candidates,
        key=lambda candidate: _candidate_artifact_rank(candidate, overall_rows),
    )[:limit]


def _candidate_artifact_rank(
    candidate: MiningCandidate,
    overall_rows: Sequence[Mapping[str, Any]],
) -> tuple[float, str]:
    sharpes = [
        row.get("sharpe")
        for row in overall_rows
        if row.get("candidate_signature") == candidate.candidate_id
        and not row.get("skipped")
        and row.get("sharpe") is not None
    ]
    mean_sharpe = _mean_or_none(sharpes)
    return (
        -(mean_sharpe if mean_sharpe is not None else float("-inf")),
        candidate.candidate_id,
    )


def _candidate_name(candidate: MiningCandidate) -> str:
    if candidate.kind == "existing_strategy":
        return f"已有策略 · {candidate.strategy_id}"
    return "因子组合 · " + " + ".join(candidate.factor_names)


def _empty_candidates_frame() -> pl.DataFrame:
    return pl.DataFrame(schema={
        "signature": pl.Utf8,
        "name": pl.Utf8,
        "kind": pl.Utf8,
        "factor_names_json": pl.Utf8,
        "strategy_id": pl.Utf8,
        "definition_json": pl.Utf8,
        "regime_state": pl.Utf8,
        "score": pl.Float64,
        "oos_return": pl.Float64,
        "oos_sharpe": pl.Float64,
        "oos_max_drawdown": pl.Float64,
        "oos_positive_fold_ratio": pl.Float64,
        "oos_n_trades": pl.Int64,
        "confidence": pl.Utf8,
        "valid_folds": pl.Int64,
        "skipped_folds": pl.Int64,
        "promoted_candidate_id": pl.Utf8,
        "published_strategy_id": pl.Utf8,
    })


def _atomic_write_parquet(frame: pl.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        frame.write_parquet(temporary)
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _panel_for_dates(panel: pl.DataFrame, labels: Sequence[str]) -> pl.DataFrame:
    return panel.filter(
        pl.col("date").cast(pl.Utf8).str.slice(0, 10).is_in(list(labels))
    )


def _date_labels(panel: pl.DataFrame) -> tuple[str, ...]:
    if panel.is_empty():
        return ()
    return tuple(
        panel.select(pl.col("date").cast(pl.Utf8).str.slice(0, 10).unique().sort())
        .to_series()
        .to_list()
    )


def _raise_if_cancelled(cancel_check: CancelCheck | None) -> None:
    if cancel_check is None:
        return
    cancelled = cancel_check() if callable(cancel_check) else cancel_check.is_set()
    if cancelled:
        raise MiningRuntimeCancelledError("mining cancelled")


def _optional_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"invalid ISO date: {value!r}") from exc


def _bounded_float(
    value: Any,
    name: str,
    minimum: float,
    maximum: float,
    *,
    exclusive_min: bool = False,
) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    valid_min = number > minimum if exclusive_min else number >= minimum
    if not math.isfinite(number) or not valid_min or number > maximum:
        left = "(" if exclusive_min else "["
        raise ValueError(f"{name} must be in {left}{minimum}, {maximum}]")
    return number


def _finite_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _int_or_none(value: Any) -> int | None:
    number = _finite_or_none(value)
    return int(number) if number is not None else None


def _mean_or_none(values: Sequence[float]) -> float | None:
    return float(np.mean(values)) if values else None


def _confidence(profile: str) -> str:
    return {"exploratory": "low", "balanced": "standard", "strict": "high"}[profile]


def _budget_exhausted(result: MiningResult) -> bool:
    if result.proxy_trials_used >= result.request.budget.max_proxy_trials:
        return True
    if result.trials_used >= result.request.budget.max_trials:
        return True
    return any("budget exhausted" in (fold.error or "") for fold in result.folds)
