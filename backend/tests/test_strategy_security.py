from __future__ import annotations

import asyncio
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import HTTPException

from app.api.strategy import AISaveRequest, ai_save
from app.strategy.ai_generator import AIStrategyGenerator
from app.strategy.engine import StrategyEngine


def _request(tmp_path):
    engine = StrategyEngine(
        strategy_dirs=[
            tmp_path / "strategies" / "custom",
            tmp_path / "strategies" / "ai",
        ],
    )
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    state = SimpleNamespace(repo=repo, strategy_engine=engine)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _valid_strategy(meta_id: str) -> str:
    return f'''import polars as pl

META = {{"id": "{meta_id}", "name": "Safe Strategy", "params": [], "scoring": {{}}}}
ENTRY_SIGNALS = []
EXIT_SIGNALS = []


def filter(df, params):
    return pl.lit(True)
'''


def test_ai_save_rejects_top_level_side_effect_before_write_or_reload(tmp_path):
    code = '''import polars as pl

META = {"id": "ai_bad", "name": "Bad Strategy"}
pl.read_parquet("/etc/passwd")


def filter(df, params):
    return pl.lit(True)
'''
    request = _request(tmp_path)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(ai_save(AISaveRequest(code=code, strategy_id="ai_bad"), request))

    assert exc.value.status_code == 400
    assert not (tmp_path / "strategies" / "ai" / "ai_bad.py").exists()


def test_ai_save_normalizes_meta_id_to_request_identity(tmp_path):
    request = _request(tmp_path)

    result = asyncio.run(
        ai_save(
            AISaveRequest(code=_valid_strategy("ai_other"), strategy_id="ai_bad"),
            request,
        )
    )

    path = tmp_path / "strategies" / "ai" / "ai_bad.py"
    assert result["ok"] is True
    assert path.exists()
    assert '"id": "ai_bad"' in path.read_text(encoding="utf-8")
    assert request.app.state.strategy_engine.get("ai_bad").file_path == path


def test_validate_code_preserves_result_contract():
    result = AIStrategyGenerator().validate_code(_valid_strategy("ai_safe"))

    assert result["valid"] is True
    assert result["meta"]["id"] == "ai_safe"
    assert result["error"] is None


def test_validate_code_or_raise_checks_expected_identity():
    with pytest.raises(ValueError, match="META.id"):
        AIStrategyGenerator.validate_code_or_raise(
            _valid_strategy("ai_other"),
            expected_strategy_id="ai_expected",
        )


def test_ai_strategy_safety_rejects_dunder_escape_inside_filter():
    code = '''import polars as pl

META = {"id": "ai_escape", "name": "Escape Strategy"}


def filter(df, params):
    return ().__class__.__mro__[1].__subclasses__()
'''

    with pytest.raises(ValueError):
        AIStrategyGenerator._validate_safety(code)


def test_ai_strategy_safety_allows_static_matrix_method():
    code = '''META = {"id": "ai_matrix", "name": "Matrix Strategy"}


class MatrixStrategy:
    @staticmethod
    def compute_signals(market, params):
        return None


MATRIX_STRATEGY = MatrixStrategy()
'''

    AIStrategyGenerator._validate_safety(code)


def test_ai_strategy_safety_rejects_other_matrix_method_decorators():
    code = '''META = {"id": "ai_matrix", "name": "Matrix Strategy"}


class MatrixStrategy:
    @classmethod
    def compute_signals(cls, market, params):
        return None


MATRIX_STRATEGY = MatrixStrategy()
'''

    with pytest.raises(ValueError, match="禁止使用函数装饰器"):
        AIStrategyGenerator._validate_safety(code)
