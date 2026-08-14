"""高级技术指标（来源：go-stock/frontend/src/components/kline/calc.ts）。

按 go-stock calc.ts 的窗口、默认参数和空值语义移植为 Polars 批量计算函数。
每个函数接收 OHLCV DataFrame 并返回附加指标列的 DataFrame；不访问网络。
"""
from __future__ import annotations

import math
from typing import Callable

import polars as pl


def _ema(x: pl.Expr, n: int) -> pl.Expr:
    return x.ewm_mean(alpha=2.0 / (n + 1), adjust=False, min_samples=n)

def _ema_values(values: list[float | None], period: int) -> list[float | None]:
    out: list[float | None] = []
    ema: float | None = None
    alpha = 2.0 / (period + 1)
    for i, value in enumerate(values):
        if value is None or not math.isfinite(value):
            out.append(None)
            continue
        if ema is None:
            if i < period - 1:
                out.append(None)
                continue
            window = values[i - period + 1 : i + 1]
            if any(v is None or not math.isfinite(v) for v in window):
                out.append(None)
                continue
            ema = sum(float(v) for v in window) / period
        else:
            ema = float(value) * alpha + ema * (1 - alpha)
        out.append(ema)
    return out

def _apply(df: pl.DataFrame, name: str, fn: Callable[[list[float]], list[float]]) -> pl.DataFrame:
    """Apply a stateful calc to one (already ordered) column."""
    vals = df.get_column(name).to_list()
    out = fn(vals)
    return df.with_columns(pl.Series(name=f"_{name}_advanced_tmp", values=out, dtype=pl.Float64))


def supertrend(df: pl.DataFrame, atr_period: int = 10, multiplier: float = 3.0) -> pl.DataFrame:
    h, l, c = (df.get_column(x).to_list() for x in ("high", "low", "close"))
    atr: list[float | None] = [None] * len(c)
    tr: list[float] = []
    for i in range(len(c)):
        tr.append(h[i] - l[i] if i == 0 else max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    if len(c) >= atr_period:
        atr[atr_period - 1] = sum(tr[:atr_period]) / atr_period
        for i in range(atr_period, len(c)):
            atr[i] = (atr[i - 1] * (atr_period - 1) + tr[i]) / atr_period
    st: list[float | None] = [None] * len(c); direction: list[int | None] = [None] * len(c)
    up = lo = None; prev_dir = 0
    for i, a in enumerate(atr):
        if a is None: continue
        ru, rl = (h[i] + l[i]) / 2 + multiplier * a, (h[i] + l[i]) / 2 - multiplier * a
        if up is not None and i and ru >= up and c[i - 1] <= up: ru = up
        if lo is not None and i and rl <= lo and c[i - 1] >= lo: rl = lo
        di = 1 if prev_dir == 0 else (1 if c[i] >= rl else -1) if prev_dir == 1 else (1 if c[i] > ru else -1)
        st[i], direction[i], up, lo, prev_dir = (rl if di == 1 else ru), di, ru, rl, di
    return df.with_columns([pl.Series("supertrend", st, dtype=pl.Float64), pl.Series("supertrend_direction", direction, dtype=pl.Int8)])


def kama(df: pl.DataFrame, period: int = 10, fast_period: int = 2, slow_period: int = 30) -> pl.DataFrame:
    c = df.get_column("close").to_list(); out = [None] * len(c)
    if len(c) > period:
        k = (2 / (fast_period + 1), 2 / (slow_period + 1)); cur = c[period]; out[period] = cur
        for i in range(period + 1, len(c)):
            vol = sum(abs(c[i-j] - c[i-j-1]) for j in range(period)); er = abs(c[i] - c[i-period]) / vol if vol else 0
            sc = (er * (k[0] - k[1]) + k[1]) ** 2; cur += sc * (c[i] - cur); out[i] = cur
    return df.with_columns(pl.Series("kama", out, dtype=pl.Float64))


def cmf(df: pl.DataFrame, period: int = 20) -> pl.DataFrame:
    r = pl.col("high") - pl.col("low")
    mfv = pl.when(r > 0).then(((pl.col("close") - pl.col("low")) - (pl.col("high") - pl.col("close"))) / r * pl.col("volume")).otherwise(0.0)
    return df.with_columns((mfv.rolling_sum(period) / pl.col("volume").rolling_sum(period)).alias("cmf"))


def aroon(df: pl.DataFrame, period: int = 25) -> pl.DataFrame:
    # arg_max/min are offsets from the window start; convert to bars since extreme.
    hi = pl.col("high").rolling_map(lambda s: s.arg_max(), window_size=period, min_samples=period)
    lo = pl.col("low").rolling_map(lambda s: s.arg_min(), window_size=period, min_samples=period)
    return df.with_columns([((period - 1 - hi) * 100 / (period - 1)).alias("aroon_up"), ((period - 1 - lo) * 100 / (period - 1)).alias("aroon_down")])
def cmo(df: pl.DataFrame, period: int = 14) -> pl.DataFrame:
    d = pl.col("close").diff()
    up = pl.when(d > 0).then(d).otherwise(0.0)
    dn = pl.when(d < 0).then(-d).otherwise(0.0)
    su, sd = up.rolling_sum(period), dn.rolling_sum(period)
    den = su + sd
    return df.with_columns(pl.when(den > 0).then((su - sd) / den * 100).otherwise(0.0).alias("cmo"))
def force_index(df: pl.DataFrame, period: int = 13) -> pl.DataFrame:
    c = df.get_column("close").to_list(); v = df.get_column("volume").to_list()
    raw = [0.0] + [(c[i] - c[i - 1]) * v[i] for i in range(1, len(c))]
    return df.with_columns(pl.Series("force_index", _ema_values(raw, period), dtype=pl.Float64))


def dema(df: pl.DataFrame, period: int = 21) -> pl.DataFrame:
    e1 = _ema_values(df.get_column("close").to_list(), period)
    e2 = _ema_values([x if x is not None else 0.0 for x in e1], period)
    out = [2 * a - b if a is not None and b is not None else None for a, b in zip(e1, e2)]
    return df.with_columns(pl.Series("dema", out, dtype=pl.Float64))
def tema(df: pl.DataFrame, period: int = 21) -> pl.DataFrame:
    c = df.get_column("close").to_list()
    e1 = _ema_values(c, period)
    e2 = _ema_values([x if x is not None else 0.0 for x in e1], period)
    e3 = _ema_values([x if x is not None else 0.0 for x in e2], period)
    out = [a + (a - b) + ((a - b) - (b - c3)) if a is not None and b is not None and c3 is not None else None for a, b, c3 in zip(e1, e2, e3)]
    return df.with_columns(pl.Series("tema", out, dtype=pl.Float64))


def hull_ma(df: pl.DataFrame, period: int = 9) -> pl.DataFrame:
    half, root = period // 2, max(1, int(math.sqrt(period)))
    def wma(e: pl.Expr, n: int) -> pl.Expr:
        return e.rolling_map(lambda s: sum((i + 1) * float(v) for i, v in enumerate(s)) / (n * (n + 1) / 2), window_size=n, min_samples=n)
    return df.with_columns((wma(2 * wma(pl.col("close"), half) - wma(pl.col("close"), period), root)).alias("hull_ma"))


def choppiness_index(df: pl.DataFrame, period: int = 14) -> pl.DataFrame:
    pc = pl.col("close").shift(1); tr = pl.max_horizontal(pl.col("high") - pl.col("low"), (pl.col("high") - pc).abs(), (pl.col("low") - pc).abs())
    return df.with_columns((100 * (tr.rolling_sum(period).log() - (pl.col("high").rolling_max(period) - pl.col("low").rolling_min(period)).log()) / math.log(period)).alias("choppiness_index"))


def elder_ray(df: pl.DataFrame, ema_period: int = 13) -> pl.DataFrame:
    e = _ema(pl.col("close"), ema_period); return df.with_columns(e.alias("_e")).with_columns([(pl.col("high") - pl.col("_e")).alias("elder_bull_power"), (pl.col("low") - pl.col("_e")).alias("elder_bear_power")]).drop("_e")


def chaikin_osc(df: pl.DataFrame, fast_period: int = 3, slow_period: int = 10) -> pl.DataFrame:
    ad = pl.when(pl.col("high") != pl.col("low")).then(((2 * pl.col("close") - pl.col("low") - pl.col("high")) / (pl.col("high") - pl.col("low")) * pl.col("volume"))).otherwise(0.0).cum_sum()
    return df.with_columns(ad.alias("_ad")).with_columns((_ema(pl.col("_ad"), fast_period) - _ema(pl.col("_ad"), slow_period)).alias("chaikin_osc")).drop("_ad")


def mass_index(df: pl.DataFrame, ema_period: int = 9, ema_period2: int = 9, sum_period: int = 25) -> pl.DataFrame:
    r = pl.col("high") - pl.col("low"); e1 = _ema(r, ema_period)
    return df.with_columns(e1.alias("_e1")).with_columns(_ema(pl.col("_e1"), ema_period).alias("_e2")).with_columns((pl.col("_e1") / pl.col("_e2")).alias("_ratio")).with_columns(_ema(pl.col("_ratio"), ema_period2).rolling_sum(sum_period).alias("mass_index")).drop(["_e1", "_e2", "_ratio"])


def ulcer_index(df: pl.DataFrame, period: int = 14) -> pl.DataFrame:
    mx = pl.col("close").rolling_max(period); dd = (pl.col("close") - mx) / mx * 100
    return df.with_columns((dd.pow(2).rolling_mean(period).sqrt()).alias("ulcer_index"))


def coppock_curve(df: pl.DataFrame, wma_len: int = 10, roc1: int = 14, roc2: int = 11) -> pl.DataFrame:
    a = (pl.col("close") / pl.col("close").shift(roc1) - 1) * 100; b = (pl.col("close") / pl.col("close").shift(roc2) - 1) * 100; s = a + b
    def w(e: pl.Expr, n: int) -> pl.Expr:
        return e.rolling_map(lambda x: sum((i+1)*float(v) for i,v in enumerate(x))/(n*(n+1)/2), window_size=n, min_samples=n)
    return df.with_columns(w(s, wma_len).alias("coppock_curve"))


INDICATORS = {"supertrend": supertrend, "kama": kama, "cmf": cmf, "aroon": aroon, "cmo": cmo, "force_index": force_index, "dema": dema, "tema": tema, "hull_ma": hull_ma, "choppiness_index": choppiness_index, "elder_ray": elder_ray, "chaikin_osc": chaikin_osc, "mass_index": mass_index, "ulcer_index": ulcer_index, "coppock_curve": coppock_curve}
