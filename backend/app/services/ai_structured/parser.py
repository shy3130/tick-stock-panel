"""模型输出解析器 — 仅做可证明安全的 JSON 提取与有限修复。

移植自 PA_Agent json_validator.py 的安全修复算法（peek-ahead 引号、智能引号归一化、
分号分隔符修复、控制字符转义、花括号深度跟踪），适配 tickflow 的 multi-variant 风格。
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.services.ai_structured.models import AIValidationIssue

# ── 模型输出常见 Unicode 瑕疵 → ASCII 归一化 ──────────────────────────────
_SMART_QUOTE_MAP = {
    "\u201c": '"',   # " → "
    "\u201d": '"',   # " → "
    "\u2018": "'",   # ' → '
    "\u2019": "'",   # ' → '
    "\u2013": "-",   # en-dash
    "\u2014": "-",   # em-dash
}

# JSON 结构性字符 — peek-ahead 判定引号是否为字符串结束
_STRING_END_CHARS = frozenset(",:}]")


def _issue(category: str, message: str, *, detail: dict[str, Any] | None = None) -> AIValidationIssue:
    return AIValidationIssue(category=category, message=message, detail=detail)


# ── Fence / 提取 ──────────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_TRAILING_FENCE_RE = re.compile(r"\n?```\s*$")
_LEADING_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?", re.IGNORECASE)


def _fence_body(text: str) -> str | None:
    """提取 ```json ... ``` 围栏内的 JSON 文本。"""
    s = text.strip()
    if not s.startswith("```"):
        return None
    rest = s[3:]
    nl = rest.find("\n")
    if nl < 0:
        return rest[:-3].strip() if rest.endswith("```") else None
    body = rest[nl + 1:]
    end = body.rfind("```")
    return body[:end].strip() if end >= 0 else None


def _normalize_unicode(text: str) -> str:
    """归一化智能引号和特殊破折号为 ASCII 等价物。"""
    for bad, good in _SMART_QUOTE_MAP.items():
        text = text.replace(bad, good)
    return text


def _extract_outer_json_object(text: str) -> str:
    """花括号深度跟踪提取首个完整 `{...}` 或 `[...]`，忽略后续 prose。

    比简单 rfind 更精确：当 prose 中含有 `}` 时不被干扰。
    """
    s = text.strip()
    start_brace = s.find("{")
    start_bracket = s.find("[")

    if start_brace < 0 and start_bracket < 0:
        return s

    # 选最先出现者
    if start_bracket < 0 or (start_brace >= 0 and start_brace < start_bracket):
        opener, closer = "{", "}"
        start = start_brace
    else:
        opener, closer = "[", "]"
        start = start_bracket

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return s[start:].strip()


# ── 安全修复算法（移植自 PA_Agent，适配 tickflow 风格） ────────────────────

def _repair_unescaped_quotes(text: str) -> str:
    """Escape 字符串值内未转义的 ``"``。

    Peek-ahead 启发式：只有当引号后第一个非空字符是结构性字符 (`,:}]`) 或 EOF 时，
    才认为引号是字符串结束符；否则视为值内的未转义引号，加反斜杠。
    """
    out: list[str] = []
    in_string = False
    escape = False
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]
        if not in_string:
            if ch == '"':
                in_string = True
            out.append(ch)
            i += 1
            continue

        if escape:
            escape = False
            out.append(ch)
            i += 1
            continue
        if ch == "\\":
            escape = True
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j >= n or text[j] in _STRING_END_CHARS:
                in_string = False
                out.append(ch)
            else:
                out.append('\\"')
            i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def _repair_semicolon_separator(text: str) -> str:
    """替换字段间的分号为逗号。

    模型偶尔输出 ``"field": "value";`` 而非 ``,``。
    只替换字符串外、后跟 ``"`` / ``}`` / ``]`` 的 ``;``。
    """
    out: list[str] = []
    in_string = False
    escape = False
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == ";":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in ('"', '}', ']'):
                out.append(",")
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _escape_control_chars_in_strings(text: str) -> str:
    """转义 JSON 字符串字面量内的原始控制字符（而非删除）。

    保留数据完整性，符合 JSON 规范要求。
    """
    out: list[str] = []
    in_string = False
    escape = False
    for ch in text:
        if not in_string:
            if ch == '"':
                in_string = True
            out.append(ch)
            continue
        if escape:
            escape = False
            out.append(ch)
            continue
        if ch == "\\":
            escape = True
            out.append(ch)
            continue
        if ch == '"':
            in_string = False
            out.append(ch)
            continue
        if ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ch < " ":
            continue
        else:
            out.append(ch)
    return "".join(out)


def _trim_trailing_commas(text: str) -> str:
    return re.sub(r",(\s*[}\]])", r"\1", text)


# ── multi-variant 解析管线 ────────────────────────────────────────────────

def _strip_and_normalize(raw: str) -> str:
    """围栏剥离 + Unicode 归一化。"""
    s = raw.strip()
    # 先归一化智能引号
    s = _normalize_unicode(s)
    # 尝试嵌入式 fence（prose 后跟 ```json```）
    m = _FENCE_RE.search(s)
    if m:
        return m.group(1).strip()
    # 全围栏 ```` ```json ... ``` ````
    if s.startswith("```"):
        m2 = _FENCE_RE.search(s)
        if m2:
            return m2.group(1).strip()
        s = _LEADING_FENCE_RE.sub("", s, count=1).strip()
    # 尾部残留 ```
    s = _TRAILING_FENCE_RE.sub("", s).strip()
    return s


def _safe_variants(raw: str) -> list[str]:
    """按安全程度产生有限候选，不修改字段值。

    管线顺序：
    1. 围栏剥离 + Unicode 归一化
    2. 原始提取（花括号深度跟踪）
    3. 控制字符转义
    4. 未转义引号修复
    5. 分号分隔符修复
    6. 尾逗号清理
    """
    s = _strip_and_normalize(raw)
    variants: list[str] = [s]

    outer = _extract_outer_json_object(s)
    if outer != s:
        variants.append(outer)

    # 控制字符转义（保留数据，比删除更安全）
    escaped = _escape_control_chars_in_strings(outer)
    if escaped not in variants:
        variants.append(escaped)

    # 未转义引号修复
    repaired_quotes = _repair_unescaped_quotes(escaped)
    if repaired_quotes not in variants:
        variants.append(repaired_quotes)

    # 分号分隔符修复
    repaired_semi = _repair_semicolon_separator(repaired_quotes)
    if repaired_semi not in variants:
        variants.append(repaired_semi)

    # 尾逗号清理
    trimmed = _trim_trailing_commas(repaired_semi)
    if trimmed not in variants:
        variants.append(trimmed)

    return variants


def parse_json(text: str) -> tuple[dict[str, Any] | list[Any] | None, list[AIValidationIssue]]:
    """解析模型输出，返回 `(value, issues)`；不补字段、不猜值。"""
    if not isinstance(text, str) or not text.strip():
        return None, [_issue("plaintext", "模型未返回 JSON 输出")]
    variants = _safe_variants(text)
    last: json.JSONDecodeError | None = None
    for candidate in variants:
        try:
            value = json.loads(candidate)
            if not isinstance(value, (dict, list)):
                return None, [_issue("invalid", "JSON 顶层必须是对象或数组")]
            return value, []
        except json.JSONDecodeError as exc:
            last = exc
            continue
    # 有括号但未能解析 -> syntax；没有任何 JSON 起始符 -> plaintext
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("[") or "{" in stripped or "[" in stripped:
        detail: dict[str, Any] = {"line": last.lineno, "column": last.colno} if last else {}
        return None, [_issue("syntax", "JSON 语法无效", detail=detail)]
    return None, [_issue("plaintext", "模型返回自然语言而非 JSON")]


def parse_ai_output(text: str) -> tuple[dict[str, Any] | list[Any] | None, list[AIValidationIssue]]:
    """兼容别名，供 runtime 与调用方使用。"""
    return parse_json(text)


def extract_json_text(text: str) -> str | None:
    """只提取外层 JSON 文本，不解析/修复；无法提取时返回 None。"""
    if not isinstance(text, str) or not text.strip():
        return None
    s = _strip_and_normalize(text)
    out = _extract_outer_json_object(s)
    return out if out.startswith(("{", "[")) and out.endswith(("}", "]")) else None
