"""异动事件识别 —— 纯函数,纯 numpy/polars,无 IO / 无网络。

移植自 go-stock 的 "股票异动" 概念。go-stock 的异动数据完全来自东方财富
``getAllStockChanges`` 接口(外部实时推送:火箭发射 / 高台跳水 / 向上缺口 /
封涨停板 等),本身不做本地计算;本模块把异动 schema 与本地可证据化的
OHLCV 规则结合,实现纯函数识别。

支持事件类型:
  - price_spike:  日内大幅涨跌(close 相对前收涨跌幅超过阈值)
  - volume_surge: 放量(成交量超过 N 日均量的倍数)
  - gap:          跳空(open 相对前收偏离超过阈值)
  - limit_move:   涨停/跌停(close 触及涨跌停价)

输入: 含 open/high/low/close(+ 可选 volume/date)的 polars 日 K DataFrame。
输出: ``list[dict]``,每条事件包含:
  - symbol:       证券代码(由调用方传入)
  - event_type:   price_spike / volume_surge / gap / limit_move
  - occurred_at:  日期字符串(取 date 列前 10 位;无 date 列则为 None)
  - index:        在 frame 中的行位置(int)
  - direction:    bullish / bearish / neutral —— 结构倾向,非交易建议
  - magnitude:    事件强度指标(涨跌幅 %、量比、缺口 % 等)
  - evidence:     量化证据(不含交易语义)
  - source:       来源标签(local:ohlcv:detect_events)

设计:
  - 向量化 numpy 特征 + 逐事件扫描,毫秒级。
  - 仅在数据充足且 OHLCV 有限时输出;NaN/Inf/非正/高低倒挂的行跳过。
  - 涨跌停价按四舍五入到分(2 位小数)计算,容忍 0.5 分浮点误差。
  - 不输出目标价、止损、买卖动作等交易语义字段(direction 仅描述结构倾向)。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl

OHLC = ("open", "high", "low", "close")

# ── 结构阈值 ────────────────────────────────────────────────
PRICE_SPIKE_PCT = 5.0       # 日内涨跌幅 |Δ| ≥ 5%
VOLUME_SURGE_RATIO = 2.0    # 量比 ≥ 2×
VOLUME_AVG_WINDOW = 20      # 均量回看窗口
GAP_PCT = 2.0               # 跳空 |Δ| ≥ 2%
LIMIT_PCT = 10.0            # 涨跌停幅度(A 股主板默认 10%)
LIMIT_PRICE_TOL = 0.005     # 涨跌停价比较容差(0.5 分)

EVENT_TYPES = ("price_spike", "volume_surge", "gap", "limit_move")
SOURCE_TAG = "local:ohlcv:detect_events"

# 禁止出现在 evidence 中的交易语义键(与 patterns.py 保持一致)
FORBIDDEN_EVIDENCE_KEYS = frozenset({
    "target_price", "target", "stop_loss", "stop", "entry_price", "entry",
    "exit_price", "exit", "action", "position", "order", "buy", "sell",
    "hold", "recommendation", "advice", "qty", "quantity",
})


# ================================================================
# 预处理
# ================================================================

def _extract_ohlcv(frame: pl.DataFrame) -> tuple[np.ndarray, ...] | None:
    """提取 OHLC(V) 为 float64 numpy 数组;缺 OHLC 列返回 None。"""
    for col in OHLC:
        if col not in frame.columns:
            return None
    o = frame.get_column("open").cast(pl.Float64, strict=False).to_numpy()
    h = frame.get_column("high").cast(pl.Float64, strict=False).to_numpy()
    l = frame.get_column("low").cast(pl.Float64, strict=False).to_numpy()
    c = frame.get_column("close").cast(pl.Float64, strict=False).to_numpy()
    if "volume" in frame.columns:
        v = frame.get_column("volume").cast(pl.Float64, strict=False).to_numpy()
    else:
        v = np.full(len(o), np.nan)
    return o, h, l, c, v


def _extract_dates(frame: pl.DataFrame) -> list:
    """提取 date 列(任意类型);无则返回空 list。"""
    if "date" not in frame.columns:
        return []
    return list(frame.get_column("date").to_list())


def _fmt_date(v) -> str | None:
    if v is None:
        return None
    s = str(v)
    return s[:10] if len(s) >= 10 else s


def _valid_mask(o, h, l, c) -> np.ndarray:
    """OHLC 有限、为正、且满足 high≥low 与高低包络的行才有效。"""
    finite = np.isfinite(o) & np.isfinite(h) & np.isfinite(l) & np.isfinite(c)
    positive = (o > 0) & (h > 0) & (l > 0) & (c > 0)
    ordered = (h >= l) & (h >= np.maximum(o, c)) & (l <= np.minimum(o, c))
    return finite & positive & ordered


def _shifted(arr: np.ndarray) -> np.ndarray:
    """前移一位(前收);首元素为 nan。"""
    out = np.empty_like(arr)
    if len(arr) > 1:
        out[1:] = arr[:-1]
    if len(arr) > 0:
        out[0] = np.nan
    return out


@dataclass(frozen=True)
class _Ctx:
    """预计算特征 + 元数据,供各事件检测器共享。"""
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    prev_c: np.ndarray        # 前收(右移一位)
    valid: np.ndarray         # OHLC 有效行
    vol_valid: np.ndarray     # volume 有限且非负
    dates: list
    symbol: str
    start: int                # 扫描起始 index(lookback)
    n: int

    def occurred_at(self, i: int) -> str | None:
        return _fmt_date(self.dates[i]) if 0 <= i < len(self.dates) else None

    def event(self, event_type: str, i: int, direction: str,
              magnitude: float, evidence: dict) -> dict:
        return {
            "symbol": self.symbol,
            "event_type": event_type,
            "occurred_at": self.occurred_at(i),
            "index": int(i),
            "direction": direction,
            "magnitude": round(float(magnitude), 4),
            "evidence": {k: (round(float(v), 4)
                             if isinstance(v, (int, float)) and not isinstance(v, bool)
                             else v)
                         for k, v in evidence.items()},
            "source": SOURCE_TAG,
        }


# ================================================================
# 单事件检测器
# ================================================================

def _price_spike(ctx: _Ctx, threshold: float) -> list[dict]:
    """日内大幅涨跌:|change_pct| ≥ threshold。"""
    out: list[dict] = []
    for i in range(max(1, ctx.start), ctx.n):
        if not (ctx.valid[i] and ctx.valid[i - 1]):
            continue
        pc = ctx.prev_c[i]
        if not np.isfinite(pc) or pc <= 0:
            continue
        change_pct = (ctx.c[i] - pc) / pc * 100.0
        if abs(change_pct) < threshold:
            continue
        direction = "bullish" if change_pct > 0 else "bearish"
        out.append(ctx.event(
            "price_spike", i, direction, abs(change_pct),
            {"change_pct": change_pct, "close": ctx.c[i], "prev_close": pc},
        ))
    return out


def _volume_surge(ctx: _Ctx, ratio: float, window: int) -> list[dict]:
    """放量:当日量 / 前 window 日均量 ≥ ratio。"""
    out: list[dict] = []
    window = max(1, window)
    lo = max(window, ctx.start)
    for i in range(lo, ctx.n):
        if not (ctx.valid[i] and ctx.vol_valid[i]):
            continue
        seg = ctx.v[i - window:i]
        if not np.all(np.isfinite(seg)) or np.any(seg < 0):
            continue
        avg = float(np.mean(seg))
        if avg <= 0:
            continue
        vr = ctx.v[i] / avg
        if vr < ratio:
            continue
        # 当日价格方向仅作上下文,不门控放量判定
        if ctx.valid[i - 1] and np.isfinite(ctx.prev_c[i]) and ctx.prev_c[i] > 0:
            change_pct = (ctx.c[i] - ctx.prev_c[i]) / ctx.prev_c[i] * 100.0
            direction = "bullish" if change_pct > 0 else (
                "bearish" if change_pct < 0 else "neutral")
            ev = {"volume": ctx.v[i], "avg_volume": avg,
                  "volume_ratio": vr, "window": window,
                  "change_pct": change_pct}
        else:
            direction = "neutral"
            ev = {"volume": ctx.v[i], "avg_volume": avg,
                  "volume_ratio": vr, "window": window}
        out.append(ctx.event("volume_surge", i, direction, vr, ev))
    return out


def _gap(ctx: _Ctx, threshold: float) -> list[dict]:
    """跳空:|open / prev_close - 1| × 100 ≥ threshold。"""
    out: list[dict] = []
    for i in range(max(1, ctx.start), ctx.n):
        if not (ctx.valid[i] and ctx.valid[i - 1]):
            continue
        pc = ctx.prev_c[i]
        if not np.isfinite(pc) or pc <= 0:
            continue
        gap_pct = (ctx.o[i] - pc) / pc * 100.0
        if abs(gap_pct) < threshold:
            continue
        direction = "bullish" if gap_pct > 0 else "bearish"
        out.append(ctx.event(
            "gap", i, direction, abs(gap_pct),
            {"gap_pct": gap_pct, "open": ctx.o[i], "prev_close": pc},
        ))
    return out


def _limit_move(ctx: _Ctx, limit_pct: float, tol: float) -> list[dict]:
    """涨停/跌停:close 触及涨跌停价(按四舍五入到分计算,容忍 tol)。"""
    out: list[dict] = []
    factor = limit_pct / 100.0
    for i in range(max(1, ctx.start), ctx.n):
        if not (ctx.valid[i] and ctx.valid[i - 1]):
            continue
        pc = ctx.prev_c[i]
        if not np.isfinite(pc) or pc <= 0:
            continue
        change_pct = (ctx.c[i] - pc) / pc * 100.0
        up_price = round(pc * (1 + factor), 2)
        down_price = round(pc * (1 - factor), 2)
        if ctx.c[i] >= up_price - tol:
            side, direction, limit_price = "up", "bullish", up_price
        elif ctx.c[i] <= down_price + tol:
            side, direction, limit_price = "down", "bearish", down_price
        else:
            continue
        out.append(ctx.event(
            "limit_move", i, direction, abs(change_pct),
            {
                "side": side,
                "limit_pct": limit_pct,
                "limit_price": limit_price,
                "close": ctx.c[i],
                "prev_close": pc,
                "change_pct": change_pct,
            },
        ))
    return out


# ================================================================
# 公共入口
# ================================================================

def detect_events(
    frame: pl.DataFrame,
    symbol: str,
    *,
    lookback: int = 120,
    price_spike_pct: float = PRICE_SPIKE_PCT,
    volume_surge_ratio: float = VOLUME_SURGE_RATIO,
    volume_avg_window: int = VOLUME_AVG_WINDOW,
    gap_pct: float = GAP_PCT,
    limit_pct: float = LIMIT_PCT,
    limit_price_tol: float = LIMIT_PRICE_TOL,
) -> list[dict]:
    """识别异动事件,返回事件列表(按 index, event_type 排序)。

    参数:
      frame:              含 open/high/low/close(+ 可选 volume/date)的日 K DataFrame。
      symbol:             证券代码,写入每条事件的 ``symbol`` 字段。
      lookback:           只扫描最近 ``lookback`` 根(默认 120)。
      price_spike_pct:    price_spike 涨跌幅阈值 %(默认 5)。
      volume_surge_ratio: volume_surge 量比阈值(默认 2×)。
      volume_avg_window:  volume_surge 均量回看窗口(默认 20)。
      gap_pct:            gap 跳空阈值 %(默认 2)。
      limit_pct:          limit_move 涨跌停幅度 %(默认 10,主板)。
      limit_price_tol:    limit_move 涨跌停价比较容差(默认 0.005 = 0.5 分)。

    返回: ``list[dict]``,每条含 symbol/event_type/occurred_at/index/direction/
    magnitude/evidence/source。数据不足或缺列时返回空列表;NaN/Inf/无效行不产生事件。
    """
    ohlcv = _extract_ohlcv(frame)
    if ohlcv is None:
        return []
    o, h, l, c, v = ohlcv
    n = len(o)
    if n < 2:
        return []
    valid = _valid_mask(o, h, l, c)
    vol_finite = np.isfinite(v)
    vol_valid = vol_finite & (v >= 0)
    prev_c = _shifted(c)
    dates = _extract_dates(frame)
    start = max(0, n - max(lookback, 1))

    ctx = _Ctx(o, h, l, c, v, prev_c, valid, vol_valid, dates, symbol, start, n)

    events: list[dict] = []
    events += _price_spike(ctx, price_spike_pct)
    if np.any(vol_finite):
        events += _volume_surge(ctx, volume_surge_ratio, volume_avg_window)
    events += _gap(ctx, gap_pct)
    events += _limit_move(ctx, limit_pct, limit_price_tol)
    events.sort(key=lambda d: (d["index"], d["event_type"]))
    return events
