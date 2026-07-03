from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import strategy


class FakeEngine:
    def __init__(self, item):
        self.item = item

    def get(self, strategy_id):
        if strategy_id != self.item.meta["id"]:
            raise ValueError(f"unknown strategy: {strategy_id}")
        return self.item


def request(item):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(strategy_engine=FakeEngine(item))))


def item(meta=None):
    return SimpleNamespace(meta=meta or {"id": "s1", "name": "策略1"})


def test_export_strategy_from_request_conditions():
    resp = strategy.export_strategy(
        "s1",
        strategy.ExportRequest(
            target="tdx",
            conditions=[
                {"left": "ma5", "op": ">", "right": "field:ma20"},
                {"left": "close", "op": ">", "right": "field:ma60"},
            ],
        ),
        request(item()),
    )

    assert resp["ok"] is True
    assert resp["target"] == "tdx"
    assert "XG:(MA(C,5)>MA(C,20) AND C>MA(C,60));" in resp["formula"]


def test_export_strategy_without_dsl_returns_ok_false():
    resp = strategy.export_strategy(
        "s1",
        strategy.ExportRequest(target="tdx"),
        request(item()),
    )

    assert resp["ok"] is False
    assert resp["unsupported"] == ["strategy has no META.export DSL"]


def test_export_strategy_rejects_unknown_target():
    with pytest.raises(HTTPException) as exc:
        strategy.export_strategy(
            "s1",
            strategy.ExportRequest(target="pine"),
            request(item()),
        )

    assert exc.value.status_code == 400


def test_export_strategy_404_unknown_strategy():
    with pytest.raises(HTTPException) as exc:
        strategy.export_strategy(
            "missing",
            strategy.ExportRequest(target="tdx"),
            request(item()),
        )

    assert exc.value.status_code == 404
