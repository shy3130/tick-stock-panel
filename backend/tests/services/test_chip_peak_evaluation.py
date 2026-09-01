from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import app.services.chip_peak_patterns.evaluation as chip_evaluation
from app.services.chip_peak_patterns.adapters import ChipReaderBundle
from app.services.chip_peak_patterns.evaluation import beta_stability, evaluate, research_arm
from app.services.chip_peak_patterns.models import (
    BETA_ARMS,
    BOOTSTRAP_ROUNDS,
    FACTOR_IDS,
    MAIN_ARM,
    MIN_OOS_EVENTS,
    MIN_OOS_SYMBOLS,
    ArmResearch,
    BetaArm,
    ChipBar,
    ChipCensorReason,
    ChipPeakRequest,
    ChipPeakResponse,
    ChipStatus,
    ChipVerdict,
    TurnoverDay,
    UnavailabilityReason,
)

DAYS = tuple(
    d
    for i in range((date(2025, 12, 31) - date(2024, 1, 2)).days + 1)
    if (d := date(2024, 1, 2) + timedelta(days=i)).weekday() < 5
)


def req(symbols=("000001.SZ", "600000.SH")):
    return ChipPeakRequest(
        symbols=list(symbols),
        start=date(2025, 1, 2),
        oos_start=date(2025, 7, 1),
        end=date(2025, 12, 31),
    )


def _paths(symbols):
    return {s: {d: 10.0 * 1.002**i for i, d in enumerate(DAYS)} for s in symbols}


class Bars:
    def __init__(self, paths, gaps=()):
        self.paths = {
            s: {d: p for d, p in v.items() if d not in set(gaps)} for s, v in paths.items()
        }

    def identity(self):
        return {"source": "synthetic-canonical"}

    def market_days(self, start, end):
        return tuple(d for d in DAYS if start <= d <= end)

    def load_bars(self, symbol, start, end):
        out = []
        previous = None
        for d in sorted(x for x in self.paths[symbol] if start <= x <= end):
            close = self.paths[symbol][d]
            opening = previous if previous is not None else close
            high = max(opening, close) * 1.005
            low = min(opening, close) * 0.995
            out.append(
                ChipBar(
                    symbol=symbol,
                    date=d,
                    open=opening,
                    high=high,
                    low=low,
                    close=close,
                    raw_open=opening,
                    raw_high=high,
                    raw_low=low,
                    raw_close=close,
                    volume=1_000_000.0,
                    amount=close * 1_000_000.0,
                )
            )
            previous = close
        return tuple(out)


class Turnover:
    def __init__(self, missing=(), stale=()):
        self.missing = set(missing)
        self.stale = set(stale)

    def identity(self):
        return {"source": "published_daily_markets_hslv_or_lagged_ltgb"}

    def turnover(self, symbol, day):
        if day in self.missing:
            return None
        return TurnoverDay(
            available_at=day + timedelta(days=1) if day in self.stale else day,
            reported_turnover_pct=1.0,
            source_day=day,
        )


class Facts:
    def __init__(self, limit=None):
        self.limit = limit

    def identity(self):
        return {"source": "synthetic-market-facts"}

    def row(self, symbol, day):
        return None if self.limit is None else SimpleNamespace(published_limit_up=self.limit)


class Presence:
    def identity(self):
        return {"source": "synthetic-universe"}

    def membership(self, symbol, day):
        return "member"


def bundle(symbols=("000001.SZ", "600000.SH"), gaps=(), missing=(), stale=(), limit=None):
    return ChipReaderBundle(
        bars=Bars(_paths(symbols), gaps),
        turnover=Turnover(missing, stale),
        market_facts=Facts(limit),
        presence=Presence(),
    )


def test_request_is_strict_and_frozen_defaults():
    assert req(("000001.SZ",)).cost_bps == 10
    with pytest.raises(ValidationError):
        ChipPeakRequest(**{**req(("000001.SZ",)).model_dump(), "extra": 1})


def test_beta_flip_fails_closed():
    audit = beta_stability(
        {
            "turnover": "positive",
            "turnover_x0.5": "negative",
            "turnover_x2": "negative",
            "geometric_0.01": "negative",
        }
    )
    assert not audit.stable and audit.consensus == "negative"


def test_research_arm_uses_frozen_gate_and_direction():
    q = {f"{i:06d}.SZ": [0.20, 0.21, 0.19] for i in range(10)}
    c = {f"{i:06d}.SZ": [0.01, 0.02, 0.00] for i in range(10)}
    result = research_arm(BetaArm.TURNOVER, q, c)
    assert result.qualified_events >= MIN_OOS_EVENTS and result.qualified_symbols >= MIN_OOS_SYMBOLS
    assert (
        result.oos_gate_passed
        and result.direction == "positive"
        and result.verdict is ChipVerdict.ACCEPTED
    )
    under = research_arm(
        BetaArm.TURNOVER, {k: v[:2] for k, v in q.items()}, {k: v[:2] for k, v in c.items()}
    )
    assert not under.oos_gate_passed and under.verdict is ChipVerdict.UNAVAILABLE


def test_unavailable_response_shape_is_strict():
    response = ChipPeakResponse(
        status=ChipStatus.UNAVAILABLE, unavailable_reason=UnavailabilityReason.TURNOVER_PROVENANCE
    )
    assert response.status.value == "unavailable"
    with pytest.raises(ValidationError):
        ChipPeakResponse(status=ChipStatus.UNAVAILABLE)


def test_complete_injected_reader_orchestrates_ok():
    response = evaluate(req(), readers=bundle())
    assert response.status is ChipStatus.OK
    assert tuple(f.factor_id for f in response.factors) == FACTOR_IDS
    assert response.arms_evaluated == BETA_ARMS
    assert (
        response.provenance is not None
        and response.provenance.identities.turnover.source
        == "published_daily_markets_hslv_or_lagged_ltgb"
    )
    assert all(a.status == "ok" for a in response.symbol_audit)
    assert any(f.diagnostics["parent_events"] > 0 for f in response.factors)
    assert any(f.censored for f in response.factors)
    assert response.factors[-1].phase2_pending and response.factors[-1].phase2_note
    assert response.model_dump_json() == evaluate(req(), readers=bundle()).model_dump_json()


def test_missing_pit_is_unavailable_when_all_symbols_fail():
    response = evaluate(
        req(("000001.SZ",)), readers=bundle(("000001.SZ",), missing={date(2025, 2, 3)})
    )
    assert (
        response.status is ChipStatus.UNAVAILABLE
        and response.unavailable_reason is UnavailabilityReason.TURNOVER_PROVENANCE
    )
    assert response.factors == [] and response.symbol_audit[0].status == "missing_pit_turnover"


def test_partial_missing_pit_keeps_other_symbols_analyzable():
    response = evaluate(req(), readers=bundle(gaps=(), missing={date(2025, 2, 3)}))
    assert response.status is ChipStatus.UNAVAILABLE or any(
        a.status == "missing_pit_turnover" for a in response.symbol_audit
    )


def test_t_plus_one_unreachable_is_censored():
    response = evaluate(
        req(("000001.SZ",)), readers=bundle(("000001.SZ",), gaps={date(2025, 9, 10)})
    )
    assert response.status is ChipStatus.OK
    assert any(
        c.reason is ChipCensorReason.ENTRY_UNREACHABLE for f in response.factors for c in f.censored
    )


def test_limit_up_entry_is_unreachable():
    response = evaluate(req(("000001.SZ",)), readers=bundle(("000001.SZ",), limit=1.0))
    assert response.status is ChipStatus.OK
    assert any(
        c.reason is ChipCensorReason.ENTRY_UNREACHABLE for f in response.factors for c in f.censored
    )


def test_beta_flip_makes_complete_reader_unavailable(monkeypatch):
    def fake(arm, qualified, control):
        direction = "positive" if arm == MAIN_ARM else "negative"
        return ArmResearch(
            arm=BetaArm(arm),
            qualified_events=40,
            qualified_symbols=10,
            control_events=40,
            control_symbols=10,
            oos_gate_passed=True,
            bootstrap={"valid_replicates": BOOTSTRAP_ROUNDS, "lower": 0.01, "upper": 0.02},
            direction=direction,
            verdict=ChipVerdict.ACCEPTED if direction == "positive" else ChipVerdict.REJECTED,
        )

    monkeypatch.setattr(chip_evaluation, "research_arm", fake)
    response = evaluate(req(), readers=bundle())
    assert (
        response.status is ChipStatus.UNAVAILABLE
        and response.unavailable_reason is UnavailabilityReason.BETA_INSTABILITY
        and response.factors == []
    )


def test_consistent_beta_arms_keep_complete_reader_ok(monkeypatch):
    def fake(arm, qualified, control):
        return ArmResearch(
            arm=BetaArm(arm),
            qualified_events=40,
            qualified_symbols=10,
            control_events=40,
            control_symbols=10,
            oos_gate_passed=True,
            bootstrap={"valid_replicates": BOOTSTRAP_ROUNDS, "lower": 0.01, "upper": 0.02},
            direction="positive",
            verdict=ChipVerdict.ACCEPTED,
        )

    monkeypatch.setattr(chip_evaluation, "research_arm", fake)
    response = evaluate(req(), readers=bundle())
    assert response.status is ChipStatus.OK and all(
        f.verdict is ChipVerdict.ACCEPTED and f.beta_stability.stable for f in response.factors
    )


def test_evaluate_without_readers_fails_closed():
    response = evaluate(req(("000001.SZ",)), readers=None)
    assert (
        response.status is ChipStatus.UNAVAILABLE
        and response.unavailable_reason is UnavailabilityReason.CANONICAL_READER
    )


@pytest.mark.parametrize(
    ("membership", "reason"),
    [
        ("not_in_pool", ChipCensorReason.PIT_UNIVERSE_INELIGIBLE),
        ("coverage_missing", ChipCensorReason.UNIVERSE_COVERAGE_MISSING),
    ],
)
def test_event_day_presence_is_fail_closed(membership, reason):
    symbol = "000001.SZ"
    readers = bundle((symbol,))
    bars = readers.bars.load_bars(symbol, DAYS[0], DAYS[-1])
    index = 260
    event = chip_evaluation.ChipEvent(
        "c1_low_single_peak",
        symbol,
        bars[index].date,
        "qualified",
        index,
    )
    next_day = {day: DAYS[i + 1] for i, day in enumerate(DAYS[:-1])}

    class EventPresence:
        def membership(self, _symbol, _day):
            return membership

    outcomes, censored = chip_evaluation._outcomes(
        event.factor_id,
        symbol,
        bars,
        (event,),
        next_day=next_day,
        facts=Facts(),
        presence=EventPresence(),
        cost=0.0,
    )

    assert outcomes == []
    assert [item.reason for item in censored] == [reason]


def test_production_turnover_normalizes_available_at_to_date():
    from app.services.chip_peak_patterns.production import PinnedChipTurnover

    class FactReader:
        def generation(self):
            return "markets-g1"

        def manifest_sha256(self):
            return "a" * 64

        def daily_turnover_fact(self, _symbol, day):
            return SimpleNamespace(
                available_at=datetime.combine(day, datetime.min.time()),
                reported_turnover_pct=0.47,
                source_day=day,
                availability_basis="daily_market_close",
            )

    adapter = PinnedChipTurnover(FactReader())
    turnover = adapter.turnover("000001.SZ", date(2025, 1, 2))
    assert turnover.available_at == date(2025, 1, 2)
    assert turnover.reported_turnover_pct == 0.47
    assert turnover.source_day == date(2025, 1, 2)
    assert adapter.identity()["source"] == "published_daily_markets_hslv_or_lagged_ltgb"


def test_production_turnover_falls_back_to_prior_close_float_shares():
    from app.services.chip_peak_patterns.production import PinnedChipTurnover

    day = date(2025, 1, 2)
    source_day = date(2024, 12, 31)

    class FactReader:
        def generation(self):
            return "markets-g1"

        def manifest_sha256(self):
            return "a" * 64

        def daily_turnover_fact(self, _symbol, _day):
            return None

        def intraday_float_shares_fact(self, _symbol, _day):
            return SimpleNamespace(
                available_at=datetime.combine(source_day, datetime.min.time()),
                float_shares=100_000_000.0,
                source_day=source_day,
                availability_basis="previous_daily_market_close",
            )

    turnover = PinnedChipTurnover(FactReader()).turnover("000001.SZ", day)
    assert turnover.reported_turnover_pct is None
    assert turnover.float_shares == 100_000_000.0
    assert turnover.source_day == source_day
    assert turnover.availability_basis == "previous_daily_market_close"


def test_horizon_returns_use_adjusted_prices_across_corporate_action():
    symbol = "000001.SZ"
    days = [date(2025, 1, 2) + timedelta(days=index) for index in range(62)]
    bars = tuple(
        ChipBar(
            symbol=symbol,
            date=day,
            open=10.0,
            high=10.1,
            low=9.9,
            close=10.0,
            raw_open=10.0 if index < 30 else 5.0,
            raw_high=10.1 if index < 30 else 5.05,
            raw_low=9.9 if index < 30 else 4.95,
            raw_close=10.0 if index < 30 else 5.0,
            volume=1_000_000.0,
            amount=10_000_000.0,
        )
        for index, day in enumerate(days)
    )
    event = chip_evaluation.ChipEvent(
        "c1_low_single_peak",
        symbol,
        days[0],
        "qualified",
        0,
    )

    outcomes, censored = chip_evaluation._outcomes(
        event.factor_id,
        symbol,
        bars,
        (event,),
        next_day={days[0]: days[1]},
        facts=Facts(),
        presence=Presence(),
        cost=0.0,
    )

    assert not censored
    assert len(outcomes) == 1
    assert outcomes[0].returns == {20: pytest.approx(0.0), 60: pytest.approx(0.0)}
