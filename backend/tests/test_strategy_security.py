from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.strategy import AISaveRequest, ai_save
from app.strategy.ai_generator import AIStrategyGenerator


class _EngineStub:
    def __init__(self) -> None:
        self.reload_called = False

    def reload(self) -> None:
        self.reload_called = True

    def has(self, strategy_id: str) -> bool:  # noqa: ARG002
        return True


def _request(tmp_path, engine: _EngineStub):
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
    engine = _EngineStub()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(ai_save(AISaveRequest(code=code, strategy_id="ai_bad"), _request(tmp_path, engine)))

    assert exc.value.status_code == 400
    assert not (tmp_path / "strategies" / "ai" / "ai_bad.py").exists()
    assert engine.reload_called is False


def test_ai_save_rejects_meta_id_mismatch_before_write_or_reload(tmp_path):
    engine = _EngineStub()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            ai_save(
                AISaveRequest(code=_valid_strategy("ai_other"), strategy_id="ai_bad"),
                _request(tmp_path, engine),
            )
        )

    assert exc.value.status_code == 400
    assert "META.id" in str(exc.value.detail)
    assert not (tmp_path / "strategies" / "ai" / "ai_bad.py").exists()
    assert engine.reload_called is False


def test_ai_strategy_safety_rejects_dunder_escape_inside_filter():
    code = '''import polars as pl

META = {"id": "ai_escape", "name": "Escape Strategy"}


def filter(df, params):
    return ().__class__.__mro__[1].__subclasses__()
'''

    with pytest.raises(ValueError):
        AIStrategyGenerator._validate_safety(code)
