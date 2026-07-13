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


# --- 回归: LLM 偏移写法 -------------------------------------------------
# 模型常给 META 加类型注解 (META: dict = {...}, ast.AnnAssign 节点)。
# 旧版匹配器只遍历 ast.Assign, 漏掉注解形式 → 报「找不到 META 字典」。

ANNOTATED_CODE = '''"""模型返回的策略 (带类型注解的 META — LLM 常见偏移)"""
import polars as pl

META: dict = {
    "id": "annotated_wrong_id",
    "name": "Placeholder",
    "description": "model desc",
    "tags": ["AI"],
    "params": [],
    "scoring": {},
}

def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    return pl.lit(True)
'''


def test_find_meta_dict_accepts_type_annotated_form():
    """META: dict = {...} 必须能被识别 (旧版会抛「找不到 META 字典」)。"""
    from app.api.strategy import _find_meta_dict

    node = _find_meta_dict(ANNOTATED_CODE)
    assert node is not None  # 能找到就说明没抛异常


def test_extract_meta_accepts_type_annotated_form():
    from app.strategy.ai_generator import AIStrategyGenerator

    meta = AIStrategyGenerator._extract_meta(ANNOTATED_CODE)
    assert meta["id"] == "annotated_wrong_id"
    assert meta["name"] == "Placeholder"


def test_normalize_strategy_meta_works_on_annotated_form():
    """端到端: AI 生成注解形式 META 时, 规范化不再报「规范化 META 失败」。"""
    code = _normalize_strategy_meta(
        ANNOTATED_CODE,
        "ai_annotated_ok",
        name="断板反包",
        description="中文描述",
    )
    assert '"id": "ai_annotated_ok"' in code
    assert '"name": "断板反包"' in code
    assert '"description": "中文描述"' in code
    assert "annotated_wrong_id" not in code


def test_normalize_build_result_succeeds_on_annotated_form():
    """模拟前端 /build/stream 的完整结果路径 (之前报错的入口)。"""
    result = {"code": ANNOTATED_CODE, "meta": {}, "valid": True, "error": None}

    normalized = _normalize_build_result(result, "ai_build_ok")

    assert normalized["valid"] is True
    assert normalized["error"] is None
    assert normalized["meta"]["id"] == "ai_build_ok"
