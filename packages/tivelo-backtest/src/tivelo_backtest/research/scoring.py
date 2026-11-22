"""策略评分字段解析。"""
from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from typing import Any, Protocol

import numpy as np
import polars as pl

from .catalog import (
    BUILTIN_CATALOG,
    FactorLookup,
    factor_calculation_order,
    factor_warmup_bars,
)
from .catalog import (
    factor_dependencies as _registry_factor_dependencies,
)
from .catalog import (
    scoring_warmups as _registry_scoring_warmups,
)
from .catalog import (
    virtual_dependencies as _registry_virtual_dependencies,
)
from .dsl import FACTOR_COLUMN, CompiledFormula, compile_formula
from .matrix import MarketDataMatrix, SignalMatrix, make_signal_matrix, validate_signal_matrix
from .py_expr import CompiledPyExpr, compile_python_expression


class MaterializationCache(Protocol):
    """Optional caller-owned content cache; the research kernel performs no I/O."""

    def cache_key(self, factor_id: str, formula: str, version: int,
                  frame: pl.DataFrame, dependencies: Collection[str]) -> str | None: ...
    def get_cached(self, key: str, rows: int) -> pl.Series | None: ...
    def put_cached(self, key: str, column: pl.Series) -> None: ...


SCORING_DIRECTION_HIGH = "high"
SCORING_DIRECTION_LOW = "low"
SCORING_DIRECTIONS = frozenset({SCORING_DIRECTION_HIGH, SCORING_DIRECTION_LOW})

_SCORE_ASSET_CHUNK_SIZE = 256

# P1 起依赖声明与预热窗口的单一权威来源为 catalog.py;
# 本常量为兼容别名, 键集合与历史版本逐项一致 (见 tests/test_factor_registry.py 快照测试)。
VIRTUAL_SCORING_DEPENDENCIES: dict[str, frozenset[str]] = dict(_registry_virtual_dependencies())

_ROLLING_SCORING_WARMUP: dict[str, int] = dict(_registry_scoring_warmups())


def effective_scoring(
    defaults: Mapping[str, Any] | None,
    overrides: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """解析有效评分；新配置可完整替换，历史配置保持局部覆盖。"""
    override_values = (overrides or {}).get("scoring")
    if (overrides or {}).get("scoring_replace") is True:
        return dict(override_values) if isinstance(override_values, Mapping) else {}
    scoring = dict(defaults or {})
    if isinstance(override_values, Mapping):
        scoring.update(override_values)
    return scoring


def effective_scoring_directions(overrides: Mapping[str, Any] | None) -> dict[str, str]:
    values = (overrides or {}).get("scoring_directions")
    if not isinstance(values, Mapping):
        return {}
    return {
        str(name): str(direction)
        for name, direction in values.items()
        if direction in SCORING_DIRECTIONS
    }


def scoring_warmup_bars(scoring: Mapping[str, Any], *, catalog: FactorLookup = BUILTIN_CATALOG) -> int:
    return factor_warmup_bars((name for name, weight in scoring.items() if weight), catalog=catalog)


def scoring_dependencies(scoring: Mapping[str, Any], *, catalog: FactorLookup = BUILTIN_CATALOG) -> set[str]:
    """把受控虚拟评分字段展开为实际数据依赖 (含 composite/custom 递归展开)。"""
    dependencies: set[str] = set()
    for name, weight in scoring.items():
        if not weight:
            continue
        dependencies.update(_registry_factor_dependencies([str(name)], catalog=catalog))
    return dependencies


def _composite_value_expr(available: set[str], name: str, *, catalog: FactorLookup) -> pl.Expr | None:
    """复合因子值 = Σ w_i * 截面 zscore(成员值); 成员可为已物化列或虚拟因子。"""
    spec = catalog.get(name)
    if spec is None or not spec.components:
        return None
    total: pl.Expr | None = None
    for member_id, weight in spec.components:
        member_expr = (
            pl.col(member_id)
            if member_id in available
            else scoring_value_expr(available, member_id, catalog=catalog)
        )
        if member_expr is None:
            return None
        mean = member_expr.mean().over("date")
        std = member_expr.std().over("date")
        piece = pl.when(std > 0).then((member_expr - mean) / std).otherwise(None) * weight
        total = piece if total is None else total + piece
    return total


def scoring_value_expr(columns: Collection[str], name: str, *, catalog: FactorLookup = BUILTIN_CATALOG) -> pl.Expr | None:
    """返回评分值表达式；依赖不完整时返回 None。"""
    available = set(columns)
    if name in available:
        return pl.col(name)
    # composite 在 VIRTUAL 字典门控之前分派 (依赖经注册表递归展开) —— P3
    spec = catalog.get(name)
    if spec is not None and spec.kind == "composite":
        return _composite_value_expr(available, name, catalog=catalog)
    dependencies = VIRTUAL_SCORING_DEPENDENCIES.get(name)
    if dependencies is None or not dependencies.issubset(available):
        return None
    if name.startswith("ma") and name.endswith("_bias"):
        period = name.removeprefix("ma").removesuffix("_bias")
        if period.isdigit():
            return _relative(pl.col("close"), pl.col(f"ma{period}"))
    if name.startswith("ema") and name.endswith("_bias"):
        period = name.removeprefix("ema").removesuffix("_bias")
        if period.isdigit():
            return _relative(pl.col("close"), pl.col(f"ema{period}"))
    if name in {"macd_dif_pct", "macd_dea_pct", "macd_hist_pct"}:
        source = name.removesuffix("_pct")
        return _ratio(pl.col(source), pl.col("close"))
    if name == "atr_pct":
        return _ratio(pl.col("atr_14"), pl.col("close"))
    if name == "boll_position":
        return _ratio(
            pl.col("close") - pl.col("boll_lower"),
            pl.col("boll_upper") - pl.col("boll_lower"),
        )
    if name == "boll_width":
        return _ratio(pl.col("boll_upper") - pl.col("boll_lower"), pl.col("ma20"))
    if name == "vol_ratio_10d":
        return _ratio(
            pl.col("volume"),
            pl.col("volume").shift(1).rolling_mean(10).over("symbol"),
        )
    if name == "vol_trend_5_10":
        return _relative(pl.col("vol_ma5"), pl.col("vol_ma10"))
    if name == "turnover_ratio_5d":
        return _relative(
            pl.col("turnover_rate"),
            pl.col("turnover_rate").shift(1).rolling_mean(5).over("symbol"),
        )
    if name == "log_amount":
        return pl.when(pl.col("amount") >= 0).then((pl.col("amount") + 1).log()).otherwise(None)
    if name == "amount_ratio_5d":
        return _relative(
            pl.col("amount"),
            pl.col("amount").shift(1).rolling_mean(5).over("symbol"),
        )
    if name == "gap_return":
        return _relative(pl.col("open"), pl.col("prev_close"))
    if name == "intraday_return":
        return _relative(pl.col("close"), pl.col("open"))
    if name == "close_position":
        return _ratio(pl.col("close") - pl.col("low"), pl.col("high") - pl.col("low"))
    if name == "distance_to_high_60d":
        return _relative(pl.col("close"), pl.col("high_60d"))
    if name == "distance_from_low_60d":
        return _relative(pl.col("close"), pl.col("low_60d"))
    if name in {
        "max_ret_20d", "ret_skew_20d", "up_days_20d",
        "amihud_20d", "vol_price_corr_20d",
    }:
        change = _daily_change_expr()
        if name == "max_ret_20d":
            return change.rolling_max(20, min_samples=20).over("symbol")
        if name == "ret_skew_20d":
            return change.rolling_skew(20, bias=True).over("symbol")
        if name == "up_days_20d":
            return (
                (change > 0).cast(pl.Float64)
                .rolling_sum(20, min_samples=20).over("symbol")
            )
        if name == "amihud_20d":
            illiquidity = _ratio(change.abs(), pl.col("amount") / 1e8)
            return illiquidity.rolling_mean(20, min_samples=20).over("symbol")
        volume = pl.col("volume")
        product = change * volume
        return _rolling_corr_expr(change, volume, product, 20).over("symbol")
    if name == "turnover_z_60d":
        baseline = pl.col("turnover_rate").shift(1)
        mean = baseline.rolling_mean(60, min_samples=60)
        std = baseline.rolling_std(60, min_samples=60)
        return (
            pl.when(std > 0).then((pl.col("turnover_rate") - mean) / std)
            .otherwise(None)
            .over("symbol")
        )
    if name == "vwap_bias":
        vwap = _ratio(pl.col("amount"), pl.col("volume") * 100.0)
        # Amount/volume describe the actual exchange quote, independently of
        # the adjustment anchor used for historical technical indicators.
        quote = pl.col("raw_close" if "raw_close" in available else "close")
        return _relative(quote, vwap)
    if name == "vol_trend_5_60":
        fast = pl.col("volume").rolling_mean(5)
        slow = pl.col("volume").rolling_mean(60)
        return _relative(fast, slow).over("symbol")
    if name in {"limit_up_count_20d", "limit_up_count_60d"}:
        window = 20 if name == "limit_up_count_20d" else 60
        hit = (pl.col("consecutive_limit_ups").fill_null(0) > 0).cast(pl.Float64)
        return hit.rolling_sum(window, min_samples=window).over("symbol")
    # ── 扩充批次 (2026-09-05): 全部滚动窗口默认 min_samples=窗口长 (fail-closed) ──
    if name == "log_float_mv":
        # 换手率 = 成交量/流通股本 → 股本 = volume/turnover_rate, 市值 = close x 股本
        quote = pl.col("raw_close" if "raw_close" in available else "close")
        return (
            pl.when((pl.col("turnover_rate") > 0) & (pl.col("volume") > 0))
            .then((quote * pl.col("volume") / pl.col("turnover_rate")).log())
            .otherwise(None)
        )
    if name == "momentum_120d":
        return _relative(
            pl.col("close"),
            pl.col("close").shift(120),
        ).over("symbol")
    if name == "mom_accel_20_60":
        return pl.col("momentum_20d") - pl.col("momentum_60d")
    if name == "rsi_14_delta_5d":
        return pl.col("rsi_14") - pl.col("rsi_14").shift(5).over("symbol")
    if name == "overnight_ret_20d":
        overnight = _relative(pl.col("open"), pl.col("prev_close"))
        return overnight.rolling_sum(20, min_samples=20).over("symbol")
    if name == "intraday_ret_20d":
        intraday = _relative(pl.col("close"), pl.col("open"))
        return intraday.rolling_sum(20, min_samples=20).over("symbol")
    if name == "downside_vol_20d":
        downside = (_daily_change_expr().clip(upper_bound=0.0) ** 2)
        return downside.rolling_mean(20, min_samples=20).sqrt().over("symbol")
    if name == "vol_regime_5_60":
        change = _daily_change_expr()
        fast = change.rolling_std(5, min_samples=5)
        slow = change.rolling_std(60, min_samples=60)
        return _ratio(fast, slow).over("symbol")
    if name == "amplitude_trend_20_60":
        fast = pl.col("amplitude").rolling_mean(20, min_samples=20)
        slow = pl.col("amplitude").rolling_mean(60, min_samples=60)
        return _relative(fast, slow).over("symbol")
    if name == "obv_trend_20d":
        change = _daily_change_expr()
        signed = change.sign() * pl.col("volume")
        total = signed.rolling_sum(20, min_samples=20)
        scale = pl.col("volume").rolling_mean(20, min_samples=20) * 20.0
        return _ratio(total, scale).over("symbol")
    if name == "amount_mean_20d":
        return (pl.col("amount") / 1e8).rolling_mean(20, min_samples=20).over("symbol")
    if name == "turnover_mean_20d":
        return pl.col("turnover_rate").rolling_mean(20, min_samples=20).over("symbol")
    if name == "turnover_std_20d":
        mean = pl.col("turnover_rate").rolling_mean(20, min_samples=20)
        std = pl.col("turnover_rate").rolling_std(20, min_samples=20)
        return _ratio(std, mean).over("symbol")
    if name == "position_240d":
        high = pl.col("close").rolling_max(240, min_samples=240)
        low = pl.col("close").rolling_min(240, min_samples=240)
        return _ratio(pl.col("close") - low, high - low).over("symbol")
    if name == "distance_to_high_240d":
        return _relative(
            pl.col("close"),
            pl.col("close").rolling_max(240, min_samples=240),
        ).over("symbol")
    if name == "kdj_kd_diff":
        return pl.col("kdj_k") - pl.col("kdj_d")
    return None


def _daily_change_expr() -> pl.Expr:
    previous = pl.col("close").shift(1)
    return _ratio(pl.col("close"), previous) - 1.0


def _rolling_corr_expr(
    left: pl.Expr, right: pl.Expr, product: pl.Expr, window: int
) -> pl.Expr:
    """Pearson correlation over a rolling window, matching the matrix kernel formula."""
    mean_left = left.rolling_mean(window, min_samples=window)
    mean_right = right.rolling_mean(window, min_samples=window)
    mean_product = product.rolling_mean(window, min_samples=window)
    mean_left_sq = (left * left).rolling_mean(window, min_samples=window)
    mean_right_sq = (right * right).rolling_mean(window, min_samples=window)
    covariance = mean_product - mean_left * mean_right
    variance_left = mean_left_sq - mean_left * mean_left
    variance_right = mean_right_sq - mean_right * mean_right
    return pl.when(
        (variance_left > 0) & (variance_right > 0)
    ).then(
        covariance / (variance_left * variance_right).sqrt()
    ).otherwise(None)


def materialize_scoring_columns(
    frame: pl.DataFrame,
    names: Collection[str],
    *,
    catalog: FactorLookup = BUILTIN_CATALOG,
    cache: MaterializationCache | None = None,
    compile_dsl: Callable[[str], CompiledFormula] | None = None,
    compile_python: Callable[[str], CompiledPyExpr] | None = None,
) -> pl.DataFrame:
    """Compute v1 enriched columns; input must already be ordered by symbol/date.

    v1 volume is lots and turnover_rate keeps its historical percentage unit.
    This compatibility path skips unresolved definitions as the host does; it
    neither reads snapshots nor proves causal or price-scale correctness.
    """
    compile_dsl = compile_dsl or (lambda text: compile_formula(text, catalog=catalog))
    compile_python = compile_python or (lambda text: compile_python_expression(text, catalog=catalog))
    requested = set(names)
    # 按依赖层批算, 同层仍共享批量缓存命中/列附着。中间列仅在本次求值中可见。
    depths: dict[str, int] = {}
    layers: dict[int, list[str]] = {}
    for name in factor_calculation_order(requested - set(frame.columns), catalog=catalog):
        spec = catalog.get(name)
        if name in frame.columns or spec is None or spec.kind == "base":
            continue
        depth = 1 + max((depths.get(dep, 0) for dep in spec.inputs), default=0)
        depths[name] = depth
        layers.setdefault(depth, []).append(name)
    for layer in layers.values():
        frame = _materialize_scoring_layer(
            frame, layer, catalog=catalog, cache=cache,
            compile_dsl=compile_dsl, compile_python=compile_python,
        )
    return frame.drop(depths.keys() - requested, strict=False)


def _materialize_scoring_layer(
    frame: pl.DataFrame, names: Collection[str], *, catalog: FactorLookup,
    cache: MaterializationCache | None,
    compile_dsl: Callable[[str], CompiledFormula],
    compile_python: Callable[[str], CompiledPyExpr],
) -> pl.DataFrame:
    # custom (DSL) 因子先物化: frame_transform 可能需要多阶段临时列 (嵌套窗口规避),
    # 与单表达式路径不同, 必须整体走帧变换 —— 与检验/试算共用同一条计算路径 (P3)。
    # P2b: 物化结果走内容寻址缓存 (键 = 定义 + 依赖列内容), 重复求值零成本;
    # 缓存故障一律回退现算, 不影响正确性。
    # 两段式: 命中列一次批量附着 (成本与因子数无关 — 每列一次 with_columns
    # 会随 K 线性拷贝帧); 未命中的逐个现算 (帧变换有依赖顺序)。
    pending: list[tuple[str, object, str | None]] = []
    hits: dict[str, pl.Series] = {}
    for name in names:
        spec = catalog.get(str(name))
        if spec is None or spec.kind != "custom" or name in frame.columns:
            continue
        if getattr(spec, "lang", "dsl") == "python":
            compiled = compile_python(spec.formula_text)
        else:
            compiled = compile_dsl(spec.formula_text)
        if not getattr(compiled, "ok", True):
            continue
        if compiled.frame_transform is None:
            continue
        key = cache.cache_key(
            str(name), spec.formula_text, spec.version, frame,
            spec.inputs | ({"symbol", "date"} & set(frame.columns)),
        ) if cache is not None else None
        if cache is not None and key is not None:
            cached = cache.get_cached(key, frame.height)
            if cached is not None:
                hits[str(name)] = cached
                continue
        pending.append((str(name), compiled, key))
    if hits:
        frame = frame.with_columns([series.alias(name) for name, series in hits.items()])
    for name, compiled, key in pending:
        if name in frame.columns:
            continue
        transformed = compiled.frame_transform(frame)
        if transformed is None:
            continue
        column = transformed.get_column(FACTOR_COLUMN).alias(name)
        if cache is not None and key is not None:
            cache.put_cached(key, column)
        # 丢弃 DSL 分阶段临时列 — 与缓存命中路径的列集合完全一致
        # (临时列本就是变换内部工件, 不应泄漏进下游帧)。
        staged_temps = [
            c for c in transformed.columns
            if c not in frame.columns and c != FACTOR_COLUMN
        ]
        frame = transformed.drop([FACTOR_COLUMN, *staged_temps]).with_columns(column)
    expressions = [
        expression.alias(name)
        for name in names
        if name not in frame.columns
        and (expression := scoring_value_expr(frame.columns, str(name), catalog=catalog)) is not None
    ]
    return frame.with_columns(expressions) if expressions else frame


def _ratio(numerator: pl.Expr, denominator: pl.Expr) -> pl.Expr:
    return pl.when(denominator.is_not_null() & (denominator != 0)).then(
        numerator / denominator
    ).otherwise(None)


def _relative(numerator: pl.Expr, denominator: pl.Expr) -> pl.Expr:
    return _ratio(numerator, denominator) - 1.0


def build_matrix_score(
    market: MarketDataMatrix,
    universe: np.ndarray,
    scoring: dict[str, float],
    order_by: str | None,
    descending: bool,
    *,
    fallback: np.ndarray,
    directions: Mapping[str, str] | None = None,
    resolve_feature: Callable[[MarketDataMatrix, str], np.ndarray] = MarketDataMatrix.field,
) -> np.ndarray:
    """Score an explicit universe from materialized decision fields or a supplied lookup."""
    weights = {name: float(weight) for name, weight in scoring.items() if float(weight) != 0.0}
    total_weight = sum(weights.values())
    if weights and total_weight > 0:
        score = np.zeros(market.shape, dtype=np.float32)
        all_finite = universe.copy()
        row_count, asset_count = market.shape
        chunk_size = min(_SCORE_ASSET_CHUNK_SIZE, asset_count)
        finite_scratch = np.empty((row_count, chunk_size), dtype=bool)
        work_mask = np.empty((row_count, chunk_size), dtype=bool)
        value_scratch = np.empty((row_count, chunk_size), dtype=np.float32)
        for name, weight in weights.items():
            values = resolve_feature(market, name)
            row_min = np.full(row_count, np.inf, dtype=np.float32)
            row_max = np.full(row_count, -np.inf, dtype=np.float32)
            for start in range(0, asset_count, chunk_size):
                stop = min(start + chunk_size, asset_count)
                width = stop - start
                finite = finite_scratch[:, :width]
                values_chunk = values[:, start:stop]
                np.isfinite(values_chunk, out=finite)
                all_finite[:, start:stop] &= finite
                finite &= universe[:, start:stop]
                np.minimum(
                    row_min,
                    np.min(values_chunk, axis=1, where=finite, initial=np.inf),
                    out=row_min,
                )
                np.maximum(
                    row_max,
                    np.max(values_chunk, axis=1, where=finite, initial=-np.inf),
                    out=row_max,
                )
            row_range = row_max - row_min
            varying_rows = np.isfinite(row_range) & (row_range > 0)
            normalized_weight = np.float32(weight / total_weight)
            for start in range(0, asset_count, chunk_size):
                stop = min(start + chunk_size, asset_count)
                width = stop - start
                finite = finite_scratch[:, :width]
                mask = work_mask[:, :width]
                scratch = value_scratch[:, :width]
                values_chunk = values[:, start:stop]
                np.isfinite(values_chunk, out=finite)
                finite &= universe[:, start:stop]
                scratch.fill(0.0)
                np.logical_and(finite, varying_rows[:, None], out=mask)
                np.subtract(values_chunk, row_min[:, None], out=scratch, where=mask)
                np.divide(scratch, row_range[:, None], out=scratch, where=mask)
                np.logical_and(finite, ~varying_rows[:, None], out=mask)
                scratch[mask] = np.float32(0.5)
                if (directions or {}).get(name) == SCORING_DIRECTION_LOW:
                    scratch[finite] = np.float32(1.0) - scratch[finite]
                scratch *= normalized_weight
                score[:, start:stop] += scratch
        score *= np.float32(100.0)
        score[~universe | ~all_finite] = 0.0
        return score

    if order_by and order_by != "score":
        values = resolve_feature(market, order_by)
        result = np.zeros(market.shape, dtype=np.float32)
        direction = np.float32(1.0 if descending else -1.0)
        for start in range(0, market.shape[1], _SCORE_ASSET_CHUNK_SIZE):
            stop = min(start + _SCORE_ASSET_CHUNK_SIZE, market.shape[1])
            values_chunk = values[:, start:stop]
            valid = universe[:, start:stop] & np.isfinite(values_chunk)
            np.multiply(
                values_chunk,
                direction,
                out=result[:, start:stop],
                where=valid,
            )
        return result
    result = np.zeros(market.shape, dtype=np.float32)
    np.copyto(result, fallback, where=universe)
    return result


def apply_matrix_selection(
    market: MarketDataMatrix,
    signals: SignalMatrix,
    candidates: np.ndarray | None = None,
    *,
    scoring: dict[str, float] | None = None,
    order_by: str | None = None,
    descending: bool = True,
    directions: Mapping[str, str] | None = None,
    resolve_feature: Callable[[MarketDataMatrix, str], np.ndarray] = MarketDataMatrix.field,
) -> SignalMatrix:
    """Apply the legacy candidate filter and framework scoring to strategy signals."""
    validate_signal_matrix(signals, market.shape)
    entry = signals.entry.astype(bool)
    if candidates is not None:
        if candidates.shape != market.shape:
            raise ValueError("candidate mask shape does not match market")
        entry &= candidates
    score = build_matrix_score(
        market,
        entry,
        scoring or {},
        order_by,
        descending,
        fallback=signals.score,
        directions=directions,
        resolve_feature=resolve_feature,
    )
    return make_signal_matrix(
        market.shape,
        entry=entry.astype(np.uint8),
        exit=signals.exit,
        score=score,
        entry_signal_code=np.where(entry, signals.entry_signal_code, -1).astype(np.int16),
        exit_signal_code=np.where(signals.exit != 0, signals.exit_signal_code, -1).astype(np.int16),
        entry_signal_ids=signals.entry_signal_ids,
        exit_signal_ids=signals.exit_signal_ids,
    )
