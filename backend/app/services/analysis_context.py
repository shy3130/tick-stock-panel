"""K 线分析上下文（P2）。

仅消费传入的规范 Polars df，产出仅用于 AI 的内存 KlineAnalysisFrame。
不写 canonical、不调用 repository/provider、不改 stock_analyzer 主链路。

公共导出（按计划 §8.1）：
- KlineAnalysisBar / KlineFeatureRow / KlineAnalysisFrame
- build_analysis_frame
- preflight_analysis → PreflightResult（含 AppError 或通过的 frame）
- assemble_prompt（分层 + token 预算裁剪）

特征为纯函数：不改输入；frame 可序列化。
默认排除形成中 bar。
preflight 精确使用 data_incomplete / stale_input。
prompt 记录预算元数据且不重复注入等价数据。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

import polars as pl
from pydantic import BaseModel, Field

from app.errors import DATA_INCOMPLETE, STALE_INPUT, AppError

# ============================================================
# Pydantic 模型（§8.1）
# ============================================================


class KlineAnalysisBar(BaseModel):
    """单根 K 线（用于 AI 上下文）。"""

    date: date | datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    closed: bool | None = None  # 显式收盘标记；缺省由构建器推断


class KlineFeatureRow(BaseModel):
    """单根 K 线的形态特征（纯 Polars 派生）。"""

    date: date | datetime
    bar_type: str = "unknown"  # bullish | bearish | doji | strong_bull | strong_bear
    body_range_ratio: float | None = None
    upper_shadow_ratio: float | None = None
    lower_shadow_ratio: float | None = None
    close_position: float | None = None  # 0..1
    range_atr: float | None = None
    ema20_pos: float | None = None  # (close-ema20)/ema20
    ema20_slope: float | None = None  # 斜率（近期）
    overlap_prev: float | None = None  # 重叠率
    inside: bool = False
    outside: bool = False
    ii: bool = False
    iii: bool = False
    ioi: bool = False
    vol_ratio: float | None = None  # 相对均量
    dist_to_key: float | None = None  # 相对关键位距离


class KlineAnalysisFrame(BaseModel):
    """AI 专用的 K 线分析帧（§8.1 envelope）。"""

    symbol: str
    market: str
    timeframe: str
    data_as_of: datetime
    source: str
    degraded: bool = False
    adjustment: Literal["qfq", "hfq", "none"]
    bars: list[KlineAnalysisBar]
    indicators: dict[str, list[float | None]] = Field(default_factory=dict)
    features: list[KlineFeatureRow]
    warmup_bars: int
    warnings: list[str] = Field(default_factory=list)


# ============================================================
# 纯 Polars 特征函数（§8.3）
# ============================================================


def _safe_div(a: pl.Expr, b: pl.Expr) -> pl.Expr:
    return pl.when((b.is_not_null()) & (b != 0)).then(a / b).otherwise(None)


def _compute_bar_features(df: pl.DataFrame) -> pl.DataFrame:
    """纯函数：基于 OHLCV + 指标列计算形态特征列（不修改输入）。"""
    if df.is_empty():
        return df

    work = df.clone()

    # 基础
    o, h, low, c = pl.col("open"), pl.col("high"), pl.col("low"), pl.col("close")
    rng = (h - low).clip(lower_bound=1e-12)
    body = (c - o).abs()

    # bar_type
    body_ratio = body / rng
    is_doji = body_ratio < 0.1
    is_bull = c > o
    is_strong = body_ratio > 0.6
    bar_type = (
        pl.when(is_doji)
        .then(pl.lit("doji"))
        .when(is_strong & is_bull)
        .then(pl.lit("strong_bull"))
        .when(is_strong & (~is_bull))
        .then(pl.lit("strong_bear"))
        .when(is_bull)
        .then(pl.lit("bullish"))
        .otherwise(pl.lit("bearish"))
    )

    # 影线
    upper_shadow = h - pl.max_horizontal(o, c)
    lower_shadow = pl.min_horizontal(o, c) - low
    upper_ratio = upper_shadow / rng
    lower_ratio = lower_shadow / rng

    # 收盘位置
    close_pos = _safe_div(c - low, rng)

    # range / atr
    atr = pl.col("atr_14") if "atr_14" in work.columns else pl.lit(None)
    range_atr = _safe_div(rng, atr)

    # ema20 位置与斜率（优先 ema20，退化 ma20）
    ema20 = (
        pl.col("ema20")
        if "ema20" in work.columns
        else (pl.col("ma20") if "ma20" in work.columns else pl.lit(None))
    )
    ema20_pos = _safe_div(c - ema20, ema20)
    ema20_prev = ema20.shift(5).over("symbol") if "symbol" in work.columns else ema20.shift(5)
    ema20_slope = _safe_div(ema20 - ema20_prev, ema20_prev)

    # 相邻重叠率
    prev_h = h.shift(1).over("symbol") if "symbol" in work.columns else h.shift(1)
    prev_l = low.shift(1).over("symbol") if "symbol" in work.columns else low.shift(1)
    inter = pl.min_horizontal(h, prev_h) - pl.max_horizontal(low, prev_l)
    union = pl.max_horizontal(h, prev_h) - pl.min_horizontal(low, prev_l)
    overlap = _safe_div(inter.clip(lower_bound=0), union.clip(lower_bound=1e-12))

    # inside / outside（无 symbol 时按传入帧顺序计算）
    inside = ((low >= prev_l) & (h <= prev_h)).fill_null(False)
    outside = ((low <= prev_l) & (h >= prev_h)).fill_null(False)

    # ii / iii / ioi（连续形态）
    prev_inside = inside.shift(1).over("symbol") if "symbol" in work.columns else inside.shift(1)
    prev2_inside = inside.shift(2).over("symbol") if "symbol" in work.columns else inside.shift(2)
    prev_outside = outside.shift(1).over("symbol") if "symbol" in work.columns else outside.shift(1)
    ii = (inside & prev_inside).fill_null(False)
    iii = (inside & prev_inside & prev2_inside).fill_null(False)
    # ioi: inside, outside, inside
    ioi = (inside & prev_outside & prev2_inside).fill_null(False)

    # 量比（优先 vol_ma5）
    vol = pl.col("volume")
    vol_ma = (
        pl.col("vol_ma5")
        if "vol_ma5" in work.columns
        else (pl.col("vol_ma10") if "vol_ma10" in work.columns else pl.lit(None))
    )
    vol_ratio = _safe_div(vol, vol_ma)

    # 关键位距离（由外层传入 key_levels 后附加）
    dist_to_key = pl.lit(None)

    out = work.with_columns(
        [
            bar_type.alias("bar_type"),
            body_ratio.alias("body_range_ratio"),
            upper_ratio.alias("upper_shadow_ratio"),
            lower_ratio.alias("lower_shadow_ratio"),
            close_pos.alias("close_position"),
            range_atr.alias("range_atr"),
            ema20_pos.alias("ema20_pos"),
            ema20_slope.alias("ema20_slope"),
            overlap.alias("overlap_prev"),
            inside.alias("inside"),
            outside.alias("outside"),
            ii.alias("ii"),
            iii.alias("iii"),
            ioi.alias("ioi"),
            vol_ratio.alias("vol_ratio"),
            dist_to_key.alias("dist_to_key"),
        ]
    )

    feat_cols = [
        "date",
        "bar_type",
        "body_range_ratio",
        "upper_shadow_ratio",
        "lower_shadow_ratio",
        "close_position",
        "range_atr",
        "ema20_pos",
        "ema20_slope",
        "overlap_prev",
        "inside",
        "outside",
        "ii",
        "iii",
        "ioi",
        "vol_ratio",
        "dist_to_key",
    ]
    return out.select([c for c in feat_cols if c in out.columns])




def _exclude_forming_bars(
    df: pl.DataFrame, data_as_of: datetime, market: str, include_forming: bool
) -> tuple[pl.DataFrame, list[str]]:
    """默认排除形成中 bar；返回 (过滤后 df, warnings)。纯函数。"""
    warnings: list[str] = []
    if include_forming or df.is_empty():
        return df, warnings

    work = df
    if "closed" in work.columns:
        filtered = work.filter(pl.col("closed") != False)  # noqa: E712
        dropped = len(work) - len(filtered)
        if dropped > 0:
            warnings.append(f"excluded {dropped} bar(s) by explicit closed=false")
        return filtered, warnings

    # 可判定：当日且 data_as_of 未到收盘时间 → 尾部为形成中
    last = work.tail(1)
    if last.is_empty():
        return work, warnings
    last_date = last["date"][0]
    if isinstance(last_date, datetime):
        last_date = last_date.date()
    if isinstance(data_as_of, datetime):
        asof_date = data_as_of.date()
        asof_time = data_as_of.time()
    else:
        asof_date = data_as_of
        asof_time = None

    if last_date != asof_date:
        return work, warnings

    # 市场收盘时间启发式（A股 15:00，HK 16:00，其它默认 16:00）
    close_hour = 15 if market.upper() in ("CN", "A", "SH", "SZ") else 16
    if asof_time is not None and asof_time.hour < close_hour:
        # 尾部 1 根视为 forming，丢弃
        filtered = work.head(len(work) - 1)
        warnings.append("excluded forming tail bar (intraday data_as_of before market close)")
        return filtered, warnings

    return work, warnings


def _ensure_min_columns(df: pl.DataFrame) -> pl.DataFrame:
    """缺失列容错：保证后续特征计算可用列。"""
    need = ["open", "high", "low", "close"]
    out = df
    for c in need:
        if c not in out.columns:
            out = out.with_columns(pl.lit(0.0).alias(c))
    if "date" not in out.columns:
        out = out.with_columns(pl.lit(date.today()).alias("date"))
    if "volume" not in out.columns:
        out = out.with_columns(pl.lit(0.0).alias("volume"))
    # 指标列可选；缺失时特征函数会产生 None
    return out


# ============================================================
# 构建器（§8.1）
# ============================================================


def build_analysis_frame(
    df: pl.DataFrame,
    *,
    symbol: str,
    market: str,
    timeframe: str,
    data_as_of: datetime,
    source: str,
    adjustment: Literal["qfq", "hfq", "none"] = "qfq",
    degraded: bool = False,
    include_forming: bool = False,
    key_levels: list[float] | None = None,
    warmup: int = 60,
) -> KlineAnalysisFrame:
    """从传入的规范 Polars df 构建 AI 专用的 KlineAnalysisFrame。

    约束：
    - 只消费传入 df，不读仓库/外部。
    - 默认排除形成中 bar。
    - 明确填充 source / data_as_of / adjustment / degraded / warnings。
    """

    if df is None or df.is_empty():
        return KlineAnalysisFrame(
            symbol=symbol,
            market=market,
            timeframe=timeframe,
            data_as_of=data_as_of,
            source=source,
            degraded=degraded,
            adjustment=adjustment,
            bars=[],
            indicators={},
            features=[],
            warmup_bars=warmup,
            warnings=["empty_input"],
        )
    warnings: list[str] = []
    missing = [c for c in ("open", "high", "low", "close", "date", "volume") if c not in df.columns]
    if missing:
        warnings.append("missing columns: " + ", ".join(missing))

    work = _ensure_min_columns(df.clone())
    finite_ohlc = pl.all_horizontal([
        pl.col(column).cast(pl.Float64, strict=False).is_finite()
        for column in ("open", "high", "low", "close")
    ])
    valid_rows = finite_ohlc & pl.col("date").is_not_null()
    before = work.height
    work = work.filter(valid_rows)
    if work.height < before:
        warnings.append(f"dropped_non_finite_or_undated_bars:{before - work.height}")
    if "volume" in work.columns:
        volume = pl.col("volume").cast(pl.Float64, strict=False)
        work = work.with_columns(
            pl.when(volume.is_finite()).then(volume).otherwise(None).alias("volume")
        )

    # 排除形成中（默认）
    work, form_warn = _exclude_forming_bars(work, data_as_of, market, include_forming)
    warnings.extend(form_warn)

    # 排序保证顺序
    if "date" in work.columns:
        work = work.sort("date")

    # 特征（纯）
    feat = _compute_bar_features(work)
    if key_levels and "close" in work.columns:
        closes = work["close"].to_list()
        dists = [
            (min((abs(float(v) - float(k)) / float(v) for k in key_levels), default=None) if v and float(v) > 0 else None)
            for v in closes
        ]
        feat = feat.with_columns(pl.Series("dist_to_key", dists))

    # 组装 bars（仅保留必要列，避免把全部指标重复进 bars）
    bar_cols = [c for c in ("date", "open", "high", "low", "close", "volume", "closed") if c in work.columns]
    bars = [
        KlineAnalysisBar(**{k: v for k, v in row.items() if k in bar_cols})
        for row in work.select(bar_cols).to_dicts()
    ]

    # 指标：按需抽取常用列（不把全部 60+ 列塞进去）
    ind_cols = [
        c
        for c in (
            "ma5",
            "ma10",
            "ma20",
            "ma60",
            "ema5",
            "ema10",
            "ema20",
            "ema60",
            "atr_14",
            "vol_ma5",
            "vol_ma10",
            "vol_ratio_5d",
        )
        if c in work.columns
    ]
    indicators: dict[str, list[float | None]] = {}
    for c in ind_cols:
        indicators[c] = [
            (None if (v is None or (isinstance(v, float) and not math.isfinite(v))) else float(v))
            for v in work[c].to_list()
        ]

    # features 转模型
    feature_rows = [KlineFeatureRow(**r) for r in feat.to_dicts()]

    # warmup 标注（实际可用收盘 bar 数）
    warmup_bars = min(warmup, len(bars))

    return KlineAnalysisFrame(
        symbol=symbol,
        market=market,
        timeframe=timeframe,
        data_as_of=data_as_of,
        source=source,
        degraded=degraded,
        adjustment=adjustment,
        bars=bars,
        indicators=indicators,
        features=feature_rows,
        warmup_bars=warmup_bars,
        warnings=warnings,
    )


# ============================================================
# Preflight（§8.2 + §10）
# ============================================================


@dataclass
class PreflightResult:
    ok: bool
    frame: KlineAnalysisFrame | None = None
    error: AppError | None = None
    warnings: list[str] = field(default_factory=list)

def preflight_analysis(
    frame: KlineAnalysisFrame,
    *,
    purpose: str | None = None,
    min_bars: int = 60,
    stale_days: int = 7,
    expected_symbol: str | None = None,
    expected_market: str | None = None,
    expected_timeframe: str | None = None,
    now: datetime | None = None,
) -> PreflightResult:
    """Fail closed before any AI call and preserve actionable error semantics."""
    conflicts: list[str] = []
    if expected_symbol is not None and frame.symbol != expected_symbol:
        conflicts.append("symbol")
    if expected_market is not None and frame.market != expected_market:
        conflicts.append("market")
    if expected_timeframe is not None and frame.timeframe != expected_timeframe:
        conflicts.append("timeframe")
    if conflicts:
        error = AppError(DATA_INCOMPLETE, "symbol/market/timeframe 冲突: " + ", ".join(conflicts))
        return PreflightResult(ok=False, error=error, warnings=list(frame.warnings))


    warnings = list(frame.warnings)
    missing_required = {
        name
        for warning in warnings
        if warning.startswith("missing columns:")
        for name in warning.split(":", 1)[1].split(",")
        if name.strip() in {"open", "high", "low", "close", "date"}
    }
    if missing_required:
        err = AppError(
            DATA_INCOMPLETE,
            "K线缺少必要字段: " + ", ".join(sorted(name.strip() for name in missing_required)),
        )
        return PreflightResult(ok=False, frame=None, error=err, warnings=warnings)

    # 1) 收盘 K 数不足
    closed_bars = [b for b in frame.bars if (b.closed is None or b.closed)]
    if len(closed_bars) < min_bars:
        err = AppError(DATA_INCOMPLETE, f"收盘日K不足{min_bars}根（当前{len(closed_bars)}）")
        return PreflightResult(ok=False, frame=None, error=err, warnings=warnings)

    # 2) EMA/ATR warmup 不足或指标缺失
    ema_values = frame.indicators.get("ema20") or frame.indicators.get("ma20") or []
    atr_values = frame.indicators.get("atr_14") or []
    if (
        frame.warmup_bars < 20
        or not any(value is not None for value in ema_values[-20:])
        or not any(value is not None for value in atr_values[-14:])
    ):
        err = AppError(DATA_INCOMPLETE, "EMA20/ATR14 warmup 不足或指标缺失")
        return PreflightResult(ok=False, frame=None, error=err, warnings=warnings)

    # 3) freshness
    current = now or datetime.now(tz=frame.data_as_of.tzinfo)
    age_days = (current.date() - frame.data_as_of.date()).days
    if age_days > stale_days:
        err = AppError(
            STALE_INPUT,
            f"数据截止 {frame.data_as_of.date()} 距今 {age_days} 天，超过 {stale_days} 天阈值",
        )
        return PreflightResult(ok=False, frame=None, error=err, warnings=warnings)

    # 4) symbol/market/timeframe 一致性（若 bars 携带 symbol 则校验）
    # 这里简化：仅检查 frame 自身元数据非空
    if not frame.symbol or not frame.market or not frame.timeframe:
        err = AppError(DATA_INCOMPLETE, "symbol/market/timeframe 缺失")
        return PreflightResult(ok=False, frame=None, error=err, warnings=warnings)

    # 5) external fallback + 交易计划用途 → 拒绝
    is_external = frame.degraded or "external" in (frame.source or "").lower() or "fallback" in (frame.source or "").lower()
    if is_external and purpose and "plan" in purpose.lower():
        err = AppError(DATA_INCOMPLETE, "外部 fallback 不得用于交易计划用途")
        return PreflightResult(ok=False, frame=None, error=err, warnings=warnings)

    # 6) 港股未复权或字段缺失 → warning（不拒绝）
    if frame.market.upper() in ("HK", "HKG"):
        if frame.adjustment != "qfq":
            warnings.append("港股未使用 qfq，建议复权后分析")
        # 字段缺失由构建器已在 warnings 记录

    return PreflightResult(ok=True, frame=frame, error=None, warnings=warnings)


# ============================================================
# 分层 Prompt Assembler（§8.4 / §8.5）
# ============================================================


def _estimate_tokens(text: str) -> int:
    """粗略 token 估算（中英文混合场景）。"""
    if not text:
        return 0
    # 经验：中文≈1.6字/token，英文≈4字/token；取保守 3.5 字/token
    return max(1, int(len(text) / 3.5))


@dataclass
class _Section:
    name: str
    content: str
    required: bool = False  # schema/source/invariant 必须保留


def assemble_prompt(
    frame: KlineAnalysisFrame,
    *,
    purpose: str,
    user_question: str = "",
    methodology: str | None = None,
    invariants: dict[str, Any] | None = None,
    max_tokens: int = 8000,
    contract: str | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """分层组装 prompt，返回 messages + 预算元数据。

    规则：
    - 稳定 contract / allowlist 方法论 / 结构化事实 / 紧凑特征 / 用户问题 / schema 不变量。
    - 按 section 估算，预算裁剪时优先丢旧 K 线与可选内容。
    - 绝不裁剪 schema / source / invariant。
    - 记录裁剪原因。
    """
    meta: dict[str, Any] = {
        "purpose": purpose,
        "max_tokens": max_tokens,
        "trimmed": [],
        "sections": {},
    }

    # 1) contract（稳定）
    contract_text = contract or (
        "你是一位严谨的量化交易研究员。只基于提供的结构化事实与特征作分析。"
        "禁止编造数据；所有结论必须可追溯到输入的 bars/features。"
        "输出必须符合后续 schema。"
    )
    sections: list[_Section] = [
        _Section("contract", contract_text, required=True),
    ]

    # 2) methodology（allowlist 加载，调用方传入）
    if methodology:
        sections.append(_Section("methodology", methodology, required=False))

    # 3) 结构化市场事实（紧凑）
    facts = {
        "symbol": frame.symbol,
        "market": frame.market,
        "timeframe": frame.timeframe,
        "data_as_of": frame.data_as_of.isoformat() if isinstance(frame.data_as_of, datetime) else str(frame.data_as_of),
        "source": frame.source,
        "degraded": frame.degraded,
        "adjustment": frame.adjustment,
        "bar_count": len(frame.bars),
        "warmup_bars": frame.warmup_bars,
    }
    facts_text = "FACTS:\n" + "\n".join(f"{k}: {v}" for k, v in facts.items())
    sections.append(_Section("facts", facts_text, required=True))

    # 4) 紧凑 K 线特征（避免重复原始列）
    # 仅保留必要列的紧凑表示；不把 indicators 全文展开
    feat_lines: list[str] = []
    for f in frame.features[-60:]:  # 先取最近 60 根，后续预算裁剪
        feat_lines.append(
            f"{f.date}|{f.bar_type}|br={_r(f.body_range_ratio)}|us={_r(f.upper_shadow_ratio)}|"
            f"ls={_r(f.lower_shadow_ratio)}|cp={_r(f.close_position)}|ra={_r(f.range_atr)}|"
            f"ep={_r(f.ema20_pos)}|es={_r(f.ema20_slope)}|ov={_r(f.overlap_prev)}|"
            f"in={int(f.inside)}|out={int(f.outside)}|ii={int(f.ii)}|iii={int(f.iii)}|ioi={int(f.ioi)}|"
            f"vr={_r(f.vol_ratio)}|dk={_r(f.dist_to_key)}"
        )
    feat_text = "FEATURES (date|type|br|us|ls|cp|ra|ep|es|ov|in|out|ii|iii|ioi|vr|dk):\n" + "\n".join(feat_lines)
    sections.append(_Section("features", feat_text, required=False))

    # 5) 用户问题
    if user_question:
        sections.append(_Section("question", f"QUESTION:\n{user_question}", required=False))

    # 6) schema + 不变量（必须保留）
    schema_text = (
        "OUTPUT_SCHEMA: 结构化 JSON，字段见调用方 output_model。"
        f" 不变量: {invariants or '无额外不变量'}。"
        " 任何违反不变量的输出将被拒绝。"
    )
    sections.append(_Section("schema", schema_text, required=True))

    # 预算裁剪（从后向前，优先丢 features 旧行与可选 section）
    total = sum(_estimate_tokens(s.content) for s in sections)
    meta["sections"]["initial"] = total

    # 绝不裁剪 required 段；只裁 features 的旧行
    if total > max_tokens:
        # 先裁 features
        if feat_lines:
            base_tokens = total - _estimate_tokens(feat_text)
            while feat_lines:
                candidate = (
                    "FEATURES (date|type|br|us|ls|cp|ra|ep|es|ov|in|out|ii|iii|ioi|vr|dk):\n"
                    + "\n".join(feat_lines)
                )
                if _estimate_tokens(candidate) + base_tokens <= max_tokens:
                    break
                feat_lines.pop(0)
                meta["trimmed"].append("trimmed oldest feature rows")
            feat_text = (
                "FEATURES (date|type|br|us|ls|cp|ra|ep|es|ov|in|out|ii|iii|ioi|vr|dk):\n"
                + "\n".join(feat_lines)
                if feat_lines
                else ""
            )
            for index, section in enumerate(sections):
                if section.name == "features":
                    sections[index] = _Section("features", feat_text, required=False)
                    break

    # 再裁可选 section（methodology / question）
    for s in sections:
        if s.required:
            continue
        if _estimate_tokens("".join(sec.content for sec in sections)) <= max_tokens:
            break
        if s.name in ("methodology", "question"):
            meta["trimmed"].append(f"dropped section {s.name} for budget")
            s.content = ""

    user_text = "\n\n".join(
        section.content
        for section in sections
        if section.content and section.name != "contract"
    )
    final_tokens = _estimate_tokens(contract_text) + _estimate_tokens(user_text)
    meta["estimated_tokens"] = final_tokens
    meta["trimmed"] = list(dict.fromkeys(meta["trimmed"]))

    messages = [
        {"role": "system", "content": contract_text},
        {"role": "user", "content": user_text},
    ]
    return messages, meta


def _r(v: float | None) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):.4f}"
    except Exception:  # noqa: BLE001
        return "-"
