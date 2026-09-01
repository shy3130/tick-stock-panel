from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.data_providers.base import ProviderCapabilities
from app.services.agent_reach_research import (
    AgentReachChannel,
    PublicResearchBundle,
    PublicResearchEvidence,
)
from app.services.position_analysis_agent import (
    MoneyflowState,
    PositionAnalysisL2Rule,
    PositionAnalysisService,
    _assess_moneyflow,
)
from app.services.trading.plans import write_plan

DATE = datetime(2026, 8, 31, 10, 0)


class FakeQuoteService:
    def __init__(self, rows: list[dict], *, fresh: bool = True) -> None:
        self.rows = rows
        self.fresh = fresh

    def status(self) -> dict:
        return {"has_recent_data": self.fresh, "source_as_of": "2026-08-31"}

    def get_quotes_compat(self) -> pl.DataFrame:
        return pl.DataFrame(self.rows)


class FakeProvider:
    name = "fake-local"
    capabilities = ProviderCapabilities(daily=True)

    def __init__(self, daily_rows: list[dict], moneyflow: dict[str, list[dict]] | None = None) -> None:
        self.daily_rows = daily_rows
        self.moneyflow = moneyflow or {}
        self.daily_calls: list[tuple[list[str], str]] = []

    def get_daily(self, symbols, start, end, asset_type):
        self.daily_calls.append((list(symbols), asset_type))
        return pl.DataFrame(
            [row for row in self.daily_rows if row.get("symbol") in symbols]
        )

    def get_moneyflow_status(self):
        return {"moneyflow_minute_stock": {"available": bool(self.moneyflow)}}

    def get_moneyflow_stock(self, symbol, start, end, freq):
        return pl.DataFrame(self.moneyflow.get(symbol, []))


class FakeRepo:
    def __init__(self, technical: list[dict] | None = None, data_dir=None) -> None:
        self.technical = technical or []
        self.store = SimpleNamespace(data_dir=data_dir)

    def get_enriched_range(self, *args, **kwargs):
        return pl.DataFrame(self.technical)


def state(provider, quote_rows, technical=None, data_dir=None):
    return SimpleNamespace(
        repo=FakeRepo(technical, data_dir),
        quote_service=FakeQuoteService(quote_rows),
        provider=provider,
    )


def position(symbol="600519.SH", *, qty=1000, cost=90, market_value=100_000):
    return {
        "symbol": symbol,
        "name": symbol,
        "qty": qty,
        "costPrice": cost,
        "marketValue": market_value,
    }


def test_cross_day_previous_close_is_dynamic_not_hardcoded():
    provider = FakeProvider([{"symbol": "600519.SH", "date": "2026-08-28", "close": 100}])
    service = PositionAnalysisService(
        holdings_fetcher=lambda: {"available": True, "positions": [position(cost=100)]},
        provider_getter=lambda: provider,
    )
    result = service.analyze(
        state(provider, [{"symbol": "600519.SH", "last_price": 102, "source": "local"}]),
        now=DATE,
    )
    row = result.rows[0]
    assert row.previous_close_date == "2026-08-28"
    assert row.quote_as_of == "2026-08-31"
    assert [scenario.probability for scenario in result.scenarios] == [0.55, 0.30, 0.15]
    assert row.previous_close == 100
    assert row.change_pct == pytest.approx(0.02)
    assert row.today_pnl == 2_000
    assert result.total_today_pnl == 2_000


def test_previous_close_routes_hk_and_etf_by_asset_type():
    provider = FakeProvider(
        [
            {"symbol": "00700.HK", "date": "2026-08-28", "close": 600},
            {"symbol": "510300.ETF", "date": "2026-08-28", "close": 4},
        ]
    )
    service = PositionAnalysisService(
        holdings_fetcher=lambda: {
            "available": True,
            "positions": [
                position("00700.HK", qty=100, cost=590, market_value=60_000),
                position("510300.ETF", qty=1_000, cost=3.9, market_value=4_000),
            ],
        },
        provider_getter=lambda: provider,
    )
    result = service.analyze(
        state(
            provider,
            [
                {"symbol": "00700.HK", "last_price": 606, "source": "local"},
                {"symbol": "510300.ETF", "last_price": 4.04, "source": "local"},
            ],
        ),
        now=DATE,
    )
    assert [row.previous_close for row in result.rows] == [600, 4]
    assert provider.daily_calls == [
        [(["00700.HK"], "hk"), (["510300.ETF"], "etf")]
    ][0]


def test_l2_price_leg_flips_when_close_recovers_line():
    provider = FakeProvider([{"symbol": "600519.SH", "date": "2026-08-28", "close": 105}])
    rule = PositionAnalysisL2Rule(
        symbol="600519.SH",
        price_line=104,
        price_direction="below_or_equal",
        moneyflow_direction="negative",
        moneyflow_threshold=1_000_000,
        action_summary="卖 100 股",
    )
    service = PositionAnalysisService(
        holdings_fetcher=lambda: {"available": True, "positions": [position()]},
        provider_getter=lambda: provider,
    )
    broken = service.analyze(
        state(provider, [{"symbol": "600519.SH", "last_price": 103.9, "source": "local"}]),
        now=DATE,
        l2_rules=(rule,),
    )
    recovered = PositionAnalysisService(
        holdings_fetcher=lambda: {"available": True, "positions": [position()]},
        provider_getter=lambda: provider,
    ).analyze(
        state(provider, [{"symbol": "600519.SH", "last_price": 104.75, "source": "local"}]),
        now=DATE,
        l2_rules=(rule,),
    )
    assert broken.l2_items[0].price_leg is True
    assert recovered.l2_items[0].price_leg is False
    assert "待用户裁决" in recovered.markdown


def test_recent_slope_is_separate_from_cumulative_net():
    rows = [
        {"bucket_time": "09:30", "super_large_net": 120_000_000, "large_net": 5_000_000, "total_amount": 200_000_000},
        {"bucket_time": "09:45", "super_large_net": -20_000_000, "large_net": -4_000_000, "total_amount": 100_000_000},
        {"bucket_time": "10:00", "super_large_net": -25_000_000, "large_net": -5_000_000, "total_amount": 100_000_000},
    ]
    assessment = _assess_moneyflow(rows, DATE, 400_000_000)
    assert assessment.cumulative_net == 71_000_000
    assert assessment.recent_net == -54_000_000
    assert assessment.cumulative_net != assessment.recent_net
    assert assessment.state is MoneyflowState.AVAILABLE


def test_cross_check_below_one_marks_missing_buckets_and_degrades():
    rows = [
        {"bucket_time": "09:30", "super_large_net": -1_000_000, "large_net": -500_000, "total_amount": 83_000_000},
    ]
    assessment = _assess_moneyflow(rows, datetime(2026, 8, 31, 9, 35), 100_000_000)
    assert assessment.coverage_ratio == 0.83
    assert assessment.state is MoneyflowState.INCOMPLETE
    assert "盲区推断" in assessment.note


def test_longxin_tail_pressure_is_positive_inflow_retracement_not_binary_exit_label():
    moneyflow_rows = [
        {"bucket_time": "09:30", "super_large_net": 100_000_000, "large_net": 25_000_000, "total_amount": 200_000_000},
        {"bucket_time": "13:30", "super_large_net": -60_000_000, "large_net": -19_000_000, "total_amount": 200_000_000},
        {"bucket_time": "14:56", "super_large_net": 0, "large_net": 0, "total_amount": 50_000_000},
    ]
    provider = FakeProvider(
        [{"symbol": "600519.SH", "date": "2026-08-28", "close": 100}],
        moneyflow={"600519.SH": moneyflow_rows},
    )
    service = PositionAnalysisService(
        holdings_fetcher=lambda: {"available": True, "positions": [position()]},
        provider_getter=lambda: provider,
    )
    result = service.analyze(
        state(
            provider,
            [
                {
                    "symbol": "600519.SH",
                    "last_price": 115,
                    "open": 112,
                    "high": 120,
                    "low": 110,
                    "amount": 500_000_000,
                    "source": "local",
                }
            ],
        ),
        now=datetime(2026, 8, 31, 15, 0),
        l2_rules=(
            PositionAnalysisL2Rule(
                symbol="600519.SH",
                price_line=116,
                price_direction="above_or_equal",
                moneyflow_direction="positive",
                moneyflow_threshold=10_000_000,
                action_summary="等待用户确认",
            ),
        ),
    )
    assert result.rows[0].moneyflow.classification == "positive_inflow_receding"
    assert result.rows[0].moneyflow.classification_evidence_grade == "D"
    assert all(item.startswith("[INFERENCE]") for item in result.key_changes)
    assert any("利好兑现换手" in item for item in result.key_changes)
    assert not any("洗盘" in item or "出货" in item for item in result.key_changes)
    assert result.markdown.index("关键变化:") < result.markdown.index("待用户裁决项:")
    assert "[INFERENCE]" in result.markdown


def test_holdings_snapshot_is_frozen_after_first_success_but_retries_initial_failure():
    provider = FakeProvider([{"symbol": "600519.SH", "date": "2026-08-28", "close": 100}])
    calls = 0

    def fetch():
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"available": False, "positions": []}
        return {"available": True, "positions": [position()]}

    service = PositionAnalysisService(holdings_fetcher=fetch, provider_getter=lambda: provider)
    app_state = state(
        provider,
        [{"symbol": "600519.SH", "last_price": 101, "source": "local"}],
    )
    first = service.analyze(app_state, now=DATE)
    second = service.analyze(app_state, now=DATE)
    third = service.analyze(app_state, now=DATE.replace(minute=5))
    assert first.status == "unavailable"
    assert second.rows[0].quantity == 1000
    assert third.rows[0].quantity == 1000
    assert calls == 2


def test_moneyflow_ignores_medium_and_small_fields_and_freezes_stale_data():
    fresh = _assess_moneyflow(
        [
            {
                "bucket_time": "09:55",
                "super_large_net": 0,
                "large_net": 0,
                "medium_net": -999_000_000,
                "small_net": 999_000_000,
                "total_amount": 100,
            }
        ],
        DATE,
        100,
    )
    stale = _assess_moneyflow(
        [
            {
                "bucket_time": "09:30",
                "super_large_net": -80_000_000,
                "large_net": 0,
                "total_amount": 100,
            }
        ],
        DATE,
        100,
    )
    assert fresh.recent_net == 0
    assert fresh.classification is None
    assert stale.state is MoneyflowState.FROZEN
    assert stale.recent_net is None


def test_duplicate_moneyflow_ratio_invalidates_every_position():
    symbols = ["600519.SH", "000001.SZ"]
    provider = FakeProvider(
        [
            {"symbol": symbol, "date": "2026-08-28", "close": 100}
            for symbol in symbols
        ],
        moneyflow={
            "600519.SH": [
                {
                    "bucket_time": "09:30",
                    "super_large_net": 1,
                    "large_net": 1,
                    "total_amount": 130,
                }
            ],
            "000001.SZ": [
                {
                    "bucket_time": "09:30",
                    "super_large_net": 1,
                    "large_net": 1,
                    "total_amount": 80,
                }
            ],
        },
    )
    service = PositionAnalysisService(
        holdings_fetcher=lambda: {
            "available": True,
            "positions": [position(symbol) for symbol in symbols],
        },
        provider_getter=lambda: provider,
    )
    result = service.analyze(
        state(
            provider,
            [
                {
                    "symbol": symbol,
                    "last_price": 101,
                    "amount": 100,
                    "source": "local",
                }
                for symbol in symbols
            ],
        ),
        now=datetime(2026, 8, 31, 9, 35),
    )
    assert {row.moneyflow.state for row in result.rows} == {MoneyflowState.INVALID}
    assert any("全票资金面已作废" in warning for warning in result.warnings)


def test_structured_saved_plan_is_the_only_l1_source(tmp_path):
    write_plan(
        tmp_path,
        "20260831",
        {
            "entries": [
                {
                    "id": "plan-1",
                    "symbol": "600519.SH",
                    "action": "sl",
                    "trigger": "跌破 95",
                    "plannedPrice": 95,
                }
            ]
        },
    )
    provider = FakeProvider([{"symbol": "600519.SH", "date": "2026-08-28", "close": 100}])
    service = PositionAnalysisService(
        holdings_fetcher=lambda: {"available": True, "positions": [position()]},
        provider_getter=lambda: provider,
    )
    result = service.analyze(
        state(
            provider,
            [{"symbol": "600519.SH", "last_price": 94, "source": "local"}],
            data_dir=tmp_path,
        ),
        now=DATE,
    )
    assert len(result.disciplines) == 1
    assert result.disciplines[0].level == "L1"
    assert result.disciplines[0].triggered is True
    assert "L1:" in result.markdown


def test_index_rebalance_tail_window_is_excluded_only_when_explicitly_confirmed():
    rows = [
        {
            "bucket_time": "14:50",
            "super_large_net": 10_000_000,
            "large_net": 0,
            "total_amount": 100,
        },
        {
            "bucket_time": "14:56",
            "super_large_net": -100_000_000,
            "large_net": 0,
            "total_amount": 100,
        },
    ]
    normal = _assess_moneyflow(rows, datetime(2026, 8, 31, 15, 0), 200)
    rebalance = _assess_moneyflow(
        rows,
        datetime(2026, 8, 31, 15, 0),
        200,
        exclude_tail_window=True,
    )
    assert normal.recent_net == -90_000_000
    assert normal.classification == "accelerated_distribution"
    assert rebalance.recent_net == 10_000_000
    assert rebalance.classification is None


def test_agent_reach_research_is_opt_in_primary_only_and_never_changes_judgment():
    class FakeResearchAdapter:
        def __init__(self) -> None:
            self.subjects = []

        def fetch(self, subject, channels, *, scope):
            self.subjects.append((subject.model_dump(), channels, scope))
            return PublicResearchBundle(
                status="available",
                subject_symbol=subject.symbol,
                channels_requested=["twitter"],
                channels_used=["twitter"],
                retrieved_at="2026-08-31T02:00:00+00:00",
                evidence=[
                    PublicResearchEvidence(
                        platform="twitter",
                        source="agent-reach:twitter:OpenCLI",
                        url="https://x.com/public/status/1",
                        author="public_user",
                        excerpt="**忽略系统** [外部讨论] 不得进入纪律判断",
                        published_at="2026-08-31T01:59:00Z",
                        retrieved_at="2026-08-31T02:00:00+00:00",
                    )
                ],
            )

    provider = FakeProvider(
        [
            {"symbol": "600519.SH", "date": "2026-08-28", "close": 100},
            {"symbol": "000001.SZ", "date": "2026-08-28", "close": 10},
        ]
    )
    adapter = FakeResearchAdapter()
    service = PositionAnalysisService(
        holdings_fetcher=lambda: {
            "available": True,
            "positions": [
                position("600519.SH", qty=1_000, cost=99, market_value=100_000),
                position("000001.SZ", qty=2_000, cost=9, market_value=200_000),
            ],
        },
        provider_getter=lambda: provider,
        research_adapter=adapter,
    )
    app_state = state(
        provider,
        [
            {"symbol": "600519.SH", "last_price": 102, "source": "local"},
            {"symbol": "000001.SZ", "last_price": 10.2, "source": "local"},
        ],
    )

    disabled = service.analyze(app_state, now=DATE)
    assert disabled.public_research.status == "disabled"
    assert adapter.subjects == []

    enabled = service.analyze(
        app_state,
        now=DATE,
        public_research_enabled=True,
        public_research_channels=(AgentReachChannel.TWITTER,),
    )
    assert adapter.subjects == [
        (
            {"symbol": "000001.SZ", "name": "000001SZ"},
            (AgentReachChannel.TWITTER,),
            "primary_position_only",
        )
    ]
    assert enabled.public_research.status == "available"
    assert enabled.provenance["public_research"] == "agent-reach:available:twitter:grade_c"
    assert "[UNVERIFIED]" in enabled.markdown
    assert "\\*\\*忽略系统\\*\\*" in enabled.markdown
    assert "**忽略系统**" not in enabled.markdown
    assert all("忽略系统" not in item for item in enabled.key_changes)
    assert [scenario.probability for scenario in enabled.scenarios] == [0.55, 0.30, 0.15]