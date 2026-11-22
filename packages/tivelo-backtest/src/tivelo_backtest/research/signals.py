"""Pure v1 daily/overlay/intraday signal computation with explicit field catalogs.

Compatibility builders retain TSP's skip-and-log behavior. Use signal_plan for
all-or-error daily evaluation. No application preferences or files are read.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import polars as pl

from .catalog import BUILTIN_CATALOG
from .intraday import INTRADAY_FEATURES

logger = logging.getLogger(__name__)

PREFIX = "csg_"                       # 自定义信号列名前缀

ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")

OPS = {">", ">=", "<", "<=", "==", "!="}

STRING_OPS = {"contains", "==", "!="}

_MAX_STR_RIGHT = 64

ALLOWED_FIELDS: frozenset[str] = frozenset({
    # 行情
    "open", "high", "low", "close", "volume", "amount", "turnover_rate",
    "consecutive_limit_ups", "consecutive_limit_downs",
    # 原始价 (涨跌停类信号判定用)
    "raw_close", "raw_high", "raw_low",
    # 涨跌停判定价 (运行时列: 生效价∓0.005 容差)
    "limit_up_price", "limit_down_price",
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
    # 异动偏离 (交易所异动规则口径, 运行时列)
    "deviate_3d", "deviate_10d", "deviate_30d",
})

_OP_BUILDERS = {
    ">":   lambda c, v: c > v,
    ">=":  lambda c, v: c >= v,
    "<":   lambda c, v: c < v,
    "<=":  lambda c, v: c <= v,
    "==":  lambda c, v: c == v,
    "!=":  lambda c, v: c != v,
    # literal=True: 右值按字面量匹配, 不当正则编译 (用户输入含 .* 等也安全)
    "contains": lambda c, v: c.cast(pl.Utf8).str.contains(v, literal=True),
}

MAX_DAYS = 60  # 偏移天数上限 (前N日的 N)

COLUMN_RE = re.compile(r"^[a-z0-9_]{1,48}$")

_OVERLAY_MAX_CONDITIONS = 8

TIMEFRAME_DAILY = "daily"

TIMEFRAME_INTRADAY = "intraday"

INTRADAY_PREFIX = "csgi_"

INTRADAY_OPS = OPS | {"cross_up", "cross_down"}

_EDGE_GROUP = ["symbol", "date"]


@dataclass(frozen=True)
class SignalFields:
    """A copied field/type allowlist; discovery belongs to the caller."""

    allowed: frozenset[str]
    string_fields: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        strings = frozenset(self.string_fields)
        object.__setattr__(self, "string_fields", strings)
        object.__setattr__(self, "allowed", frozenset(self.allowed) | strings)


DEFAULT_FIELDS = SignalFields(ALLOWED_FIELDS | {spec.id for spec in BUILTIN_CATALOG.specs})


def allowed_fields(*, fields: SignalFields = DEFAULT_FIELDS) -> frozenset[str]:
    return fields.allowed



def _parse_days(c: dict, key: str, i: int) -> int:
    """解析并校验条件的天数偏移 (leftDays / rightDays)。返回 0..MAX_DAYS。"""
    raw = c.get(key, 0)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"第 {i+1} 个条件: {key} 必须是整数: {raw!r}")  # noqa: B904 — preserve legacy exception chaining
    if n < 0 or n > MAX_DAYS:
        raise ValueError(f"第 {i+1} 个条件: {key} 必须在 0..{MAX_DAYS} 之间: {n}")
    return n


def _parse_right(right: str, *, string_mode: bool = False, fields: SignalFields = DEFAULT_FIELDS) -> tuple[str, object]:
    """解析右值。返回 ('field', colname) / ('const', float) / ('const_str', str)。

    数值模式接受三种形式:
      - 数字 (int / float / 数字字符串) → 常量
      - "field:字段名" → 字段引用
      - 裸字段名 (在白名单内) → 自动视为字段引用
        (AI 生成偶尔漏写 field: 前缀; 白名单字段名不可能是数字, 无歧义)

    string 模式 (左字段是 string 扩展字段): 只接受非空字符串字面量
    (概念/行业名), 不支持字段引用 —— "字段A包含字段B" 无业务语义且
    会与 field: 前缀解析产生歧义。
    """
    if string_mode:
        if not isinstance(right, str) or not right.strip():
            raise ValueError("字符串条件的右值必须是非空字符串 (如概念/行业名)")
        if right.startswith("field:"):
            raise ValueError("字符串条件不支持字段引用右值, 请填字符串字面量")
        if len(right) > _MAX_STR_RIGHT:
            raise ValueError(f"字符串右值过长 (≤{_MAX_STR_RIGHT} 字符): {right[:20]}…")
        return ("const_str", right.strip())
    if isinstance(right, (int, float)):
        return ("const", float(right))
    if not isinstance(right, str):
        raise ValueError(f"非法右值: {right!r}")
    allowed = allowed_fields(fields=fields)
    if right.startswith("field:"):
        col = right[len("field:"):]
        if col not in allowed:
            raise ValueError(f"右值字段不在白名单: {col}")
        return ("field", col)
    # 纯数字
    try:
        return ("const", float(right))
    except ValueError:
        pass
    # 裸字段名 — 兜底容错, 仍受白名单约束
    if right in allowed:
        return ("field", right)
    raise ValueError(f"非法右值（应为 field:xxx 或数字）: {right!r}")


def validate(sig: dict, *, fields: SignalFields = DEFAULT_FIELDS) -> None:
    """校验一个信号定义，非法则抛 ValueError（含中文信息）。"""
    sid = sig.get("id", "")
    if not isinstance(sid, str) or not ID_RE.match(sid):
        raise ValueError(f"信号 id 非法（仅小写字母数字下划线，1-40字符）: {sid!r}")
    explicit_column = sig.get("column")
    if explicit_column is not None and (
        not isinstance(explicit_column, str) or not COLUMN_RE.match(explicit_column)
    ):
        raise ValueError(f"信号 column 非法（仅小写字母数字下划线，1-48字符）: {explicit_column!r}")
    if not isinstance(sig.get("name"), str) or not sig["name"].strip():
        raise ValueError("信号 name 不能为空")
    if sig.get("kind") not in ("entry", "exit", "both"):
        raise ValueError("kind 必须是 entry / exit / both")
    timeframe = sig.get("timeframe", TIMEFRAME_DAILY)
    if timeframe not in (TIMEFRAME_DAILY, TIMEFRAME_INTRADAY):
        raise ValueError(f"timeframe 必须是 {TIMEFRAME_DAILY} / {TIMEFRAME_INTRADAY}: {timeframe!r}")
    conds = sig.get("conditions")
    if not isinstance(conds, list) or len(conds) == 0:
        raise ValueError("conditions 不能为空")
    if len(conds) > 8:
        raise ValueError("conditions 最多 8 条")
    if timeframe == TIMEFRAME_INTRADAY:
        _validate_intraday(sig)
        return
    string_fields = fields.string_fields
    for i, c in enumerate(conds):
        if not isinstance(c, dict):
            raise ValueError(f"第 {i+1} 个条件格式错误")
        left = c.get("left", "")
        if left not in allowed_fields(fields=fields):
            raise ValueError(f"第 {i+1} 个条件: 字段 {left!r} 不在白名单")
        is_str = left in string_fields
        if is_str:
            if c.get("op") not in STRING_OPS:
                raise ValueError(
                    f"第 {i+1} 个条件: 字符串字段 {left!r} 仅支持 "
                    f"{'/'.join(sorted(STRING_OPS))} 运算符"
                )
        elif c.get("op") == "contains":
            raise ValueError(f"第 {i+1} 个条件: contains 仅用于字符串扩展字段")
        elif c.get("op") not in OPS:
            raise ValueError(f"第 {i+1} 个条件: 运算符 {c.get('op')!r} 非法")
        _parse_right(c.get("right"), string_mode=is_str, fields=fields)   # 会校验右值字段/数字/字符串
        _parse_days(c, "leftDays", i)   # 左字段偏移
        _parse_days(c, "rightDays", i)  # 右字段偏移


def column_name(signal_id: str) -> str:
    """信号 id → DataFrame 列名（加前缀）。"""
    return f"{PREFIX}{signal_id}"


def resolve_column(sig: dict) -> str:
    """信号定义 → 输出列名。

    默认 ``csg_{id}``; 定义可携带显式 ``column`` 字段改写列名 (如迁移自
    内置信号的定义沿用 ``signal_*`` 原名, 下游消费方零改动)。显式列名
    仅允许小写字母数字下划线, 防止任意列名注入。
    """
    explicit = sig.get("column")
    if explicit:
        if not isinstance(explicit, str) or not COLUMN_RE.match(explicit):
            raise ValueError(f"信号 column 非法（仅小写字母数字下划线，1-48字符）: {explicit!r}")
        return explicit
    return column_name(sig["id"])


def _col(name: str, days: int = 0) -> pl.Expr:
    """构造列表达式; days>0 时取 N 个交易日前的值 (按 symbol 分组 shift)。"""
    expr = pl.col(name)
    if days > 0:
        expr = expr.shift(days).over("symbol")
    return expr


def validate_overlay_conditions(conditions: object, *, fields: SignalFields = DEFAULT_FIELDS) -> None:
    """校验叠加条件列表 (策略 override 的 overlay_filter 键)。非法抛 ValueError。

    空列表合法 (= 清空叠加条件)。字段白名单与运算符同自定义信号
    (allowed_fields 含注册表因子与字符串扩展字段); 日期偏移同源支持
    (leftDays/rightDays, 0..MAX_DAYS)。
    """
    if not isinstance(conditions, list):
        raise ValueError("overlay_filter 必须是条件数组")
    if not conditions:
        return
    if len(conditions) > _OVERLAY_MAX_CONDITIONS:
        raise ValueError(f"叠加条件最多 {_OVERLAY_MAX_CONDITIONS} 条")
    string_fields = fields.string_fields
    for i, c in enumerate(conditions):
        if not isinstance(c, dict):
            raise ValueError(f"第 {i+1} 个叠加条件格式错误")
        left = c.get("left", "")
        if left not in allowed_fields(fields=fields):
            raise ValueError(f"第 {i+1} 个叠加条件: 字段 {left!r} 不在白名单")
        is_str = left in string_fields
        if is_str:
            if c.get("op") not in STRING_OPS:
                raise ValueError(
                    f"第 {i+1} 个叠加条件: 字符串字段 {left!r} 仅支持 "
                    f"{'/'.join(sorted(STRING_OPS))} 运算符"
                )
        elif c.get("op") not in OPS:
            raise ValueError(f"第 {i+1} 个叠加条件: 运算符 {c.get('op')!r} 非法")
        _parse_right(c.get("right"), string_mode=is_str, fields=fields)
        _parse_days(c, "leftDays", i)   # 左字段偏移
        _parse_days(c, "rightDays", i)  # 右字段偏移


def overlay_max_days(conditions: object) -> int:
    """叠加条件里最大的日期偏移 (0 = 无偏移)。调用方据此评估所需历史窗口。"""
    if not isinstance(conditions, list):
        return 0
    max_days = 0
    for c in conditions:
        if not isinstance(c, dict):
            continue
        for key in ("leftDays", "rightDays"):
            try:
                days = int(c.get(key, 0) or 0)
            except (TypeError, ValueError):
                continue
            if days > max_days:
                max_days = days
    return max_days


def build_overlay_expr(conditions: list[dict], *, fields: SignalFields = DEFAULT_FIELDS) -> pl.Expr | None:
    """把叠加条件编译成一条 AND 组合的布尔表达式 (无 csg_ 前缀, 不落盘)。

    与 build_expressions 同一套编译件 (_OP_BUILDERS/_parse_right/_col);
    日期偏移 (leftDays/rightDays) 与自定义信号同口径, 按 symbol 分组 shift。
    条件已被 validate_overlay_conditions 拦过一遍, 这里编译失败直接抛
    ValueError (fail-closed, 不静默跳过 — 静默跳过等于悄悄放宽过滤,
    与硬过滤语义冲突)。
    null 语义: 表达式不做 fill, 由调用方统一 fill_null(False)
    (缺数据日不入选)。
    """
    if not conditions:
        return None
    string_fields = fields.string_fields
    parts: list[pl.Expr] = []
    for c in conditions:
        left, op = c["left"], c["op"]
        is_str = left in string_fields
        kind, val = _parse_right(c["right"], string_mode=is_str, fields=fields)
        left_days = int(c.get("leftDays", 0) or 0)
        right_days = int(c.get("rightDays", 0) or 0)
        right_expr = _col(val, right_days) if kind == "field" else val
        parts.append(_OP_BUILDERS[op](_col(left, left_days), right_expr))
    combined = parts[0]
    for p in parts[1:]:
        combined = combined & p
    return combined


def overlay_missing_columns(conditions: list[dict], columns) -> list[str]:
    """叠加条件引用、且不在 columns 里的根字段 (供 fail-closed 检查)。"""
    if not conditions:
        return []
    missing: list[str] = []
    have = set(columns)
    for c in conditions:
        left = str(c.get("left", ""))
        if left and left not in have and left not in missing:
            missing.append(left)
        right = c.get("right")
        if isinstance(right, str) and right.startswith("field:"):
            col = right[len("field:"):]
            if col and col not in have and col not in missing:
                missing.append(col)
    return missing


def build_expressions(signals: list[dict], allow_shift: bool = True, *, fields: SignalFields = DEFAULT_FIELDS) -> dict[str, pl.Expr]:
    """把多个自定义信号编译成 {column_name: pl.Expr}。

    - 只处理 enabled != False 的信号。
    - 单个信号内多条件用 ``&`` 串联（AND）。
    - allow_shift=False 时, 跳过带日期偏移 (leftDays/rightDays>0) 的信号
      (盘中单日快照上 .shift 跨 symbol 语义不正确, 优雅降级)。
    - 编译失败的信号被跳过并告警（不影响其它信号）。
    """
    out: dict[str, pl.Expr] = {}
    string_fields = fields.string_fields
    for sig in signals:
        if sig.get("enabled") is False:
            continue
        try:
            conds = sig["conditions"]
            col_name = resolve_column(sig)
            parts: list[pl.Expr] = []
            for c in conds:
                left_days = int(c.get("leftDays", 0) or 0)
                right_days = int(c.get("rightDays", 0) or 0)
                # 盘中路径不支持偏移 → 跳过整个信号
                if not allow_shift and (left_days > 0 or right_days > 0):
                    raise ValueError("盘中实时路径不支持日期偏移条件, 已跳过")
                left = c["left"]
                op = c["op"]
                is_str = left in string_fields
                if is_str and op not in STRING_OPS:
                    raise ValueError(f"字符串字段 {left!r} 不支持运算符 {op!r}")
                if op == "contains" and not is_str:
                    raise ValueError(f"contains 仅用于字符串扩展字段: {left!r}")
                kind, val = _parse_right(c["right"], string_mode=is_str, fields=fields)
                right_expr = _col(val, right_days) if kind == "field" else val
                parts.append(_OP_BUILDERS[op](_col(left, left_days), right_expr))
            combined = parts[0]
            for p in parts[1:]:
                combined = combined & p
            out[col_name] = combined
        except Exception as e:
            logger.warning("custom signal compile failed %s: %s", sig.get("id"), e)
    return out


def prev_day_fields(signals: list[dict]) -> set[str]:
    """全部定义里以 leftDays/rightDays=1 引用的根字段集合。

    供当日(实时)路径预 join 昨日值: compute_enriched_today 据此 select
    ``_prev_{field}`` 列, build_expressions_prev 再把它们映射进表达式。
    偏移 >1 的字段当日路径无解 (需多日历史), 不在此列。
    """
    fields: set[str] = set()
    for sig in signals:
        for c in sig.get("conditions") or []:
            if not isinstance(c, dict):
                continue
            if int(c.get("leftDays", 0) or 0) == 1 and isinstance(c.get("left"), str):
                fields.add(c["left"])
            right = c.get("right")
            if (
                int(c.get("rightDays", 0) or 0) == 1
                and isinstance(right, str)
                and right.startswith("field:")
            ):
                fields.add(right[len("field:"):])
    return fields


def build_expressions_prev(
    signals: list[dict],
    prev_available: set[str],
    *, fields: SignalFields = DEFAULT_FIELDS,
) -> dict[str, pl.Expr]:
    """当日(实时)路径的表达式构建: 偏移=1 的字段改读 ``_prev_{field}`` join 列。

    - 昨日值列缺失 (prev_available 不含该字段) 或偏移 >1 的信号被跳过并告警
      (单日快照无法回看多日; 与旧内置行为的差异在于昨日值由 join 提供)。
    - 其余编译逻辑与 build_expressions 完全同源。
    """
    def _col_prev(name: str, days: int) -> pl.Expr:
        if days == 0:
            return pl.col(name)
        if days == 1:
            return pl.col(f"_prev_{name}")
        raise ValueError(f"当日路径不支持 {days} 日偏移: {name}")

    out: dict[str, pl.Expr] = {}
    string_fields = fields.string_fields
    for sig in signals:
        if sig.get("enabled") is False:
            continue
        try:
            col_name = resolve_column(sig)
            parts: list[pl.Expr] = []
            for c in sig["conditions"]:
                left_days = int(c.get("leftDays", 0) or 0)
                right_days = int(c.get("rightDays", 0) or 0)
                if left_days > 1 or right_days > 1:
                    raise ValueError("当日实时路径不支持 >1 日偏移, 已跳过")
                left = c["left"]
                op = c["op"]
                is_str = left in string_fields
                kind, val = _parse_right(c["right"], string_mode=is_str, fields=fields)
                # 偏移字段须已由调用方 join 出 _prev_ 列
                for name, days in ((left, left_days), (val if kind == "field" else None, right_days)):
                    if name is None:
                        continue
                    if days == 1 and name not in prev_available:
                        raise ValueError(f"当日路径缺少昨日值列: _prev_{name}")
                right_expr = _col_prev(val, right_days) if kind == "field" else val
                parts.append(_OP_BUILDERS[op](_col_prev(left, left_days), right_expr))
            combined = parts[0]
            for p in parts[1:]:
                combined = combined & p
            out[col_name] = combined
        except Exception as e:
            logger.warning("custom signal (today) compile failed %s: %s", sig.get("id"), e)
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


def intraday_column_name(signal_id: str) -> str:
    """盘中信号 id → 分钟帧列名(加 csgi_ 前缀)。"""
    return f"{INTRADAY_PREFIX}{signal_id}"


def is_intraday_column(name: str) -> bool:
    return name.startswith(INTRADAY_PREFIX) and ID_RE.fullmatch(name.removeprefix(INTRADAY_PREFIX)) is not None


def _parse_right_intraday(right: object) -> tuple[str, object]:
    """盘中条件的右值: ('const', float) 或 ('field', 特征名)。"""
    if isinstance(right, (int, float)):
        return ("const", float(right))
    if not isinstance(right, str):
        raise ValueError(f"非法右值: {right!r}")
    if right.startswith("field:"):
        col = right[len("field:"):]
        if col not in INTRADAY_FEATURES:
            raise ValueError(f"盘中右值字段不在白名单: {col}")
        return ("field", col)
    try:
        return ("const", float(right))
    except ValueError:
        pass
    if right in INTRADAY_FEATURES:
        return ("field", right)
    raise ValueError(f"非法盘中右值(应为 field:特征 或数字): {right!r}")


def _validate_intraday(sig: dict) -> None:
    """校验盘中信号定义, 非法抛 ValueError。"""
    conds = sig.get("conditions")
    for i, c in enumerate(conds):
        if not isinstance(c, dict):
            raise ValueError(f"第 {i+1} 个条件格式错误")
        left = c.get("left", "")
        if left not in INTRADAY_FEATURES:
            raise ValueError(f"第 {i+1} 个条件: 盘中字段 {left!r} 不在白名单")
        if c.get("op") not in INTRADAY_OPS:
            raise ValueError(f"第 {i+1} 个条件: 运算符 {c.get('op')!r} 非法(盘中额外支持 cross_up/cross_down)")
        _parse_right_intraday(c.get("right"))
        if int(c.get("leftDays", 0) or 0) or int(c.get("rightDays", 0) or 0):
            raise ValueError(f"第 {i+1} 个条件: 盘中信号不支持日期偏移(leftDays/rightDays)")
    min_bars = sig.get("min_bars", 0)
    try:
        n = int(min_bars)
    except (TypeError, ValueError):
        raise ValueError(f"min_bars 必须是整数: {min_bars!r}")  # noqa: B904
    if n < 0 or n > 240:
        raise ValueError(f"min_bars 必须在 0..240 之间: {n}")


def build_intraday_expressions(signals: list[dict]) -> dict[str, pl.Expr]:
    """把盘中信号编译为特征帧上的「条件」表达式(AND 组合, 未做上升沿)。

    表达式在 intraday_features.build_feature_frame 产出的帧上求值;
    上升沿须通过 apply_intraday_edges 在 DataFrame 层两步计算 —
    对已含 .over() 窗口的组合表达式直接 shift().over() 是窗口嵌套,
    Polars 会返回全 null。编译失败的信号跳过并告警。
    """
    out: dict[str, pl.Expr] = {}
    for sig in signals:
        if sig.get("enabled") is False or sig.get("timeframe") != TIMEFRAME_INTRADAY:
            continue
        try:
            parts: list[pl.Expr] = []
            for c in sig["conditions"]:
                left = pl.col(c["left"])
                kind, val = _parse_right_intraday(c["right"])
                op = c["op"]
                if op == "cross_up":
                    # 前一根 bar 未满足 且 当前 bar 满足; 右值为常量时不 shift 字面量
                    if kind == "field":
                        prev_ok = left.shift(1).over(_EDGE_GROUP) <= pl.col(val).shift(1).over(_EDGE_GROUP)
                        cur_ok = left > pl.col(val)
                    else:
                        prev_ok = left.shift(1).over(_EDGE_GROUP) <= val
                        cur_ok = left > val
                    parts.append(prev_ok & cur_ok)
                elif op == "cross_down":
                    if kind == "field":
                        prev_ok = left.shift(1).over(_EDGE_GROUP) >= pl.col(val).shift(1).over(_EDGE_GROUP)
                        cur_ok = left < pl.col(val)
                    else:
                        prev_ok = left.shift(1).over(_EDGE_GROUP) >= val
                        cur_ok = left < val
                    parts.append(prev_ok & cur_ok)
                else:
                    right = pl.col(val) if kind == "field" else val
                    parts.append(_OP_BUILDERS[op](left, right))
            combined = parts[0]
            for p in parts[1:]:
                combined = combined & p
            out[intraday_column_name(sig["id"])] = combined
        except Exception as e:
            logger.warning("intraday signal compile failed %s: %s", sig.get("id"), e)
    return out


def apply_intraday_edges(frame: pl.DataFrame, exprs: dict[str, pl.Expr]) -> pl.DataFrame:
    """对特征帧求值盘中信号: 先算条件列, 再取「当日条件上升沿」为布尔列。

    上升沿: 条件 false→true 的那根 bar 为 true; 首根 bar(前值为 null)不触发;
    条件含 null(特征不足)视为 false。四条消费路径(监控/实盘/回测/回放)
    必须共用本函数, 保证口径一致。
    """
    if frame.is_empty() or not exprs:
        return frame
    df = frame.with_columns([e.fill_null(False).alias(n) for n, e in exprs.items()])
    return df.with_columns([
        (
            pl.col(n)
            & ~pl.col(n).shift(1).over(_EDGE_GROUP).fill_null(True)
        ).cast(pl.Boolean).alias(n)
        for n in exprs
    ])


def inject_intraday_columns(minutes: pl.DataFrame, features: pl.DataFrame, exprs: dict[str, pl.Expr]) -> pl.DataFrame:
    """Join computed edges to the original minute rows without changing their order."""
    if features.is_empty() or not exprs:
        return minutes
    keys = ["symbol", "datetime"]
    evaluated = apply_intraday_edges(features, exprs).select(*keys, *exprs)
    return minutes.join(evaluated, on=keys, how="left", maintain_order="left").with_columns(
        pl.col(list(exprs)).fill_null(False).cast(pl.Boolean)
    )
