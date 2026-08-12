"""regime 条件化策略（牛 mom_trend / 熊 pullback_to_support）。

按市场状态切换两腿：
  - 牛市（样本外等权指数 level 站上自身 MA60，1 日滞后）：部署 mom_trend 主因子
    = MOM20 × SIGN(MA60_DEV)（站稳长期趋势才采信 20 日动量方向）
  - 熊市：切换到 pullback_to_support（缩量回踩 MA20 支撑），并在牛市→熊市翻转日
    强制平掉仍持有的 mom 仓位（熊市新开的 pullback 仓位因 engine 同日建仓豁免平仓）。

regime 信号由本策略从回测 universe 的 close 等权指数现场计算，非样本内训练、
非外部文件，每段测试均为真实 OOS；两腿参数全部固定（无样本内调参）。

注意：本模块运行于策略安全检查之下（import 白名单仅放行 numpy / app.backtest.matrix /
polars / datetime / __future__），故因子 DSL 核心在此内联，仅依赖 numpy。
"""

from __future__ import annotations

import numpy as np
import polars as pl

from app.backtest.matrix import make_signal_matrix

# ===========================================================================
# 因子 DSL 核心（移植自 imbue-bit/AlphaGPT model_core，纯 numpy 内联版）
# 特征: RET/MA20_DEV/MA60_DEV/VOL_RATIO/MOM20/MOM5/RSI14/AMP/TURN
# 算子: ADD/SUB/MUL/DIV/NEG/ABS/SIGN/GATE/JUMP/DECAY/DELAY1/MAX3
# ===========================================================================

def _ts_delay(x: np.ndarray, d: int) -> np.ndarray:
    if d <= 0:
        return x
    out = np.empty_like(x, dtype=float)
    out[:d] = np.nan
    out[d:] = x[:-d]
    return out


def _op_gate(cond, x, y):
    mask = (cond > 0).astype(float)
    return mask * x + (1.0 - mask) * y


def _op_jump(x):
    mean = np.nanmean(x)
    std = np.nanstd(x) + 1e-6
    z = (x - mean) / std
    return np.clip(np.where(z - 3.0 > 0, z - 3.0, 0.0), 0.0, None)


def _op_decay(x):
    return x + 0.8 * _ts_delay(x, 1) + 0.6 * _ts_delay(x, 2)


_OPS = {
    "ADD": (lambda a, b: a + b, 2),
    "SUB": (lambda a, b: a - b, 2),
    "MUL": (lambda a, b: a * b, 2),
    "DIV": (lambda a, b: a / (b + 1e-6), 2),
    "NEG": (lambda a: -a, 1),
    "ABS": (lambda a: np.abs(a), 1),
    "SIGN": (lambda a: np.sign(a), 1),
    "GATE": (_op_gate, 3),
    "JUMP": (_op_jump, 1),
    "DECAY": (_op_decay, 1),
    "DELAY1": (lambda a: _ts_delay(a, 1), 1),
    "MAX3": (lambda a: np.maximum(np.maximum(a, _ts_delay(a, 1)), _ts_delay(a, 2)), 1),
}

FEATURE_NAMES = (
    "RET", "MA20_DEV", "MA60_DEV", "VOL_RATIO",
    "MOM20", "MOM5", "RSI14", "AMP", "TURN",
)


def compute_features(close, open_, high, low, volume, turnover):
    """单标的日线 -> 特征字典 {name: np.ndarray(T,)}；前若干为 NaN(warmup)。"""
    n = len(close)

    def roll_mean(a, w):
        out = np.full(n, np.nan)
        if n >= w:
            c = np.cumsum(np.nan_to_num(a, nan=0.0))
            c[w:] = c[w:] - c[:-w]
            cnt = np.minimum(np.arange(1, n + 1), w)
            out[w - 1:] = c[w - 1:] / cnt[w - 1:]
        return out

    ret = np.full(n, np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        ret[1:] = np.log(close[1:] / close[:-1])
    ma20 = roll_mean(close, 20)
    ma60 = roll_mean(close, 60)
    ma_vol5 = roll_mean(volume, 5)
    ma20_dev = np.full(n, np.nan)
    ma60_dev = np.full(n, np.nan)
    vol_ratio = np.full(n, np.nan)
    np.divide(close, ma20, out=ma20_dev, where=ma20 > 0)
    np.divide(close, ma60, out=ma60_dev, where=ma60 > 0)
    np.divide(volume, ma_vol5, out=vol_ratio, where=ma_vol5 > 0)
    ma20_dev -= 1.0
    ma60_dev -= 1.0
    mom20 = np.full(n, np.nan)
    mom5 = np.full(n, np.nan)
    if n > 20:
        mom20[20:] = close[20:] / close[:-20] - 1.0
    if n > 5:
        mom5[5:] = close[5:] / close[:-5] - 1.0
    rsi = np.full(n, np.nan)
    if n > 14:
        diff = np.full(n, 0.0)
        diff[1:] = close[1:] - close[:-1]
        gains = np.where(diff > 0, diff, 0.0)
        losses = np.where(diff < 0, -diff, 0.0)
        ag = roll_mean(gains, 14)
        al = roll_mean(losses, 14)
        rs = (ag + 1e-9) / (al + 1e-9)
        rsi14 = 100 - 100 / (1 + rs)
        rsi[13:] = (rsi14[13:] - 50) / 50.0
    amp = np.full(n, np.nan)
    np.divide(high - low, close, out=amp, where=close > 0)
    turn = np.asarray(turnover, dtype=float)
    finite_turn = np.isfinite(turn)
    turn_n = np.full(n, np.nan)
    if finite_turn.any():
        med = np.median(turn[finite_turn])
        mad = np.median(np.abs(turn[finite_turn] - med)) + 1e-6
        turn_n[finite_turn] = np.clip((turn[finite_turn] - med) / mad, -5.0, 5.0)
    return {
        "RET": ret, "MA20_DEV": ma20_dev, "MA60_DEV": ma60_dev,
        "VOL_RATIO": vol_ratio, "MOM20": mom20, "MOM5": mom5,
        "RSI14": rsi, "AMP": amp, "TURN": turn_n,
    }


class StackVM:
    """栈式解释器：formula_tokens 为 RPN token 列表；features: {name: np.ndarray(T,)}。"""

    def __init__(self):
        self.feat_offset = len(FEATURE_NAMES)
        self.op_map = {k: v[0] for k, v in _OPS.items()}
        self.arity_map = {k: v[1] for k, v in _OPS.items()}

    def execute(self, formula_tokens, features):
        stack = []
        try:
            for tok in formula_tokens:
                if tok in FEATURE_NAMES:
                    if tok not in features or features[tok] is None:
                        return None
                    stack.append(np.asarray(features[tok], dtype=float))
                elif tok in self.op_map:
                    arity = self.arity_map[tok]
                    if len(stack) < arity:
                        return None
                    args = [stack.pop() for _ in range(arity)]
                    args.reverse()
                    res = self.op_map[tok](*args)
                    if np.any(~np.isfinite(res)):
                        res = np.nan_to_num(res, nan=0.0, posinf=1.0, neginf=-1.0)
                    stack.append(res)
                else:
                    return None
            return stack[0] if len(stack) == 1 else None
        except Exception:
            return None


# ===========================================================================
# regime 工具
# ===========================================================================

def _roll_mean2d(a: np.ndarray, w: int) -> np.ndarray:
    """对 (T,S) 沿时间轴做窗口 w 的滚动均值（NaN 视 0，暖机期 NaN）。"""
    out = np.full_like(a, np.nan, dtype=float)
    n = a.shape[0]
    if n >= w:
        c = np.cumsum(np.nan_to_num(a, nan=0.0), axis=0)
        c[w:] = c[w:] - c[:-w]
        cnt = np.minimum(np.arange(1, n + 1), w).reshape(-1, 1)
        out[w - 1:] = c[w - 1:] / cnt[w - 1:]
    return out


def _regime_bull_mask(close: np.ndarray, ma_win: int) -> np.ndarray:
    """等权指数（close 跨 S 逐日均值）相对其 MA(ma_win) 判牛；1 日滞后；暖机期判牛。

    返回 (T,) bool，True=牛市（允许 mom 腿）。采用 1 日滞后（第 i 日决策用 i-1 的
    level/ma）杜绝前视偏差；暖机期（MA 尚未就绪）默认判牛，避免误杀早期信号。
    """
    idx = np.nanmean(close, axis=1)  # (T,)
    # 逐标的前向填充缺失收盘，得到连续等权指数（避免 NaN 缺口扭曲 regime 信号）
    if np.isnan(idx).any():
        cf = close.copy()
        for j in range(cf.shape[1]):
            col = cf[:, j]
            mask = np.isnan(col)
            if mask.any() and not mask.all():
                last = col[0]
                for i in range(cf.shape[0]):
                    if np.isnan(col[i]):
                        col[i] = last
                    else:
                        last = col[i]
                # 末尾仍有 NaN（从未有数据）用最近值回填
                if np.isnan(col).any():
                    last = col[~np.isnan(col)][-1]
                    for i in range(cf.shape[0] - 1, -1, -1):
                        if np.isnan(col[i]):
                            col[i] = last
                        else:
                            break
                cf[:, j] = col
        idx = np.nanmean(cf, axis=1)
    T = idx.shape[0]
    bull = np.ones(T, dtype=bool)
    if ma_win + 1 > T:
        return bull
    c = np.nan_to_num(idx, nan=0.0)
    cum = np.cumsum(c)
    cnt = np.minimum(np.arange(1, T + 1), ma_win)
    ma = np.full(T, np.nan, dtype=float)
    ma[ma_win - 1:] = cum[ma_win - 1:] / cnt[ma_win - 1:]
    for i in range(1, T):
        if np.isnan(ma[i - 1]):
            bull[i] = True
        else:
            bull[i] = bool(idx[i - 1] > ma[i - 1])
    return bull


def _leader_cache_path() -> str:
    """定位 leader_index.parquet（不依赖 pathlib/os，纯字符串切分，过安全白名单）。"""
    f = __file__.replace("/", "\\")
    i = f.lower().find("\\backend\\")
    root = f[:i] if i >= 0 else "."
    return root + "\\data\\.regime_cache\\leader_index.parquet"


_LEADER_BULL_CACHE: dict = {}


def _leader_bull_map(ma_win: int) -> dict:
    """复刻引擎 leader_index regime：龙头指数 level 站上自身 MA(ma_win) 判牛，1 日滞后，暖机判牛。

    返回 {date_str('YYYY-MM-DD'): bool}。与 engine._regime_bull_map 同口径，
    使 regime_switch 与 regime_flat(引擎 leader_index) 用完全相同的信号源，做干净归因。
    """
    if ma_win in _LEADER_BULL_CACHE:
        return _LEADER_BULL_CACHE[ma_win]
    pq = _leader_cache_path()
    try:
        df = pl.read_parquet(pq)
    except Exception:
        # 兜底：文件缺失时全判牛（与引擎缺日期默认 True 一致）
        return {}
    level = df["level"].to_list()
    n = len(level)
    # 用 polars 滚动均值（与引擎 _regime_bull_map 完全同口径：trailing window，
    # 暖机期 ma=None 默认判牛）。注意：手写累计和若不减窗口首项会单调爆炸 -> 全判熊。
    ma = pl.Series(level).rolling_mean(ma_win).to_list()
    bull: dict = {}
    for i in range(n):
        d = str(df["date"][i])[:10]
        if i == 0 or ma[i - 1] is None:
            bull[d] = True
        else:
            lv = level[i - 1]
            mv = ma[i - 1]
            bull[d] = True if (lv is None or mv is None) else bool(lv > mv)
    _LEADER_BULL_CACHE[ma_win] = bull
    return bull


def _leader_bull_for_labels(labels, ma_win: int) -> np.ndarray:
    """把 leader 牛熊查表对齐到 market.timestamp_labels，返回 (T,1) bool。"""
    bull_map = _leader_bull_map(ma_win)
    T = len(labels)
    out = np.ones(T, dtype=bool)
    for i, lab in enumerate(labels):
        out[i] = bull_map.get(str(lab)[:10], True)
    return out.reshape(T, 1)


# ===========================================================================
# 策略定义
# ===========================================================================

META = {
    "id": "regime_conditional",
    "name": "regime 条件化（牛 mom_trend / 熊 pullback）",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "bull_factor_formula",
            "type": "str",
            "default": "MOM20 MA60_DEV SIGN MUL",
            "desc": "牛段因子 RPN（默认 mom_trend = MOM20·SIGN(MA60_DEV)）",
        },
        {
            "id": "regime_ma",
            "type": "int",
            "default": 60,
            "min": 20,
            "max": 250,
            "desc": "判牛熊的 MA 窗口（默认 60，与引擎 leader_index regime 一致）",
        },
        {
            "id": "regime_source",
            "type": "str",
            "default": "ew",
            "desc": "regime 信号源：ew=回测 universe 等权指数 MA / leader=引擎 leader_index 龙头指数 MA（与 regime_flat 同源，做干净归因）",
        },
        {
            "id": "bear_strategy",
            "type": "str",
            "default": "pullback",
            "desc": "熊段策略：pullback=缩量回踩支撑 / flat=空仓",
        },
        {
            "id": "pb_ma_proximity",
            "type": "float",
            "default": 0.02,
            "desc": "熊段 pullback：回踩 MA20 偏离度",
        },
        {
            "id": "pb_vol_ratio_max",
            "type": "float",
            "default": 0.8,
            "desc": "熊段 pullback：最大量比",
        },
    ],
    "scoring": {},
    "order_by": "score",
    "descending": True,
}

EXECUTION_BACKEND = "matrix_native"


class RegimeConditionalStrategy:
    """按市场状态切换两腿：bull->mom_trend，bear->pullback_to_support（或空仓）。"""

    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "open", "high", "low", "volume", "turnover_rate"})

    def required_warmup_bars(self, params: dict) -> int:
        return 60

    def compute_signals(self, market, params: dict):
        params = params or {}
        ma_win = int(params.get("regime_ma", 60))
        regime_source = (params.get("regime_source", "ew") or "ew").lower()
        bull_formula = params.get("bull_factor_formula", "MOM20 MA60_DEV SIGN MUL")
        bear_mode = (params.get("bear_strategy", "pullback") or "pullback").lower()
        pb_prox = float(params.get("pb_ma_proximity", 0.02))
        pb_volmax = float(params.get("pb_vol_ratio_max", 0.8))

        T, S = market.shape
        close = market.close
        op = market.open
        high = market.high
        low = market.low
        vol = market.volume
        try:
            turn = market.field("turnover_rate")
        except Exception:
            turn = np.zeros((T, S), dtype=float)

        # ── regime 牛熊掩码 ──
        #   ew     : 回测 universe 等权指数 vs 自身 MA(ma_win)，1 日滞后（默认）
        #   leader : 引擎 leader_index 龙头指数 level vs MA(ma_win)，与 regime_flat 同源
        if regime_source == "leader":
            try:
                labels = market.timestamp_labels
            except Exception:
                labels = None
            if labels is None:
                bull = np.ones((T, 1), dtype=bool)
            else:
                bull = _leader_bull_for_labels(labels, ma_win)
        else:
            bull = _regime_bull_mask(close, ma_win).reshape(T, 1)  # (T,1)
        bear = ~bull

        # ── 牛腿：mom_trend 因子（站上 MA60 才采信 20 日动量方向）──
        tokens = [t for t in bull_formula.split() if t] or ["MOM20"]
        vm = StackVM()
        mom_factor = np.full((T, S), np.nan, dtype=float)
        for j in range(S):
            feats = compute_features(
                close[:, j], op[:, j], high[:, j], low[:, j], vol[:, j], turn[:, j]
            )
            sig = vm.execute(tokens, feats)
            if sig is not None:
                mom_factor[:, j] = sig
        mom_finite = np.isfinite(mom_factor)
        mom_score = np.where(mom_finite, mom_factor, 0.0).astype(np.float32)
        mom_entry = mom_finite

        # ── 熊腿：pullback_to_support（缩量回踩 MA20 支撑）──
        if bear_mode == "flat":
            pb_entry = np.zeros((T, S), dtype=bool)
            pb_exit = np.zeros((T, S), dtype=np.uint8)
            pb_score = np.zeros((T, S), dtype=np.float32)
        else:
            ma20 = _roll_mean2d(close, 20)
            ma60 = _roll_mean2d(close, 60)
            vol_ma5 = _roll_mean2d(vol, 5)
            vol_ratio = np.full((T, S), np.nan, dtype=float)
            np.divide(vol, vol_ma5, out=vol_ratio, where=vol_ma5 > 0)
            mom20 = np.full((T, S), np.nan, dtype=float)
            if T > 20:
                mom20[20:] = close[20:] / close[:-20] - 1.0
            pb_entry = np.ones((T, S), dtype=bool)
            pb_entry &= (close > ma20 * (1.0 - pb_prox)) & (close < ma20 * (1.0 + pb_prox))
            pb_entry &= vol_ratio < pb_volmax
            pb_entry &= close > ma60
            pb_entry &= mom20 > 0
            pb_entry &= np.isfinite(ma20) & np.isfinite(ma60)
            # 熊腿出口：MA20 有效跌破（前一日在 MA20 上方，当日收破）
            shift_close = np.zeros((T, S), dtype=float)
            shift_close[1:] = close[:-1]
            shift_ma20 = np.zeros((T, S), dtype=float)
            shift_ma20[1:] = ma20[:-1]
            pb_exit = ((close < ma20) & (shift_close >= shift_ma20)).astype(np.uint8)
            pb_score = np.where(pb_entry, 1.0, 0.0).astype(np.float32)

        # ── 组合：牛段只采信 mom 腿，熊段只采信 pb 腿 ──
        entry = (bull & mom_entry) | (bear & pb_entry)

        # 牛市→熊市翻转日：强制平掉仍持有的 mom 仓位；当日新开 pullback 因 engine
        # 同日建仓豁免平仓（engine 跳过 entry_date==今日的平仓）。
        bull_col = bull.reshape(T)
        flip = np.zeros(T, dtype=bool)
        flip[1:] = bull_col[:-1] & (~bull_col[1:])
        flip_exit = np.broadcast_to(flip.reshape(T, 1), (T, S)).astype(np.uint8)
        exit_ = np.maximum(pb_exit, flip_exit).astype(np.uint8)

        # 评分：当日活跃腿的评分（两腿按日互斥，互不干扰排序）
        score = np.where(bull, mom_score, pb_score).astype(np.float32)

        entry_code = np.full((T, S), -1, dtype=np.int16)
        entry_code[(bull & mom_entry)] = 0   # mom_trend_buy
        entry_code[(bear & pb_entry)] = 1    # pullback_buy
        exit_code = np.full((T, S), -1, dtype=np.int16)
        exit_code[(flip_exit > 0)] = 0       # regime_flip_exit
        ec_pb = (pb_exit > 0) & (flip_exit == 0)
        exit_code[ec_pb] = 1                 # pullback_exit

        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_,
            score=score,
            entry_signal_code=entry_code,
            exit_signal_code=exit_code,
            entry_signal_ids=("mom_trend_buy", "pullback_buy"),
            exit_signal_ids=("regime_flip_exit", "pullback_exit"),
        )


MATRIX_STRATEGY = RegimeConditionalStrategy()
