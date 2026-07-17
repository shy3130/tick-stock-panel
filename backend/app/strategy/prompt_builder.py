"""策略提示词组装器 — 两步定制流程的提示词生成。

职责: 加载对应步骤的 Markdown 指南，拼接用户输入，组装 LLM 提示词。
不知道: LLM 调用、API、前端、引擎执行。
"""
from __future__ import annotations

from pathlib import Path

# 运行时依赖的提示词文档（随 backend/app 打包进 Docker，避免 .dockerignore 排除 docs/ 导致运行时缺失）
_DOCS_DIR = Path(__file__).resolve().parent / "prompts"
_cache: dict[str, str] = {}


def _load_doc(name: str) -> str:
    if name not in _cache:
        path = _DOCS_DIR / name
        _cache[name] = path.read_text(encoding="utf-8") if path.exists() else ""
    return _cache[name]


DIRECTION_CN = {"long": "做多", "short": "做空", "monitor": "监控"}


def build_step1(
    name: str,
    description: str,
    direction: str,
    rules: str,
    strategy_id: str = "",
    execution_backend: str = "polars_expr",
) -> str:
    """步骤1：规则 → 完整策略代码（参数 + 信号 + 评分 + 告警）

    注意: 生成规范已在 ai_generator.py 的 system prompt 中加载，
    此处只拼用户输入以降低网关超时概率。
    """
    id_line = f"\n策略ID（必须使用此ID）：{strategy_id}" if strategy_id else ""

    return f"""请根据以下用户输入生成完整策略代码：

策略名称：{name}{id_line}
策略描述：{description}
选股方向：{DIRECTION_CN.get(direction, direction)}
执行后端：{execution_backend}
策略规则：
{rules}

输出要求：
1. 严格遵循系统提示中的策略文件结构和安全限制。
2. 严格使用指定执行后端；matrix_native 只定义 MATRIX_STRATEGY，polars_expr 只定义 filter()。若 polars 规则确需历史窗口，改用 python_history_legacy + filter_history()。
3. META 必须声明 asset_types 和 timeframes，并生成完整执行与交易元数据。
4. 只输出 Python 代码。"""


def build_step2(current_code: str, instruction: str) -> str:
    """步骤2：修改策略任意部分"""
    guide = _load_doc("strategy-builder-step2.md")

    return f"""{guide}

---

当前策略代码：
```python
{current_code}
```

用户修改指令：
{instruction}

只输出修改后的完整 Python 代码。"""
