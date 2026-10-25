"""AI 策略 META 规范化回归测试。"""
from __future__ import annotations

import pytest

from app.api.strategy import _normalize_build_result, _normalize_strategy_meta
from app.strategy.ai_generator import AIStrategyGenerator

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


@pytest.mark.parametrize("alias", ["STRATEGY_META", "meta"])
def test_normalize_strategy_meta_accepts_common_aliases(alias):
    from app.strategy.ai_generator import AIStrategyGenerator

    raw = RAW_CODE.replace("META =", f"{alias} =", 1)

    code = _normalize_strategy_meta(raw, "ai_alias_ok", name="别名策略")

    compile(code, "<strategy>", "exec")
    assert "META =" in code
    assert f"{alias} =" not in code
    assert AIStrategyGenerator._extract_meta(code)["id"] == "ai_alias_ok"


def test_validate_code_rejects_missing_meta():
    from app.strategy.ai_generator import AIStrategyGenerator

    code = """import polars as pl

def filter(df, params):
    return pl.lit(True)
"""

    result = AIStrategyGenerator().validate_code(code)

    assert result["valid"] is False
    assert "找不到 META 字典" in result["error"]


def test_validate_code_ignores_meta_inside_function():
    from app.strategy.ai_generator import AIStrategyGenerator

    code = """import polars as pl

def filter(df, params):
    META = {"id": "nested"}
    return pl.lit(True)
"""

    result = AIStrategyGenerator().validate_code(code)

    assert result["valid"] is False
    assert "找不到 META 字典" in result["error"]


def test_validate_code_rejects_missing_strategy_entrypoint():
    from app.strategy.ai_generator import AIStrategyGenerator

    result = AIStrategyGenerator().validate_code('META = {"id": "no_filter"}')

    assert result["valid"] is False
    assert result["error"] == "找不到策略入口函数 filter() 或 filter_history()"


def test_validate_code_accepts_matrix_strategy_entrypoint():
    from app.strategy.ai_generator import AIStrategyGenerator

    code = '''META = {"id": "matrix", "execution_backend": "matrix_native"}
EXECUTION_BACKEND = "matrix_native"
MATRIX_STRATEGY = object()
'''

    result = AIStrategyGenerator().validate_code(code)

    assert result["valid"] is True
    assert result["error"] is None


def test_validate_code_accepts_controlled_virtual_scoring_field():
    code = RAW_CODE.replace(
        '"scoring": {},',
        '"scoring": {"ma20_bias": 0.6, "vol_ratio_5d": 0.4},',
    )

    result = AIStrategyGenerator().validate_code(code)

    assert result["valid"] is True


def test_validate_code_rejects_unknown_scoring_field():
    code = RAW_CODE.replace(
        '"scoring": {},',
        '"scoring": {"close_above_ma20": 1.0},',
    )

    result = AIStrategyGenerator().validate_code(code)

    assert result["valid"] is False
    assert "close_above_ma20" in result["error"]


def test_validate_code_rejects_param_without_id():
    code = RAW_CODE.replace(
        '"params": [],',
        '"params": [{"name": "volume_ratio", "default": 1.5}],',
    )

    result = AIStrategyGenerator().validate_code(code)

    assert result["valid"] is False
    assert result["error"] == "META.params[0] 缺少非空 id"


def test_validate_code_rejects_missing_matrix_strategy_entrypoint():
    from app.strategy.ai_generator import AIStrategyGenerator

    code = '''META = {"id": "matrix", "execution_backend": "matrix_native"}
EXECUTION_BACKEND = "matrix_native"
'''

    result = AIStrategyGenerator().validate_code(code)

    assert result["valid"] is False
    assert result["error"] == "找不到 Matrix 策略入口 MATRIX_STRATEGY"


def test_extract_code_block_prefers_complete_strategy():
    from app.strategy.ai_generator import AIStrategyGenerator

    content = f"""```python
print("draft")
```

```python
{RAW_CODE}
```
"""

    assert AIStrategyGenerator._extract_code_block(content) == RAW_CODE.strip()


# ── META 闭合 `}` 写在最后一键行尾 (内联多行) ─────────────────
#
# AI 生成的策略常把 META 写成多行但闭合括号跟在最后一个键后面:
#   META = {"id": ..., "name": ...,
#           "params": []}
# 旧实现只找「独占一行的 }」, 找不到就把新字段追加到整个 block 末尾,
# 于是 research_only 落到字典外面, 生成 `unexpected indent` 的非法代码。

INLINE_CLOSE_CODE = '''"""模型返回的策略"""
import polars as pl

META = {"id": "ai_wrong", "name": "English", "description": "d",
        "tags": [], "asset_types": ["stock"], "timeframes": ["1d"],
        "basic_filter": {}, "params": []}


def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    return df
'''


def test_insert_meta_field_handles_inline_closing_brace():
    """闭合 `}` 在末键行尾时, 新字段必须still 插进字典内。"""
    import ast

    out = _normalize_strategy_meta(INLINE_CLOSE_CODE, "ai_fixed", "中文名")
    ast.parse(out)  # 语法必须合法
    meta = AIStrategyGenerator._extract_meta(out)
    assert meta.get("id") == "ai_fixed"
    assert meta.get("name") == "中文名"


def test_set_meta_bool_field_handles_inline_closing_brace():
    """research_only 注入走的是另一条改写路径, 同样要合法。"""
    import ast

    from app.api.strategy import _set_meta_bool_field

    out = _set_meta_bool_field(INLINE_CLOSE_CODE, "research_only", True)
    ast.parse(out)
    meta = AIStrategyGenerator._extract_meta(out)
    assert meta.get("research_only") is True


def test_inline_meta_full_save_path_is_valid_python():
    """走完整规范化 + 布尔注入的组合, 模拟 AI 保存策略的真实链路。"""
    import ast

    from app.api.strategy import _set_meta_bool_field

    step1 = _normalize_strategy_meta(INLINE_CLOSE_CODE, "ai_combo", "组合名")
    step2 = _set_meta_bool_field(step1, "research_only", True)
    ast.parse(step2)
    meta = AIStrategyGenerator._extract_meta(step2)
    assert meta.get("id") == "ai_combo"
    assert meta.get("research_only") is True
