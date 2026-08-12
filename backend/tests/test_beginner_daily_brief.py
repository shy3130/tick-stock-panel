from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest


@pytest.fixture(autouse=True)
def _stable_published_source_for_advisor_unit_tests(monkeypatch):
    from app.api import advisor as advisor_api

    monkeypatch.setattr(
        advisor_api,
        "research_snapshot_source_problem",
        lambda _data_dir, _snapshot: None,
    )


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


def _published_snapshot() -> dict:
    return {
        "schema_version": 1,
        "snapshot_id": "a" * 64,
        "as_of": "2026-07-24",
        "published_at": "2026-07-24T08:10:00+00:00",
        "audits": _trusted_audits(),
        "strategy_cache": _cache(),
    }


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


def _market_overview(
    *,
    score: int = 60,
    label: str = "偏暖",
    as_of: str = "2026-07-24",
    total: int = 5_500,
) -> dict:
    return {
        "as_of": as_of,
        "breadth": {"total": total},
        "emotion": {"score": score, "label": label},
    }


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


def test_daily_brief_preserves_published_snapshot_provenance():
    from app.services.advisor import build_beginner_daily_brief

    recommendations = _recommendations()
    recommendations["snapshot_id"] = "a" * 64
    recommendations["snapshot_published_at"] = "2026-07-24T08:10:00+00:00"

    brief = build_beginner_daily_brief(recommendations)

    assert brief["snapshot_id"] == "a" * 64
    assert brief["snapshot_published_at"] == "2026-07-24T08:10:00+00:00"


def test_daily_brief_explains_when_market_data_is_newer_than_strategy_cache():
    from app.services.advisor import build_beginner_daily_brief

    audits = [
        _audit(
            dataset=dataset,
            observed_end="2026-07-29"
            if dataset in {"daily", "daily_enriched"}
            else ("2020-01-02" if dataset == "adj_factor" else "2026-07-29"),
        )
        for dataset in ("instruments", "daily", "adj_factor", "daily_enriched")
    ]
    cache = _cache()
    cache["as_of"] = "2026-07-28"
    for result in cache["results"].values():
        result["as_of"] = "2026-07-28"

    brief = build_beginner_daily_brief(
        _recommendations(audits=audits, cache=cache)
    )

    assert brief["action_state"] == "OBSERVE_ONLY"
    assert "策略结果仍为 2026-07-28" in brief["today_message"]
    assert "日K已更新至 2026-07-29" in brief["today_message"]
    assert brief["next_step"] == "请重新运行盘后刷新, 并等待策略重算校验通过。"


def test_daily_brief_observe_only_when_market_emotion_is_cold():
    from app.services.advisor import build_beginner_daily_brief

    recommendations = _recommendations(cache=_cache())
    recommendations["market_overview"] = _market_overview(score=38, label="偏冷")
    brief = build_beginner_daily_brief(recommendations)

    assert brief["data_gate"]["decision"] == "PASS"
    assert brief["market_gate"] == {
        "decision": "BLOCK",
        "as_of": "2026-07-24",
        "breadth_total": 5_500,
        "emotion_score": 38,
        "emotion_label": "偏冷",
        "reasons": ["市场情绪为偏冷(38分), 低于新手研究门槛45分"],
    }
    assert brief["action_state"] == "OBSERVE_ONLY"
    assert "偏冷" in brief["today_message"]
    assert "只观察" in brief["today_message"]
    assert "模拟成交" in brief["today_message"]


def test_daily_brief_fails_closed_when_market_overview_is_stale_or_empty():
    from app.services.advisor import build_beginner_daily_brief

    stale_recommendations = _recommendations(cache=_cache())
    stale_recommendations["market_overview"] = _market_overview(as_of="2026-07-23")
    stale = build_beginner_daily_brief(stale_recommendations)
    empty_recommendations = _recommendations(cache=_cache())
    empty_recommendations["market_overview"] = _market_overview(total=0)
    empty = build_beginner_daily_brief(empty_recommendations)

    assert stale["action_state"] == "OBSERVE_ONLY"
    assert stale["market_gate"]["decision"] == "BLOCK"
    assert any("日期" in reason for reason in stale["market_gate"]["reasons"])
    assert empty["action_state"] == "OBSERVE_ONLY"
    assert empty["market_gate"]["decision"] == "BLOCK"
    assert any("覆盖" in reason for reason in empty["market_gate"]["reasons"])


def test_daily_brief_no_candidate_when_no_go_candidate_survives_risk_gates():
    from app.services.advisor import build_beginner_daily_brief

    brief = build_beginner_daily_brief(_recommendations(cache=_cache(limit_up=True)))

    assert brief["action_state"] == "NO_CANDIDATE"
    assert brief["data_gate"]["decision"] == "PASS"
    assert brief["candidates"] == []
    assert brief["excluded_counts"]["hard_risk"] == 1


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
    assert candidate["symbol"] == "600000.SH"
    assert candidate["name"] == "Stock 0"
    assert candidate["research_decision"] == "GO"
    assert candidate["candidate_state"] == "GO1"
    assert candidate["go_streak"] == 1
    assert candidate["global_rank"] == 1
    assert candidate["lot_size"] == 100
    assert candidate["lot_cost"] == 1000.0
    assert candidate["deterministic_reasons"] == [
        "研究判断: 可进入研究清单",
        "策略共识: 2条独立策略给出了同向结果",
    ]
    assert candidate["observation_conditions"] == [
        "今天是第 1 个可信交易日确认",
        "下一可信交易日仍通过相同筛选才升级为可模拟练习",
    ]
    assert candidate["invalidation_conditions"] == [
        "任一必需数据回执异常或运行时校验失败",
        "下一可信交易日不再通过筛选时, 连续天数立即归零",
        "出现涨跌停、异常涨跌幅、ST或其他风险标记",
    ]
    assert candidate["risk_flags"] == []
    assert {
        "symbol": candidate["symbol"],
        "name": candidate["name"],
        "research_decision": candidate["research_decision"],
    } == {
        "symbol": "600000.SH",
        "name": "Stock 0",
        "research_decision": "GO",
    }
    forbidden = {"target_price", "price_target", "trade_instruction", "instruction"}
    assert not forbidden.intersection(candidate)


def test_daily_brief_distinguishes_first_go_day_from_ready_second_day():
    from app.services.advisor import build_beginner_daily_brief

    recommendations = _recommendations(cache=_cache())
    recommendations["trading_dates"] = ["2026-07-23", "2026-07-24"]
    recommendations["practice_capital"] = 10_000

    first_day = build_beginner_daily_brief(recommendations)
    assert first_day["action_state"] == "RESEARCH_ONLY"
    assert first_day["candidates"][0]["candidate_state"] == "GO1"
    assert first_day["candidates"][0]["go_streak"] == 1
    assert "第1天" in first_day["today_message"]

    recommendations["candidate_history"] = [
        {
            "as_of": "2026-07-23",
            "candidates": recommendations["candidates"],
        }
    ]
    second_day = build_beginner_daily_brief(recommendations)
    assert second_day["action_state"] == "SIMULATE_ONLY"
    assert second_day["candidates"][0]["candidate_state"] == "READY"
    assert second_day["candidates"][0]["go_streak"] == 2
    assert "连续2个交易日" in second_day["today_message"]


def test_published_candidate_history_keeps_verified_archive_after_live_revision(
    monkeypatch,
    tmp_path,
):
    """Later data corrections must not erase what an immutable snapshot recorded."""
    from app.api import advisor as advisor_api

    snapshot = _published_snapshot()
    monkeypatch.setattr(
        advisor_api,
        "load_research_snapshot_history",
        lambda *_args, **_kwargs: [snapshot],
    )
    monkeypatch.setattr(
        advisor_api,
        "research_snapshot_source_problem",
        lambda *_args, **_kwargs: {
            "code": "RESEARCH_SOURCE_DRIFT_AFTER_PUBLICATION",
            "reason": "历史分区后来发生修订",
            "next_action": "无需覆盖历史决策记录",
        },
    )
    monkeypatch.setattr(
        advisor_api,
        "_load_adjustment_event_symbols",
        lambda *_args, **_kwargs: (set(), None),
    )

    history = advisor_api._published_candidate_history(
        tmp_path,
        "2026-07-25",
    )

    assert len(history) == 1
    assert history[0]["as_of"] == "2026-07-24"
    assert history[0]["snapshot_id"] == "a" * 64
    assert history[0]["candidates"]


def test_daily_brief_api_filters_full_ranked_set_before_top_three(monkeypatch, tmp_path):
    from app.api import advisor as advisor_api

    current = _recommendations(cache=_cache(symbols=4))
    current["candidates"][0]["symbol"] = "300001.SZ"
    current["candidates"][1]["symbol"] = "688001.SH"
    current["candidates"][2]["symbol"] = "920001.BJ"
    hidden = current["candidates"][3]
    monkeypatch.setattr(
        advisor_api,
        "_persisted_recommendations",
        lambda _request, *, limit: {
            **current,
            "candidates": current["candidates"][:limit],
        },
    )
    monkeypatch.setattr(
        advisor_api,
        "_published_candidate_history",
        lambda _data_dir, _as_of: [
            {"as_of": "2026-07-23", "candidates": [hidden]}
        ],
    )
    monkeypatch.setattr(
        advisor_api,
        "_trading_dates_through",
        lambda _data_dir, _as_of: ["2026-07-23", "2026-07-24"],
    )
    monkeypatch.setattr(advisor_api, "_practice_capital", lambda _data_dir: 10_000)
    monkeypatch.setattr(
        advisor_api,
        "_market_overview_for_brief",
        lambda _request, _as_of: _market_overview(),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
            )
        )
    )

    brief = advisor_api._persisted_daily_brief(request)

    assert brief["action_state"] == "SIMULATE_ONLY"
    assert [candidate["symbol"] for candidate in brief["candidates"]] == [
        hidden["symbol"]
    ]
    assert brief["candidates"][0]["candidate_state"] == "READY"


def test_daily_brief_does_not_turn_no_candidate_into_simulated_buy_permission():
    from app.services.advisor import build_beginner_daily_brief

    brief = build_beginner_daily_brief(_recommendations(cache=_cache(limit_up=True)))

    assert brief["action_state"] == "NO_CANDIDATE"
    assert brief["candidates"] == []
    assert "没有" in brief["today_message"]


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
    monkeypatch.setattr(
        advisor_api,
        "load_latest_research_snapshot",
        lambda _data_dir: _published_snapshot(),
    )
    monkeypatch.setattr(
        advisor_api,
        "build_market_overview",
        lambda **kwargs: _market_overview(),
        raising=False,
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
            ),
        ),
    )

    brief = advisor_api.daily_brief(request)

    assert brief["action_state"] == "NO_CANDIDATE"
    assert brief["data_phase"] == {
        "phase": "EOD_SEALED",
        "as_of": "2026-07-24",
        "sealed_as_of": "2026-07-24",
        "daily_as_of": None,
        "enriched_as_of": None,
        "strategy_as_of": "2026-07-24",
        "market_phase": None,
        "last_quote_ms": None,
    }
    assert brief["data_gate"]["runtime_problems"] == []
    assert brief["candidates"] == []
    assert brief["excluded_counts"]["hard_risk"] == 1
    assert brief["method"] == {
        "kind": "deterministic",
        "policy_factors_included": False,
        "ai_can_change_score": False,
        "auto_trading": False,
    }


def test_daily_brief_blocks_snapshot_when_same_day_source_changed_after_publish(
    monkeypatch,
    tmp_path,
):
    from app.api import advisor as advisor_api

    factor_path = tmp_path / "adj_factor" / "all.parquet"
    factor_path.parent.mkdir(parents=True)
    pl.DataFrame(
        {"symbol": ["600000.SH"], "trade_date": [date(2026, 7, 23)]}
    ).write_parquet(factor_path)
    monkeypatch.setattr(
        advisor_api,
        "load_latest_research_snapshot",
        lambda _data_dir: _published_snapshot(),
    )
    monkeypatch.setattr(
        advisor_api,
        "research_snapshot_source_problem",
        lambda _data_dir, _snapshot: {
            "code": "RESEARCH_SOURCE_DRIFT_AFTER_PUBLICATION",
            "reason": "研究快照发布后, 同日行情源文件又发生变化, 当前候选已失效",
            "next_action": "请重新运行一次盘后刷新, 用最终行情重算策略并发布新快照。",
        },
    )
    monkeypatch.setattr(
        advisor_api,
        "build_market_overview",
        lambda **_kwargs: _market_overview(),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
            )
        )
    )

    brief = advisor_api.daily_brief(request)

    assert brief["action_state"] == "OBSERVE_ONLY"
    assert brief["data_gate"]["decision"] == "BLOCK"
    assert brief["data_gate"]["runtime_problems"][0]["code"] == (
        "RESEARCH_SOURCE_DRIFT_AFTER_PUBLICATION"
    )
    assert "发生变化" in brief["today_message"]


def test_daily_brief_endpoint_applies_the_same_cold_market_gate_as_dashboard(
    monkeypatch,
    tmp_path,
):
    from app.api import advisor as advisor_api

    factor_path = tmp_path / "adj_factor" / "all.parquet"
    factor_path.parent.mkdir(parents=True)
    pl.DataFrame(
        {"symbol": ["600000.SH"], "trade_date": [date(2026, 7, 23)], "ex_factor": [1.0]}
    ).write_parquet(factor_path)
    monkeypatch.setattr(advisor_api, "load_latest_audits", lambda data_dir: _trusted_audits())
    monkeypatch.setattr(advisor_api.strategy_cache, "read_cache", lambda data_dir: _cache())
    monkeypatch.setattr(
        advisor_api,
        "load_latest_research_snapshot",
        lambda _data_dir: _published_snapshot(),
    )
    monkeypatch.setattr(
        advisor_api,
        "build_market_overview",
        lambda **kwargs: _market_overview(score=38, label="偏冷"),
        raising=False,
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
            ),
        ),
    )

    brief = advisor_api.daily_brief(request)

    assert brief["action_state"] == "OBSERVE_ONLY"
    assert brief["market_gate"]["decision"] == "BLOCK"
    assert brief["market_gate"]["emotion_score"] == 38


def test_daily_brief_monitors_previous_sealed_plan_during_next_session(
    monkeypatch,
    tmp_path,
):
    """A trusted prior-day plan stays visible while today's rows remain provisional."""
    from app.api import advisor as advisor_api

    factor_path = tmp_path / "adj_factor" / "all.parquet"
    factor_path.parent.mkdir(parents=True)
    pl.DataFrame(
        {"symbol": ["600001.SH"], "trade_date": [date(2026, 7, 24)]}
    ).write_parquet(factor_path)

    persisted_cache = _cache()
    realtime_results = _cache()["results"]
    for result in realtime_results.values():
        result["as_of"] = "2026-07-27"

    monkeypatch.setattr(
        advisor_api.strategy_cache,
        "read_cache",
        lambda _data_dir: persisted_cache,
    )
    monkeypatch.setattr(
        advisor_api,
        "load_latest_research_snapshot",
        lambda _data_dir: _published_snapshot(),
    )
    monkeypatch.setattr(
        advisor_api,
        "build_market_overview",
        lambda **_kwargs: _market_overview(),
        raising=False,
    )
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        latest_daily_date=lambda: date(2026, 7, 27),
        latest_enriched_date=lambda _asset_type="stock": date(2026, 7, 27),
    )
    quote_service = SimpleNamespace(
        status=lambda: {
            "enabled": True,
            "running": True,
            "is_trading_hours": True,
            "market_phase": "morning",
            "last_fetch_ms": 1_774_580_400_000,
        }
    )
    monitor_engine = SimpleNamespace(
        latest_strategy_results=lambda: realtime_results,
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                repo=repo,
                quote_service=quote_service,
                monitor_engine=monitor_engine,
            ),
        ),
    )

    brief = advisor_api.daily_brief(request)

    assert brief["action_state"] == "OBSERVE_ONLY"
    assert brief["as_of"] == "2026-07-24"
    assert brief["data_phase"]["phase"] == "LIVE_PROVISIONAL"
    assert brief["data_phase"]["as_of"] == "2026-07-27"
    assert "正在监控 2026-07-24 盘后计划" in brief["today_message"]
    assert [candidate["symbol"] for candidate in brief["candidates"]] == ["600000.SH"]
    monitor = brief["candidates"][0]["plan_monitor"]
    assert monitor["status"] == "TRIGGERED"
    assert monitor["as_of"] == "2026-07-27"
    assert monitor["last_price"] == 10.0
    assert monitor["change_pct"] == 0.01
    assert set(monitor["strategy_ids"]) == {"trend_breakout", "bullish_alignment"}
    assert any("两条独立策略" in item for item in monitor["evidence"])

    for result in realtime_results.values():
        result["rows"][0]["status"] = "limit_up"
    risk_blocked = advisor_api.daily_brief(request)
    blocked_monitor = risk_blocked["candidates"][0]["plan_monitor"]
    assert blocked_monitor["status"] == "INVALIDATED"
    assert any("涨停" in item for item in blocked_monitor["evidence"])


def test_daily_brief_closes_untriggered_plan_after_live_session(
    monkeypatch,
    tmp_path,
):
    """An untouched prior-day plan stays pending intraday and expires after close."""
    from app.api import advisor as advisor_api

    factor_path = tmp_path / "adj_factor" / "all.parquet"
    factor_path.parent.mkdir(parents=True)
    pl.DataFrame(
        {"symbol": ["600001.SH"], "trade_date": [date(2026, 7, 24)]}
    ).write_parquet(factor_path)

    live_cache = _cache()
    live_cache["as_of"] = "2026-07-27"
    for result in live_cache["results"].values():
        result["as_of"] = "2026-07-27"
        result["rows"] = []

    monkeypatch.setattr(
        advisor_api.strategy_cache,
        "read_cache",
        lambda _data_dir: live_cache,
    )
    monkeypatch.setattr(
        advisor_api,
        "load_latest_research_snapshot",
        lambda _data_dir: _published_snapshot(),
    )
    monkeypatch.setattr(
        advisor_api,
        "build_market_overview",
        lambda **_kwargs: _market_overview(),
        raising=False,
    )
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        latest_daily_date=lambda: date(2026, 7, 27),
        latest_enriched_date=lambda _asset_type="stock": date(2026, 7, 27),
    )
    phase = {"value": "morning"}
    quote_service = SimpleNamespace(
        status=lambda: {
            "enabled": True,
            "running": True,
            "is_trading_hours": phase["value"] == "morning",
            "market_phase": phase["value"],
            "last_fetch_ms": 1_774_580_400_000,
        }
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(repo=repo, quote_service=quote_service),
        ),
    )

    intraday = advisor_api.daily_brief(request)
    assert intraday["candidates"][0]["plan_monitor"]["status"] == "PENDING"
    assert any(
        "尚未达到研究复核条件" in item
        for item in intraday["candidates"][0]["plan_monitor"]["evidence"]
    )

    phase["value"] = "close_final"
    after_close = advisor_api.daily_brief(request)
    assert after_close["data_phase"]["phase"] == "EOD_PENDING"
    assert after_close["candidates"][0]["plan_monitor"]["status"] == "INVALIDATED"
    assert any(
        "截至收盘" in item
        for item in after_close["candidates"][0]["plan_monitor"]["evidence"]
    )
    assert "新快照替换 2026-07-24 盘后计划" in after_close["today_message"]


@pytest.mark.parametrize(
    ("market_phase", "expected_phase", "message_fragment"),
    [
        ("morning", "LIVE_PROVISIONAL", "盘中数据已更新至 2026-08-03"),
        ("close_final", "EOD_PENDING", "盘后行情已更新至 2026-08-03"),
    ],
)
def test_daily_brief_identifies_unsealed_data_as_provisional_instead_of_stale(
    monkeypatch,
    tmp_path,
    market_phase,
    expected_phase,
    message_fragment,
):
    """Today's persisted intraday rows must not be described as an old-day sync failure."""
    from app.api import advisor as advisor_api

    factor_path = tmp_path / "adj_factor" / "all.parquet"
    factor_path.parent.mkdir(parents=True)
    pl.DataFrame(
        {"symbol": ["600000.SH"], "trade_date": [date(2026, 7, 31)]}
    ).write_parquet(factor_path)

    stale_audits = [
        _audit(
            dataset=dataset,
            observed_end="2026-07-31"
            if dataset in {"daily", "daily_enriched"}
            else ("2026-06-30" if dataset == "adj_factor" else "2026-08-03"),
        )
        for dataset in ("instruments", "daily", "adj_factor", "daily_enriched")
    ]
    live_cache = _cache()
    live_cache["as_of"] = "2026-08-03"
    for result in live_cache["results"].values():
        result["as_of"] = "2026-08-03"

    monkeypatch.setattr(advisor_api, "load_latest_audits", lambda _data_dir: stale_audits)
    monkeypatch.setattr(
        advisor_api.strategy_cache,
        "read_cache",
        lambda _data_dir: live_cache,
    )
    monkeypatch.setattr(
        advisor_api,
        "load_latest_research_snapshot",
        lambda _data_dir: None,
    )
    monkeypatch.setattr(
        advisor_api,
        "build_market_overview",
        lambda **_kwargs: _market_overview(as_of="2026-08-03"),
        raising=False,
    )
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        latest_daily_date=lambda: date(2026, 8, 3),
        latest_enriched_date=lambda _asset_type="stock": date(2026, 8, 3),
    )
    quote_service = SimpleNamespace(
        status=lambda: {
            "enabled": True,
            "running": True,
            "is_trading_hours": market_phase == "morning",
            "market_phase": market_phase,
            "last_fetch_ms": 1_785_724_200_000,
        }
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(repo=repo, quote_service=quote_service),
        ),
    )

    brief = advisor_api.daily_brief(request)

    assert brief["action_state"] == "OBSERVE_ONLY"
    assert brief["data_phase"] == {
        "phase": expected_phase,
        "as_of": "2026-08-03",
        "sealed_as_of": None,
        "daily_as_of": "2026-08-03",
        "enriched_as_of": "2026-08-03",
        "strategy_as_of": "2026-08-03",
        "market_phase": market_phase,
        "last_quote_ms": 1_785_724_200_000,
    }
    assert message_fragment in brief["today_message"]
    assert "尚未封存" in brief["today_message"].replace("盘后封存", "封存")
    assert "日K截止日 2026-07-31" not in brief["today_message"]
    assert "等待盘后刷新" in brief["next_step"]
    assert brief["candidates"] == []
    assert not any(
        "日K截止日 2026-07-31 与策略日期 2026-08-03 不一致" in reason
        for reason in brief["data_gate"]["reasons"]
    )
    phase_word = "盘中临时数据" if expected_phase == "LIVE_PROVISIONAL" else "盘后待封存数据"
    assert any(
        f"{phase_word}已更新至 2026-08-03" in reason
        for reason in brief["data_gate"]["reasons"]
    )
    assert brief["data_gate"]["datasets"]["daily"]["next_actions"] == [
        "等待盘后刷新完成并生成同日可信回执, 不需要重复手动同步。"
    ]


def _brief_human_strings(brief: dict) -> list[str]:
    strings = [brief["today_message"], brief["next_step"], brief["disclaimer"]]
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
        "NO_CANDIDATE",
        "RESEARCH_ONLY",
    ]
    assert [brief["data_gate"]["decision"] for brief in briefs] == ["BLOCK", "PASS", "PASS"]
    assert briefs[1]["candidates"] == []
    assert briefs[2]["candidates"][0]["research_decision"] == "GO"
    assert {brief["disclaimer"] for brief in briefs} == {
        "仅供个人研究与模拟练习; 进入研究清单不构成任何交易指令或收益承诺, 历史结果不代表未来表现。"
    }

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
