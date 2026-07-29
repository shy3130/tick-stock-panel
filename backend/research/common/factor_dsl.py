"""AlphaGPT 精华移植：可搜索的因子 DSL + StackVM（股权版）。

移植自 imbue-bit/AlphaGPT 的 model_core/{vocab,ops,vm}.py，
- 因子 = 特征(leaves) + 算子 组成的 RPN token 序列
- StackVM 把公式在特征张量上向量化执行成信号
区别在于：
  1. 特征换成 A 股日线（收益/均线偏离/量比/动量/RSI/振幅/换手）
  2. 不依赖 torch，纯 numpy（便于接入我们的 Polars 回测）
  3. 公式用可读 token 名而非 id，便于调试与 LLM 生成
"""

from __future__ import annotations

import numpy as np

# ---------------------------------------------------------------------------
# 算子库（与 AlphaGPT OPS_CONFIG 同构，纯 numpy 实现，1D 数组=时间序列）
# ---------------------------------------------------------------------------

def _ts_delay(x: np.ndarray, d: int) -> np.ndarray:
    if d <= 0:
        return x
    out = np.empty_like(x, dtype=float)
    out[:d] = np.nan
    out[d:] = x[:-d]
    return out


def _op_gate(cond: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    mask = (cond > 0).astype(float)
    return mask * x + (1.0 - mask) * y


def _op_jump(x: np.ndarray) -> np.ndarray:
    mean = np.nanmean(x)
    std = np.nanstd(x) + 1e-6
    z = (x - mean) / std
    return np.clip(np.where(z - 3.0 > 0, z - 3.0, 0.0), 0.0, None)


def _op_decay(x: np.ndarray) -> np.ndarray:
    return x + 0.8 * _ts_delay(x, 1) + 0.6 * _ts_delay(x, 2)


OPS = {
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

# 特征名（leaves）。FEATURE_NAMES 顺序即词表基础。
FEATURE_NAMES = (
    "RET",        # 对数收益
    "MA20_DEV",   # 收盘价/MA20 - 1（回踩支撑偏离）
    "MA60_DEV",   # 收盘价/MA60 - 1（中期趋势偏离）
    "VOL_RATIO",  # 量比 = volume / MA5(volume)
    "MOM20",      # 20 日动量
    "MOM5",       # 5 日动量
    "RSI14",      # 相对强弱 (归一化到 -1~1)
    "AMP",        # (high-low)/close 振幅
    "TURN",       # 换手率（robust 归一）
)

# 词表大小 = 特征数 + 算子数
VOCAB_SIZE = len(FEATURE_NAMES) + len(OPS)


# ---------------------------------------------------------------------------
# 特征工程：单个 symbol 的日线 -> 特征字典 {name: np.array(T,)}
# ---------------------------------------------------------------------------

def compute_features(df, close, open_, high, low, volume, turnover):
    """df: polars DataFrame 已按 date 排序；其余为 np 数组（来自 df[col].to_numpy()）。
    返回 {name: np.ndarray(T,)}，前若干为 NaN（warmup）。"""
    n = len(close)
    ret = np.full(n, np.nan)
    ret[1:] = np.log(close[1:] / close[:-1])

    def roll_mean(a, w):
        out = np.full(n, np.nan)
        if n >= w:
            c = np.cumsum(np.nan_to_num(a, nan=0.0))
            c[w:] = c[w:] - c[:-w]
            cnt = np.arange(1, n + 1)
            cnt = np.minimum(cnt, w)
            out[w - 1:] = c[w - 1:] / cnt[w - 1:]
        return out

    ma20 = roll_mean(close, 20)
    ma60 = roll_mean(close, 60)
    ma_vol5 = roll_mean(volume, 5)

    ma20_dev = np.where(ma20 > 0, close / ma20 - 1.0, np.nan)
    ma60_dev = np.where(ma60 > 0, close / ma60 - 1.0, np.nan)
    vol_ratio = np.where(ma_vol5 > 0, volume / ma_vol5, np.nan)

    mom20 = np.full(n, np.nan)
    mom5 = np.full(n, np.nan)
    if n > 20:
        mom20[20:] = close[20:] / close[:-20] - 1.0
    if n > 5:
        mom5[5:] = close[5:] / close[:-5] - 1.0

    # RSI14 归一化到 (-1,1)
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

    amp = np.where(close > 0, (high - low) / close, np.nan)

    # 换手率 robust 归一（中位数/MAD）
    turn = np.asarray(turnover, dtype=float)
    med = np.nanmedian(turn)
    mad = np.nanmedian(np.abs(turn - med)) + 1e-6
    turn_n = np.clip((turn - med) / mad, -5.0, 5.0)

    return {
        "RET": ret,
        "MA20_DEV": ma20_dev,
        "MA60_DEV": ma60_dev,
        "VOL_RATIO": vol_ratio,
        "MOM20": mom20,
        "MOM5": mom5,
        "RSI14": rsi,
        "AMP": amp,
        "TURN": turn_n,
    }


# ---------------------------------------------------------------------------
# StackVM：执行 RPN 公式
# ---------------------------------------------------------------------------

class StackVM:
    """栈式解释器：formula_tokens 为 token 名列表（RPN）。
    features: {name: np.ndarray(T,)}。返回信号 np.ndarray(T,) 或 None（无效）。"""

    def __init__(self):
        self.feat_offset = len(FEATURE_NAMES)
        self.op_map = {name: cfg[0] for name, cfg in OPS.items()}
        self.arity_map = {name: cfg[1] for name, cfg in OPS.items()}

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
            if len(stack) == 1:
                return stack[0]
            return None
        except Exception:
            return None


# ---------------------------------------------------------------------------
# 随机 RPN 公式生成（保证语法有效）
# ---------------------------------------------------------------------------

import random as _rng_mod


def gen_formula(rng: _rng_mod.Random = None, max_len: int = 10):
    """生成语法有效的 RPN 公式（用可读 token 名）。"""
    rng = rng or _rng_mod
    feats = list(FEATURE_NAMES)
    binary = [k for k, v in OPS.items() if v[1] == 2]
    unary = [k for k, v in OPS.items() if v[1] == 1]
    ternary = [k for k, v in OPS.items() if v[1] == 3]

    tokens: list[str] = []
    stack = 0
    while len(tokens) < max_len:
        if stack == 0:
            t = rng.choice(feats)
            tokens.append(t)
            stack += 1
        elif stack == 1:
            r = rng.random()
            if r < 0.5:
                t = rng.choice(feats)
                tokens.append(t)
                stack += 1
            elif r < 0.78:
                t = rng.choice(unary)
                tokens.append(t)  # stack 不变
            elif r < 0.95 and ternary:
                t = rng.choice(ternary)
                tokens.append(t)
                stack += 2
            else:
                t = rng.choice(binary)
                tokens.append(t)
                stack -= 1
        else:  # stack >= 2
            r = rng.random()
            if r < 0.28:
                t = rng.choice(feats)
                tokens.append(t)
                stack += 1
            elif r < 0.5:
                t = rng.choice(unary)
                tokens.append(t)
            elif r < 0.62 and ternary:
                t = rng.choice(ternary)
                tokens.append(t)
                stack += 2
            else:
                t = rng.choice(binary)
                tokens.append(t)
                stack -= 1
    while stack > 1:
        tokens.append(rng.choice(binary))
        stack -= 1
    return tokens


def formula_to_str(tokens):
    return " ".join(tokens)
