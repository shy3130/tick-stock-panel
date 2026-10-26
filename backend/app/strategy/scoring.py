"""策略评分字段解析。"""
from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

import polars as pl

from app.factors.registry import (
    factor_dependencies as _registry_factor_dependencies,
)
from app.factors.registry import get_factor as _registry_get_factor
from app.factors.registry import scoring_warmups as _registry_scoring_warmups
from app.factors.registry import virtual_dependencies as _registry_virtual_dependencies

SCORING_DIRECTION_HIGH = "high"
SCORING_DIRECTION_LOW = "low"
SCORING_DIRECTIONS = frozenset({SCORING_DIRECTION_HIGH, SCORING_DIRECTION_LOW})

# P1 起依赖声明与预热窗口的单一权威来源为 app/factors/registry.py;
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


def scoring_warmup_bars(scoring: Mapping[str, Any]) -> int:
    warmups: list[int] = [
        _ROLLING_SCORING_WARMUP.get(str(name), 1)
        for name, weight in scoring.items()
        if weight
    ]
    # composite/custom 因子的预热来自注册表 (P3)
    for name, weight in scoring.items():
        if not weight:
            continue
        spec = _registry_get_factor(str(name))
        if spec is not None and spec.kind in ("custom", "composite"):
            warmups.append(spec.warmup_bars)
    return max(warmups, default=1)


def scoring_dependencies(scoring: Mapping[str, Any]) -> set[str]:
    """把受控虚拟评分字段展开为实际数据依赖 (含 composite/custom 递归展开)。"""
    dependencies: set[str] = set()
    for name, weight in scoring.items():
        if not weight:
            continue
        dependencies.update(_registry_factor_dependencies([str(name)]))
    return dependencies


def _composite_value_expr(available: set[str], name: str) -> pl.Expr | None:
    """复合因子值 = Σ w_i * 截面 zscore(成员值); 成员可为已物化列或虚拟因子。"""
    spec = _registry_get_factor(name)
    if spec is None or not spec.components:
        return None
    total: pl.Expr | None = None
    for member_id, weight in spec.components:
        member_expr = (
            pl.col(member_id)
            if member_id in available
            else scoring_value_expr(available, member_id)
        )
        if member_expr is None:
            return None
        mean = member_expr.mean().over("date")
        std = member_expr.std().over("date")
        piece = pl.when(std > 0).then((member_expr - mean) / std).otherwise(None) * weight
        total = piece if total is None else total + piece
    return total


def scoring_value_expr(columns: Collection[str], name: str) -> pl.Expr | None:
    """返回评分值表达式；依赖不完整时返回 None。"""
    available = set(columns)
    if name in available:
        return pl.col(name)
    # composite 在 VIRTUAL 字典门控之前分派 (依赖经注册表递归展开) —— P3
    spec = _registry_get_factor(name)
    if spec is not None and spec.kind == "composite":
        return _composite_value_expr(available, name)
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
        return _relative(pl.col("close"), vwap)
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
        return (
            pl.when((pl.col("turnover_rate") > 0) & (pl.col("volume") > 0))
            .then((pl.col("close") * pl.col("volume") / pl.col("turnover_rate")).log())
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
) -> pl.DataFrame:
    # custom (DSL) 因子先物化: frame_transform 可能需要多阶段临时列 (嵌套窗口规避),
    # 与单表达式路径不同, 必须整体走帧变换 —— 与检验/试算共用同一条计算路径 (P3)。
    # P2b: 物化结果走内容寻址缓存 (键 = 定义 + 依赖列内容), 重复求值零成本;
    # 缓存故障一律回退现算, 不影响正确性。
    from app.factors.dsl import FACTOR_COLUMN, compile_formula_cached
    from app.factors.materialize_cache import cache_key, get_cached, put_cached
    from app.factors.py_expr import compile_python_expression_cached

    # 两段式: 命中列一次批量附着 (成本与因子数无关 — 每列一次 with_columns
    # 会随 K 线性拷贝帧); 未命中的逐个现算 (帧变换有依赖顺序)。
    pending: list[tuple[str, object, str | None]] = []
    hits: dict[str, pl.Series] = {}
    for name in names:
        spec = _registry_get_factor(str(name))
        if spec is None or spec.kind != "custom" or name in frame.columns:
            continue
        if getattr(spec, "lang", "dsl") == "python":
            compiled = compile_python_expression_cached(spec.formula_text)
        else:
            compiled = compile_formula_cached(spec.formula_text)
        if not getattr(compiled, "ok", True):
            continue
        if compiled.frame_transform is None:
            continue
        key = cache_key(
            str(name), spec.formula_text, spec.version, frame,
            set(spec.dependencies) | set(compiled.referenced_factors),
        )
        if key is not None:
            cached = get_cached(key, frame.height)
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
        if key is not None:
            put_cached(key, column)
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
        and (expression := scoring_value_expr(frame.columns, str(name))) is not None
    ]
    return frame.with_columns(expressions) if expressions else frame


def _ratio(numerator: pl.Expr, denominator: pl.Expr) -> pl.Expr:
    return pl.when(denominator.is_not_null() & (denominator != 0)).then(
        numerator / denominator
    ).otherwise(None)


def _relative(numerator: pl.Expr, denominator: pl.Expr) -> pl.Expr:
    return _ratio(numerator, denominator) - 1.0
