"""自定义因子策略（AlphaGPT DSL 移植，自包含版）。

把 StackVM 编译出的 RPN 因子公式作为横截面选股信号接入 tickflow 回测引擎。
因子公式通过 config.params["factor_formula"] 传入；框架按 max_positions + score(descending)
选股；退出由风控(stop_loss / take_profit / max_hold_days)统一处理，本策略只给 entry。

引擎自动扫描 app/strategy/builtin/ 加载本模块（无需改引擎代码）。
注意：策略模块运行于安全沙箱，禁止 import os/sys；故因子 DSL 核心在此内联，仅依赖 numpy。
"""

from __future__ import annotations

import numpy as np

from app.backtest.matrix import make_signal_matrix

# ===========================================================================
# 因子 DSL 核心（移植自 imbue-bit/AlphaGPT model_core，纯 numpy，自包含）
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
# 策略定义
# ===========================================================================

META = {
    "id": "custom_factor",
    "name": "自定义因子(AlphaGPT DSL)",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "factor_formula",
            "type": "str",
            "default": "MA60_DEV VOL_RATIO DECAY MA60_DEV ADD ADD SIGN MA60_DEV MOM5 SUB ADD",
            "desc": "RPN 因子公式（特征: RET/MA20_DEV/MA60_DEV/VOL_RATIO/MOM20/MOM5/RSI14/AMP/TURN; 算子: ADD/SUB/MUL/DIV/NEG/ABS/SIGN/GATE/JUMP/DECAY/DELAY1/MAX3）",
        },
    ],
    "scoring": {},
    "order_by": "score",
    "descending": True,
}

EXECUTION_BACKEND = "matrix_native"


class CustomFactorStrategy:
    """用 StackVM 把 RPN 因子公式编译为横截面选股信号。"""

    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "volume", "turnover_rate"})

    def required_warmup_bars(self, params: dict) -> int:
        return 60

    def compute_signals(self, market, params: dict):
        formula = (params or {}).get(
            "factor_formula",
            "MA60_DEV VOL_RATIO DECAY MA60_DEV ADD ADD SIGN MA60_DEV MOM5 SUB ADD",
        )
        tokens = [t for t in formula.split() if t]
        if not tokens:
            tokens = ["RET", "MOM20", "ADD"]

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

        vm = StackVM()
        factor = np.full((T, S), np.nan, dtype=float)
        for j in range(S):
            feats = compute_features(
                close[:, j], op[:, j], high[:, j], low[:, j], vol[:, j], turn[:, j]
            )
            sig = vm.execute(tokens, feats)
            if sig is not None:
                factor[:, j] = sig

        finite = np.isfinite(factor)
        score = np.where(finite, factor, 0.0).astype(np.float32)
        entry = finite.astype(np.uint8)
        entry_code = np.where(entry, 0, -1).astype(np.int16)
        return make_signal_matrix(
            market.shape,
            entry=entry,
            exit=np.zeros((T, S), dtype=np.uint8),
            score=score,
            entry_signal_code=entry_code,
            entry_signal_ids=("factor_buy",),
        )


MATRIX_STRATEGY = CustomFactorStrategy()
