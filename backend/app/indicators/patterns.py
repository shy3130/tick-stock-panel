"""K 线形态识别 —— 纯函数,纯 numpy/polars,无 IO / 无网络。

输入: 包含 OHLC(V) 列的 polars 日 K DataFrame(内存中,通常来自 KlineRepository 缓存)。
输出: ``list[dict]``,每条命中包含:
  - pattern:     形态名(snake_case)
  - index:       在 frame 中的行位置(int)
  - date:        日期字符串(取 date 列前 10 位;无 date 列则为 None)
  - direction:   bullish / bearish / neutral —— 蜡烛图本身的倾向,非交易建议
  - confidence:  [0,1] 结构强度,越大越典型
  - evidence:    实体/影线比值、趋势等结构证据(不含交易语义)

形态定义来源: 经典日本蜡烛图(Steve Nison / TA-Lib CDL 系列)的纯 OHLC 结构判定。
go-stock 的 "形态选股" 将 MORNING_STAR/EVENING_STAR/BLACK_CLOUD_TOPS/PREGNANT/
BEARISH_ENGULFING 等作为 JSON 过滤字段下发给外部行情接口,本身不做本地计算;
本模块把这些经典形态用可证据化的比值规则在本地实现。

设计:
  - 向量化 numpy 特征 + 逐形态扫描,毫秒级。
  - 仅在数据充足且 OHLC 有限时输出;NaN/Inf/非正/高低倒挂的行跳过。
  - 不输出目标价、止损、买卖动作等交易语义字段(direction 仅描述蜡烛结构倾向)。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl

OHLC = ("open", "high", "low", "close")

# ── 结构阈值 ────────────────────────────────────────────────
DOJI_MAX_BODY_RATIO = 0.10      # 十字星:实体 / 全振幅 ≤ 10%
HAMMER_SHADOW_MULT = 2.0        # 锤子/倒锤:长影 >= 2x 实体
SMALL_UPPER_FRAC = 0.30         # 锤子:短影 ≤ 30% 全振幅
HARAMI_BODY_FRAC = 0.50         # 孕线:当前实体 ≤ 前根实体 50%
LARGE_BODY_FRAC = 0.50          # "大实体":实体 ≥ 50% 全振幅
BREAKOUT_WINDOW = 20            # 突破:回看前 N 根的高低点
TREND_PERIOD = 10               # 趋势代理:SMA 周期

# 形态 → 默认方向(供文档/校验;实际方向由结构判定)
PATTERN_DIRECTIONS = {
    "doji": "neutral",
    "hammer": "bullish",
    "inverted_hammer": "bullish",
    "engulfing_bullish": "bullish",
    "engulfing_bearish": "bearish",
    "harami": "neutral",
    "piercing": "bullish",
    "dark_cloud": "bearish",
    "morning_star": "bullish",
    "evening_star": "bearish",
    "three_white_soldiers": "bullish",
    "three_black_crows": "bearish",
    "inside_bar": "neutral",
    "breakout": "neutral",  # 实际按突破方向标注
}


# ================================================================
# 预处理
# ================================================================

def _extract_ohlc(frame: pl.DataFrame) -> tuple[np.ndarray, ...] | None:
    """提取 OHLC 为 float64 numpy 数组;缺列返回 None。"""
    for col in OHLC:
        if col not in frame.columns:
            return None
    o = np.asarray(frame.get_column("open").to_numpy(), dtype=float)
    h = np.asarray(frame.get_column("high").to_numpy(), dtype=float)
    l = np.asarray(frame.get_column("low").to_numpy(), dtype=float)
    c = np.asarray(frame.get_column("close").to_numpy(), dtype=float)
    return o, h, l, c


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


def _features(o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray):
    """逐根 K 线的结构特征(向量化)。

    返回 (body, rng, upper, lower, mid, bull, bear, body_ratio)。
    body_ratio = 实体 / 全振幅(振幅为 0 时为 nan)。
    """
    body = np.abs(c - o)
    rng = h - l
    upper = h - np.maximum(o, c)
    lower = np.minimum(o, c) - l
    mid = (o + c) / 2.0
    bull = c > o
    bear = c < o
    with np.errstate(divide="ignore", invalid="ignore"):
        body_ratio = np.where(rng > 0, body / rng, np.nan)
    return body, rng, upper, lower, mid, bull, bear, body_ratio


def _valid_mask(o, h, l, c) -> np.ndarray:
    """OHLC 有限、为正、且满足 high≥low 与高低包络的行才有效。"""
    finite = np.isfinite(o) & np.isfinite(h) & np.isfinite(l) & np.isfinite(c)
    positive = (o > 0) & (h > 0) & (l > 0) & (c > 0)
    ordered = (h >= l) & (h >= np.maximum(o, c)) & (l <= np.minimum(o, c))
    return finite & positive & ordered


def _trend(c: np.ndarray, period: int = TREND_PERIOD) -> np.ndarray:
    """SMA 趋势代理:close 在 SMA(period) 之上=up,之下=down,否则 none。

    仅作为 evidence 上下文,不门控形态判定(避免周期选择导致的漏检)。
    含 NaN 的窗口判定为 none。
    """
    n = len(c)
    labels = np.array(["none"] * n, dtype=object)
    if n < period or period < 1:
        return labels
    finite = np.isfinite(c).astype(float)
    filled = np.where(finite == 1.0, c, 0.0)
    cs = np.concatenate(([0.0], np.cumsum(filled)))
    cc = np.concatenate(([0.0], np.cumsum(finite)))
    for i in range(period - 1, n):
        s = cs[i + 1] - cs[i + 1 - period]
        k = cc[i + 1] - cc[i + 1 - period]
        if k == period and np.isfinite(c[i]):
            sma = s / period
            if c[i] > sma:
                labels[i] = "up"
            elif c[i] < sma:
                labels[i] = "down"
    return labels


def _scale(value: float, lo: float, hi: float) -> float:
    """把 value 从 [lo, hi] 线性映射到 [0, 1] 并截断。"""
    if not np.isfinite(value):
        return 0.0
    if hi <= lo:
        return 0.0 if value < lo else 1.0
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))


def _conf(floor: float, primary: float, lo: float, hi: float) -> float:
    """结构主比值映射到 [floor, 1.0] 的置信度。"""
    return round(floor + (1.0 - floor) * _scale(primary, lo, hi), 4)


@dataclass(frozen=True)
class _Bars:
    """预计算的全部特征 + 元数据,供各形态检测器共享。"""
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    body: np.ndarray
    rng: np.ndarray
    upper: np.ndarray
    lower: np.ndarray
    mid: np.ndarray
    bull: np.ndarray
    bear: np.ndarray
    body_ratio: np.ndarray
    valid: np.ndarray
    trend: np.ndarray
    dates: list
    start: int  # 扫描起始 index(lookback)
    n: int

    def tr(self, i: int) -> str:
        return str(self.trend[i]) if 0 <= i < self.n else "none"

    def hit(self, pattern: str, i: int, direction: str, confidence: float,
            evidence: dict) -> dict:
        return {
            "pattern": pattern,
            "index": int(i),
            "date": _fmt_date(self.dates[i]) if 0 <= i < len(self.dates) else None,
            "direction": direction,
            "confidence": round(float(confidence), 4),
            "evidence": {k: (round(float(v), 4)
                             if isinstance(v, (int, float)) and not isinstance(v, bool)
                             else v)
                         for k, v in evidence.items()},
        }


# ================================================================
# 单根形态
# ================================================================

def _doji(b: _Bars) -> list[dict]:
    """十字星:实体极小(≤ 全振幅 DOJI_MAX_BODY_RATIO)。方向 neutral。"""
    out = []
    for i in range(b.start, b.n):
        if not b.valid[i] or not np.isfinite(b.body_ratio[i]):
            continue
        br = b.body_ratio[i]
        if br <= DOJI_MAX_BODY_RATIO and b.rng[i] > 0:
            conf = _conf(0.4, DOJI_MAX_BODY_RATIO - br, 0.0, DOJI_MAX_BODY_RATIO)
            out.append(b.hit("doji", i, "neutral", conf, {
                "body_ratio": br, "body": b.body[i], "range": b.rng[i],
                "trend": b.tr(i),
            }))
    return out


def _hammer(b: _Bars) -> list[dict]:
    """锤子线:长下影(>= 2x 实体)、小上影、实体居上。方向 bullish。"""
    out = []
    for i in range(b.start, b.n):
        if not b.valid[i] or b.body[i] <= 0 or b.rng[i] <= 0:
            continue
        ratio = b.lower[i] / b.body[i]
        if (ratio >= HAMMER_SHADOW_MULT
                and b.upper[i] <= SMALL_UPPER_FRAC * b.rng[i]
                and b.lower[i] > 0):
            conf = _conf(0.4, ratio, HAMMER_SHADOW_MULT, 5.0)
            out.append(b.hit("hammer", i, "bullish", conf, {
                "lower_shadow_body_ratio": ratio,
                "upper_shadow": b.upper[i], "lower_shadow": b.lower[i],
                "body": b.body[i], "trend": b.tr(i),
            }))
    return out


def _inverted_hammer(b: _Bars) -> list[dict]:
    """倒锤头:长上影(>= 2x 实体)、小下影、实体居下。方向 bullish。"""
    out = []
    for i in range(b.start, b.n):
        if not b.valid[i] or b.body[i] <= 0 or b.rng[i] <= 0:
            continue
        ratio = b.upper[i] / b.body[i]
        if (ratio >= HAMMER_SHADOW_MULT
                and b.lower[i] <= SMALL_UPPER_FRAC * b.rng[i]
                and b.upper[i] > 0):
            conf = _conf(0.4, ratio, HAMMER_SHADOW_MULT, 5.0)
            out.append(b.hit("inverted_hammer", i, "bullish", conf, {
                "upper_shadow_body_ratio": ratio,
                "upper_shadow": b.upper[i], "lower_shadow": b.lower[i],
                "body": b.body[i], "trend": b.tr(i),
            }))
    return out


# ================================================================
# 双根形态
# ================================================================

def _engulfing(b: _Bars) -> list[dict]:
    """看涨/看跌吞没:当前实体完全包覆前根实体,且方向相反。"""
    out = []
    for i in range(max(b.start, 1), b.n):
        if not (b.valid[i] and b.valid[i - 1]):
            continue
        j = i - 1
        po, pc = b.o[j], b.c[j]
        co, cc = b.o[i], b.c[i]
        bprev, bcur = b.body[j], b.body[i]
        if bprev <= 0 or bcur <= bprev:
            continue
        # 看涨吞没:前根阴、当前阳、当前实体包覆前根实体
        if b.bear[j] and b.bull[i] and co <= pc and cc >= po:
            conf = _conf(0.5, bcur / bprev, 1.0, 3.0)
            out.append(b.hit("engulfing_bullish", i, "bullish", conf, {
                "prev_body": bprev, "curr_body": bcur,
                "body_ratio_curr_over_prev": bcur / bprev, "trend": b.tr(i),
            }))
        # 看跌吞没:前根阳、当前阴、当前实体包覆前根实体
        elif b.bull[j] and b.bear[i] and co >= pc and cc <= po:
            conf = _conf(0.5, bcur / bprev, 1.0, 3.0)
            out.append(b.hit("engulfing_bearish", i, "bearish", conf, {
                "prev_body": bprev, "curr_body": bcur,
                "body_ratio_curr_over_prev": bcur / bprev, "trend": b.tr(i),
            }))
    return out


def _harami(b: _Bars) -> list[dict]:
    """孕线(身怀六甲):前根大实体,当前小实体完全包含在前根实体内。

    方向随前根颜色:前阴→bullish(看涨孕线),前阳→bearish(看跌孕线)。
    """
    out = []
    for i in range(max(b.start, 1), b.n):
        if not (b.valid[i] and b.valid[i - 1]):
            continue
        j = i - 1
        bprev, bcur = b.body[j], b.body[i]
        if bprev <= 0 or bcur <= 0:
            continue
        if bcur > bprev * HARAMI_BODY_FRAC:
            continue
        hi_prev = max(b.o[j], b.c[j])
        lo_prev = min(b.o[j], b.c[j])
        hi_cur = max(b.o[i], b.c[i])
        lo_cur = min(b.o[i], b.c[i])
        if hi_cur <= hi_prev and lo_cur >= lo_prev:
            ratio = bprev / bcur
            conf = _conf(0.4, ratio, 2.0, 5.0)
            direction = "bullish" if b.bear[j] else "bearish"
            out.append(b.hit("harami", i, direction, conf, {
                "prev_body": bprev, "curr_body": bcur,
                "prev_body_over_curr": ratio, "trend": b.tr(i),
            }))
    return out


def _piercing(b: _Bars) -> list[dict]:
    """刺透形态:前根阴、当前开在前根低点之下(跳空)、收在前根实体中点之上。

    方向 bullish。
    """
    out = []
    for i in range(max(b.start, 1), b.n):
        if not (b.valid[i] and b.valid[i - 1]):
            continue
        j = i - 1
        if not (b.bear[j] and b.bull[i]):
            continue
        bprev = b.body[j]
        if bprev <= 0:
            continue
        if b.o[i] >= b.l[j]:  # 需跳空低开到前根低点之下
            continue
        mid_prev = b.mid[j]
        if b.c[i] > mid_prev and b.c[i] < b.o[j]:  # 收在中点上方但未及前开(非吞没)
            depth = (b.c[i] - mid_prev) / bprev  # [0, 0.5]
            conf = _conf(0.4, depth, 0.0, 0.5)
            out.append(b.hit("piercing", i, "bullish", conf, {
                "prev_body": bprev, "recovery_depth": depth, "trend": b.tr(i),
            }))
    return out


def _dark_cloud(b: _Bars) -> list[dict]:
    """乌云盖顶:前根阳、当前开在前根高点之上(跳空)、收在前根实体中点之下。

    方向 bearish。
    """
    out = []
    for i in range(max(b.start, 1), b.n):
        if not (b.valid[i] and b.valid[i - 1]):
            continue
        j = i - 1
        if not (b.bull[j] and b.bear[i]):
            continue
        bprev = b.body[j]
        if bprev <= 0:
            continue
        if b.o[i] <= b.h[j]:  # 需跳空高开到前根高点之上
            continue
        mid_prev = b.mid[j]
        if b.c[i] < mid_prev and b.c[i] > b.o[j]:  # 收在中点下方但未及前开(非吞没)
            depth = (mid_prev - b.c[i]) / bprev  # [0, 0.5]
            conf = _conf(0.4, depth, 0.0, 0.5)
            out.append(b.hit("dark_cloud", i, "bearish", conf, {
                "prev_body": bprev, "penetration_depth": depth, "trend": b.tr(i),
            }))
    return out


def _inside_bar(b: _Bars) -> list[dict]:
    """内包线:当前高低完全被前根高低包住。方向 neutral。"""
    out = []
    for i in range(max(b.start, 1), b.n):
        if not (b.valid[i] and b.valid[i - 1]):
            continue
        j = i - 1
        if b.rng[j] <= 0:
            continue
        if b.h[i] <= b.h[j] and b.l[i] >= b.l[j] and b.rng[i] < b.rng[j]:
            containment = (b.rng[j] - b.rng[i]) / b.rng[j]
            conf = _conf(0.3, containment, 0.0, 0.7)
            out.append(b.hit("inside_bar", i, "neutral", conf, {
                "prev_range": b.rng[j], "curr_range": b.rng[i],
                "containment": containment, "trend": b.tr(i),
            }))
    return out


# ================================================================
# 三根形态
# ================================================================

def _morning_star(b: _Bars) -> list[dict]:
    """早晨之星:大阴 + 小实体(星,收低) + 大阳收回第一根实体中点之上。"""
    out = []
    for i in range(max(b.start, 2), b.n):
        i1, i0 = i - 1, i - 2
        if not (b.valid[i] and b.valid[i1] and b.valid[i0]):
            continue
        if not (b.bear[i0] and b.bull[i]):
            continue
        if b.body_ratio[i0] < LARGE_BODY_FRAC or b.body_ratio[i] < LARGE_BODY_FRAC:
            continue
        if b.body[i1] > b.body[i0] * HARAMI_BODY_FRAC:  # 星:小实体
            continue
        if b.c[i1] >= b.c[i0]:  # 星收盘低于第一根(延续下跌)
            continue
        mid0 = b.mid[i0]
        if b.c[i] >= mid0:  # 第三根收复进入第一根实体上半
            recovery = (b.c[i] - mid0) / b.body[i0]
            conf = _conf(0.4, recovery, 0.0, 0.5)
            out.append(b.hit("morning_star", i, "bullish", conf, {
                "bar1_body_ratio": b.body_ratio[i0],
                "star_body_ratio": b.body_ratio[i1],
                "bar3_body_ratio": b.body_ratio[i],
                "recovery_depth": recovery, "trend": b.tr(i),
            }))
    return out


def _evening_star(b: _Bars) -> list[dict]:
    """黄昏之星:大阳 + 小实体(星,收高) + 大阴跌入第一根实体中点之下。"""
    out = []
    for i in range(max(b.start, 2), b.n):
        i1, i0 = i - 1, i - 2
        if not (b.valid[i] and b.valid[i1] and b.valid[i0]):
            continue
        if not (b.bull[i0] and b.bear[i]):
            continue
        if b.body_ratio[i0] < LARGE_BODY_FRAC or b.body_ratio[i] < LARGE_BODY_FRAC:
            continue
        if b.body[i1] > b.body[i0] * HARAMI_BODY_FRAC:
            continue
        if b.c[i1] <= b.c[i0]:  # 星收盘高于第一根(延续上涨)
            continue
        mid0 = b.mid[i0]
        if b.c[i] <= mid0:
            decline = (mid0 - b.c[i]) / b.body[i0]
            conf = _conf(0.4, decline, 0.0, 0.5)
            out.append(b.hit("evening_star", i, "bearish", conf, {
                "bar1_body_ratio": b.body_ratio[i0],
                "star_body_ratio": b.body_ratio[i1],
                "bar3_body_ratio": b.body_ratio[i],
                "decline_depth": decline, "trend": b.tr(i),
            }))
    return out


def _three_soldiers_crows(b: _Bars) -> list[dict]:
    """三白兵(看涨)/ 三乌鸦(看跌):三根同向大实体、渐次创新高/低、开在前根实体内。"""
    out = []
    for i in range(max(b.start, 2), b.n):
        i1, i0 = i - 1, i - 2
        if not (b.valid[i] and b.valid[i1] and b.valid[i0]):
            continue
        brs = (b.body_ratio[i0], b.body_ratio[i1], b.body_ratio[i])
        if any(not np.isfinite(x) or x < LARGE_BODY_FRAC for x in brs):
            continue
        # 三白兵
        if (b.bull[i0] and b.bull[i1] and b.bull[i]
                and b.c[i1] > b.c[i0] and b.c[i] > b.c[i1]
                and b.o[i1] >= b.o[i0] and b.o[i1] <= b.c[i0]
                and b.o[i] >= b.o[i1] and b.o[i] <= b.c[i1]):
            mean_br = sum(brs) / 3
            conf = _conf(0.5, mean_br, LARGE_BODY_FRAC, 0.85)
            out.append(b.hit("three_white_soldiers", i, "bullish", conf, {
                "mean_body_ratio": mean_br, "trend": b.tr(i),
            }))
        # 三乌鸦
        elif (b.bear[i0] and b.bear[i1] and b.bear[i]
                and b.c[i1] < b.c[i0] and b.c[i] < b.c[i1]
                and b.o[i1] <= b.o[i0] and b.o[i1] >= b.c[i0]
                and b.o[i] <= b.o[i1] and b.o[i] >= b.c[i1]):
            mean_br = sum(brs) / 3
            conf = _conf(0.5, mean_br, LARGE_BODY_FRAC, 0.85)
            out.append(b.hit("three_black_crows", i, "bearish", conf, {
                "mean_body_ratio": mean_br, "trend": b.tr(i),
            }))
    return out


# ================================================================
# 突破
# ================================================================

def _prior_extreme(arr: np.ndarray, w: int, i: int, mode: str) -> float:
    """arr[i-w : i] 的 max/min(前 w 根,不含当前);不足返回 nan。"""
    if i < w:
        return float("nan")
    seg = arr[i - w:i]
    if mode == "max":
        return float(np.nanmax(seg))
    return float(np.nanmin(seg))


def _breakout(b: _Bars, window: int) -> list[dict]:
    """突破:收盘突破前 window 根的最高价(阻力)或最低价(支撑)。

    方向随突破方向(bullish/bearish)。
    """
    out = []
    w = max(2, window)
    for i in range(max(b.start, w), b.n):
        if not b.valid[i]:
            continue
        resist = _prior_extreme(b.h, w, i, "max")
        support = _prior_extreme(b.l, w, i, "min")
        if not np.isfinite(resist) or not np.isfinite(support) or resist <= 0:
            continue
        close = b.c[i]
        ev: dict = {"window": w, "resistance": resist, "support": support,
                    "trend": b.tr(i)}
        if close > resist:
            magnitude = (close - resist) / resist
            conf = _conf(0.4, magnitude, 0.0, 0.05)
            ev["breakout_magnitude"] = magnitude
            out.append(b.hit("breakout", i, "bullish", conf, ev))
        elif close < support:
            magnitude = (support - close) / support
            conf = _conf(0.4, magnitude, 0.0, 0.05)
            ev["breakdown_magnitude"] = magnitude
            out.append(b.hit("breakout", i, "bearish", conf, ev))
    return out


# ================================================================
# 公共入口
# ================================================================

def detect_candlestick_patterns(
    frame: pl.DataFrame,
    lookback: int = 120,
    *,
    breakout_window: int = BREAKOUT_WINDOW,
    trend_period: int = TREND_PERIOD,
) -> list[dict]:
    """识别经典 K 线形态,返回命中列表(按 index, pattern 排序)。

    参数:
      frame: 含 open/high/low/close(+ 可选 date/volume)的日 K DataFrame。
      lookback: 只扫描最近 ``lookback`` 根(默认 120)。
      breakout_window: 突破形态回看前 N 根高低点(默认 20)。
      trend_period: 趋势代理 SMA 周期(默认 10),仅写入 evidence。

    返回: ``list[dict]``,每条含 pattern/index/date/direction/confidence/evidence。
    数据不足或缺列时返回空列表;NaN/Inf/无效行不产生命中。
    """
    ohlc = _extract_ohlc(frame)
    if ohlc is None:
        return []
    o, h, l, c = ohlc
    n = len(o)
    if n == 0:
        return []
    body, rng, upper, lower, mid, bull, bear, body_ratio = _features(o, h, l, c)
    valid = _valid_mask(o, h, l, c)
    trend = _trend(c, trend_period)
    dates = _extract_dates(frame)
    start = max(0, n - max(lookback, 1))

    b = _Bars(o, h, l, c, body, rng, upper, lower, mid, bull, bear,
              body_ratio, valid, trend, dates, start, n)

    hits: list[dict] = []
    hits += _doji(b)
    hits += _hammer(b)
    hits += _inverted_hammer(b)
    hits += _engulfing(b)
    hits += _harami(b)
    hits += _piercing(b)
    hits += _dark_cloud(b)
    hits += _morning_star(b)
    hits += _evening_star(b)
    hits += _three_soldiers_crows(b)
    hits += _inside_bar(b)
    hits += _breakout(b, breakout_window)
    hits.sort(key=lambda d: (d["index"], d["pattern"]))
    return hits
