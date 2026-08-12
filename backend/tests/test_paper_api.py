from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _warm_market_overview(monkeypatch):
    from app.api import advisor as advisor_api

    monkeypatch.setattr(
        advisor_api,
        "build_market_overview",
        lambda **kwargs: {
            "as_of": "2026-07-29",
            "breadth": {"total": 5_500},
            "emotion": {"score": 60, "label": "偏暖"},
        },
        raising=False,
    )


def _client(tmp_path) -> TestClient:
    from app.api import paper

    app = FastAPI()
    app.include_router(paper.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


def _write_gate_context(
    data_dir,
    *,
    blocked: bool,
    research_candidate: bool = False,
) -> None:
    from app.services.research_snapshot import publish_research_snapshot

    factor_path = data_dir / "adj_factor" / "all.parquet"
    factor_path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "trade_date": [date(2026, 7, 27)],
            "ex_factor": [1.0],
        }
    ).write_parquet(factor_path)

    rows = [
        {
            "symbol": "600000.SH",
            "name": "浦发银行",
            "close": 10.0,
            "change_pct": 0.01,
            "score": 82.0,
            "status": "normal",
        }
    ]

    def write_inputs(as_of: str) -> None:
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
                "recorded_at": f"{as_of}T15:10:00+00:00",
            }
            (quality_dir / f"{dataset}.json").write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )

        cache_path = data_dir / "user_data" / "strategy_cache.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {
                    "as_of": as_of,
                    "updated_at": 1,
                    "results": {
                        "test_strategy_a": {
                            "as_of": as_of,
                            "total": len(rows),
                            "rows": rows,
                        },
                        "test_strategy_b": {
                            "as_of": as_of,
                            "total": len(rows),
                            "rows": [{**row, "score": 78.0} for row in rows],
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        for dataset in ("kline_daily", "kline_daily_enriched"):
            source_path = data_dir / dataset / f"date={as_of}" / "part.parquet"
            source_path.parent.mkdir(parents=True, exist_ok=True)
            pl.DataFrame(
                {
                    "symbol": ["600000.SH"],
                    "date": [as_of],
                    "close": [10.0],
                }
            ).write_parquet(source_path)

    if not blocked and not research_candidate:
        write_inputs("2026-07-28")
        publish_research_snapshot(data_dir)

    write_inputs("2026-07-29")
    if not blocked:
        publish_research_snapshot(data_dir)


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


def test_trade_route_rejects_research_only_action_even_when_data_gate_passes(tmp_path):
    _write_gate_context(tmp_path, blocked=False, research_candidate=True)

    response = _client(tmp_path).post("/api/paper/trades", json=_trade_payload())

    assert response.status_code == 409
    assert "第1个确认日" in response.json()["detail"]
    assert not (tmp_path / "user_data" / "paper_account.json").exists()


def test_simulated_buy_requires_ready_state_for_the_same_symbol(tmp_path, monkeypatch):
    from app.api import advisor as advisor_api

    monkeypatch.setattr(
        advisor_api,
        "_persisted_daily_brief",
        lambda _request: {
            "action_state": "SIMULATE_ONLY",
            "candidates": [
                {"symbol": "600001.SH", "candidate_state": "READY"}
            ],
        },
    )

    response = _client(tmp_path).post("/api/paper/trades", json=_trade_payload())

    assert response.status_code == 409
    assert "可模拟练习" in response.json()["detail"]
    assert not (tmp_path / "user_data" / "paper_account.json").exists()


def test_existing_simulated_position_can_be_sold_when_daily_buy_gate_is_blocked(
    tmp_path,
    monkeypatch,
):
    from app.api import advisor as advisor_api
    from app.services import paper_account

    paper_account.record_trade(
        tmp_path,
        symbol="600000.SH",
        name="浦发银行",
        side="BUY",
        quantity=100,
        price=10.0,
        trade_date="2026-07-28",
        plan_note="历史模拟持仓",
        invalidation_note="仅验证退出不被新买门禁锁死",
    )
    monkeypatch.setattr(
        advisor_api,
        "_persisted_daily_brief",
        lambda _request: {
            "action_state": "OBSERVE_ONLY",
            "candidates": [],
        },
    )
    payload = {**_trade_payload(), "side": "SELL"}

    response = _client(tmp_path).post("/api/paper/trades", json=payload)

    assert response.status_code == 200
    assert response.json()["positions"] == []
    assert response.json()["journal"][-1]["side"] == "SELL"


def test_simulated_account_exposes_position_weight_and_concentration_risk(tmp_path):
    from app.services import paper_account

    account = paper_account.record_trade(
        tmp_path,
        symbol="600000.SH",
        name="浦发银行",
        side="BUY",
        quantity=100,
        price=10.0,
        trade_date="2026-07-28",
        plan_note="验证投资人风险视图",
        invalidation_note="只验证模拟账户",
    )

    [position] = account["positions"]
    assert position["portfolio_weight_pct"] == 10.05
    assert position["invested_weight_pct"] == 100.0
    assert account["portfolio_risk"] == {
        "position_count": 1,
        "cash_pct": 89.95,
        "invested_pct": 10.05,
        "largest_position_pct": 10.05,
        "largest_invested_position_pct": 100.0,
        "concentration_hhi": 1.0,
        "concentration_level": "EXTREME",
        "warnings": [
            {
                "code": "SINGLE_POSITION_CONCENTRATION",
                "message": "当前持仓内部100%集中于单一股票",
            }
        ],
    }


def test_empty_simulated_account_reports_no_portfolio_concentration(tmp_path):
    from app.services import paper_account

    account = paper_account.get_account(tmp_path, as_of=date(2026, 7, 29))

    assert account["portfolio_risk"] == {
        "position_count": 0,
        "cash_pct": 100.0,
        "invested_pct": 0.0,
        "largest_position_pct": 0.0,
        "largest_invested_position_pct": 0.0,
        "concentration_hhi": 0.0,
        "concentration_level": "NONE",
        "warnings": [],
    }


def test_two_equal_simulated_positions_report_high_internal_concentration(tmp_path):
    from app.services import paper_account

    for symbol, name in (("600000.SH", "浦发银行"), ("600001.SH", "测试股票")):
        account = paper_account.record_trade(
            tmp_path,
            symbol=symbol,
            name=name,
            side="BUY",
            quantity=100,
            price=10.0,
            trade_date="2026-07-28",
            plan_note="验证组合集中度",
            invalidation_note="只验证模拟账户",
        )

    assert [item["invested_weight_pct"] for item in account["positions"]] == [
        50.0,
        50.0,
    ]
    assert account["portfolio_risk"]["concentration_hhi"] == 0.5
    assert account["portfolio_risk"]["concentration_level"] == "HIGH"
    assert account["portfolio_risk"]["warnings"] == [
        {
            "code": "POSITION_CONCENTRATION",
            "message": "最大单一持仓占已投资资产50.00%",
        }
    ]


def test_trade_route_rejects_cold_market_even_without_a_research_candidate(
    tmp_path,
    monkeypatch,
):
    from app.api import advisor as advisor_api

    _write_gate_context(tmp_path, blocked=False)
    monkeypatch.setattr(
        advisor_api,
        "build_market_overview",
        lambda **kwargs: {
            "as_of": "2026-07-29",
            "breadth": {"total": 5_500},
            "emotion": {"score": 38, "label": "偏冷"},
        },
        raising=False,
    )

    response = _client(tmp_path).post("/api/paper/trades", json=_trade_payload())

    assert response.status_code == 409
    assert "偏冷" in response.json()["detail"]
    assert not (tmp_path / "user_data" / "paper_account.json").exists()


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
