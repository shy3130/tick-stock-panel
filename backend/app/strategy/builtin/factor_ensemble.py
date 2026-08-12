"""因子 ensemble 策略：把多个语义 RPN 因子横截面 z-score 归一后等权平均成单一评分。

设计：
  - 每个因子（RPN，纯 9 特征 + 算子组合）逐标的算 (T,S) 原始信号；
  - 每个交易日 t 对每因子在截面 S 上做 z-score 归一（clip ±3），消除量纲/波动率差异；
  - 多因子归一信号等权平均 -> ensemble 评分；entry=ensmble>0（共识看多才做多），
    score=ensemble，由引擎 score_weight 选前 mp 只持仓。
  - 两腿无样本内训练：因子公式与权重全部固定；归一用当日截面统计量（样本外）。

注意：本模块运行于策略安全检查之下（import 白名单仅放行 numpy / app.backtest.matrix /
polars / datetime / __future__），故因子 DSL 核心在此内联，仅依赖 numpy。
"""

from __future__ import annotations

import numpy as np

from app.backtest.matrix import make_signal_matrix

# ===========================================================================
# 因子 DSL 核心（与 regime_conditional 同源的内联版，纯 numpy）
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


# 6 个语义因子（来自 P8 假设检验）：趋势确认动量 / 量能确认动量 / 防追高 / RSI 加权 /
# 均线偏离 / 纯动量。全部为纯特征组合、无常量字面量。
DEFAULT_FACTORS = [
    "MOM20 MA60_DEV SIGN MUL",   # mom_trend
    "MOM20 VOL_RATIO SIGN MUL",  # mom_vol
    "MOM20 MA20_DEV ABS DIV",    # mom_anti_ext
    "MOM20 RSI14 MUL",           # mom_rsi
    "MA20_DEV",                  # ma20_dev
    "MOM20",                     # mom20
]


def _zscore_rows(x: np.ndarray) -> np.ndarray:
    """对 (T,S) 沿截面 S 做 z-score（每行一个交易日），clip ±3，NaN 视 0。"""
    x = np.nan_to_num(x, nan=0.0)
    mean = x.mean(axis=1, keepdims=True)
    std = x.std(axis=1, keepdims=True) + 1e-8
    z = np.clip((x - mean) / std, -3.0, 3.0)
    return z


META = {
    "id": "factor_ensemble",
    "name": "因子 ensemble（多语义因子横截面归一等权）",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "factor_formulas",
            "type": "list",
            "default": DEFAULT_FACTORS,
            "desc": "参与 ensemble 的 RPN 因子列表（默认 6 个语义动量因子）",
        },
        {
            "id": "min_factors",
            "type": "int",
            "default": 1,
            "desc": "单标的至少需几个因子信号有限才纳入（默认 1）",
        },
    ],
    "scoring": {},
    "order_by": "score",
    "descending": True,
}

EXECUTION_BACKEND = "matrix_native"


class FactorEnsembleStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "open", "high", "low", "volume", "turnover_rate"})

    def required_warmup_bars(self, params: dict) -> int:
        return 60

    def compute_signals(self, market, params: dict):
        params = params or {}
        formulas = params.get("factor_formulas", DEFAULT_FACTORS) or DEFAULT_FACTORS
        min_factors = int(params.get("min_factors", 1))

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
        # 逐因子算 (T,S) 原始信号
        raw = []
        for f in formulas:
            tokens = [t for t in f.split() if t] or ["MOM20"]
            sig = np.full((T, S), np.nan, dtype=float)
            for j in range(S):
                feats = compute_features(
                    close[:, j], op[:, j], high[:, j], low[:, j], vol[:, j], turn[:, j]
                )
                r = vm.execute(tokens, feats)
                if r is not None:
                    sig[:, j] = r
            raw.append(np.nan_to_num(sig, nan=0.0))

        if len(raw) == 0:
            raw = [np.zeros((T, S), dtype=float)]
        stack = np.stack(raw, axis=0)  # (F,T,S)
        # 逐因子横截面归一
        norm = np.stack([_zscore_rows(stack[k]) for k in range(stack.shape[0])], axis=0)
        ensemble = norm.mean(axis=0).astype(np.float32)  # (T,S)

        finite_cnt = np.sum(np.array([~np.isnan(r) for r in raw]), axis=0)  # (T,S)
        entry = (ensemble > 0) & (finite_cnt >= min_factors)

        entry_code = np.full((T, S), -1, dtype=np.int16)
        entry_code[entry] = 0
        exit_code = np.full((T, S), -1, dtype=np.int16)

        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=np.zeros((T, S), dtype=np.uint8),
            score=ensemble,
            entry_signal_code=entry_code,
            exit_signal_code=exit_code,
            entry_signal_ids=("ensemble_buy",),
            exit_signal_ids=(),
        )


MATRIX_STRATEGY = FactorEnsembleStrategy()
