from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import backtest


def test_factor_manifest_does_not_need_engine():
    out = backtest.factor_manifest()

    assert len(out["factors"]) >= 10
    assert out["factors"][0]["id"].startswith("alpha101_")


def test_compare_rejects_unknown_factor_before_engine():
    req = backtest.FactorCompareRequest(factor_ids=["alpha101_missing"])
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    with pytest.raises(HTTPException) as exc:
        backtest.factor_compare(req, request)

    assert exc.value.status_code == 400
    assert "unknown factor" in exc.value.detail
