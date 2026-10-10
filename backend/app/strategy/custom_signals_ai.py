"""AI 生成自定义信号 — 组装提示词 + 解析并校验 AI 返回的结构化条件。

职责:
  - build_messages(description): 把用户一句描述 + 字段白名单/运算符/格式要求组装成 LLM 消息
  - parse_and_validate(text): 把 LLM 返回的 JSON 解析为 {name, conditions}，并复用
    custom_signals.validate() 做白名单/运算符/偏移校验（安全闸门）

不知道: API、AI 调用、持久化。纯函数，无副作用。
"""
from __future__ import annotations

import json
import re

from app.indicators.pipeline import ENRICHED_COLUMNS, ENRICHED_COLUMNS_BY_CATEGORY
from app.strategy import custom_signals

# 分类 → 中文标签（与 /api/custom-signals/options 的分组一致）
_GROUP_LABELS = {
    "basic": "基础", "ma": "均线 MA", "ema": "指数均线 EMA",
    "macd": "MACD", "boll": "布林带 BOLL", "kdj": "KDJ",
    "atr": "ATR", "volume": "量价", "extremes": "极值",
    "momentum": "动量", "volatility": "波动率", "rsi": "RSI",
}
# 行情类字段不在 ENRICHED_COLUMNS_BY_CATEGORY 里, 单独归一组
_QUOTE_FIELDS = {
    "open", "high", "low", "close", "volume", "amount", "turnover_rate",
    "consecutive_limit_ups", "consecutive_limit_downs",
}

_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL)


def _format_fields() -> str:
    """按类别格式化白名单字段(key(中文标签)), 供 LLM 参考.

    行情/指标类物理列之后追加注册表因子, 分组与 /api/custom-signals/options
    的 factor 分组一致: 因子是预计算因子值, 同样可作为条件字段比较.
    """
    allowed = custom_signals.allowed_fields()
    lines: list[str] = []
    quote = sorted(f for f in _QUOTE_FIELDS if f in allowed)
    lines.append(
        "行情: " + ", ".join(f"{f}({ENRICHED_COLUMNS.get(f, f)})" for f in quote)
    )
    for cat, label in _GROUP_LABELS.items():
        fields = [f for f in ENRICHED_COLUMNS_BY_CATEGORY.get(cat, []) if f in allowed]
        if fields:
            lines.append(
                f"{label}: "
                + ", ".join(f"{f}({ENRICHED_COLUMNS.get(f, f)})" for f in fields)
            )
    from app.factors.registry import all_factors

    factor_groups: dict[str, list[str]] = {}
    for spec in all_factors():
        if spec.id in custom_signals.ALLOWED_FIELDS:
            continue  # 已作为物理列出现在清单里
        label = spec.label
        if spec.asset_types == frozenset({"stock"}):
            label += "·仅股票"
        factor_groups.setdefault(spec.group, []).append(f"{spec.id}({label})")
    for group, items in sorted(factor_groups.items()):
        lines.append(f"因子·{group}: " + ", ".join(sorted(items)))
    # string 扩展字段 (概念/行业归属): 只支持 contains/==/!=, 右值为字符串字面量
    from app.factors.ext_factors import ext_string_field_entries

    str_entries = ext_string_field_entries()
    if str_entries:
        lines.append(
            "字符串字段(仅 contains/==/!=): "
            + ", ".join(f"{e['key']}({e['label']})" for e in str_entries)
        )
    return "\n".join(lines)


_SYSTEM_TEMPLATE = """你是A股量化信号设计专家。用户会描述一个信号思路，你要把它拆解为布尔条件组合（多条件之间是「且」关系，即同时满足），并输出结构化 JSON 供系统编译为策略/回测/监控信号。

可用字段（白名单，只能使用以下字段，禁止自造或使用白名单之外的字段）：
{fields}
其中「因子·」开头的行是平台预计算的因子值（动量/波动/量价等衍生特征），可直接比较数值构造条件。

运算符（op）：>  >=  <  <=  ==  !=
字符串字段额外支持 contains(包含子串, 如概念/行业归属判断), 右值为字符串字面量, 如 "AI"、"半导体".

右值（right）：
- 数字：写字符串形式，如 "2"、"3000"、"0.05"
- 另一字段：必须带 "field:" 前缀，如 "field:ma20"；严禁裸写字段名，如 "macd_dea" 应写成 "field:macd_dea"
- 字符串字面量: 仅当左字段是「字符串字段」时使用(配合 contains/==/!=), 如所属概念包含AI写成 {{"left": "字符串字段", "op": "contains", "right": "AI", "leftDays": 0, "rightDays": 0}}

日期偏移（leftDays / rightDays）：取 N 个交易日前的值，0 = 当日最新；范围 0~{max_days}。只有明确需要「前N日」时才使用偏移。

要求：
1. 只输出一个 JSON 对象，禁止 markdown 代码块、禁止任何解释或多余文字。
2. JSON 结构固定为：
{{"name": "简短中文信号名称(≤12字)", "conditions": [
  {{"left": "字段", "op": "运算符", "right": "数字字符串或field:字段", "leftDays": 0, "rightDays": 0}}
]}}
   示例（右值引用另一字段时必须带 field: 前缀，不能裸写字段名）：
   {{"name": "MACD金叉", "conditions": [
     {{"left": "macd_dif", "op": ">", "right": "field:macd_dea", "leftDays": 0, "rightDays": 0}}
   ]}}
3. conditions 至少 1 个、最多 8 个；优先用最少的条件表达清晰的思路。
4. 多条件必须能同时满足，不要输出互相矛盾的条件。"""


def build_messages(description: str) -> list[dict]:
    """组装 LLM 消息：[system 提示词, user 描述]。"""
    system = _SYSTEM_TEMPLATE.format(
        fields=_format_fields(),
        max_days=custom_signals.MAX_DAYS,
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": description},
    ]


def parse_and_validate(text: str) -> dict:
    """解析并校验 AI 返回的 JSON → {"name", "conditions"}。非法时抛 ValueError。

    只取 name 与 conditions；id / kind 由用户在表单里填写，不信任 AI。
    """
    raw = _extract_json_object(text)
    if not isinstance(raw, dict):
        raise ValueError("AI 返回的 JSON 不是对象")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("AI 未返回信号名称 name")
    name = name.strip()[:30]
    conditions_raw = raw.get("conditions")
    if not isinstance(conditions_raw, list) or not conditions_raw:
        raise ValueError("AI 未返回任何条件 conditions")

    conditions = [_normalize_condition(c) for c in conditions_raw]

    # 复用现有白名单/运算符/偏移校验作为安全闸门（id/kind 用占位值）。
    probe = {
        "id": "aigenerated",
        "name": name,
        "kind": "both",
        "conditions": conditions,
    }
    custom_signals.validate(probe)
    return {"name": name, "conditions": conditions}


def _normalize_condition(c: object) -> dict:
    if not isinstance(c, dict):
        raise ValueError("条件的每一项必须是 JSON 对象")
    left = c.get("left")
    op = c.get("op")
    right = c.get("right")
    if right is None:
        raise ValueError("条件缺少右值 right")
    if isinstance(right, bool):
        raise ValueError("右值不能是布尔值")
    if isinstance(right, (int, float)):
        right = _num_to_str(right)
    if not isinstance(right, str) or not right.strip():
        raise ValueError(f"右值非法: {right!r}")
    right = right.strip()
    # 兜底: AI 偶尔漏写 field: 前缀的裸字段名, 补全为规范形式 (含因子字段)
    if not right.startswith("field:") and right in custom_signals.allowed_fields():
        right = f"field:{right}"
    return {
        "left": str(left),
        "op": str(op),
        "right": right,
        "leftDays": _norm_days(c.get("leftDays")),
        "rightDays": _norm_days(c.get("rightDays")),
    }


def _norm_days(value: object) -> object:
    """归一化日期偏移为 int（缺省 0）；无法转换时原样返回，由 validate 报中文错误。"""
    if value is None:
        return 0
    if isinstance(value, bool):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _num_to_str(value) -> str:
    if isinstance(value, int):
        return str(value)
    f = float(value)
    return str(int(f)) if f.is_integer() else str(f)


def _extract_json_object(text: str) -> object:
    """从 LLM 文本提取 JSON 对象（多级容错）。

    依次尝试: 整段 → markdown 围栏内 → 首个 {...} 平衡块;
    每级再对 尾随垃圾 / 尾逗号 做轻量修复。全部失败才报错。
    """
    source = text or ""
    candidates: list[str] = []
    stripped = source.strip()
    if stripped:
        candidates.append(stripped)
    candidates.extend(
        match.group(1).strip() for match in _FENCED_JSON_RE.finditer(source)
    )
    brace = _first_brace_block(source)
    if brace and brace.strip() not in candidates:
        candidates.append(brace)
    last_error: Exception | None = None
    for candidate in candidates:
        parsed = _try_parse_json(candidate)
        if parsed is not None:
            return parsed
        try:
            json.loads(candidate)
        except json.JSONDecodeError as e:
            last_error = e
    raise ValueError(f"AI 返回的不是合法 JSON: {last_error}")


def _try_parse_json(candidate: str) -> object | None:
    """尽力解析一段可能带尾随垃圾 / 尾逗号的 JSON；失败返 None。"""
    variants = [candidate.strip()]
    last = candidate.rfind("}")
    if last >= 0 and last < len(candidate) - 1:
        variants.append(candidate[:last + 1].strip())
    for v in variants:
        if not v:
            continue
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            pass
        # 去掉数组/对象结尾的多余逗号 (AI 常见错误): `,}` / `,]`
        cleaned = re.sub(r",\s*([}\]])", r"\1", v)
        if cleaned != v:
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                pass
    return None


def _first_brace_block(text: str) -> str:
    """括号配对截取首个 {...} 块（AI 偶尔混入前后解释文字时的兜底）。"""
    start = text.find("{")
    if start < 0:
        return text
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]
