from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _write_trusted_context(data_dir) -> None:
    from app.services.research_snapshot import publish_research_snapshot

    as_of = "2026-07-29"
    quality_dir = data_dir / "data_quality"
    quality_dir.mkdir(parents=True)
    for dataset in ("instruments", "daily", "adj_factor", "daily_enriched"):
        observed_end = "2026-07-01" if dataset == "adj_factor" else as_of
        receipt = {
            "schema_version": 1,
            "provider": "derived" if dataset == "daily_enriched" else "tushare",
            "dataset": dataset,
            "status": "ok",
            "row_count": 1,
            "returned_symbols": ["600000.SH"],
            "missing_symbols": [],
            "coverage_ratio": 1.0,
            "fallback_used": False,
            "synthetic": False,
            "issues": [],
            "observed_start": "2026-07-01",
            "observed_end": observed_end,
            "recorded_at": "2026-07-29T15:10:00+00:00",
        }
        (quality_dir / f"{dataset}.json").write_text(
            json.dumps(receipt, ensure_ascii=False),
            encoding="utf-8",
        )

    factor_dir = data_dir / "adj_factor"
    factor_dir.mkdir()
    pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "trade_date": [date(2026, 7, 1)],
        }
    ).write_parquet(factor_dir / "all.parquet")

    user_dir = data_dir / "user_data"
    user_dir.mkdir()
    cache = {
        "as_of": as_of,
        "updated_at": 1,
        "results": {
            "trend_breakout": {
                "as_of": as_of,
                "total": 1,
                "rows": [
                    {
                        "symbol": "600000.SH",
                        "name": "浦发银行",
                        "close": 10.0,
                        "change_pct": 0.01,
                        "score": 82.0,
                        "status": "normal",
                    }
                ],
            },
            "bullish_alignment": {
                "as_of": as_of,
                "total": 1,
                "rows": [
                    {
                        "symbol": "600000.SH",
                        "name": "浦发银行",
                        "close": 10.0,
                        "change_pct": 0.01,
                        "score": 78.0,
                        "status": "normal",
                    }
                ],
            },
        },
    }
    (user_dir / "strategy_cache.json").write_text(
        json.dumps(cache, ensure_ascii=False),
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
    publish_research_snapshot(data_dir)


def test_daily_brief_and_paper_account_http_smoke_use_temporary_data_dir(
    tmp_path,
    monkeypatch,
):
    from app.api import advisor, paper

    _write_trusted_context(tmp_path)
    monkeypatch.setattr(
        advisor,
        "build_market_overview",
        lambda **kwargs: {
            "as_of": "2026-07-29",
            "breadth": {"total": 5_500},
            "emotion": {"score": 60, "label": "偏暖"},
        },
        raising=False,
    )
    app = FastAPI()
    app.include_router(advisor.router)
    app.include_router(paper.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    client = TestClient(app)

    brief_response = client.get("/api/advisor/daily-brief")
    account_response = client.get("/api/paper/account")

    assert brief_response.status_code == 200
    brief = brief_response.json()
    assert brief["action_state"] == "RESEARCH_ONLY"
    assert brief["data_gate"]["decision"] == "PASS"
    assert set(brief["data_gate"]["datasets"]) == {
        "instruments",
        "daily",
        "adj_factor",
        "daily_enriched",
    }
    assert len(brief["candidates"]) == 1
    assert brief["method"] == {
        "kind": "deterministic",
        "policy_factors_included": False,
        "ai_can_change_score": False,
        "auto_trading": False,
    }

    assert account_response.status_code == 200
    account = account_response.json()
    assert account["cash"] == 10_000.0
    assert account["total_equity"] == 10_000.0
    assert account["positions"] == []
    assert account["fee_assumptions"]["commission_rate"] == 0.0003
    assert (tmp_path / "user_data" / "paper_account.json").exists()
