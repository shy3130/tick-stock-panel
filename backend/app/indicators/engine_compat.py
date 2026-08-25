"""engine/technicals 兼容计算层 — Polars 实现。

完整移植 engine/technicals 的 22 个缺口指标到 tickflow 的 Polars 流水线,
与 engine Go 源码保持数值一致 (EMA alpha=2/(N+1), adjust=False;
MA/HHV/LLV/STD/SUM 使用完整窗口 min_samples=N; TAQ STD 使用 ddof=0;
DMI 平滑周期 2N-1; KTN 使用 TR 的 20 日简单均值)。

设计:
  - 输入契约: symbol, date, open, high, low, close, volume
  - 按 symbol 分组 (.over("symbol")), 不同标的绝不串行
  - 空 DataFrame 返回空 DataFrame, 不创建临时列
  - 临时列在返回前删除, 只保留 41 个公开指标列
  - 零分母/NaN/Inf 结果由末尾 is_finite gate 统一转 null
"""
from __future__ import annotations

import logging
from datetime import date

import polars as pl

logger = logging.getLogger(__name__)

# ── 常量 ────────────────────────────────────────────────
ENGINE_COMPAT_HISTORY_DAYS = 300
ENGINE_COMPAT_WARMUP_CALENDAR_DAYS = 330
ENGINE_COMPAT_MAX_WINDOW = 120

# ── 41 个公开输出列 (column → 中文标签) ─────────────────
ENGINE_COMPAT_COLUMNS: dict[str, str] = {
    "expma_12":  "EXPMA12日指数均线",       "expma_50":  "EXPMA50日指数均线",
    "trix":      "TRIX三重指数平滑",         "trix_ma":   "TRIX信号线",
    "bbi":       "BBI多空指标",              "dfma_dif":  "DFMA平行线差",
    "dfma":      "DFMA信号线",               "dmi_pdi":   "DMI多方方向指标",
    "dmi_mdi":   "DMI空方方向指标",          "dmi_adx":   "DMI趋势ADX",
    "dmi_adxr":  "DMI评估ADXR",              "xsii_upper":"薛斯通道II上轨",
    "xsii_lower":"薛斯通道II下轨",           "xsii_mid":  "薛斯通道II中轨",
    "wr_14":     "威廉指标WR14",              "cci_14":    "CCI商品通道指标14",
    "psy_12":    "PSY心理线12",              "psyma_6":   "PSY心理线均线6",
    "bias_6":    "BIAS乖离率6",              "bias_12":   "BIAS乖离率12",
    "bias_24":   "BIAS乖离率24",             "roc_12":    "ROC变动率12",
    "roc_ma_6":  "ROC均线6",                 "mtm_12":    "MTM动量12",
    "mtm_ma_6":  "MTM均线6",                 "dpo_20":    "DPO区间震荡20",
    "dpo_ma_6":  "DPO均线6",                 "ktn_mid":   "肯特纳通道中轨",
    "ktn_upper": "肯特纳通道上轨",           "ktn_lower": "肯特纳通道下轨",
    "taq_mid":   "趋势波动通道中轨",         "taq_upper": "趋势波动通道上轨",
    "taq_lower": "趋势波动通道下轨",         "obv":       "OBV能量潮",
    "vr_26":     "VR容量比率26",             "emv_14":    "EMV简易波动14",
    "emv_ma_14": "EMV均线14",                "mfi_14":    "MFI资金流量14",
    "cr_26":     "CR能量指标26",             "mass_9_25": "MASS梅斯线",
    "asi":       "ASI累计振动升降",
}

ENGINE_COMPAT_COLUMNS_BY_CATEGORY: dict[str, list[str]] = {
    "engine_trend": [
        "expma_12", "expma_50", "trix", "trix_ma", "bbi",
        "dfma_dif", "dfma", "dmi_pdi", "dmi_mdi", "dmi_adx", "dmi_adxr",
        "xsii_upper", "xsii_lower", "xsii_mid",
    ],
    "engine_oscillator": [
        "wr_14", "cci_14", "psy_12", "psyma_6",
        "bias_6", "bias_12", "bias_24",
        "roc_12", "roc_ma_6", "mtm_12", "mtm_ma_6",
        "dpo_20", "dpo_ma_6",
    ],
    "engine_channel": [
        "ktn_mid", "ktn_upper", "ktn_lower",
        "taq_mid", "taq_upper", "taq_lower",
    ],
    "engine_volume": [
        "obv", "vr_26", "emv_14", "emv_ma_14",
        "mfi_14", "cr_26", "mass_9_25", "asi",
    ],
}

# ── 盘中递推状态列 ────────────────────────────────────────
ENGINE_COMPAT_LIVE_STATE_COLUMNS: frozenset[str] = frozenset({
    "_ec_open_hist", "_ec_high_hist", "_ec_low_hist",
    "_ec_close_hist", "_ec_volume_hist",
    "_ec_expma_12", "_ec_expma_50",
    "_ec_trix_e1", "_ec_trix_e2", "_ec_trix_e3",
    "_ec_ktn_mid",
    "_ec_dmi_mtr", "_ec_dmi_dmp", "_ec_dmi_dmm",
    "_ec_obv", "_ec_asi",
    "_ec_trix_hist", "_ec_dmi_dx_hist", "_ec_dmi_adx_hist",
})


# ── 辅助 ─────────────────────────────────────────────────
def _alpha(n: int) -> float:
    return 2.0 / (n + 1)


def _ema(col: pl.Expr, n: int) -> pl.Expr:
    return col.ewm_mean(alpha=_alpha(n), adjust=False, min_samples=1).over("symbol")


def _clean(col: pl.Expr) -> pl.Expr:
    return pl.when(col.is_finite()).then(col).otherwise(None)


def _tr() -> pl.Expr:
    pc = pl.col("close").shift(1).over("symbol")
    return pl.max_horizontal(
        pl.col("high") - pl.col("low"),
        (pc - pl.col("high")).abs(),
        (pc - pl.col("low")).abs(),
    )


def compute_engine_compat_indicators(df: pl.DataFrame) -> pl.DataFrame:
    """计算 22 个 engine 兼容指标的 41 个输出列。"""
    if df.is_empty():
        return df

    df = df.sort(["symbol", "date"])
    c, h, l, v, o = pl.col("close"), pl.col("high"), pl.col("low"), pl.col("volume"), pl.col("open")
    s = "symbol"
    pc = c.shift(1).over(s)

    df = df.with_columns(_tr().alias("_ec_tr"))
    typ = (h + l + c) / 3

    # ── 趋势 ─────────────────────────────────────────────
    expma_12 = _ema(c, 12)
    expma_50 = _ema(c, 50)

    e1 = _ema(c, 12)
    e2 = _ema(e1, 12)
    e3 = _ema(e2, 12)
    trix = (e3 - e3.shift(1).over(s)) / e3.shift(1).over(s) * 100
    trix_ma = trix.rolling_mean(9).over(s)

    bbi = sum(c.rolling_mean(n).over(s) for n in (3, 6, 12, 24)) / 4

    dfma_dif = c.rolling_mean(10).over(s) - c.rolling_mean(50).over(s)
    dfma = dfma_dif.rolling_mean(10).over(s)

    # DMI (N=14, smooth=2N-1=27, M=6)
    ds = 27
    hd = h - h.shift(1).over(s)
    ld = l.shift(1).over(s) - l
    mtr = _ema(pl.col("_ec_tr"), ds)
    dmp = _ema(pl.when((hd > 0) & (hd > ld)).then(hd).otherwise(0.0), ds)
    dmm = _ema(pl.when((ld > 0) & (ld > hd)).then(ld).otherwise(0.0), ds)
    pdi = dmp * 100 / mtr
    mdi = dmm * 100 / mtr
    dx = (mdi - pdi).abs() / (pdi + mdi) * 100
    adx = dx.rolling_mean(6).over(s)
    adxr = (adx + adx.shift(6).over(s)) / 2

    xsii_upper = h.rolling_max(120).over(s)
    xsii_lower = l.rolling_min(120).over(s)
    xsii_mid = c.rolling_mean(20).over(s)

    # ── 震荡 ─────────────────────────────────────────────
    wr_hhv = h.rolling_max(14).over(s)
    wr_llv = l.rolling_min(14).over(s)
    wr_14 = (wr_hhv - c) / (wr_hhv - wr_llv) * 100

    # CCI: AVEDEV 固定展开 (14 项绝对偏差求和 / 14)
    tm14 = typ.rolling_mean(14).over(s)
    avedev = pl.sum_horizontal(
        (typ.shift(k).over(s) - tm14).abs() for k in range(14)
    ) / 14
    cci_14 = (typ - tm14) / (avedev * 0.015)

    up = (c > pc).fill_null(False).cast(pl.Int32)
    psy_12 = up.rolling_sum(12).over(s).fill_null(0).cast(pl.Float64) / 12 * 100
    psyma_6 = psy_12.rolling_mean(6).over(s)

    def _bias(n: int) -> pl.Expr:
        ma = c.rolling_mean(n).over(s)
        return (c - ma) / ma * 100

    roc_ref = c.shift(12).over(s)
    roc_12 = (c - roc_ref) / roc_ref * 100
    roc_ma_6 = roc_12.rolling_mean(6).over(s)

    mtm_12 = c - c.shift(12).over(s)
    mtm_ma_6 = mtm_12.rolling_mean(6).over(s)

    dpo_ref = c.rolling_mean(20).over(s).shift(20 // 2 + 1).over(s)
    dpo_20 = c - dpo_ref
    dpo_ma_6 = dpo_20.rolling_mean(6).over(s)

    # ── 通道 ─────────────────────────────────────────────
    ktn_mid = _ema(typ, 20)
    ktn_ch = pl.col("_ec_tr").rolling_mean(20).over(s)
    ktn_upper = ktn_mid + ktn_ch * 2
    ktn_lower = ktn_mid - ktn_ch * 2

    taq_mid = typ.rolling_mean(20).over(s)
    taq_std = typ.rolling_std(20, ddof=0).over(s)
    taq_upper = taq_mid + taq_std * 2
    taq_lower = taq_mid - taq_std * 2

    # ── 量价 ─────────────────────────────────────────────
    obv_signed = pl.when(c > pc).then(v).otherwise(
        pl.when(c < pc).then(-v).otherwise(0.0)
    )
    obv = obv_signed.cum_sum().over(s)

    vr_th = pl.when(c > pc).then(v).otherwise(0.0).rolling_sum(26).over(s)
    vr_tl = pl.when(c < pc).then(v).otherwise(0.0).rolling_sum(26).over(s)
    vr_tq = pl.when(c == pc).then(v).otherwise(0.0).rolling_sum(26).over(s)
    vr_26 = (vr_th * 2 + vr_tq) * 100 / (vr_tl * 2 + vr_tq)

    emv_mid = (h + l) / 2 - (h.shift(1).over(s) + l.shift(1).over(s)) / 2
    emv_br = pl.max_horizontal(h - l, (pc - l).abs())
    emv_14 = emv_mid * v / emv_br / 100
    emv_ma_14 = emv_14.rolling_mean(14).over(s)

    mfi_mr = pl.when(typ > typ.shift(1).over(s)).then(typ * v).otherwise(0.0).rolling_sum(14).over(s)
    mfi_nr = pl.when(typ < typ.shift(1).over(s)).then(typ * v).otherwise(0.0).rolling_sum(14).over(s)
    mfi_14 = mfi_mr * 100 / (mfi_mr + mfi_nr)

    # CR: Go 的 MAX(NaN,0) 返回 NaN, 毒化 SUM; 用 null→NaN 模拟
    cr_mid_s = typ.shift(1).over(s)
    cr_raw_up = h - cr_mid_s
    cr_raw_lo = cr_mid_s - l
    cr_up = pl.when(cr_raw_up > 0).then(cr_raw_up).when(cr_raw_up <= 0).then(0.0).otherwise(float("nan"))
    cr_lo = pl.when(cr_raw_lo > 0).then(cr_raw_lo).when(cr_raw_lo <= 0).then(0.0).otherwise(float("nan"))
    cr_26 = cr_up.rolling_sum(26).over(s) / cr_lo.rolling_sum(26).over(s) * 100

    mass_diff = h - l
    mass_ma1 = mass_diff.rolling_mean(9).over(s)
    mass_ma2 = mass_ma1.rolling_mean(9).over(s)
    mass_9_25 = (mass_ma1 / mass_ma2).rolling_sum(25).over(s)

    asi_a = (h - pc).abs()
    asi_b = (l - pc).abs()
    asi_c = (h - l.shift(1).over(s)).abs()
    asi_d = (pc - o.shift(1).over(s)).abs()
    asi_r = pl.max_horizontal(asi_a, asi_b) + pl.max_horizontal(asi_c, asi_d) / 2
    asi_x = (c - pc) + (c - o) / 2 + (pc - o.shift(1).over(s)) / 4
    asi_si = pl.when(asi_r > 0).then(asi_x / asi_r * 50).otherwise(0.0)
    asi = asi_si.cum_sum().over(s)

    # ── 写入 + 清理 ──────────────────────────────────────
    df = df.with_columns([
        expma_12.alias("expma_12"), expma_50.alias("expma_50"),
        trix.alias("trix"), trix_ma.alias("trix_ma"),
        bbi.alias("bbi"),
        dfma_dif.alias("dfma_dif"), dfma.alias("dfma"),
        pdi.alias("dmi_pdi"), mdi.alias("dmi_mdi"),
        adx.alias("dmi_adx"), adxr.alias("dmi_adxr"),
        xsii_upper.alias("xsii_upper"), xsii_lower.alias("xsii_lower"), xsii_mid.alias("xsii_mid"),
        wr_14.alias("wr_14"), cci_14.alias("cci_14"),
        psy_12.alias("psy_12"), psyma_6.alias("psyma_6"),
        _bias(6).alias("bias_6"), _bias(12).alias("bias_12"), _bias(24).alias("bias_24"),
        roc_12.alias("roc_12"), roc_ma_6.alias("roc_ma_6"),
        mtm_12.alias("mtm_12"), mtm_ma_6.alias("mtm_ma_6"),
        dpo_20.alias("dpo_20"), dpo_ma_6.alias("dpo_ma_6"),
        ktn_mid.alias("ktn_mid"), ktn_upper.alias("ktn_upper"), ktn_lower.alias("ktn_lower"),
        taq_mid.alias("taq_mid"), taq_upper.alias("taq_upper"), taq_lower.alias("taq_lower"),
        obv.alias("obv"), vr_26.alias("vr_26"),
        emv_14.alias("emv_14"), emv_ma_14.alias("emv_ma_14"),
        mfi_14.alias("mfi_14"), cr_26.alias("cr_26"),
        mass_9_25.alias("mass_9_25"), asi.alias("asi"),
    ])

    df = df.with_columns([_clean(pl.col(col)).alias(col) for col in ENGINE_COMPAT_COLUMNS])
    df = df.drop([c for c in ["_ec_tr"] if c in df.columns])
    return df


def build_engine_compat_live_state(history: pl.DataFrame, as_of: date) -> pl.DataFrame:
    """从完整历史计算盘中递推状态。

    只在历史满足 120 根时才写状态；否则不返回该 symbol。
    """
    if history.is_empty():
        return pl.DataFrame()

    hist = history.filter(pl.col("date") <= as_of).sort(["symbol", "date"])
    # 如果 history 已包含 engine compat 列 (来自 enriched history cache), 直接复用
    if "expma_12" in history.columns and "asi" in history.columns:
        full = hist
    else:
        full = compute_engine_compat_indicators(hist)
    last = full.group_by("symbol", maintain_order=True).last()

    counts = hist.group_by("symbol").agg(pl.len().alias("_ec_n"))
    last = last.join(counts, on="symbol", how="inner").filter(pl.col("_ec_n") >= 120).drop("_ec_n")
    if last.is_empty():
        return pl.DataFrame()

    s = "symbol"

    # 标量递推值
    state = last.select(
        s,
        pl.col("expma_12").alias("_ec_expma_12"),
        pl.col("expma_50").alias("_ec_expma_50"),
        pl.col("ktn_mid").alias("_ec_ktn_mid"),
        pl.col("obv").alias("_ec_obv"),
        pl.col("asi").alias("_ec_asi"),
    )

    # TRIX triple-EMA 中间值
    trix_df = (
        hist.sort([s, "date"])
        .with_columns(pl.col("close").ewm_mean(alpha=_alpha(12), adjust=False, min_samples=1).over(s).alias("_e1"))
        .with_columns(pl.col("_e1").ewm_mean(alpha=_alpha(12), adjust=False, min_samples=1).over(s).alias("_e2"))
        .with_columns(pl.col("_e2").ewm_mean(alpha=_alpha(12), adjust=False, min_samples=1).over(s).alias("_e3"))
        .group_by(s, maintain_order=True).last()
        .select(s, pl.col("_e1").alias("_ec_trix_e1"), pl.col("_e2").alias("_ec_trix_e2"), pl.col("_e3").alias("_ec_trix_e3"))
    )
    state = state.join(trix_df, on=s, how="inner")

    # DMI MTR/DMP/DMM
    ds = 27
    hd = pl.col("high") - pl.col("high").shift(1).over(s)
    ld = pl.col("low").shift(1).over(s) - pl.col("low")
    pcv = pl.col("close").shift(1).over(s)
    trr = pl.max_horizontal(
        pl.col("high") - pl.col("low"),
        (pcv - pl.col("high")).abs(),
        (pcv - pl.col("low")).abs(),
    )
    dmi_df = (
        hist.sort([s, "date"])
        .with_columns(trr.over(s).alias("_tr"))
        .with_columns([
            trr.over(s).ewm_mean(alpha=_alpha(ds), adjust=False, min_samples=1).over(s).alias("_mtr"),
            pl.when((hd > 0) & (hd > ld)).then(hd).otherwise(0.0)
                .ewm_mean(alpha=_alpha(ds), adjust=False, min_samples=1).over(s).alias("_dmp"),
            pl.when((ld > 0) & (ld > hd)).then(ld).otherwise(0.0)
                .ewm_mean(alpha=_alpha(ds), adjust=False, min_samples=1).over(s).alias("_dmm"),
        ])
        .group_by(s, maintain_order=True).last()
        .select(s, pl.col("_mtr").alias("_ec_dmi_mtr"), pl.col("_dmp").alias("_ec_dmi_dmp"), pl.col("_dmm").alias("_ec_dmi_dmm"))
    )
    state = state.join(dmi_df, on=s, how="inner")

    # trix_hist (前 8 个 trix 值), dmi_dx_hist (前 5), dmi_adx_hist (前 6)
    trix_hist = (
        full.select(s, "trix").filter(pl.col("trix").is_not_null())
        .group_by(s).agg(pl.col("trix").tail(8).alias("_ec_trix_hist"))
    )
    state = state.join(trix_hist, on=s, how="inner")

    dx_expr = (full["dmi_mdi"] - full["dmi_pdi"]).abs() / (full["dmi_pdi"] + full["dmi_mdi"]) * 100
    dx_hist = (
        full.select(s).with_columns(dx_expr.alias("_dx"))
        .filter(pl.col("_dx").is_not_null() & pl.col("_dx").is_finite())
        .group_by(s).agg(pl.col("_dx").tail(5).alias("_ec_dmi_dx_hist"))
    )
    state = state.join(dx_hist, on=s, how="inner")

    adx_hist = (
        full.select(s, "dmi_adx").filter(pl.col("dmi_adx").is_not_null())
        .group_by(s).agg(pl.col("dmi_adx").tail(6).alias("_ec_dmi_adx_hist"))
    )
    state = state.join(adx_hist, on=s, how="inner")

    # 119 根原始 OHLCV 尾部
    tail_n = ENGINE_COMPAT_MAX_WINDOW - 1
    raw_tail = (
        hist.sort([s, "date"]).group_by(s).agg([
            pl.col("open").tail(tail_n).alias("_ec_open_hist"),
            pl.col("high").tail(tail_n).alias("_ec_high_hist"),
            pl.col("low").tail(tail_n).alias("_ec_low_hist"),
            pl.col("close").tail(tail_n).alias("_ec_close_hist"),
            pl.col("volume").tail(tail_n).alias("_ec_volume_hist"),
        ])
    )
    state = state.join(raw_tail, on=s, how="inner")
    return state


def compute_engine_compat_today(
    live_state: pl.DataFrame, today_ohlcv: pl.DataFrame
) -> pl.DataFrame:
    """用递推状态 + 今日 OHLCV 增量计算今日 engine 兼容指标。

    缺少任一兼容状态列时返回空 DataFrame。
    """
    if live_state.is_empty() or today_ohlcv.is_empty():
        return pl.DataFrame()
    if not ENGINE_COMPAT_LIVE_STATE_COLUMNS.issubset(live_state.columns):
        return pl.DataFrame()

    s = "symbol"

    # 从状态列表 + 今日 OHLCV 重建 120 根历史, 全量重算窗口指标
    joined = live_state.join(today_ohlcv, on=s, how="inner")
    if joined.is_empty():
        return pl.DataFrame()

    # live_state 经 _build_live_agg 以 LEFT JOIN 注入 engine compat 状态:
    # 未通过 warmup 的标的其 _ec_*_hist 列为 null。重建窗口前剔除这些行,
    # 否则下方 len(row["_ec_open_hist"]) 会触发 TypeError: NoneType has no len(),
    # 中断整批增量计算 (历史 enriched NoneType 异常根因)。这些标的仅缺 41 列
    # engine/technicals 兼容指标 (运行时计算, 非持久化), 不影响主链路。
    _hist_cols = (
        "_ec_open_hist", "_ec_high_hist", "_ec_low_hist",
        "_ec_close_hist", "_ec_volume_hist",
    )
    joined = joined.filter(
        pl.all_horizontal([pl.col(c).is_not_null() for c in _hist_cols])
    )
    if joined.is_empty():
        return pl.DataFrame()

    rows = joined.to_dicts()
    parts: list[pl.DataFrame] = []
    for row in rows:
        n = len(row["_ec_open_hist"])
        parts.append(pl.DataFrame({
            s: [row[s]] * (n + 1),
            "date": [row.get("date", date(2000, 1, 1))] * (n + 1),
            "open":   list(row["_ec_open_hist"]) + [row["open"]],
            "high":   list(row["_ec_high_hist"]) + [row["high"]],
            "low":    list(row["_ec_low_hist"]) + [row["low"]],
            "close":  list(row["_ec_close_hist"]) + [row["close"]],
            "volume": list(row["_ec_volume_hist"]) + [row["volume"]],
        }))
    rebuilt = compute_engine_compat_indicators(pl.concat(parts, how="vertical"))
    window_today = rebuilt.group_by(s, maintain_order=True).last()

    # EMA 递推列 (120 根窗口 EMA 与全历史 EMA 有偏差, 用状态值增量校正)
    a12, a50, a20, ads = _alpha(12), _alpha(50), _alpha(20), _alpha(27)

    df = joined.with_columns([
        (a12 * pl.col("close") + (1 - a12) * pl.col("_ec_expma_12")).alias("expma_12"),
        (a50 * pl.col("close") + (1 - a50) * pl.col("_ec_expma_50")).alias("expma_50"),
    ])

    # TRIX triple-EMA 递推
    te1 = a12 * pl.col("close") + (1 - a12) * pl.col("_ec_trix_e1")
    te2 = a12 * te1 + (1 - a12) * pl.col("_ec_trix_e2")
    te3 = a12 * te2 + (1 - a12) * pl.col("_ec_trix_e3")
    trix = (te3 - pl.col("_ec_trix_e3")) / pl.col("_ec_trix_e3") * 100
    trix_ma = (pl.col("_ec_trix_hist").list.sum() + trix) / (pl.col("_ec_trix_hist").list.len() + 1)

    # KTN: mid 用递推, channel 宽度用重建窗口值
    typ_today = (pl.col("high") + pl.col("low") + pl.col("close")) / 3
    ktn_mid_inc = a20 * typ_today + (1 - a20) * pl.col("_ec_ktn_mid")
    ktn_ch_w = (window_today["ktn_upper"] - window_today["ktn_mid"]) / 2
    ktn_upper_inc = ktn_mid_inc + ktn_ch_w * 2
    ktn_lower_inc = ktn_mid_inc - ktn_ch_w * 2

    # DMI 递推
    hd_t = pl.col("high") - pl.col("_ec_high_hist").list.last()
    ld_t = pl.col("_ec_low_hist").list.last() - pl.col("low")
    pc_last = pl.col("_ec_close_hist").list.last()
    tr_t = pl.max_horizontal(
        pl.col("high") - pl.col("low"),
        (pc_last - pl.col("high")).abs(),
        (pc_last - pl.col("low")).abs(),
    )
    mtr_t = ads * tr_t + (1 - ads) * pl.col("_ec_dmi_mtr")
    dmp_t = pl.when((hd_t > 0) & (hd_t > ld_t)).then(hd_t).otherwise(0.0)
    dmm_t = pl.when((ld_t > 0) & (ld_t > hd_t)).then(ld_t).otherwise(0.0)
    dmp_inc = ads * dmp_t + (1 - ads) * pl.col("_ec_dmi_dmp")
    dmm_inc = ads * dmm_t + (1 - ads) * pl.col("_ec_dmi_dmm")
    pdi_inc = dmp_inc * 100 / mtr_t
    mdi_inc = dmm_inc * 100 / mtr_t
    dx_t = (mdi_inc - pdi_inc).abs() / (pdi_inc + mdi_inc) * 100
    adx_inc = (pl.col("_ec_dmi_dx_hist").list.sum() + dx_t) / (pl.col("_ec_dmi_dx_hist").list.len() + 1)
    adxr_inc = (adx_inc + pl.col("_ec_dmi_adx_hist").list.first()) / 2

    # OBV / ASI 递推
    obv_delta = pl.when(pl.col("close") > pc_last).then(pl.col("volume")).otherwise(
        pl.when(pl.col("close") < pc_last).then(-pl.col("volume")).otherwise(0.0)
    )
    obv_inc = pl.col("_ec_obv") + obv_delta

    prev_open = pl.col("_ec_open_hist").list.last()
    prev_low = pl.col("_ec_low_hist").list.last()
    asi_a = (pl.col("high") - pc_last).abs()
    asi_b = (pl.col("low") - pc_last).abs()
    asi_c = (pl.col("high") - prev_low).abs()
    asi_d = (pc_last - prev_open).abs()
    asi_r = pl.max_horizontal(asi_a, asi_b) + pl.max_horizontal(asi_c, asi_d) / 2
    asi_x = (pl.col("close") - pc_last) + (pl.col("close") - pl.col("open")) / 2 + (pc_last - prev_open) / 4
    asi_si = pl.when(asi_r > 0).then(asi_x / asi_r * 50).otherwise(0.0)
    asi_inc = pl.col("_ec_asi") + asi_si

    # 组合: EMA 递推列 + 重建窗口列
    ema_cols = df.with_columns([
        trix.alias("trix"), trix_ma.alias("trix_ma"),
        ktn_mid_inc.alias("ktn_mid"), ktn_upper_inc.alias("ktn_upper"), ktn_lower_inc.alias("ktn_lower"),
        pdi_inc.alias("dmi_pdi"), mdi_inc.alias("dmi_mdi"),
        adx_inc.alias("dmi_adx"), adxr_inc.alias("dmi_adxr"),
        obv_inc.alias("obv"), asi_inc.alias("asi"),
    ]).select([s, "expma_12", "expma_50", "trix", "trix_ma",
               "ktn_mid", "ktn_upper", "ktn_lower",
               "dmi_pdi", "dmi_mdi", "dmi_adx", "dmi_adxr", "obv", "asi"])

    skip = set(ema_cols.columns[1:])
    window_select = [s] + [c for c in ENGINE_COMPAT_COLUMNS if c not in skip]
    window_part = window_today.select(window_select)

    result = ema_cols.join(window_part, on=s, how="inner")
    result = result.with_columns([_clean(pl.col(col)).alias(col) for col in ENGINE_COMPAT_COLUMNS if col in result.columns])
    return result
