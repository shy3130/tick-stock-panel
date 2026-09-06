"""AI 策略草稿门测试 — 保存即 research_only 草稿, 显式 publish 才公开。

覆盖:
  1. AI 策略保存后为草稿态(research_only=True), 不进公开列表
  2. 自定义策略不受门控(零回归)
  3. publish 翻转草稿 → 公开
  4. publish 拒绝非 AI 策略 / 已公开策略
  5. _set_meta_bool_field 的插入与替换两条路径
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.strategy import (
    StrategyCodeSaveRequest,
    _save_strategy_code,
    _set_meta_bool_field,
    publish_ai_strategy,
)
from app.strategy.ai_generator import AIStrategyGenerator
from app.strategy.engine import StrategyEngine


def _code(strategy_id: str, name: str = "测试策略") -> str:
    return f'''"""测试策略"""
import polars as pl

META = {{
    "id": "{strategy_id}",
    "name": "{name}",
    "description": "测试描述",
    "tags": ["测试"],
    "params": [],
    "scoring": {{}},
}}

ENTRY_SIGNALS = []
EXIT_SIGNALS = []
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 20

RULES = """
1. 测试规则一
2. 测试规则二
3. 测试规则三
"""

def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    return pl.lit(True)
'''


def _request(tmp_path):
    ai_dir = tmp_path / "strategies" / "ai"
    custom_dir = tmp_path / "strategies" / "custom"
    engine = StrategyEngine(strategy_dirs=[custom_dir, ai_dir])
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo, strategy_engine=engine)))


def _save_ai(tmp_path, sid: str) -> dict:
    """保存一个 AI 来源的新建策略, 返回 (request, save_result)。"""
    request = _request(tmp_path)
    req = StrategyCodeSaveRequest(
        strategy_id=sid,
        target_source="ai",
        mode="create",
        code=_code("wrong"),
        name="AI 草稿",
    )
    return request, _save_strategy_code(req, request)


def test_ai_strategy_saved_as_research_only_draft(tmp_path):
    request, result = _save_ai(tmp_path, "ai_draft")

    assert result["ok"] is True
    assert result["research_only"] is True
    assert request.app.state.strategy_engine.get("ai_draft").meta["research_only"] is True
    # 草稿态不进公开列表(list_strategies 对 research_only 过滤)
    public_ids = {
        meta["id"]
        for meta in request.app.state.strategy_engine.list_strategies()
        if not meta.get("research_only")
    }
    assert "ai_draft" not in public_ids


def test_custom_strategy_not_gated(tmp_path):
    request = _request(tmp_path)
    req = StrategyCodeSaveRequest(
        strategy_id="custom_draft",
        target_source="custom",
        mode="create",
        code=_code("wrong"),
        name="自定义策略",
    )

    result = _save_strategy_code(req, request)

    assert result["research_only"] is False
    assert request.app.state.strategy_engine.get("custom_draft").meta.get("research_only") is not True


def test_publish_ai_strategy_flips_to_public(tmp_path):
    request, _ = _save_ai(tmp_path, "ai_draft")

    result = publish_ai_strategy("ai_draft", request)

    assert result == {"ok": True, "strategy_id": "ai_draft"}
    assert request.app.state.strategy_engine.get("ai_draft").meta["research_only"] is False


def test_publish_rejects_non_ai_strategy(tmp_path):
    request = _request(tmp_path)
    req = StrategyCodeSaveRequest(
        strategy_id="custom_pub",
        target_source="custom",
        mode="create",
        code=_code("wrong"),
        name="自定义策略",
    )
    _save_strategy_code(req, request)

    with pytest.raises(HTTPException) as exc_info:
        publish_ai_strategy("custom_pub", request)

    assert exc_info.value.status_code == 400
    assert "AI 策略" in exc_info.value.detail


def test_publish_rejects_already_public(tmp_path):
    request, _ = _save_ai(tmp_path, "ai_draft")
    publish_ai_strategy("ai_draft", request)

    with pytest.raises(HTTPException) as exc_info:
        publish_ai_strategy("ai_draft", request)

    assert exc_info.value.status_code == 400
    assert "已是公开状态" in exc_info.value.detail


def test_set_meta_bool_field_insert_then_replace():
    code = 'META = {\n    "id": "x",\n}\n'

    inserted = _set_meta_bool_field(code, "research_only", True)
    assert '"research_only": True' in inserted
    assert AIStrategyGenerator._extract_meta(inserted)["research_only"] is True

    replaced = _set_meta_bool_field(inserted, "research_only", False)
    assert '"research_only": False' in replaced
    assert '"research_only": True' not in replaced
    assert AIStrategyGenerator._extract_meta(replaced)["research_only"] is False
