"""AI 策略 META 规范化回归测试。"""
from __future__ import annotations

from app.api.strategy import _normalize_build_result, _normalize_strategy_meta

RAW_CODE = '''"""模型返回的策略"""
import polars as pl

META = {
    "id": "custom_wrong_id",
    "name": "English Placeholder",
    "description": "model desc",
    "tags": ["AI"],
    "params": [],
    "scoring": {},
}

ENTRY_SIGNALS = []
EXIT_SIGNALS = []
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 20
ALERTS = []

def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    return pl.lit(True)
'''


def test_normalize_strategy_meta_forces_ai_id_and_chinese_name():
    code = _normalize_strategy_meta(
        RAW_CODE,
        "ai_test123",
        name="断板反包",
        description="中文描述",
    )

    assert '"id": "ai_test123"' in code
    assert '"name": "断板反包"' in code
    assert '"description": "中文描述"' in code
    assert "custom_wrong_id" not in code
    assert "English Placeholder" not in code


def test_normalize_build_result_updates_code_and_meta():
    result = {"code": RAW_CODE, "meta": {}, "valid": True, "error": None}

    normalized = _normalize_build_result(
        result,
        "ai_from_frontend",
        name="中文策略名",
        description="前端描述",
    )

    assert normalized["valid"] is True
    assert normalized["meta"]["id"] == "ai_from_frontend"
    assert normalized["meta"]["name"] == "中文策略名"
    assert normalized["meta"]["description"] == "前端描述"
    assert '"id": "ai_from_frontend"' in normalized["code"]


def test_normalize_strategy_meta_inserts_missing_name_fields():
    raw = '''import polars as pl

META = {
    "id": "wrong",
    "tags": []
}

def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    return pl.lit(True)
'''

    code = _normalize_strategy_meta(raw, "ai_inserted", name="中文名", description="描述")

    compile(code, "<strategy>", "exec")
    assert '"id": "ai_inserted"' in code
    assert '"name": "中文名"' in code
    assert '"description": "描述"' in code
