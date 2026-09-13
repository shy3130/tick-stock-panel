# -*- coding: utf-8 -*-
"""AI 策略迭代闭环的定向测试 (stub 生成器与工具循环, 不依赖真实 AI key 与数据)。

覆盖三层:
  1. _rewrite_meta_id 纯函数 (三种 META 声明形式 + 缺失 id 插入)
  2. tool_catalog.build_tool_schemas + execute_tool 的 list_* 分发与未知工具
  3. AIStrategyIterator.iterate 完整状态流转 (收敛 / 校验失败 / 末轮改进补 final 回测)
"""
from __future__ import annotations

import ast
import asyncio
import json
import sys
import tempfile
import types
from pathlib import Path

from app.strategy import ai_iterator as it
from app.strategy.ai_generator import AIStrategyGenerator as RealGen, find_meta_assignment
from app.strategy.ai_iterator import AIStrategyIterator, _rewrite_meta_id
from app.services import tool_catalog

V1_CODE = '''"""v1 策略"""
import polars as pl

META = {
    "id": "ai_placeholder",
    "name": "v1",
    "description": "v1",
    "tags": ["ai"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [],
    "scoring": {"change_pct": 1.0},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "polars_expr"

def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    return pl.col("close") > pl.col("ma5")
'''

# v2 与 v1 不同: 增加成交量条件
V2_CODE = V1_CODE.replace(
    'return pl.col("close") > pl.col("ma5")',
    'return (pl.col("close") > pl.col("ma5")) & (pl.col("volume") > pl.col("vol_ma5") * 1.2)',
).replace('"name": "v1"', '"name": "v2"')

META = {
    "name": "v1",
    "description": "v1",
    "tags": ["ai"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [],
    "scoring": {"change_pct": 1.0},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}


def _meta_id_of(code: str) -> str:
    """从代码里提取 META 的 id (走真实 find_meta_assignment + literal_eval)。"""
    found = find_meta_assignment(code)
    assert found is not None, "找不到 META"
    _, value = found
    return ast.literal_eval(value)["id"]


# ── Part 1: _rewrite_meta_id ──────────────────────────────

def test_rewrite_multiline_assign():
    out = _rewrite_meta_id(V1_CODE, "ai_abcd1234", META)
    ast.parse(out)  # 必须仍是合法 Python
    assert _meta_id_of(out) == "ai_abcd1234"
    assert "META = {" in out
    assert "STRATEGY_META" not in out


def test_rewrite_normalize_variable_name():
    code = V1_CODE.replace("META = {", "STRATEGY_META = {")
    out = _rewrite_meta_id(code, "ai_x", META)
    ast.parse(out)
    assert _meta_id_of(out) == "ai_x"
    assert "STRATEGY_META" not in out
    assert "META = {" in out


def test_rewrite_annassign():
    code = V1_CODE.replace("META = {", "META: dict = {")
    out = _rewrite_meta_id(code, "ai_y", META)
    ast.parse(out)
    assert _meta_id_of(out) == "ai_y"


def test_rewrite_insert_missing_id():
    code = V1_CODE.replace('    "id": "ai_placeholder",\n', "")
    out = _rewrite_meta_id(code, "ai_z", META)
    ast.parse(out)
    assert _meta_id_of(out) == "ai_z"


# ── Part 2: tool_catalog ──────────────────────────────────

def _stub_module(monkeypatch, fullname: str, **attrs) -> types.ModuleType:
    """往 sys.modules 塞 stub 模块 (monkeypatch 在测试后自动恢复)。"""
    mod = types.ModuleType(fullname)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, fullname, mod)
    parts = fullname.split(".")
    for i in range(1, len(parts)):
        parent = ".".join(parts[:i])
        if parent not in sys.modules:
            pkg = types.ModuleType(parent)
            pkg.__path__ = []
            monkeypatch.setitem(sys.modules, parent, pkg)
    return mod


class _FakeFactorSpec:
    def __init__(self, fid):
        self.id = fid
        self.label = "因子"
        self.group = "g"
        self.formula_text = "f"
        self.kind = "k"
        self.dependencies = {"close"}
        self.asset_types = {"stock"}
        self.stability = "stable"
        self.pit = False


def test_build_tool_schemas():
    schemas = tool_catalog.build_tool_schemas()
    names = [s["function"]["name"] for s in schemas]
    assert names == [
        "list_factors", "list_strategies", "list_data_capabilities", "run_backtest",
    ]
    for s in schemas:
        assert s["type"] == "function"
        assert s["function"]["parameters"]["type"] == "object"


def test_execute_list_factors(monkeypatch):
    _stub_module(
        monkeypatch,
        "app.factors.registry",
        all_factors=lambda asset_type=None, stable_only=False: [
            _FakeFactorSpec("f1"), _FakeFactorSpec("f2"),
        ],
    )
    result = asyncio.run(tool_catalog.execute_tool("list_factors", {}))
    assert result["ok"] is True
    assert [f["id"] for f in result["result"]["factors"]] == ["f1", "f2"]


def test_execute_list_data_capabilities(monkeypatch):
    _stub_module(
        monkeypatch,
        "app.data_providers.capabilities",
        CAPABILITY_REGISTRY=[
            {"id": "daily", "label": "日K", "desc": "d", "tf_tier": 0},
        ],
    )
    result = asyncio.run(tool_catalog.execute_tool("list_data_capabilities", {}))
    assert result["ok"] is True
    assert result["result"]["capabilities"][0]["id"] == "daily"


class _FakeEngine:
    def __init__(self):
        self.reloads = 0

    def has(self, sid):
        return False

    def reload(self):
        self.reloads += 1

    def list_strategies(self, include_research=False):
        return [{"id": "builtin_1", "name": "内置", "description": "", "tags": [],
                 "asset_types": ["stock"], "timeframes": ["1d"], "execution_backend": "polars_expr",
                 "source": "builtin", "params": []}]


def test_execute_list_strategies():
    engine = _FakeEngine()
    result = asyncio.run(tool_catalog.execute_tool("list_strategies", {}, engine=engine))
    assert result["ok"] is True
    assert result["result"]["strategies"][0]["id"] == "builtin_1"


def test_execute_unknown_tool():
    result = asyncio.run(tool_catalog.execute_tool("no_such_tool", {}))
    assert result["ok"] is False
    assert "未知工具" in result["error"]


# ── Part 3: AIStrategyIterator.iterate 状态流转 ────────────

class FakeGenerator:
    """替换 AIStrategyGenerator: generate 返回 v1, validate_code 对改进版返回 valid。"""

    def needs_structural_repair(self, result):
        return False

    def _get_guide(self):
        return "guide"

    async def generate(self, prompt):
        return {"code": V1_CODE, "meta": dict(META), "valid": True, "error": None}

    def validate_code(self, code):
        stripped = RealGen._extract_code_block(code)
        return {"code": stripped, "meta": dict(META), "valid": True, "error": None}

    async def repair_code(self, code, error):
        raise AssertionError("不应进入 repair_code 分支")


def _fake_tool_loop(call_log: list):
    """按调用次数返回对话: 第 1 轮 run_backtest→改进, 第 2 轮原样输出(收敛)。"""
    async def fake(messages, tools, *, execute_tool, max_rounds, temperature, max_tokens):
        call_log.append(len(call_log) + 1)
        if len(call_log) == 1:
            return [
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "run_backtest",
                                 "arguments": json.dumps({"strategy_id": "draft"})},
                }]},
                {"role": "tool", "tool_call_id": "call_1",
                 "content": json.dumps({"ok": True, "result": {"stats": {
                     "sharpe": 1.5, "max_drawdown": -0.08, "win_rate": 0.55, "total_return": 0.12,
                 }}}, ensure_ascii=False)},
                {"role": "assistant", "content": "改进夏普\n```python\n" + V2_CODE + "\n```"},
            ]
        return [
            {"role": "assistant", "content": "已无改进空间\n```python\n" + V2_CODE + "\n```"},
        ]

    return fake


def test_iterate_full_flow(monkeypatch):
    call_log: list = []
    monkeypatch.setattr(it, "AIStrategyGenerator", FakeGenerator)
    monkeypatch.setattr(it, "generate_ai_text_with_tools", _fake_tool_loop(call_log))

    engine = _FakeEngine()
    with tempfile.TemporaryDirectory() as tmp:
        result = asyncio.run(
            AIStrategyIterator(max_rounds=2).iterate(
                "做一个均线多头策略", engine=engine, data_dir=tmp,
            )
        )
        draft_id = result["draft_strategy_id"]
        # 在 with 块内读落盘文件 (退出后 TemporaryDirectory 会清理)
        draft_path = Path(tmp) / "strategies" / "ai" / f"{draft_id}.py"
        assert draft_path.exists()
        on_disk = draft_path.read_text(encoding="utf-8")

    assert draft_id.startswith("ai_"), draft_id
    assert len(draft_id) == len("ai_") + 8

    # 2 轮: 第 1 轮改进, 第 2 轮收敛 (收敛路径 final 已被回测, 不补末行)
    assert len(result["rounds"]) == 2, result["rounds"]
    r1, r2 = result["rounds"]
    assert r1["stats"] is not None and r1["stats"]["sharpe"] == 1.5
    assert r1["change_summary"] == "改进夏普"
    assert r2["stats"] is None
    assert "收敛" in r2["change_summary"]

    assert result["final_code"].strip() == V2_CODE.strip()
    assert result["final_meta"]["id"] == draft_id

    # 引擎 reload: v1 保存 1 次 + 第 1 轮改进保存 1 次 = 2 次
    assert engine.reloads == 2, engine.reloads

    # 草稿文件 META.id 与文件名对齐, 且落盘的是改进后的 v2, research_only 已注入
    assert _meta_id_of(on_disk) == draft_id
    assert "vol_ma5" in on_disk
    assert ast.literal_eval(find_meta_assignment(on_disk)[1]).get("research_only") is True


def test_iterate_validation_failure_breaks(monkeypatch):
    """改进版校验失败 -> 记录 error 并 break, 不落盘坏代码。"""
    call_log: list = []

    class BadGen(FakeGenerator):
        def validate_code(self, code):
            return {"code": code, "meta": {}, "valid": False, "error": "找不到策略入口函数 filter()"}

    monkeypatch.setattr(it, "AIStrategyGenerator", BadGen)
    monkeypatch.setattr(it, "generate_ai_text_with_tools", _fake_tool_loop(call_log))

    engine = _FakeEngine()
    with tempfile.TemporaryDirectory() as tmp:
        result = asyncio.run(
            AIStrategyIterator(max_rounds=2).iterate("p", engine=engine, data_dir=tmp)
        )
    assert len(result["rounds"]) == 1
    assert "校验失败" in result["rounds"][0]["change_summary"]
    assert engine.reloads == 1  # 只有 v1 落盘


def test_iterate_final_backtest_appended(monkeypatch):
    """末轮是改进版 (耗尽 max_rounds) -> final 从未被回测, 补一次末行「最终版回测」。"""
    call_log: list = []
    monkeypatch.setattr(it, "AIStrategyGenerator", FakeGenerator)
    monkeypatch.setattr(it, "generate_ai_text_with_tools", _fake_tool_loop(call_log))

    async def fake_final_backtest(self, data_dir, draft_id):
        return {"sharpe": 2.0}

    monkeypatch.setattr(AIStrategyIterator, "_backtest_final", fake_final_backtest)

    engine = _FakeEngine()
    with tempfile.TemporaryDirectory() as tmp:
        result = asyncio.run(
            AIStrategyIterator(max_rounds=1).iterate("p", engine=engine, data_dir=tmp)
        )

    # 1 轮改进 + 1 行最终版回测
    assert len(result["rounds"]) == 2, result["rounds"]
    r1, r2 = result["rounds"]
    assert r1["change_summary"] == "改进夏普"
    assert r2["change_summary"] == "最终版回测"
    assert r2["stats"] == {"sharpe": 2.0}
