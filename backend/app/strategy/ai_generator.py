"""AI 策略生成器 — 读取策略开发文档 + 调用 LLM 生成策略代码。

职责: 接收用户自然语言描述 → 读取 prompts/strategy-guide.md → 调用 LLM → 返回策略代码。
不知道: 引擎内部、API、前端、配置持久化、回测。
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 策略开发精简指南路径 (随 backend/app 打包进 Docker, 避免 .dockerignore 排除 docs/ 导致运行时缺失)
GUIDE_PATH = Path(__file__).resolve().parent / "prompts" / "strategy-guide-compact.md"

_SYSTEM_PREFIX = """你是A股量化策略设计专家。根据用户描述的需求，参考下方的《策略开发指南》生成一个完整的策略Python文件。

文件与范围铁律（不可违反）:
1. 只创建这一个策略文件：只生成一个 .py 文件，绝不创建多文件、不拆分模块、不跨文件引用
2. 绝不触碰项目源码：不要写任何会修改 backend/、docs/、frontend/ 等现有文件的代码；不要 import os/sys/pathlib 等文件系统模块
3. 不得放入内置策略目录：AI 生成的策略只属于 data/strategies/ai/，文件名/ID 用 ai_ 前缀；内置目录 backend/app/strategy/builtin/ 由项目维护，AI 不得染指
4. 只 import polars as pl，不 import 其他模块

要求:
1. 用户可能调整的策略阈值通过 META["params"] 暴露；公式常数、固定窗口边界、布尔开关不必强行参数化
2. 遵循指南中的文件结构，但优先贴合用户规则，不要为了套模板歪曲策略含义
3. ENTRY_SIGNALS/EXIT_SIGNALS 根据策略逻辑自行选择匹配的信号列，不要照搬示例
4. scoring 权重根据策略核心逻辑定制，总和 = 1.0
5. 优先使用 Polars 表达式、窗口函数、聚合和 with_columns/filter 实现，避免逐行/逐股 Python 循环；只有表达式难以描述的复杂状态机才使用 partition_by/to_dicts
6. 直接输出Python代码，不要输出其他内容

--- 策略开发指南 ---

"""


class AIStrategyGenerator:
    """AI 策略生成器"""

    def __init__(self) -> None:
        self._guide_cache: str | None = None

    def _get_guide(self) -> str:
        if self._guide_cache is None:
            if GUIDE_PATH.exists():
                self._guide_cache = GUIDE_PATH.read_text(encoding="utf-8")
            else:
                logger.warning("strategy guide not found at %s", GUIDE_PATH)
                self._guide_cache = ""
        return self._guide_cache

    async def generate(self, user_prompt: str) -> dict:
        """根据用户描述生成策略代码

        Returns: {"code": str, "meta": dict, "valid": bool, "error": str | None}
        """
        guide = self._get_guide()

        # 调用 LLM
        code = await self._call_llm(user_prompt, guide)
        return self.validate_code(code)

    async def stream(self, user_prompt: str):
        """Yield generated strategy code deltas from the configured AI provider."""
        from app.services.ai_provider import stream_ai_text

        guide = self._get_guide()
        async for chunk in stream_ai_text(
            [
                {"role": "system", "content": _SYSTEM_PREFIX + guide},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=3000,
        ):
            yield chunk

    def validate_code(self, code: str) -> dict:
        code = self._extract_code_block(code)

        # 验证
        try:
            meta = self.validate_code_or_raise(code)
        except (SyntaxError, ValueError) as e:
            return {"code": code, "meta": {}, "valid": False, "error": str(e)}

        return {"code": code, "meta": meta, "valid": True, "error": None}

    async def _call_llm(self, user_prompt: str, guide: str) -> str:
        """Call the configured AI provider and return generated strategy code."""
        from app.services.ai_provider import generate_ai_text

        content = await generate_ai_text(
            [
                {"role": "system", "content": _SYSTEM_PREFIX + guide},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=3000,
        )
        return self._extract_code_block(content)

    @staticmethod
    def _extract_code_block(content: str) -> str:
        # Extract fenced code if the model wrapped the answer in Markdown.
        if "```python" in content:
            return content.split("```python", 1)[1].split("```", 1)[0].strip()
        if "```" in content:
            return content.split("```", 1)[1].split("```", 1)[0].strip()
        return content.strip()

    # import 白名单: 策略文件只允许 polars (见 strategy-guide.md「只 import polars」)。
    # 白名单而非黑名单 — 黑名单挡不住 ctypes/importlib/builtins/pickle 等未列出的危险模块。
    _ALLOWED_IMPORT_MODULES = frozenset({"polars", "__future__"})

    @classmethod
    def validate_code_or_raise(cls, code: str, expected_strategy_id: str | None = None) -> dict:
        """Validate generated/saved strategy code without executing it."""
        cls._validate_safety(code)
        try:
            meta = cls._extract_meta(code)
        except Exception as e:
            raise ValueError(f"解析META失败: {e}") from e
        if not isinstance(meta, dict):
            raise ValueError("META 必须是字典")
        strategy_id = meta.get("id")
        if expected_strategy_id is not None and strategy_id != expected_strategy_id:
            raise ValueError("META.id 必须与 strategy_id 一致")
        return meta

    @classmethod
    def _validate_safety(cls, code: str) -> None:
        """AST 级安全检查: import 白名单 + 危险内建调用拦截。"""
        tree = ast.parse(code)
        cls._validate_top_level(tree)

        forbidden_calls = {"open", "exec", "eval", "compile", "__import__",
                           "globals", "locals", "vars", "dir", "getattr",
                           "setattr", "delattr", "type", "input", "breakpoint",
                           "exit", "quit"}
        forbidden_names = {"__builtins__", "builtins"}
        forbidden_attr_calls = {
            "read_csv", "read_parquet", "read_database", "read_delta", "read_excel",
            "read_ipc", "read_ndjson", "scan_csv", "scan_parquet", "scan_delta",
            "scan_ipc", "scan_ndjson", "sql",
        }

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] not in cls._ALLOWED_IMPORT_MODULES:
                        raise ValueError(f"禁止 import {alias.name} (策略只允许 import polars)")
            if isinstance(node, ast.ImportFrom):
                mod = (node.module or "").split(".")[0]
                if mod not in cls._ALLOWED_IMPORT_MODULES:
                    raise ValueError(f"禁止 from {node.module} import (策略只允许 import polars)")
            if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
                raise ValueError(f"禁止访问特殊属性 {node.attr}")
            if isinstance(node, ast.Name) and node.id in forbidden_names:
                raise ValueError(f"禁止访问 {node.id}")
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in forbidden_calls:
                    raise ValueError(f"禁止调用 {node.func.id}()")
                if isinstance(node.func, ast.Attribute) and node.func.attr in forbidden_attr_calls:
                    raise ValueError(f"禁止调用 {node.func.attr}()")

    @staticmethod
    def _validate_top_level(tree: ast.Module) -> None:
        """Reject import-time side effects; strategy files may only define data and functions."""
        for stmt in tree.body:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                continue
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                continue
            if isinstance(stmt, ast.FunctionDef):
                if stmt.decorator_list:
                    raise ValueError("禁止使用函数装饰器")
                continue
            if isinstance(stmt, ast.Assign):
                AIStrategyGenerator._require_literal_assignment(stmt.value)
                continue
            if isinstance(stmt, ast.AnnAssign):
                AIStrategyGenerator._require_literal_assignment(stmt.value)
                continue
            raise ValueError(f"禁止顶层执行语句: {stmt.__class__.__name__}")

    @staticmethod
    def _require_literal_assignment(value: ast.AST | None) -> None:
        if value is None:
            return
        try:
            ast.literal_eval(value)
        except (ValueError, SyntaxError) as e:
            raise ValueError("顶层赋值仅允许纯字面量") from e

    @staticmethod
    def _extract_meta(code: str) -> dict:
        """从代码字符串中提取 META 字典（不执行代码, 仅接受字面量）

        兼容两种声明: META = {...} (Assign) 和 META: dict = {...} (AnnAssign)。
        与 api.strategy._find_meta_dict 保持同一套匹配逻辑。
        """
        tree = ast.parse(code)
        for node in ast.walk(tree):
            value = None
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "META":
                        value = node.value
                        break
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                    and node.target.id == "META":
                value = node.value
            if value is not None:
                try:
                    return ast.literal_eval(value)
                except (ValueError, SyntaxError) as e:
                    raise ValueError(f"META 必须是纯字面量字典: {e}") from e
        return {}
