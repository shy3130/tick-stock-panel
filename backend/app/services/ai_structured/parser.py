"""模型输出解析器 — 仅做可证明安全的 JSON 提取与有限修复。"""
from __future__ import annotations

import json
import re
from typing import Any

from app.services.ai_structured.models import AIValidationIssue

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _issue(category: str, message: str, *, detail: dict[str, Any] | None = None) -> AIValidationIssue:
    return AIValidationIssue(category=category, message=message, detail=detail)


def _fence_body(text: str) -> str | None:
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


def _outer_json(text: str) -> str:
    s = text.strip()
    obj, arr = s.find("{"), s.find("[")
    if obj < 0 and arr < 0:
        return s
    if arr < 0 or (obj >= 0 and obj < arr):
        end = s.rfind("}")
        start = obj
    else:
        end = s.rfind("]")
        start = arr
    return s[start:end + 1] if end > start else s


def _trim_trailing_commas(text: str) -> str:
    return re.sub(r",(\s*[}\]])", r"\1", text)


def _safe_variants(raw: str) -> list[str]:
    """按安全程度产生有限候选，不修改字段值。"""
    s = raw.strip()
    fenced = _fence_body(s)
    if fenced is not None:
        s = fenced
    variants = [s]
    outer = _outer_json(s)
    if outer != s:
        variants.append(outer)
    # 控制字符只会出现在字符串中且 JSON 不允许，删除是唯一不猜值的修复。
    clean = _CTRL_RE.sub("", s)
    if clean not in variants:
        variants.append(clean)
    clean_outer = _outer_json(clean)
    if clean_outer not in variants:
        variants.append(clean_outer)
    trimmed = _trim_trailing_commas(clean_outer)
    if trimmed not in variants:
        variants.append(trimmed)
    return variants


def parse_json(text: str) -> tuple[dict[str, Any] | list[Any] | None, list[AIValidationIssue]]:
    """解析模型输出，返回 `(value, issues)`；不补字段、不猜值。"""
    if not isinstance(text, str) or not text.strip():
        return None, [_issue("plaintext", "模型未返回 JSON 输出")]
    variants = _safe_variants(text)
    for idx, candidate in enumerate(variants):
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
        return None, [_issue("syntax", "JSON 语法无效", detail={"line": last.lineno, "column": last.colno})]
    return None, [_issue("plaintext", "模型返回自然语言而非 JSON")]


def parse_ai_output(text: str) -> tuple[dict[str, Any] | list[Any] | None, list[AIValidationIssue]]:
    """兼容别名，供 runtime 与调用方使用。"""
    return parse_json(text)


def extract_json_text(text: str) -> str | None:
    """只提取外层 JSON 文本，不解析/修复；无法提取时返回 None。"""
    if not isinstance(text, str) or not text.strip():
        return None
    s = _fence_body(text) or text.strip()
    out = _outer_json(s)
    return out if out.startswith(("{", "[")) and out.endswith(("}", "]")) else None
