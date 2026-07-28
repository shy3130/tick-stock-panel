from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(tmp_path) -> TestClient:
    from app.api import paper

    app = FastAPI()
    app.include_router(paper.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


def _write_gate_context(data_dir, *, blocked: bool) -> None:
    as_of = "2026-07-29"
    quality_dir = data_dir / "data_quality"
    quality_dir.mkdir(parents=True, exist_ok=True)
    for dataset in ("instruments", "daily", "adj_factor", "daily_enriched"):
        coverage = 0.50 if blocked and dataset == "daily" else 1.0
        observed_end = "2020-01-02" if dataset == "adj_factor" else as_of
        payload = {
            "schema_version": 1,
            "provider": "derived" if dataset == "daily_enriched" else "tushare",
            "dataset": dataset,
            "status": "partial" if coverage < 1 else "ok",
            "row_count": 100,
            "returned_symbols": [],
            "missing_symbols": [],
            "coverage_ratio": coverage,
            "fallback_used": False,
            "synthetic": False,
            "issues": [],
            "observed_start": "2020-01-02",
            "observed_end": observed_end,
            "recorded_at": "2026-07-29T15:10:00+00:00",
        }
        (quality_dir / f"{dataset}.json").write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

    factor_path = data_dir / "adj_factor" / "all.parquet"
    factor_path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "trade_date": [date(2026, 7, 28)],
            "ex_factor": [1.0],
        }
    ).write_parquet(factor_path)

    cache_path = data_dir / "user_data" / "strategy_cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "as_of": as_of,
                "updated_at": 1,
                "results": {
                    "test_strategy": {
                        "as_of": as_of,
                        "total": 0,
                        "rows": [],
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _trade_payload() -> dict:
    return {
        "symbol": "600000.SH",
        "name": "浦发银行",
        "side": "BUY",
        "quantity": 100,
        "price": 10.0,
        "trade_date": "2026-07-29",
        "plan_note": "只做模拟练习",
        "invalidation_note": "失效后停止跟踪",
    }


def test_get_account_uses_real_http_route_and_lazily_initializes(tmp_path):
    response = _client(tmp_path).get("/api/paper/account")

    assert response.status_code == 200
    body = response.json()
    assert body["cash"] == 10_000.0
    assert body["fee_assumptions"]["commission_rate"] == 0.0003
    assert (tmp_path / "user_data" / "paper_account.json").exists()


def test_reset_route_returns_chinese_400_for_bad_confirmation_or_value(tmp_path):
    client = _client(tmp_path)

    bad_confirmation = client.post(
        "/api/paper/reset",
        json={"initial_cash": 5000, "confirmation": "reset"},
    )
    bad_value = client.post(
        "/api/paper/reset",
        json={"initial_cash": 8000, "confirmation": "RESET"},
    )

    assert bad_confirmation.status_code == 400
    assert "RESET" in bad_confirmation.json()["detail"]
    assert bad_value.status_code == 400
    assert "初始资金" in bad_value.json()["detail"]


def test_trade_route_rejects_persisted_blocked_gate_with_409(tmp_path):
    _write_gate_context(tmp_path, blocked=True)

    response = _client(tmp_path).post("/api/paper/trades", json=_trade_payload())

    assert response.status_code == 409
    assert "数据检查" in response.json()["detail"]
    assert not (tmp_path / "user_data" / "paper_account.json").exists()


def test_trade_route_allows_fill_only_after_persisted_gate_passes(tmp_path):
    _write_gate_context(tmp_path, blocked=False)
    client = _client(tmp_path)

    response = client.post("/api/paper/trades", json=_trade_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["cash"] == 8_995.0
    assert body["journal"][0]["plan_note"] == "只做模拟练习"
    assert body["journal"][0]["invalidation_note"] == "失效后停止跟踪"
    assert body["journal"][0]["side"] == "BUY"


def test_trade_route_returns_chinese_400_for_domain_validation(tmp_path):
    _write_gate_context(tmp_path, blocked=False)
    payload = _trade_payload()
    payload["quantity"] = 50

    response = _client(tmp_path).post("/api/paper/trades", json=payload)

    assert response.status_code == 400
    assert "100 股" in response.json()["detail"]


def test_low_proceeds_sell_returns_400_without_changing_account(tmp_path):
    _write_gate_context(tmp_path, blocked=False)
    client = _client(tmp_path)
    buy = _trade_payload()
    buy["price"] = 99.95
    buy["trade_date"] = "2026-07-27"
    assert client.post("/api/paper/trades", json=buy).status_code == 200
    path = tmp_path / "user_data" / "paper_account.json"
    persisted_before = path.read_text(encoding="utf-8")

    sell = {
        **_trade_payload(),
        "side": "SELL",
        "quantity": 1,
        "price": 0.01,
        "trade_date": "2026-07-29",
    }
    response = client.post("/api/paper/trades", json=sell)

    assert response.status_code == 400
    assert "现金" in response.json()["detail"]
    assert path.read_text(encoding="utf-8") == persisted_before
    account = client.get("/api/paper/account").json()
    assert account["positions"][0]["quantity"] == 100
    assert len(account["journal"]) == 1


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("name", "股" * 81, "股票名称"),
        ("plan_note", "计" * 501, "模拟计划"),
        ("invalidation_note", "失" * 501, "失效条件"),
    ],
)
def test_trade_route_returns_chinese_400_for_oversized_manual_text(
    tmp_path,
    field,
    value,
    message,
):
    _write_gate_context(tmp_path, blocked=False)
    payload = _trade_payload()
    payload[field] = value

    response = _client(tmp_path).post("/api/paper/trades", json=payload)

    assert response.status_code == 400
    assert message in response.json()["detail"]
    assert not (tmp_path / "user_data" / "paper_account.json").exists()


def test_trade_route_missing_manual_fill_fields_returns_chinese_400(tmp_path):
    _write_gate_context(tmp_path, blocked=False)

    response = _client(tmp_path).post(
        "/api/paper/trades",
        json={"symbol": "600000.SH"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]


@pytest.mark.parametrize("path", ["/api/paper/reset", "/api/paper/trades"])
@pytest.mark.parametrize(
    ("body_kind", "request_kwargs"),
    [
        ("empty", {"content": b""}),
        ("array", {"json": []}),
        ("scalar", {"json": 123}),
        (
            "malformed",
            {
                "content": b"{",
                "headers": {"content-type": "application/json"},
            },
        ),
    ],
)
def test_mutating_routes_return_chinese_400_for_non_object_or_invalid_json_body(
    tmp_path,
    path,
    body_kind,
    request_kwargs,
):
    _write_gate_context(tmp_path, blocked=False)

    response = _client(tmp_path).post(path, **request_kwargs)

    assert response.status_code == 400, body_kind
    detail = response.json()["detail"]
    assert any(word in detail for word in ("请求", "内容", "JSON"))


@pytest.mark.parametrize(
    ("path", "field"),
    [
        ("/api/paper/reset", "initial_cash"),
        ("/api/paper/trades", "quantity"),
    ],
)
def test_mutating_routes_return_chinese_400_for_5000_digit_json_integer(
    tmp_path,
    path,
    field,
):
    _write_gate_context(tmp_path, blocked=False)
    raw_json = f'{{"{field}":' + ("9" * 5_000) + "}"

    response = _client(tmp_path).post(
        path,
        content=raw_json.encode(),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert "请求" in detail


@pytest.mark.parametrize(
    ("path", "expected_properties"),
    [
        (
            "/api/paper/reset",
            {"initial_cash", "confirmation"},
        ),
        (
            "/api/paper/trades",
            {
                "symbol",
                "name",
                "side",
                "quantity",
                "price",
                "trade_date",
                "plan_note",
                "invalidation_note",
            },
        ),
    ],
)
def test_mutating_routes_publish_request_body_schema(
    tmp_path,
    path,
    expected_properties,
):
    document = _client(tmp_path).get("/openapi.json").json()
    request_body = document["paths"][path]["post"]["requestBody"]
    body_schema = request_body["content"]["application/json"]["schema"]
    reference = body_schema["$ref"]
    schema_name = reference.rsplit("/", 1)[-1]
    properties = document["components"]["schemas"][schema_name]["properties"]

    assert expected_properties <= set(properties)
