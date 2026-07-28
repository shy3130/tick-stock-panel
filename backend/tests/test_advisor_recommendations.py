from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import polars as pl
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
        "next_actions": [],
    }
    [candidate] = result["candidates"]
    assert candidate["symbol"] == "600000.SH"
    assert candidate["decision"] == "GO"
    assert candidate["decision_label"] == "可进入研究清单"
    assert candidate["strategy_count"] == 2
    assert candidate["strategies"] == ["bullish_alignment", "trend_breakout"]
    assert candidate["score"] == 89.1
    assert candidate["score_method"] == "0.7x最高策略分 + 0.3x平均策略分 + 共识加分"
    assert candidate["risk_flags"] == []
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
    assert candidate["risk_flags"] == [
        {
            "code": "LIMIT_UP",
            "message": "当前处于涨停或一字涨停状态, 不作为可追入候选",
        }
    ]
    assert "涨停" in candidate["risk_reasons"][0]


def test_advisor_quarantines_finite_daily_return_above_30_percent():
    from app.services.advisor import build_advisor_recommendations

    cache = _cache()
    for result in cache["results"].values():
        result["rows"][0]["change_pct"] = 0.50

    recommendation = build_advisor_recommendations(_trusted_audits(), cache)

    [candidate] = recommendation["candidates"]
    assert candidate["decision"] == "NO-GO"
    assert candidate["risk_flags"] == [
        {
            "code": "ABNORMAL_DAILY_RETURN",
            "message": "当日涨跌幅绝对值超过 30%, 已隔离并等待人工复核",
        }
    ]
    assert candidate["risk_reasons"] == [
        "当日涨跌幅绝对值超过 30%, 已隔离并等待人工复核"
    ]


def test_advisor_quarantines_same_day_adjustment_event_and_50_percent_move():
    from app.services.advisor import build_advisor_recommendations

    cache = _cache()
    for result in cache["results"].values():
        result["rows"][0]["change_pct"] = 0.50

    recommendation = build_advisor_recommendations(
        _trusted_audits(),
        cache,
        adjustment_event_symbols={"600000.SH"},
    )

    [candidate] = recommendation["candidates"]
    assert candidate["decision"] == "NO-GO"
    assert {flag["code"] for flag in candidate["risk_flags"]} == {
        "ADJUSTMENT_EVENT_ON_AS_OF",
        "ABNORMAL_DAILY_RETURN",
    }
    assert candidate["score"] == 89.1


@pytest.mark.parametrize("close", [None, float("nan"), float("inf"), 0.0, -1.0])
def test_advisor_quarantines_missing_non_finite_or_non_positive_close(close):
    from app.services.advisor import build_advisor_recommendations

    cache = _cache()
    for result in cache["results"].values():
        result["rows"][0]["close"] = close

    recommendation = build_advisor_recommendations(_trusted_audits(), cache)

    [candidate] = recommendation["candidates"]
    assert candidate["decision"] == "NO-GO"
    assert candidate["risk_flags"] == [
        {
            "code": "INVALID_PRICE",
            "message": "收盘价缺失、非有限数或不大于 0, 已隔离并等待人工复核",
        }
    ]


def test_advisor_limit_down_block_has_stable_risk_code():
    from app.services.advisor import build_advisor_recommendations

    cache = _cache()
    for result in cache["results"].values():
        result["rows"][0]["status"] = "limit_down"

    recommendation = build_advisor_recommendations(_trusted_audits(), cache)

    [candidate] = recommendation["candidates"]
    assert candidate["decision"] == "NO-GO"
    assert candidate["risk_flags"] == [
        {"code": "LIMIT_DOWN", "message": "当前处于跌停状态"}
    ]
    assert candidate["risk_reasons"] == ["当前处于跌停状态"]


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("change_pct", 0.50, "ABNORMAL_DAILY_RETURN"),
        ("close", float("nan"), "INVALID_PRICE"),
        ("status", "limit_up", "LIMIT_UP"),
        ("status", "limit_down", "LIMIT_DOWN"),
    ],
)
def test_advisor_quarantines_risk_from_non_representative_strategy_row(
    field,
    value,
    expected_code,
):
    from app.services.advisor import build_advisor_recommendations

    cache = _cache()
    cache["results"]["bullish_alignment"]["rows"][0][field] = value

    recommendation = build_advisor_recommendations(_trusted_audits(), cache)

    [candidate] = recommendation["candidates"]
    assert candidate["decision"] == "NO-GO"
    assert [flag["code"] for flag in candidate["risk_flags"]] == [expected_code]
    assert candidate["score"] == 89.1
    assert candidate["close"] == 10.2


def test_advisor_api_reads_only_persisted_audits_and_strategy_cache(monkeypatch, tmp_path):
    from app.api import advisor as advisor_api

    factor_path = tmp_path / "adj_factor" / "all.parquet"
    factor_path.parent.mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "trade_date": [date(2026, 7, 23)],
            "ex_factor": [1.0],
        }
    ).write_parquet(factor_path)
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


def test_advisor_api_quarantines_symbol_with_adjustment_event_on_strategy_date(
    monkeypatch,
    tmp_path,
):
    from app.api import advisor as advisor_api

    factor_path = tmp_path / "adj_factor" / "all.parquet"
    factor_path.parent.mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["600000.SH", "000001.SZ"],
            "trade_date": [date(2026, 7, 24), date(2026, 7, 23)],
            "ex_factor": [1.1, 1.0],
        }
    ).write_parquet(factor_path)
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

    [candidate] = result["candidates"]
    assert candidate["decision"] == "NO-GO"
    assert [flag["code"] for flag in candidate["risk_flags"]] == [
        "ADJUSTMENT_EVENT_ON_AS_OF"
    ]


@pytest.mark.parametrize(
    "factor_state",
    [
        "missing",
        "corrupt",
        "missing_symbol",
        "missing_trade_date",
        "malformed_trade_date",
    ],
)
def test_advisor_api_blocks_unavailable_adjustment_factor_file_without_crashing(
    monkeypatch,
    tmp_path,
    factor_state,
):
    from app.api import advisor as advisor_api

    factor_path = tmp_path / "adj_factor" / "all.parquet"
    if factor_state != "missing":
        factor_path.parent.mkdir(parents=True)
    if factor_state == "corrupt":
        factor_path.write_bytes(b"not parquet")
    elif factor_state == "missing_symbol":
        pl.DataFrame(
            {
                "trade_date": [date(2026, 7, 24)],
                "ex_factor": [1.1],
            }
        ).write_parquet(factor_path)
    elif factor_state == "missing_trade_date":
        pl.DataFrame(
            {
                "symbol": ["600000.SH"],
                "ex_factor": [1.1],
            }
        ).write_parquet(factor_path)
    elif factor_state == "malformed_trade_date":
        pl.DataFrame(
            {
                "symbol": ["600000.SH"],
                "trade_date": ["not-a-date"],
                "ex_factor": [1.1],
            }
        ).write_parquet(factor_path)
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

    gate = result["data_gate"]
    reason = "除权因子文件缺失、无法读取或结构不完整, 无法核对策略日期的除权除息事件"
    action = (
        "请重新同步除权因子, 并确认 all.parquet 包含 symbol、trade_date 列后"
        "再重新生成研究清单。"
    )
    assert gate["decision"] == "BLOCK"
    assert gate["runtime_problems"] == [
        {
            "code": "ADJ_FACTOR_RUNTIME_UNAVAILABLE",
            "reason": reason,
            "next_action": action,
        }
    ]
    assert reason in gate["reasons"]
    assert action in gate["next_actions"]
    assert reason in gate["datasets"]["adj_factor"]["reasons"]
    assert action in gate["datasets"]["adj_factor"]["next_actions"]
    assert result["candidates"][0]["decision"] == "NO-GO"


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
        "next_actions": ["请重新运行除权因子同步, 生成最新可信度回执后再试。"],
    }
    assert gate["datasets"]["daily_enriched"]["status"] == "missing"
    assert gate["datasets"]["daily_enriched"]["reasons"] == [
        "缺少派生日K可信度回执"
    ]
    assert gate["datasets"]["daily_enriched"]["next_actions"] == [
        "请重新运行派生日K同步, 生成最新可信度回执后再试。"
    ]
    assert gate["next_actions"] == [
        "请重新运行除权因子同步, 生成最新可信度回执后再试。",
        "请重新运行派生日K同步, 生成最新可信度回执后再试。",
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
    assert any(
        "补齐缺失标的" in action
        for action in result["data_gate"]["datasets"]["adj_factor"]["next_actions"]
    )


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
    assert any("补齐缺失标的" in action for action in dataset["next_actions"])
    assert any("同步到策略日期" in action for action in dataset["next_actions"])


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
    assert result["data_gate"]["datasets"][dataset]["next_actions"]


def test_advisor_blocks_malformed_receipt_fields_with_explicit_remediation():
    from app.services.advisor import build_advisor_recommendations

    malformed = _audit(dataset="adj_factor")
    malformed.update(
        provider="",
        status="mystery",
        coverage_ratio="NaN",
        fallback_used="false",
    )
    malformed.pop("synthetic")

    result = build_advisor_recommendations(
        _trusted_audits(adj_factor=malformed),
        _cache(),
    )

    dataset = result["data_gate"]["datasets"]["adj_factor"]
    assert result["data_gate"]["decision"] == "BLOCK"
    assert any("provider 必须是非空字符串" in reason for reason in dataset["reasons"])
    assert any("status 必须是" in reason for reason in dataset["reasons"])
    assert any("coverage_ratio 必须是" in reason for reason in dataset["reasons"])
    assert any("fallback_used 必须是布尔值" in reason for reason in dataset["reasons"])
    assert any("synthetic 必须是布尔值" in reason for reason in dataset["reasons"])
    assert dataset["next_actions"] == [
        "请检查除权因子数据源配置并重新同步, 以重新生成有效可信度回执。"
    ]
    assert dataset["next_actions"][0] in result["data_gate"]["next_actions"]
    assert all(row["decision"] == "NO-GO" for row in result["candidates"])


@pytest.mark.parametrize(
    "coverage_ratio",
    [float("nan"), float("inf"), -0.01, 1.01, "0.99", 10**1000],
)
def test_advisor_rejects_non_finite_out_of_range_or_coerced_coverage(
    coverage_ratio,
):
    from app.services.advisor import build_advisor_recommendations

    malformed = _audit(dataset="adj_factor")
    malformed["coverage_ratio"] = coverage_ratio

    result = build_advisor_recommendations(
        _trusted_audits(adj_factor=malformed),
        _cache(),
    )

    dataset = result["data_gate"]["datasets"]["adj_factor"]
    assert result["data_gate"]["decision"] == "BLOCK"
    assert any("coverage_ratio 必须是" in reason for reason in dataset["reasons"])
    assert dataset["next_actions"]


@pytest.mark.parametrize("malformed_first", [True, False])
def test_advisor_validates_all_duplicate_receipts_regardless_of_order(
    malformed_first,
):
    from app.services.advisor import build_advisor_recommendations

    malformed = _audit(dataset="adj_factor")
    malformed["status"] = "mystery"
    valid = _audit(dataset="adj_factor")
    duplicates = [malformed, valid] if malformed_first else [valid, malformed]
    audits = [
        _audit(dataset="instruments"),
        _audit(dataset="daily"),
        *duplicates,
        _audit(dataset="daily_enriched", provider="derived"),
    ]

    result = build_advisor_recommendations(audits, _cache())

    dataset = result["data_gate"]["datasets"]["adj_factor"]
    assert result["data_gate"]["decision"] == "BLOCK"
    assert any("重复" in reason for reason in dataset["reasons"])
    assert any("status 必须是" in reason for reason in dataset["reasons"])
    assert dataset["next_actions"] == [
        "请清理重复的除权因子回执并重新同步, 确保只保留一份最新可信度回执。",
        "请检查除权因子数据源配置并重新同步, 以重新生成有效可信度回执。",
    ]


@pytest.mark.parametrize(
    "dataset",
    ["instruments", "daily", "adj_factor", "daily_enriched"],
)
def test_advisor_blocks_duplicate_required_dataset_even_when_both_are_valid(
    dataset,
):
    from app.services.advisor import build_advisor_recommendations

    audits = _trusted_audits()
    duplicate = next(receipt for receipt in audits if receipt["dataset"] == dataset)
    audits.append(dict(duplicate))

    result = build_advisor_recommendations(audits, _cache())

    detail = result["data_gate"]["datasets"][dataset]
    assert result["data_gate"]["decision"] == "BLOCK"
    assert any("收到 2 份重复回执" in reason for reason in detail["reasons"])
    assert any("只保留一份最新可信度回执" in action for action in detail["next_actions"])


def test_advisor_blocks_when_over_limit_json_receipt_is_omitted(tmp_path):
    from app.data_providers.trust import load_latest_audits
    from app.services.advisor import build_advisor_recommendations

    out = tmp_path / "data_quality" / "adj_factor.json"
    out.parent.mkdir(parents=True)
    out.write_text(
        (
            '{"schema_version":1,"dataset":"adj_factor","provider":"tushare",'
            '"status":"ok","coverage_ratio":'
            + ("9" * 5_000)
            + ',"fallback_used":false,"synthetic":false}'
        ),
        encoding="utf-8",
    )
    audits = [
        _audit(dataset="instruments"),
        _audit(dataset="daily"),
        *load_latest_audits(tmp_path),
        _audit(dataset="daily_enriched", provider="derived"),
    ]

    result = build_advisor_recommendations(audits, _cache())

    detail = result["data_gate"]["datasets"]["adj_factor"]
    assert result["data_gate"]["decision"] == "BLOCK"
    assert detail["status"] == "missing"
    assert detail["reasons"] == ["缺少除权因子可信度回执"]
    assert detail["next_actions"] == [
        "请重新运行除权因子同步, 生成最新可信度回执后再试。"
    ]
