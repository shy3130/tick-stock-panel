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

from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

# ── 常量 ────────────────────────────────────────────────
PREFIX = "csg_"                       # 自定义信号列名前缀
ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
OPS = {">", ">=", "<", "<=", "==", "!="}
# string 扩展字段 (概念/行业归属等) 的运算符: contains 为字面量包含
# (非正则, 用户输入不进入 pattern 编译), ==/!= 为字符串精确比较。
STRING_OPS = {"contains", "==", "!="}
_MAX_STR_RIGHT = 64

# 字段白名单：只允许这些列出现在条件里（防注入）。均为数值型。
# 与 ENRICHED_COLUMNS 的数值列保持一致，排除 symbol/date/name 等非数值列。
ALLOWED_FIELDS: frozenset[str] = frozenset({
    # 行情
    "open", "high", "low", "close", "volume", "amount", "turnover_rate",
    "consecutive_limit_ups", "consecutive_limit_downs",
    # 不复权价: 按实际价位设条件时用, 前复权价会被分红送转整体下调
    "raw_close", "raw_high", "raw_low",
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

# 运算符 → Polars 表达式构造器（输入 col_expr, value）
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


def _string_ext_fields() -> frozenset[str]:
    """string 扩展字段列名 (概念/行业等); 解析/校验按字段 dtype 分发。"""
    try:
        from app.factors.ext_factors import ext_string_fields

        return ext_string_fields()
    except Exception:
        return frozenset()


def allowed_fields() -> frozenset[str]:
    """条件可引用字段 = 物化列白名单 并入 注册表因子 与 string 扩展字段。

    因子列在历史路径 (compute_signals) 由 materialize_factor_columns 复用
    评分物化管线补算; 盘中单日快照无滚动窗口, 依赖因子的信号被 inject 以
    缺列告警跳过 (与日期偏移条件同样的优雅降级)。
    string 扩展字段 (ext_{表}_{字段}, 概念/行业归属) 只支持 contains/==/!=,
    在帧组装时由 attach_ext_columns 注入, 不注册为因子 (数值口径约束)。
    """
    from app.factors.registry import all_factors

    return frozenset(ALLOWED_FIELDS | {spec.id for spec in all_factors()} | _string_ext_fields())


def materialize_factor_columns(
    df: pl.DataFrame,
    exprs: dict[str, pl.Expr],
    needed: set[str] | None = None,
) -> pl.DataFrame:
    """把信号表达式引用、且 df 缺失的注册表因子列补算出来。

    复用评分物化路径 (materialize_scoring_columns) — 与检验/评分同一条计算
    逻辑, 不引入第二套实现。非注册表列不在此处理 (缺列仍由 inject 告警跳过)。
    """
    if df.is_empty() or not exprs:
        return df
    cols = set(df.columns)
    missing: set[str] = set()
    for name, roots in expression_dependencies(exprs).items():
        if needed is not None and name not in needed:
            continue
        missing.update(root for root in roots if root not in cols)
    if not missing:
        return df
    from app.factors.registry import all_factors

    factor_ids = {spec.id for spec in all_factors()}
    to_compute = missing & factor_ids
    if not to_compute:
        return df
    from app.strategy.scoring import materialize_scoring_columns

    return materialize_scoring_columns(df, sorted(to_compute))


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
    atomic_write_text(p, json.dumps(sig, ensure_ascii=False, indent=2))


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


def _parse_right(right: str, *, string_mode: bool = False) -> tuple[str, object]:
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
    allowed = allowed_fields()
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


def validate(sig: dict) -> None:
    """校验一个信号定义，非法则抛 ValueError（含中文信息）。"""
    sid = sig.get("id", "")
    if not isinstance(sid, str) or not ID_RE.match(sid):
        raise ValueError(f"信号 id 非法（仅小写字母数字下划线，1-40字符）: {sid!r}")
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
    string_fields = _string_ext_fields()
    for i, c in enumerate(conds):
        if not isinstance(c, dict):
            raise ValueError(f"第 {i+1} 个条件格式错误")
        left = c.get("left", "")
        if left not in allowed_fields():
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
        _parse_right(c.get("right"), string_mode=is_str)   # 会校验右值字段/数字/字符串
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
    string_fields = _string_ext_fields()
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
                is_str = left in string_fields
                if is_str and op not in STRING_OPS:
                    raise ValueError(f"字符串字段 {left!r} 不支持运算符 {op!r}")
                if op == "contains" and not is_str:
                    raise ValueError(f"contains 仅用于字符串扩展字段: {left!r}")
                kind, val = _parse_right(c["right"], string_mode=is_str)
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


# ══ 盘中信号(timeframe="intraday")═════════════════════════
# 与日线自定义信号同一套 left/op/right 条件结构, 但:
#   - 字段白名单换成分钟特征(intraday_features.INTRADAY_FEATURES);
#   - 运算符额外支持 cross_up / cross_down(序列上穿/下穿 另一序列或阈值);
#   - 不支持 leftDays/rightDays 日期偏移;
#   - 信号列名前缀 csgi_, 注入对象是分钟特征帧而非日线 enriched。
# 语义: 信号输出 = 当日条件组合的上升沿(false→true), 首根 bar 不触发。

from app.strategy.intraday_features import INTRADAY_FEATURES  # noqa: E402

TIMEFRAME_DAILY = "daily"
TIMEFRAME_INTRADAY = "intraday"
INTRADAY_PREFIX = "csgi_"
INTRADAY_OPS = OPS | {"cross_up", "cross_down"}
_EDGE_GROUP = ["symbol", "date"]


def intraday_column_name(signal_id: str) -> str:
    """盘中信号 id → 分钟帧列名(加 csgi_ 前缀)。"""
    return f"{INTRADAY_PREFIX}{signal_id}"


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


# ── 盘中信号定义加载(带指纹缓存: 引擎/监控高频路径用) ──────────
_intraday_cache: dict[Path, tuple[object, list[dict]]] = {}


def _dir_fingerprint(d: Path) -> tuple:
    """目录内 *.json 的 (文件名, mtime) 指纹 — 创建/删除/编辑都会变化。"""
    try:
        return tuple(sorted((f.name, f.stat().st_mtime_ns) for f in d.glob("*.json")))
    except OSError:
        return ()


def load_intraday_all(data_dir: Path) -> list[dict]:
    """读取全部启用的盘中信号定义(带缓存)。

    盘中评估与引擎注入每分钟执行, 不宜每次全量读盘; save/delete 端点
    调用 invalidate_intraday_cache() 主动失效。
    """
    d = _dir(data_dir)
    fp = _dir_fingerprint(d)
    cached = _intraday_cache.get(data_dir)
    if cached is not None and cached[0] == fp:
        return cached[1]
    sigs = [
        s for s in load_all(data_dir)
        if s.get("timeframe") == TIMEFRAME_INTRADAY and s.get("enabled") is not False
    ]
    _intraday_cache[data_dir] = (fp, sigs)
    return sigs


def invalidate_intraday_cache() -> None:
    _intraday_cache.clear()


# ── 信号命名映射 (告警文案把 csg_/csgi_ 列名翻译成用户命名) ──────────
_names_cache: dict[Path, tuple[object, dict[str, str]]] = {}


def signal_names(data_dir: Path) -> dict[str, str]:
    """自定义信号列名 (csg_/csgi_) → 用户命名的映射, 带目录指纹缓存。

    指纹含文件名 + mtime, 保存/删除信号后自动失效重载, 调用方无需配合失效。
    """
    d = _dir(data_dir)
    fp = _dir_fingerprint(d)
    cached = _names_cache.get(data_dir)
    if cached is not None and cached[0] == fp:
        return cached[1]
    names: dict[str, str] = {}
    for s in load_all(data_dir):
        sid, name = s.get("id"), s.get("name")
        if sid and name:
            names[column_name(sid)] = name
            names[intraday_column_name(sid)] = name
    _names_cache[data_dir] = (fp, names)
    return names
