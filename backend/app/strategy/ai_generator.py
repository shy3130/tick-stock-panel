"""AI 策略生成器 — 读取策略开发文档 + 调用 LLM 生成策略代码。

职责: 接收用户自然语言描述 → 读取 prompts/strategy-guide.md → 调用 LLM → 返回策略代码。
不知道: 引擎内部、API、前端、配置持久化、回测。
"""
from __future__ import annotations

import ast
import importlib
import logging
import math
import re
import sys
import types
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
3. 策略只落在自己的目录：AI 生成的策略只属于 data/strategies/ai/，文件名/ID 用 ai_ 前缀；不要引用 app/strategy/ 下的任何项目文件
4. polars 策略只 import polars 和 datetime；matrix_native 策略只允许 import numpy 以及 from app.backtest.matrix import 所需矩阵协议和算子

要求:
1. 用户可能调整的策略阈值通过 META["params"] 暴露，每项使用 id/label/type/default/min/max/step（type 支持 float/int/bool/select/date/string）；公式常数、固定窗口边界、布尔开关不必强行参数化
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


# ================================================================
# 策略安全校验辅助 (GH #475: 静态黑名单可被别名/拼接/模块命名空间绕过)
# ================================================================
_DUNDER_RE = re.compile(r"__[A-Za-z0-9_]*__")
# dunder 名字/字符串白名单: `if __name__ == "__main__"` 常见且无害
_BENIGN_DUNDER_NAMES = frozenset({"__name__"})
_BENIGN_DUNDER_TEXTS = frozenset({"__name__", "__main__"})

# 危险内建: 任何引用 (Load) 都拒绝, 不只直接调用 —
# `g = getattr` 是纯赋值 (无 Call 节点), 调用 g(...) 时 func.id 也不是 getattr。
_FORBIDDEN_REF_NAMES = frozenset({
    "open", "exec", "eval", "compile", "__import__",
    "globals", "locals", "vars", "dir", "getattr",
    "setattr", "delattr", "type", "input", "breakpoint",
    "memoryview", "exit", "quit",
})

# 危险属性名: 按属性名匹配任意接收者 (x.system() 也拦)。收录进程/命令执行、
# 文件读写原语与常见模块命名空间句柄;
# 不能收录 open/type/dir 等 — market.open 是合法行情列名。
_FORBIDDEN_ATTR_NAMES = frozenset({
    # 进程与命令执行
    "system", "popen", "Popen", "spawn", "spawnl", "spawnle", "spawnlp",
    "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe", "posix_spawn",
    "posix_spawnp", "fork", "forkpty", "execv", "execve", "execvp", "execvpe",
    "execl", "execle", "execlp", "execlpe", "check_call", "check_output",
    "getoutput", "getstatusoutput", "startfile", "kill", "killpg",
    # 模块命名空间句柄 (点进去即拿到 os/sys/subprocess 等)
    "os", "sys", "io", "importlib", "builtins", "subprocess", "ctypes",
    "ctypeslib", "pickle", "marshal", "shutil", "socket", "runpy", "pathlib",
    "posix", "nt", "ntpath", "posixpath", "genericpath", "platform",
    "sysconfig", "types", "typing", "dataclasses", "contextlib", "contextvars",
    "weakref", "multiprocessing", "asyncio", "threading", "webbrowser",
    "inspect", "faulthandler", "atexit", "distutils", "setuptools",
    "pkg_resources", "pip",
    # 文件读写原语 (polars / numpy 命名空间可直接调用)
    "write_csv", "write_parquet", "write_json", "write_ndjson", "write_ipc",
    "write_database", "write_excel", "sink_parquet", "sink_csv", "sink_ndjson",
    "sink_ipc", "savetxt", "savez", "savez_compressed", "tofile", "fromfile",
    "read_csv", "read_parquet", "read_json", "read_ndjson", "read_ipc",
    "read_excel", "read_database", "scan_csv", "scan_parquet", "scan_ndjson",
    "scan_ipc", "loadtxt", "genfromtxt", "memmap", "load_module", "save",
    "load", "loads",
    # 类型对象句柄 (构造任意可调用/代码对象的原料)
    "Path", "PurePath", "ModuleType", "CodeType", "FunctionType", "MethodType",
    "BuiltinFunctionType", "LambdaType", "MappingProxyType",
})

_MODULE_ATTR_CACHE: dict[str, frozenset[str]] = {}
# 允许探测的模块根: 已加载的直接探测; 未加载时只对框架/科学计算库 import,
# 不对策略代码点名的任意模块名做 import (避免校验本身成为执行路径)。
_INTROSPECTABLE_ROOTS = frozenset({"polars", "numpy", "app", "datetime", "__future__"})


def _module_attr_names(module_path: str) -> frozenset[str]:
    """模块命名空间里的「模块对象」属性名 (os / shutil / json 等句柄)。

    策略拿到任意模块对象即可绕道任意代码执行 (pl.os.system / np.lib /
    np.ctypeslib...), 因此白名单模块的模块类型属性一律不可访问、不可 from
    导入。按模块动态探测 (跟随库版本自更新), 探测失败返回空集合 — 常见危险
    名仍由 _FORBIDDEN_ATTR_NAMES 兜住, 不因探测失败放行已知逃逸。
    """
    cached = _MODULE_ATTR_CACHE.get(module_path)
    if cached is not None:
        return cached
    try:
        module = sys.modules.get(module_path)
        if module is None:
            if module_path.split(".", 1)[0] not in _INTROSPECTABLE_ROOTS:
                raise ImportError(module_path)
            module = importlib.import_module(module_path)
        names = frozenset(
            name
            for name, value in vars(module).items()
            if isinstance(value, types.ModuleType)
        )
    except Exception:  # noqa: BLE001 — 探测失败不阻断校验, 走静态兜底
        names = frozenset()
    _MODULE_ATTR_CACHE[module_path] = names
    return names


def _is_dunder(name: str) -> bool:
    return bool(_DUNDER_RE.fullmatch(name))


def _fold_string(node: ast.AST) -> str | None:
    """尽量折叠字符串常量拼接 (`"__imp" + "ort__"`), 供 dunder 检查使用。

    非全常量表达式 (含 f-string 插值、变量) 返回 None, 不误判。
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _fold_string(node.left)
        right = _fold_string(node.right) if left is not None else None
        if left is None or right is None:
            return None
        return left + right
    return None


def _dotted_name(node: ast.AST) -> str | None:
    """把 `a.b.c` 形式的属性链还原成点分字符串; 非纯名字链返回 None。"""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


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
            # None = 不传上限: 策略代码较长, 推理模型思考 token 计入预算会截断代码
            max_tokens=None,
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
            # None = 不传上限: 策略代码较长, 推理模型思考 token 计入预算会截断代码
            max_tokens=None,
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
        "app.strategy.research.factor_rank_research",
        "app.strategy.market_data",   # 新增: 策略可读取指数/ETF 日K
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
        """AST 级安全检查: import 白名单 + 危险引用/属性/字符串拦截。

        注意: AST 名单不是真正的沙箱, 只能拦截已知攻击模式。真正的隔离需要
        在受限子进程里执行策略 (后续 P0)。此处按逃逸手法逐类封堵 (GH #475):
        - 危险内建 (getattr/eval/__import__...) 的任何引用, 含别名赋值右值
        - 任意 dunder 名字 / dunder 属性 / 含 dunder 的字符串 (含拼接折叠)
        - 白名单模块的模块类型属性 (pl.os / np.ctypeslib / np.lib ...) 与 from 导入
        - 进程执行与文件读写原语属性名 (x.system / x.write_csv ...)
        """
        tree = ast.parse(code)

        allowed_import_modules = cls._ALLOWED_IMPORT_MODULES | extra_allowed_import_modules
        forbidden_names = _FORBIDDEN_REF_NAMES - extra_allowed_calls

        def _module_allowed(module: str) -> bool:
            return (
                module in allowed_import_modules
                or module.split(".", 1)[0] in allowed_import_modules
            )

        def _path_allowed(path: str) -> bool:
            """点分路径是否指向白名单模块本体或它的祖先包。"""
            return path in allowed_import_modules or any(
                allowed.startswith(f"{path}.") for allowed in allowed_import_modules
            )

        # `import X as Y` 的绑定: 用于识别「模块对象的属性访问」。
        module_aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        module_aliases[alias.asname] = alias.name
                    else:
                        root = alias.name.split(".", 1)[0]
                        module_aliases[root] = root

        def _reject_dunder_text(text: str, where: str) -> None:
            matches = _DUNDER_RE.findall(text)
            if matches and any(m not in _BENIGN_DUNDER_TEXTS for m in matches):
                raise ValueError(
                    f"禁止{where} {text[:40]!r} (策略不允许 dunder 遍历逃逸)"
                )

        # 模块/函数文档字符串不参与执行, 说明性文字里的 dunder 提法不拦。
        docstrings: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", None) or []
                if body and isinstance(body[0], ast.Expr) \
                        and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if not _module_allowed(alias.name):
                        raise ValueError(f"禁止 import {alias.name} (不在策略安全白名单)")
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if not _module_allowed(mod):
                    raise ValueError(f"禁止 from {node.module} import (不在策略安全白名单)")
                module_attrs = _module_attr_names(mod)
                for alias in node.names:
                    if alias.name == "*":
                        raise ValueError(f"禁止 from {mod} import * (通配导入绕过白名单)")
                    if alias.name in forbidden_names or alias.name in _FORBIDDEN_ATTR_NAMES:
                        raise ValueError(f"禁止导入 {mod}.{alias.name} (策略不允许的危险符号)")
                    if alias.name in module_attrs:
                        raise ValueError(
                            f"禁止导入 {mod}.{alias.name} (模块对象可绕道执行任意命令)"
                        )
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if node.id in forbidden_names:
                    raise ValueError(f"禁止引用 {node.id} (策略不允许的执行/内省入口)")
                if _is_dunder(node.id) and node.id not in _BENIGN_DUNDER_NAMES:
                    raise ValueError(f"禁止引用 {node.id} (策略不允许 dunder 逃逸入口)")
            if isinstance(node, ast.Attribute):
                if _is_dunder(node.attr):
                    raise ValueError(f"禁止访问属性 {node.attr} (策略不允许 dunder 遍历逃逸)")
                dotted = _dotted_name(node)
                root = dotted.split(".", 1)[0] if dotted else None
                if dotted is not None and root in module_aliases:
                    # 模块链上的属性: 拦截模块命名空间逃逸 (pl.os / np.lib ...)
                    resolved = module_aliases[root] + dotted[len(root):]
                    if _path_allowed(resolved):
                        continue
                    if node.attr in _FORBIDDEN_ATTR_NAMES:
                        raise ValueError(
                            f"禁止访问 {resolved} (模块/进程/文件句柄, 可绕道执行任意命令)"
                        )
                    parent = resolved.rsplit(".", 1)[0]
                    if _path_allowed(parent) and node.attr in _module_attr_names(parent):
                        raise ValueError(
                            f"禁止访问 {resolved} (模块对象可绕道执行任意命令)"
                        )
                    continue
                if node.attr in _FORBIDDEN_ATTR_NAMES:
                    raise ValueError(f"禁止访问属性 {node.attr} (策略不允许的进程/文件原语)")
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in docstrings:
                _reject_dunder_text(node.value, "字符串常量")
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                folded = _fold_string(node)
                if folded is not None:
                    _reject_dunder_text(folded, "拼接字符串")
            if isinstance(node, ast.Subscript):
                folded = _fold_string(node.slice)
                if folded is not None:
                    _reject_dunder_text(folded, "下标访问")

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
