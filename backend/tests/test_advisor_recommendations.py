from __future__ import annotations

from types import SimpleNamespace

import pytest


def _audit(
    *,
    dataset: str,
    status: str = "ok",
    provider: str = "tushare",
    coverage_ratio: float = 1.0,
    observed_end: str | None = "2026-07-24",
    fallback_used: bool = False,
    synthetic: bool = False,
) -> dict:
    return {
        "schema_version": 1,
        "provider": provider,
        "dataset": dataset,
        "status": status,
        "row_count": 100,
        "returned_symbols": [],
        "missing_symbols": [],
        "coverage_ratio": coverage_ratio,
        "fallback_used": fallback_used,
        "synthetic": synthetic,
        "issues": [],
        "observed_start": "2026-01-02",
        "observed_end": observed_end,
        "recorded_at": "2026-07-24T16:00:00+00:00",
    }


def _cache(*, limit_up: bool = False) -> dict:
    row = {
        "symbol": "600000.SH",
        "name": "浦发银行",
        "close": 10.2,
        "change_pct": 0.025,
        "score": 82.0,
        "status": "limit_up" if limit_up else "normal",
    }
    return {
        "as_of": "2026-07-24",
        "updated_at": 1,
        "results": {
            "trend_breakout": {
                "as_of": "2026-07-24",
                "total": 1,
                "rows": [row],
            },
            "bullish_alignment": {
                "as_of": "2026-07-24",
                "total": 1,
                "rows": [{**row, "score": 76.0}],
            },
        },
    }


def _trusted_audits(**overrides: dict) -> list[dict]:
    audits = {
        "instruments": _audit(dataset="instruments"),
        "daily": _audit(dataset="daily"),
        "adj_factor": _audit(
            dataset="adj_factor",
            observed_end="2026-06-10",
        ),
        "daily_enriched": _audit(
            dataset="daily_enriched",
            provider="derived",
        ),
    }
    audits.update(overrides)
    return list(audits.values())


def test_advisor_go_requires_fresh_audited_data_and_strategy_consensus():
    from app.services.advisor import build_advisor_recommendations

    result = build_advisor_recommendations(
        _trusted_audits(),
        _cache(),
    )

    assert result["as_of"] == "2026-07-24"
    assert result["data_gate"]["decision"] == "PASS"
    assert result["data_gate"]["provider"] == "tushare"
    assert set(result["data_gate"]["datasets"]) == {
        "instruments",
        "daily",
        "adj_factor",
        "daily_enriched",
    }
    assert result["data_gate"]["datasets"]["daily_enriched"] == {
        "status": "ok",
        "provider": "derived",
        "coverage_ratio": 1.0,
        "observed_start": "2026-01-02",
        "observed_end": "2026-07-24",
        "reasons": [],
    }
    [candidate] = result["candidates"]
    assert candidate["symbol"] == "600000.SH"
    assert candidate["decision"] == "GO"
    assert candidate["decision_label"] == "可进入研究清单"
    assert candidate["strategy_count"] == 2
    assert candidate["strategies"] == ["bullish_alignment", "trend_breakout"]
    assert candidate["score"] == 89.1
    assert candidate["score_method"] == "0.7x最高策略分 + 0.3x平均策略分 + 共识加分"
    assert candidate["ai_generated"] is False


def test_advisor_blocks_go_when_market_data_is_stale():
    from app.services.advisor import build_advisor_recommendations

    result = build_advisor_recommendations(
        _trusted_audits(
            daily=_audit(dataset="daily", observed_end="2026-07-23"),
        ),
        _cache(),
    )

    assert result["data_gate"]["decision"] == "BLOCK"
    assert "截止日" in result["data_gate"]["reasons"][0]
    assert result["candidates"][0]["decision"] == "NO-GO"


def test_advisor_blocks_synthetic_or_fallback_receipts():
    from app.services.advisor import build_advisor_recommendations

    result = build_advisor_recommendations(
        _trusted_audits(
            daily=_audit(dataset="daily", fallback_used=True, synthetic=True),
        ),
        _cache(),
    )

    assert result["data_gate"]["decision"] == "BLOCK"
    assert any("伪造" in reason or "回退" in reason for reason in result["data_gate"]["reasons"])
    assert all(row["decision"] == "NO-GO" for row in result["candidates"])


def test_advisor_requires_at_least_95_percent_daily_coverage():
    from app.services.advisor import build_advisor_recommendations

    result = build_advisor_recommendations(
        _trusted_audits(
            daily=_audit(dataset="daily", status="partial", coverage_ratio=0.94),
        ),
        _cache(),
    )

    assert result["data_gate"]["decision"] == "BLOCK"
    assert "94.0%" in result["data_gate"]["reasons"][0]


def test_advisor_marks_single_strategy_as_wait_instead_of_go():
    from app.services.advisor import build_advisor_recommendations

    cache = _cache()
    cache["results"].pop("bullish_alignment")
    result = build_advisor_recommendations(
        _trusted_audits(),
        cache,
    )

    [candidate] = result["candidates"]
    assert candidate["decision"] == "WAIT"
    assert candidate["decision_label"] == "等待更多确认"


def test_advisor_never_labels_a_limit_up_candidate_as_go():
    from app.services.advisor import build_advisor_recommendations

    result = build_advisor_recommendations(
        _trusted_audits(),
        _cache(limit_up=True),
    )

    [candidate] = result["candidates"]
    assert candidate["decision"] == "NO-GO"
    assert "涨停" in candidate["risk_reasons"][0]


def test_advisor_api_reads_only_persisted_audits_and_strategy_cache(monkeypatch, tmp_path):
    from app.api import advisor as advisor_api

    monkeypatch.setattr(
        advisor_api,
        "load_latest_audits",
        lambda data_dir: _trusted_audits(),
    )
    monkeypatch.setattr(advisor_api.strategy_cache, "read_cache", lambda data_dir: _cache())
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
            ),
        ),
    )

    result = advisor_api.recommendations(request, limit=10)

    assert result["data_gate"]["decision"] == "PASS"
    assert result["candidates"][0]["decision"] == "GO"


def test_advisor_explicitly_blocks_missing_required_receipts():
    from app.services.advisor import build_advisor_recommendations

    result = build_advisor_recommendations(
        [_audit(dataset="instruments"), _audit(dataset="daily")],
        _cache(),
    )

    gate = result["data_gate"]
    assert gate["decision"] == "BLOCK"
    assert gate["datasets"]["adj_factor"] == {
        "status": "missing",
        "provider": None,
        "coverage_ratio": 0.0,
        "observed_start": None,
        "observed_end": None,
        "reasons": ["缺少除权因子可信度回执"],
    }
    assert gate["datasets"]["daily_enriched"]["status"] == "missing"
    assert gate["datasets"]["daily_enriched"]["reasons"] == [
        "缺少派生日K可信度回执"
    ]
    assert all(row["decision"] == "NO-GO" for row in result["candidates"])


def test_advisor_treats_adj_factor_dates_as_event_stream_metadata():
    from app.services.advisor import build_advisor_recommendations

    result = build_advisor_recommendations(
        _trusted_audits(
            adj_factor=_audit(
                dataset="adj_factor",
                observed_end="2020-01-02",
            ),
        ),
        _cache(),
    )

    assert result["data_gate"]["decision"] == "PASS"
    assert result["data_gate"]["datasets"]["adj_factor"]["observed_end"] == "2020-01-02"


def test_advisor_blocks_partial_adj_factor_below_95_percent_coverage():
    from app.services.advisor import build_advisor_recommendations

    result = build_advisor_recommendations(
        _trusted_audits(
            adj_factor=_audit(
                dataset="adj_factor",
                status="partial",
                coverage_ratio=0.94,
                observed_end="2020-01-02",
            ),
        ),
        _cache(),
    )

    assert result["data_gate"]["decision"] == "BLOCK"
    assert "94.0%" in result["data_gate"]["datasets"]["adj_factor"]["reasons"][0]


def test_advisor_requires_fresh_95_percent_daily_enriched_coverage():
    from app.services.advisor import build_advisor_recommendations

    result = build_advisor_recommendations(
        _trusted_audits(
            daily_enriched=_audit(
                dataset="daily_enriched",
                provider="derived",
                status="partial",
                coverage_ratio=0.94,
                observed_end="2026-07-23",
            ),
        ),
        _cache(),
    )

    dataset = result["data_gate"]["datasets"]["daily_enriched"]
    assert result["data_gate"]["decision"] == "BLOCK"
    assert any("94.0%" in reason for reason in dataset["reasons"])
    assert any("策略日期" in reason for reason in dataset["reasons"])


@pytest.mark.parametrize(
    ("dataset", "audit"),
    [
        ("daily", _audit(dataset="daily", status="error")),
        ("adj_factor", _audit(dataset="adj_factor", status="invalid")),
        (
            "daily_enriched",
            _audit(dataset="daily_enriched", provider="derived", status="empty"),
        ),
        ("daily", _audit(dataset="daily", synthetic=True)),
        ("adj_factor", _audit(dataset="adj_factor", fallback_used=True)),
        (
            "daily_enriched",
            _audit(dataset="daily_enriched", provider="derived", synthetic=True),
        ),
    ],
)
def test_advisor_blocks_unsafe_critical_dataset_receipts(dataset, audit):
    from app.services.advisor import build_advisor_recommendations

    result = build_advisor_recommendations(
        _trusted_audits(**{dataset: audit}),
        _cache(),
    )

    assert result["data_gate"]["decision"] == "BLOCK"
    assert result["data_gate"]["datasets"][dataset]["reasons"]
