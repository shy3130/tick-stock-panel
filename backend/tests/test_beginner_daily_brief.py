from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import polars as pl


def _audit(*, dataset: str, observed_end: str = "2026-07-24") -> dict:
    return {
        "schema_version": 1,
        "provider": "derived" if dataset == "daily_enriched" else "tushare",
        "dataset": dataset,
        "status": "ok",
        "row_count": 100,
        "returned_symbols": [],
        "missing_symbols": [],
        "coverage_ratio": 1.0,
        "fallback_used": False,
        "synthetic": False,
        "issues": [],
        "observed_start": "2026-01-02",
        "observed_end": observed_end,
        "recorded_at": "2026-07-24T16:00:00+00:00",
    }


def _trusted_audits() -> list[dict]:
    return [
        _audit(dataset="instruments"),
        _audit(dataset="daily"),
        _audit(dataset="adj_factor", observed_end="2020-01-02"),
        _audit(dataset="daily_enriched"),
    ]


def _cache(*, symbols: int = 1, limit_up: bool = False) -> dict:
    results: dict[str, dict] = {}
    for strategy_id, score in (("trend_breakout", 82.0), ("bullish_alignment", 76.0)):
        rows = []
        for index in range(symbols):
            rows.append(
                {
                    "symbol": f"60000{index}.SH",
                    "name": f"Stock {index}",
                    "close": 10.0 + index,
                    "change_pct": 0.01,
                    "score": score - index,
                    "status": "limit_up" if limit_up else "normal",
                }
            )
        results[strategy_id] = {"as_of": "2026-07-24", "total": len(rows), "rows": rows}
    return {"as_of": "2026-07-24", "updated_at": 1, "results": results}


def _recommendations(
    *,
    audits: list[dict] | None = None,
    cache: dict | None = None,
    adjustment_factor_problem: dict[str, str] | None = None,
) -> dict:
    from app.services.advisor import build_advisor_recommendations

    return build_advisor_recommendations(
        audits or _trusted_audits(),
        cache or _cache(),
        adjustment_factor_problem=adjustment_factor_problem,
    )


def test_daily_brief_observe_only_propagates_blocked_runtime_factor_risk():
    from app.services.advisor import build_beginner_daily_brief

    runtime_problem = {
        "code": "ADJ_FACTOR_RUNTIME_UNAVAILABLE",
        "reason": "Adjustment-factor data cannot be read",
        "next_action": "Resync adjustment-factor data",
    }
    brief = build_beginner_daily_brief(
        _recommendations(adjustment_factor_problem=runtime_problem)
    )

    assert brief["action_state"] == "OBSERVE_ONLY"
    assert brief["data_gate"]["decision"] == "BLOCK"
    assert brief["data_gate"]["runtime_problems"] == [runtime_problem]
    assert "数据" in brief["today_message"]
    assert brief["next_step"]


def test_daily_brief_simulate_only_when_no_go_candidate_survives_risk_gates():
    from app.services.advisor import build_beginner_daily_brief

    brief = build_beginner_daily_brief(_recommendations(cache=_cache(limit_up=True)))

    assert brief["action_state"] == "SIMULATE_ONLY"
    assert brief["data_gate"]["decision"] == "PASS"
    assert all(candidate["research_decision"] != "GO" for candidate in brief["candidates"])
    assert brief["candidates"][0]["risk_flags"] == [
        {"code": "LIMIT_UP", "message": "当前处于涨停或一字涨停状态, 不作为可追入候选"}
    ]


def test_daily_brief_research_only_exposes_at_most_three_deterministic_candidates():
    from app.services.advisor import build_beginner_daily_brief

    brief = build_beginner_daily_brief(_recommendations(cache=_cache(symbols=4)))

    assert brief["action_state"] == "RESEARCH_ONLY"
    assert [candidate["symbol"] for candidate in brief["candidates"]] == [
        "600000.SH",
        "600001.SH",
        "600002.SH",
    ]
    assert len(brief["candidates"]) == 3
    candidate = brief["candidates"][0]
    assert candidate == {
        "symbol": "600000.SH",
        "name": "Stock 0",
        "research_decision": "GO",
        "deterministic_reasons": [
            "研究判断: 可进入研究清单",
            "策略共识: 2条独立策略给出了同向结果",
        ],
        "observation_conditions": [
            "下次复核后, 数据检查仍然合格",
            "下次复核后, 研究结论仍为可进入研究清单",
        ],
        "invalidation_conditions": [
            "任一必需数据回执异常或运行时校验失败",
            "下次复核后, 研究结论不再是可进入研究清单",
            "下次复核后出现任一风险标记",
        ],
        "risk_flags": [],
    }
    forbidden = {"target_price", "price_target", "trade_instruction", "instruction"}
    assert not forbidden.intersection(candidate)


def test_daily_brief_endpoint_uses_persisted_audits_cache_and_runtime_risk(
    monkeypatch,
    tmp_path,
):
    from app.api import advisor as advisor_api

    factor_path = tmp_path / "adj_factor" / "all.parquet"
    factor_path.parent.mkdir(parents=True)
    pl.DataFrame(
        {"symbol": ["600000.SH"], "trade_date": [date(2026, 7, 24)], "ex_factor": [1.1]}
    ).write_parquet(factor_path)
    monkeypatch.setattr(advisor_api, "load_latest_audits", lambda data_dir: _trusted_audits())
    monkeypatch.setattr(advisor_api.strategy_cache, "read_cache", lambda data_dir: _cache())
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
            ),
        ),
    )

    brief = advisor_api.daily_brief(request)

    assert brief["action_state"] == "SIMULATE_ONLY"
    assert brief["data_gate"]["runtime_problems"] == []
    assert brief["candidates"][0]["risk_flags"] == [
        {
            "code": "ADJUSTMENT_EVENT_ON_AS_OF",
            "message": "策略日期发生除权除息事件, 已隔离并等待人工复核",
        }
    ]
    assert brief["method"] == {
        "kind": "deterministic",
        "policy_factors_included": False,
        "ai_can_change_score": False,
        "auto_trading": False,
    }


def _brief_human_strings(brief: dict) -> list[str]:
    strings = [brief["today_message"], brief["next_step"]]
    for candidate in brief["candidates"]:
        for field in (
            "deterministic_reasons",
            "observation_conditions",
            "invalidation_conditions",
        ):
            strings.extend(candidate[field])
    return strings


def test_daily_brief_human_copy_hides_internal_tokens_for_every_action_state():
    from app.services.advisor import build_beginner_daily_brief

    runtime_problem = {
        "code": "ADJ_FACTOR_RUNTIME_UNAVAILABLE",
        "reason": "Adjustment-factor data cannot be read",
        "next_action": "Resync adjustment-factor data",
    }
    briefs = [
        build_beginner_daily_brief(
            _recommendations(adjustment_factor_problem=runtime_problem)
        ),
        build_beginner_daily_brief(_recommendations(cache=_cache(limit_up=True))),
        build_beginner_daily_brief(_recommendations(cache=_cache())),
    ]

    assert [brief["action_state"] for brief in briefs] == [
        "OBSERVE_ONLY",
        "SIMULATE_ONLY",
        "RESEARCH_ONLY",
    ]
    assert [brief["data_gate"]["decision"] for brief in briefs] == ["BLOCK", "PASS", "PASS"]
    assert briefs[1]["candidates"][0]["research_decision"] == "NO-GO"
    assert briefs[2]["candidates"][0]["research_decision"] == "GO"

    forbidden_tokens = {
        "PASS",
        "BLOCK",
        "GO",
        "WAIT",
        "NO-GO",
        "trend_breakout",
        "bullish_alignment",
    }
    for brief in briefs:
        human_copy = "\n".join(_brief_human_strings(brief))
        assert not any(token in human_copy for token in forbidden_tokens)
