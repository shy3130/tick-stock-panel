"""自定义信号 — 用户用「字段 + 运算符 + 值」组合出的布尔信号。

职责:
  - 从 data/user_data/custom_signals/*.json 加载信号定义
  - 把每个信号的 conditions 编译成一条 Polars 布尔表达式（AND 组合）
  - 供 pipeline 在 compute_signals / compute_enriched_today 末尾注入为列

不知道: 引擎、AI、API、回测、监控。纯函数 + 模块级缓存。

设计:
  - 信号列名加前缀 ``csg_`` 避免与内置 ``signal_`` 列冲突。
  - 回测/选股/监控都按列名找信号，因此注入列后零特殊处理即可三处生效。
  - 字段白名单 + 固定运算符集，杜绝任意表达式注入。
  - 第一版只支持 AND（多条件同时满足）。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)

# ── 常量 ────────────────────────────────────────────────
PREFIX = "csg_"                       # 自定义信号列名前缀
ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
OPS = {">", ">=", "<", "<=", "==", "!="}

# 字段白名单：只允许这些列出现在条件里（防注入）。均为数值型。
# 与 ENRICHED_COLUMNS 的数值列保持一致，排除 symbol/date/name 等非数值列。
ALLOWED_FIELDS: frozenset[str] = frozenset({
    # 行情
    "open", "high", "low", "close", "volume", "amount", "turnover_rate",
    "consecutive_limit_ups", "consecutive_limit_downs",
    # 基础
    "prev_close", "change_pct", "change_amount", "amplitude",
    # 均线 / 指数均线
    "ma5", "ma10", "ma20", "ma30", "ma60",
    "ema5", "ema10", "ema20", "ema30", "ema60",
    # MACD / BOLL / KDJ / ATR
    "macd_dif", "macd_dea", "macd_hist",
    "boll_upper", "boll_lower",
    "kdj_k", "kdj_d", "kdj_j",
    "atr_14",
    # 量价 / 极值 / 动量 / 波动率 / RSI
    "vol_ma5", "vol_ma10", "vol_ratio_5d",
    "high_60d", "low_60d",
    "momentum_5d", "momentum_10d", "momentum_20d", "momentum_30d", "momentum_60d",
    "annual_vol_20d",
    "rsi_6", "rsi_14", "rsi_24",
})

# 运算符 → Polars 表达式构造器（输入 col_expr, value）
_OP_BUILDERS = {
    ">":   lambda c, v: c > v,
    ">=":  lambda c, v: c >= v,
    "<":   lambda c, v: c < v,
    "<=":  lambda c, v: c <= v,
    "==":  lambda c, v: c == v,
    "!=":  lambda c, v: c != v,
}


# ── 持久化（镜像 strategy/config.py 的写法）──────────────
def _dir(data_dir: Path) -> Path:
    d = data_dir / "user_data" / "custom_signals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(data_dir: Path, signal_id: str) -> Path:
    return _dir(data_dir) / f"{signal_id}.json"


def load_all(data_dir: Path) -> list[dict]:
    """读取全部自定义信号定义。损坏的文件被跳过。"""
    d = _dir(data_dir)
    out: list[dict] = []
    for f in sorted(d.glob("*.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception as e:
            logger.warning("custom signal load failed %s: %s", f.name, e)
    return out


def save_one(data_dir: Path, sig: dict) -> None:
    p = _path(data_dir, sig["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sig, ensure_ascii=False, indent=2), encoding="utf-8")


def delete_one(data_dir: Path, signal_id: str) -> bool:
    p = _path(data_dir, signal_id)
    if p.exists():
        p.unlink()
        return True
    return False


# ── 校验 ────────────────────────────────────────────────

MAX_DAYS = 60  # 偏移天数上限 (前N日的 N)


def _parse_days(c: dict, key: str, i: int) -> int:
    """解析并校验条件的天数偏移 (leftDays / rightDays)。返回 0..MAX_DAYS。"""
    raw = c.get(key, 0)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"第 {i+1} 个条件: {key} 必须是整数: {raw!r}")
    if n < 0 or n > MAX_DAYS:
        raise ValueError(f"第 {i+1} 个条件: {key} 必须在 0..{MAX_DAYS} 之间: {n}")
    return n


def _parse_right(right: str) -> tuple[str, object]:
    """解析右值。返回 ('field', colname) 或 ('const', float)。"""
    if isinstance(right, (int, float)):
        return ("const", float(right))
    if not isinstance(right, str):
        raise ValueError(f"非法右值: {right!r}")
    if right.startswith("field:"):
        col = right[len("field:"):]
        if col not in ALLOWED_FIELDS:
            raise ValueError(f"右值字段不在白名单: {col}")
        return ("field", col)
    # 纯数字
    try:
        return ("const", float(right))
    except ValueError:
        raise ValueError(f"非法右值（应为 field:xxx 或数字）: {right!r}")


def validate(sig: dict) -> None:
    """校验一个信号定义，非法则抛 ValueError（含中文信息）。"""
    sid = sig.get("id", "")
    if not isinstance(sid, str) or not ID_RE.match(sid):
        raise ValueError(f"信号 id 非法（仅小写字母数字下划线，1-40字符）: {sid!r}")
    if not isinstance(sig.get("name"), str) or not sig["name"].strip():
        raise ValueError("信号 name 不能为空")
    if sig.get("kind") not in ("entry", "exit", "both"):
        raise ValueError("kind 必须是 entry / exit / both")
    conds = sig.get("conditions")
    if not isinstance(conds, list) or len(conds) == 0:
        raise ValueError("conditions 不能为空")
    if len(conds) > 8:
        raise ValueError("conditions 最多 8 条")
    for i, c in enumerate(conds):
        if not isinstance(c, dict):
            raise ValueError(f"第 {i+1} 个条件格式错误")
        left = c.get("left", "")
        if left not in ALLOWED_FIELDS:
            raise ValueError(f"第 {i+1} 个条件: 字段 {left!r} 不在白名单")
        if c.get("op") not in OPS:
            raise ValueError(f"第 {i+1} 个条件: 运算符 {c.get('op')!r} 非法")
        _parse_right(c.get("right"))   # 会校验右值字段/数字
        _parse_days(c, "leftDays", i)   # 左字段偏移
        _parse_days(c, "rightDays", i)  # 右字段偏移


# ── 编译为 Polars 表达式 ─────────────────────────────────
def column_name(signal_id: str) -> str:
    """信号 id → DataFrame 列名（加前缀）。"""
    return f"{PREFIX}{signal_id}"


def _col(name: str, days: int = 0) -> pl.Expr:
    """构造列表达式; days>0 时取 N 个交易日前的值 (按 symbol 分组 shift)。"""
    expr = pl.col(name)
    if days > 0:
        expr = expr.shift(days).over("symbol")
    return expr


def build_expressions(signals: list[dict], allow_shift: bool = True) -> dict[str, pl.Expr]:
    """把多个自定义信号编译成 {column_name: pl.Expr}。

    - 只处理 enabled != False 的信号。
    - 单个信号内多条件用 ``&`` 串联（AND）。
    - allow_shift=False 时, 跳过带日期偏移 (leftDays/rightDays>0) 的信号
      (盘中单日快照上 .shift 跨 symbol 语义不正确, 优雅降级)。
    - 编译失败的信号被跳过并告警（不影响其它信号）。
    """
    out: dict[str, pl.Expr] = {}
    for sig in signals:
        if sig.get("enabled") is False:
            continue
        try:
            conds = sig["conditions"]
            col_name = column_name(sig["id"])
            parts: list[pl.Expr] = []
            for c in conds:
                left_days = int(c.get("leftDays", 0) or 0)
                right_days = int(c.get("rightDays", 0) or 0)
                # 盘中路径不支持偏移 → 跳过整个信号
                if not allow_shift and (left_days > 0 or right_days > 0):
                    raise ValueError("盘中实时路径不支持日期偏移条件, 已跳过")
                left = c["left"]
                op = c["op"]
                kind, val = _parse_right(c["right"])
                right_expr = _col(val, right_days) if kind == "field" else val
                parts.append(_OP_BUILDERS[op](_col(left, left_days), right_expr))
            combined = parts[0]
            for p in parts[1:]:
                combined = combined & p
            out[col_name] = combined
        except Exception as e:
            logger.warning("custom signal compile failed %s: %s", sig.get("id"), e)
    return out


def expression_dependencies(exprs: dict[str, pl.Expr] | None = None) -> dict[str, frozenset[str]]:
    """返回自定义信号列到根字段的依赖映射。"""
    source = exprs if exprs is not None else {}
    return {name: frozenset(_expr_root_columns(expr)) for name, expr in source.items()}


def inject(
    df: pl.DataFrame,
    exprs: dict[str, pl.Expr],
    needed: set[str] | None = None,
) -> pl.DataFrame:
    """把编译好的信号表达式作为列加入 df。

    ``needed=None`` 保持历史全量语义；传入集合时只注入被请求的自定义信号。
    缺失依赖会明确告警，避免回测静默丢失信号。
    """
    if df.is_empty() or not exprs:
        return df
    cols = set(df.columns)
    add: dict[str, pl.Expr] = {}
    for name, expr in exprs.items():
        if needed is not None and name not in needed:
            continue
        # 提取该表达式引用的所有字段列，缺失则跳过（避免运行时报错）
        required = _expr_root_columns(expr)
        if required.issubset(cols):
            add[name] = expr
        else:
            logger.warning(
                "custom signal %s missing dependencies: %s",
                name,
                sorted(required - cols),
            )
    if add:
        df = df.with_columns([e.alias(n) for n, e in add.items()])
    return df


def _expr_root_columns(expr: pl.Expr) -> set[str]:
    """尽力提取表达式里出现的列名。失败则返回空集（保守跳过）。"""
    try:
        # Polars 的 meta.root_names() 返回表达式引用的根列名
        names = expr.meta.root_names()
        return set(names)
    except Exception:
        return set()
