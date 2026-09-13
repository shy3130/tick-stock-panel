"""AI 策略迭代器 — 生成 v1 → 跑回测 → 诊断 → 修改, 有界闭环。

只自动化「生成—诊断—修改」, 回测只读、轮次有上限, 最终仍由人拍板
(docs/strategy-iteration.md 第 0/1 节纪律)。产物落盘到 data/strategies/ai/,
不自动上线。复用 ai_generator.AIStrategyGenerator 做生成与校验, 复用
services.tool_catalog 做工具目录 + 回测桥。
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from pprint import pformat
from typing import Any

from app.services import tool_catalog
from app.services.ai_provider import generate_ai_text_with_tools
from app.strategy.ai_generator import AIStrategyGenerator, _SYSTEM_PREFIX, find_meta_assignment

# 每轮 (生成/诊断/修改一次) 内部的工具调用预算: 至少 2 次 LLM 调用
# (1 次发起 run_backtest, 1 次读结果产出改进版), 留少量检索/换参余量。
_CYCLE_TOOL_BUDGET = 4

_ITERATION_SUFFIX = """

--- 工具使用与迭代纪律 ---

你拥有以下工具 (通过 OpenAI function calling 调用):
- list_factors(asset_type?, stable_only?): 检索因子目录 (约 100 个因子)
- list_strategies(): 检索已加载策略目录 (含 research 研究模板)
- list_data_capabilities(): 检索数据源能力
- run_backtest(strategy_id, params?, start?, end?, asset_type?): 回测已保存策略, 返回精简指标 (总收益/年化/最大回撤/夏普/胜率/盈亏比/交易次数等)

迭代纪律 (不可违反):
1. 每轮先用 run_backtest 回测当前草稿, 拿基准指标, 再诊断问题。
2. 一次只改一个主题 (先修一个短板), 改动依据必须来自回测指标或目录检索结果, 不凭想象调参。
3. run_backtest 是只读操作, 不修改策略文件; 你只能输出改进后的完整策略代码。
4. 输出改进版前用一行说明改了什么、预期哪些指标变化; 若已无明显改进空间, 原样输出当前代码。
5. 最终只输出完整策略 Python 文件 (用 ```python 代码块包裹)。
"""


class AIStrategyIterator:
    """用工具循环把 AI 策略生成升级为「生成→回测→诊断→修改」闭环。"""

    def __init__(self, *, max_rounds: int = 4) -> None:
        self._max_rounds = max_rounds

    async def iterate(
        self,
        prompt: str,
        *,
        engine,
        data_dir: str,
    ) -> dict[str, Any]:
        """执行有界迭代, 返回:
        {
            "draft_strategy_id": str,
            "rounds": [{"round": int, "stats": dict | None, "change_summary": str}, ...],
            "final_code": str,
            "final_meta": dict,
        }
        """
        generator = AIStrategyGenerator()
        tools = tool_catalog.build_tool_schemas()

        # 1. 生成 v1 (复用现有单次生成 + 结构修复)
        result = await generator.generate(prompt)
        if generator.needs_structural_repair(result):
            result = await generator.repair_code(result["code"], result["error"])
        if not result.get("valid"):
            raise ValueError(f"初始策略生成失败: {result.get('error')}")

        draft_id = self._alloc_draft_id(engine)
        code, meta = result["code"], result["meta"]
        self._save_draft(engine, data_dir, draft_id, code, meta)
        current_code, current_meta = code, {**meta, "id": draft_id}

        rounds: list[dict[str, Any]] = []
        final_backtested = False  # final 版(当前 current_code)是否已有回测证据
        for round_no in range(1, self._max_rounds + 1):
            full = await generate_ai_text_with_tools(
                self._iteration_messages(generator, current_code, prompt, draft_id),
                tools,
                execute_tool=self._make_execute_tool(engine, data_dir),
                max_rounds=_CYCLE_TOOL_BUDGET,
                temperature=0.3,
                max_tokens=None,
            )
            # stats 是本轮「改动前」的 current_code 回测基准 (LLM 先回测再产出改进版)
            stats = _extract_backtest_stats(full)
            final_text = _last_assistant_content(full)
            improved = generator.validate_code(final_text)
            if not improved.get("valid"):
                rounds.append({
                    "round": round_no,
                    "stats": stats,
                    "change_summary": f"改进版校验失败: {improved.get('error')}",
                })
                final_backtested = True  # final 仍是 current_code, 已被本轮回测
                break

            new_code, new_meta = improved["code"], improved["meta"]
            if new_code.strip() == current_code.strip():
                rounds.append({
                    "round": round_no,
                    "stats": stats,
                    "change_summary": "无改动, 视为收敛",
                })
                final_backtested = True  # 收敛, final 仍是 current_code
                break

            self._save_draft(engine, data_dir, draft_id, new_code, new_meta)
            current_code, current_meta = new_code, {**new_meta, "id": draft_id}
            rounds.append({
                "round": round_no,
                "stats": stats,
                "change_summary": _extract_summary(final_text),
            })
            final_backtested = False  # 本轮改进了 current_code, 新的 final 尚未回测

        # 循环耗尽且末轮是改进版时, final 版从未被回测, 补一次作为末行证据
        if not final_backtested:
            final_stats = await self._backtest_final(data_dir, draft_id)
            rounds.append({
                "round": len(rounds) + 1,
                "stats": final_stats,
                "change_summary": "最终版回测",
            })

        return {
            "draft_strategy_id": draft_id,
            "rounds": rounds,
            "final_code": current_code,
            "final_meta": current_meta,
        }

    async def _backtest_final(self, data_dir: str, draft_id: str) -> dict | None:
        """对最终版草稿补一次回测, 返回精简 stats; 失败返回 None (末行证据尽力而为)。"""
        try:
            result = await asyncio.to_thread(
                tool_catalog.run_backtest, data_dir, strategy_id=draft_id
            )
            return result.get("stats")
        except Exception:  # noqa: BLE001 — 末行证据不强依赖回测成功
            return None

    def _alloc_draft_id(self, engine) -> str:
        while True:
            draft_id = f"ai_{uuid.uuid4().hex[:8]}"
            if not engine.has(draft_id):
                return draft_id

    @staticmethod
    def _save_draft(engine, data_dir: str, draft_id: str, code: str, meta: dict) -> None:
        """写草稿文件 + 热重载; META.id 强制对齐 draft_id (engine 以 meta["id"] 为键)。

        草稿强制 research_only=True (复用 #255 的发布闸): 不进公开列表、不可运行,
        只有人点 publish 才上线, 守住「回测验证与上线由人拍板」的纪律。
        """
        out_dir = Path(data_dir) / "strategies" / "ai"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{draft_id}.py"
        meta = {**meta, "research_only": True}
        path.write_text(_rewrite_meta_id(code, draft_id, meta), encoding="utf-8")
        engine.reload()

    def _iteration_messages(
        self,
        generator: AIStrategyGenerator,
        current_code: str,
        prompt: str,
        draft_id: str,
    ) -> list[dict]:
        system = _SYSTEM_PREFIX + generator._get_guide() + _ITERATION_SUFFIX
        user = (
            f"策略草稿已保存为 {draft_id}。你可以调用 run_backtest 回测它 "
            f"(strategy_id={draft_id}), 或用 list_factors / list_strategies / "
            f"list_data_capabilities 检索目录。\n\n"
            f"原始需求:\n{prompt}\n\n"
            f"当前策略代码:\n```python\n{current_code}\n```\n\n"
            f"请回测当前草稿, 依据指标诊断, 输出改进后的完整策略代码; "
            f"若已无明显改进空间, 原样输出当前代码。"
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def _make_execute_tool(self, engine, data_dir: str):
        async def _execute(name: str, args: dict) -> dict:
            return await tool_catalog.execute_tool(name, args, engine=engine, data_dir=data_dir)

        return _execute


def _rewrite_meta_id(code: str, strategy_id: str, meta: dict) -> str:
    """把 META 赋值整段重写为 id=strategy_id 的规范形式 (变量名归一为 META)。

    草稿落盘要求 META.id 与文件名一致 (engine 以 meta["id"] 为键, run_backtest 按
    strategy_id 查找)。整段重写避免正则改 id 的边界情况, pformat 输出仍是合法 Python 字面量。
    """
    found = find_meta_assignment(code)
    if found is None:
        raise ValueError("找不到 META 字典")
    target, value = found
    new_meta = dict(meta)
    new_meta["id"] = strategy_id

    lines = code.splitlines(keepends=True)
    start_line = target.lineno - 1
    start_col = target.col_offset
    end_line = (value.end_lineno or value.lineno) - 1
    end_col = value.end_col_offset or value.col_offset

    replacement = f"META = {pformat(new_meta, width=100, sort_dicts=False)}"
    first = lines[start_line]
    last = lines[end_line]
    lines[start_line] = first[:start_col] + replacement + last[end_col:]
    if start_line != end_line:
        del lines[start_line + 1 : end_line + 1]
    return "".join(lines)


def _last_assistant_content(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "assistant" and not m.get("tool_calls"):
            return m.get("content") or ""
    return ""


def _extract_backtest_stats(messages: list[dict]) -> dict | None:
    """从 role:tool 消息中取出最近一次 run_backtest 的精简 stats。"""
    stats = None
    for m in messages:
        if m.get("role") != "tool":
            continue
        try:
            payload = json.loads(m.get("content") or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict) or not payload.get("ok"):
            continue
        result = payload.get("result")
        if isinstance(result, dict) and isinstance(result.get("stats"), dict):
            stats = result["stats"]
    return stats


def _extract_summary(text: str) -> str:
    """取第一个 ``` 之前的文字作为变更说明, 精简为一行。"""
    pre = text.split("```", 1)[0].strip()
    lines = [ln.strip() for ln in pre.splitlines() if ln.strip()]
    if not lines:
        return "已应用改进版"
    return " ".join(lines)[:120]
