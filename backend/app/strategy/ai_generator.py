"""AI 策略生成器 — 读取策略开发文档 + 调用 LLM 生成策略代码。

职责: 接收用户自然语言描述 → 读取 prompts/strategy-guide.md → 调用 LLM → 返回策略代码。
不知道: 引擎内部、API、前端、配置持久化、回测。
"""
from __future__ import annotations

import ast
import logging
import math
import re
from pathlib import Path

from app.indicators.pipeline import ENRICHED_COLUMNS
from app.strategy.scoring import VIRTUAL_SCORING_DEPENDENCIES

logger = logging.getLogger(__name__)

# 策略开发精简指南路径 (随 backend/app 打包进 Docker, 避免 .dockerignore 排除 docs/ 导致运行时缺失)
GUIDE_PATH = Path(__file__).resolve().parent / "prompts" / "strategy-guide-compact.md"

_SYSTEM_PREFIX = """你是A股量化策略设计专家。根据用户描述的需求，参考下方的《策略开发指南》生成一个完整的策略Python文件。

文件与范围铁律（不可违反）:
1. 只创建这一个策略文件：只生成一个 .py 文件，绝不创建多文件、不拆分模块、不跨文件引用
2. 绝不触碰项目源码：不要写任何会修改 backend/、docs/、frontend/ 等现有文件的代码；不要 import os/sys/pathlib 等文件系统模块
3. 不得放入内置策略目录：AI 生成的策略只属于 data/strategies/ai/，文件名/ID 用 ai_ 前缀；内置目录 backend/app/strategy/builtin/ 由项目维护，AI 不得染指
4. polars 策略只 import polars 和 datetime；matrix_native 策略只允许 import numpy 以及 from app.backtest.matrix import 所需矩阵协议和算子

要求:
1. 用户可能调整的策略阈值通过 META["params"] 暴露，每项使用 id/label/type/default/min/max/step；公式常数、固定窗口边界、布尔开关不必强行参数化
2. 遵循指南中的文件结构，但优先贴合用户规则，不要为了套模板歪曲策略含义
3. ENTRY_SIGNALS/EXIT_SIGNALS 根据策略逻辑自行选择匹配的信号列，不要照搬示例
4. scoring 权重根据策略核心逻辑定制，总和 = 1.0；键只能使用指南中的真实数值字段或受控虚拟评分字段 ma20_bias，不得创造条件名称作为评分列
5. 优先使用 Polars 表达式、窗口函数、聚合和 with_columns/filter 实现，避免逐行/逐股 Python 循环；只有表达式难以描述的复杂状态机才使用 partition_by/to_dicts
6. 直接输出Python代码，不要输出其他内容
7. 元数据必须使用模块顶层的 META = {...} 或 META: dict = {...}，不得省略或改名；并且必须定义所选执行后端要求的策略入口

--- 策略开发指南 ---

"""

_META_NAMES = ("META", "STRATEGY_META", "meta")
_FENCED_CODE_RE = re.compile(
    r"```(?P<language>[^\n`]*)\r?\n(?P<code>.*?)```",
    re.DOTALL,
)
_POLARS_ENTRYPOINT_ERROR = "找不到策略入口函数 filter() 或 filter_history()"
_MATRIX_ENTRYPOINT_ERROR = "找不到 Matrix 策略入口 MATRIX_STRATEGY"

_POLARS_SCORING_FIELDS = frozenset(
    name
    for name in ENRICHED_COLUMNS
    if name not in {"symbol", "date", "name"} and not name.startswith("signal_")
) | frozenset(VIRTUAL_SCORING_DEPENDENCIES)
_MATRIX_SCORING_FIELDS = frozenset({
    "open", "high", "low", "close", "volume", "amount", "turnover_rate",
    "total_shares", "float_shares", "consecutive_limit_ups",
    "consecutive_limit_downs", "prev_close", "change_pct", "change_amount",
    "amplitude", "ma5", "ma10", "ma20", "ma30", "ma60", "boll_upper",
    "boll_lower", "high_60d", "low_60d", "momentum_5d", "momentum_10d",
    "momentum_20d", "momentum_30d", "momentum_60d", "annual_vol_20d",
    "rsi_6", "rsi_14", "rsi_24", "vol_ratio_5d", "ma20_bias",
})


def _top_level_assignment(
    tree: ast.Module,
    name: str,
) -> tuple[ast.Name, ast.expr | None] | None:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            target = next(
                (item for item in node.targets
                 if isinstance(item, ast.Name) and item.id == name),
                None,
            )
            if target is not None:
                return target, node.value
        elif isinstance(node, ast.AnnAssign) \
                and isinstance(node.target, ast.Name) \
                and node.target.id == name:
            return node.target, node.value
    return None


def find_meta_assignment(code: str) -> tuple[ast.Name, ast.Dict] | None:
    """Find a supported module-level META assignment without executing code."""
    tree = ast.parse(code)
    for name in _META_NAMES:
        found = _top_level_assignment(tree, name)
        if found is not None:
            target, value = found
            if not isinstance(value, ast.Dict):
                raise ValueError(f"{name} 必须是字面量字典")
            return target, value
    return None


def _strategy_execution_backend(tree: ast.Module, meta: dict | None = None) -> str:
    found = _top_level_assignment(tree, "EXECUTION_BACKEND")
    if found is not None:
        try:
            value = ast.literal_eval(found[1])
        except (ValueError, SyntaxError):
            value = None
        if isinstance(value, str):
            return value
    if isinstance(meta, dict) and isinstance(meta.get("execution_backend"), str):
        return meta["execution_backend"]
    if any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "filter_history"
        for node in tree.body
    ):
        return "python_history_legacy"
    return "polars_expr"


def _strategy_entrypoint_error(code: str, meta: dict | None = None) -> str | None:
    tree = ast.parse(code)
    if _strategy_execution_backend(tree, meta) == "matrix_native":
        return None if _top_level_assignment(tree, "MATRIX_STRATEGY") else _MATRIX_ENTRYPOINT_ERROR
    has_polars_entrypoint = any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"filter", "filter_history"}
        for node in tree.body
    )
    return None if has_polars_entrypoint else _POLARS_ENTRYPOINT_ERROR


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
        result = self.validate_code(code)
        if self.needs_structural_repair(result):
            return await self.repair_code(result["code"], result["error"])
        return result

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
            self._validate_safety(code)
        except SyntaxError as e:
            return {
                "code": code,
                "meta": {},
                "valid": False,
                "error": f"Python 语法错误: {e.msg}",
            }
        except ValueError as e:
            return {
                "code": code,
                "meta": {},
                "valid": False,
                "error": str(e),
            }

        # 试加载获取 META
        try:
            meta = self._extract_meta(code)
        except Exception as e:
            return {
                "code": code,
                "meta": {},
                "valid": False,
                "error": f"解析META失败: {e}",
            }

        entrypoint_error = _strategy_entrypoint_error(code, meta)
        if entrypoint_error:
            return {
                "code": code,
                "meta": meta,
                "valid": False,
                "error": entrypoint_error,
            }

        try:
            self._validate_meta_semantics(code, meta)
        except ValueError as e:
            return {
                "code": code,
                "meta": meta,
                "valid": False,
                "error": str(e),
            }

        return {
            "code": code,
            "meta": meta,
            "valid": True,
            "error": None,
        }

    @staticmethod
    def needs_structural_repair(result: dict) -> bool:
        error = result.get("error") or ""
        return error.startswith("解析META失败:") or error in {
            _POLARS_ENTRYPOINT_ERROR,
            _MATRIX_ENTRYPOINT_ERROR,
        } or error.startswith(("META.params", "META.scoring"))

    @staticmethod
    def _validate_meta_semantics(code: str, meta: dict) -> None:
        params = meta.get("params", [])
        if isinstance(params, (list, tuple)):
            for index, item in enumerate(params):
                if isinstance(item, dict) and not str(item.get("id") or "").strip():
                    raise ValueError(f"META.params[{index}] 缺少非空 id")

        scoring = meta.get("scoring", {})
        if not isinstance(scoring, dict):
            raise ValueError("META.scoring 必须是字典")
        if not scoring:
            return

        for name, weight in scoring.items():
            if not isinstance(name, str) or not name:
                raise ValueError("META.scoring 字段名必须是非空字符串")
            if isinstance(weight, bool) or not isinstance(weight, (int, float)) \
                    or not math.isfinite(float(weight)) or weight < 0:
                raise ValueError(f"META.scoring[{name!r}] 权重必须是非负有限数值")
        total_weight = sum(float(weight) for weight in scoring.values())
        if not math.isclose(total_weight, 1.0, rel_tol=0.0, abs_tol=1e-6):
            raise ValueError("META.scoring 权重总和必须为 1.0")

        backend = _strategy_execution_backend(ast.parse(code), meta)
        if backend == "python_history_legacy":
            return
        allowed = _MATRIX_SCORING_FIELDS if backend == "matrix_native" else _POLARS_SCORING_FIELDS
        unknown = sorted(set(scoring) - set(allowed))
        if unknown:
            raise ValueError(
                f"META.scoring 引用了不可用字段: {unknown}; "
                "请使用真实数值字段或受控虚拟字段 ma20_bias"
            )

    async def repair_code(self, code: str, error: str) -> dict:
        """Ask the model once for a complete replacement after a structural error."""
        try:
            backend = _strategy_execution_backend(ast.parse(code))
        except SyntaxError:
            backend = "polars_expr"
        if backend == "matrix_native":
            entrypoint_requirement = (
                '保留 EXECUTION_BACKEND = "matrix_native"，定义 MATRIX_STRATEGY，'
                "不得添加 filter() 或 filter_history()"
            )
        else:
            entrypoint_requirement = (
                "保留原执行后端，并定义对应的 filter() 或 filter_history()"
            )
        prompt = f"""上一次生成的策略代码未通过结构校验。

校验错误：{error}

请输出修复后的完整策略 Python 文件。必须保留原策略意图和参数，使用模块顶层
META = {{...}}，{entrypoint_requirement}。只输出完整 Python 代码。

上一次代码：
```python
{code}
```"""
        repaired = await self._call_llm(prompt, self._get_guide())
        return self.validate_code(repaired)

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
        blocks = list(_FENCED_CODE_RE.finditer(content))
        for match in blocks:
            candidate = match.group("code").strip()
            try:
                found = find_meta_assignment(candidate)
                if found is not None:
                    meta = ast.literal_eval(found[1])
                    if isinstance(meta, dict) and _strategy_entrypoint_error(candidate, meta) is None:
                        return candidate
            except (SyntaxError, ValueError):
                continue
        for match in blocks:
            if match.group("language").strip().lower() in {"python", "py"}:
                return match.group("code").strip()
        if blocks:
            return blocks[0].group("code").strip()
        return content.strip()

    # import 白名单: Polars 与矩阵策略只开放执行协议所需模块。
    # 白名单而非黑名单 — 黑名单挡不住 ctypes/importlib/builtins/pickle 等未列出的危险模块。
    _ALLOWED_IMPORT_MODULES = frozenset({
        "polars",
        "numpy",
        "app.backtest.matrix",
        "datetime",
        "__future__",
    })

    @classmethod
    def _validate_safety(
        cls,
        code: str,
        *,
        extra_allowed_import_modules: frozenset[str] = frozenset(),
        extra_allowed_calls: frozenset[str] = frozenset(),
    ) -> None:
        """AST 级安全检查: import 白名单 + 危险内建调用拦截 + dunder 遍历拦截。

        注意: AST 名单不是真正的沙箱, 只能拦截常见攻击模式。真正的隔离需要
        在受限子进程里执行策略 (后续 P0)。此处拦截已知的逃逸技巧:
        - __globals__ / __builtins__ / __class__ / __subclasses__ / __mro__ 等属性访问
        - ["__import__"] / ["__builtins__"] 等字符串下标访问
        """
        tree = ast.parse(code)

        allowed_import_modules = cls._ALLOWED_IMPORT_MODULES | extra_allowed_import_modules
        forbidden_calls = {
            "open", "exec", "eval", "compile", "__import__",
            "globals", "locals", "vars", "dir", "getattr",
            "setattr", "delattr", "type", "input", "breakpoint",
        } - extra_allowed_calls

        def _module_allowed(module: str) -> bool:
            return (
                module in allowed_import_modules
                or module.split(".", 1)[0] in allowed_import_modules
            )

        # dunder 属性名: 访问这些属性可逃逸出策略沙箱拿到 os/subprocess 等
        forbidden_dunder_attrs = {
            "__globals__", "__builtins__", "__class__", "__subclasses__",
            "__mro__", "__bases__", "__base__", "__dict__", "__code__",
            "__import__", "__loader__", "__spec__", "__wrapped__",
        }
        # 字符串下标访问的危险名: x["__builtins__"] / x["__import__"]
        forbidden_subscript_strs = {
            "__builtins__", "__import__", "__globals__",
        }

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if not _module_allowed(alias.name):
                        raise ValueError(f"禁止 import {alias.name} (不在策略安全白名单)")
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if not _module_allowed(mod):
                    raise ValueError(f"禁止 from {node.module} import (不在策略安全白名单)")
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in forbidden_calls:
                    raise ValueError(f"禁止调用 {node.func.id}()")
            # 拦截 dunder 属性访问: x.__globals__ / ().__class__ 等
            if isinstance(node, ast.Attribute) and node.attr in forbidden_dunder_attrs:
                raise ValueError(f"禁止访问属性 {node.attr} (策略不允许 dunder 遍历逃逸)")
            # 拦截字符串下标访问危险名: x["__builtins__"]
            if isinstance(node, ast.Subscript):
                sl = node.slice
                if isinstance(sl, ast.Constant) and isinstance(sl.value, str) \
                        and sl.value in forbidden_subscript_strs:
                    raise ValueError(f"禁止下标访问 {sl.value} (策略不允许 dunder 遍历逃逸)")

    @staticmethod
    def _extract_meta(code: str) -> dict:
        """从代码字符串中提取 META 字典（不执行代码, 仅接受字面量）

        兼容两种声明: META = {...} (Assign) 和 META: dict = {...} (AnnAssign)。
        与 api.strategy._find_meta_dict 保持同一套匹配逻辑。
        """
        found = find_meta_assignment(code)
        if found is None:
            raise ValueError("找不到 META 字典")
        _, value = found
        try:
            meta = ast.literal_eval(value)
        except (ValueError, SyntaxError) as e:
            raise ValueError(f"META 必须是纯字面量字典: {e}") from e
        if not isinstance(meta, dict):
            raise ValueError("META 必须是纯字面量字典")
        return meta
