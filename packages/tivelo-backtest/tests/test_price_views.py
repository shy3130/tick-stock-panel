from datetime import UTC, datetime
from decimal import Decimal, Inexact, localcontext
from types import SimpleNamespace

import pytest
from tivelo_contracts.data import AdjustmentFactor, Bar, BarQuery
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import PriceValue, PriceViewSpec
from tivelo_contracts.protocols import PriceViewProvider

from tivelo_backtest.legacy.snapshot_daily import read_legacy_daily_panel
from tivelo_backtest.price_views import StaticPriceView


def at(day, hour=7):
    return datetime(2025, 1, day, hour, tzinfo=UTC)


def bar(day=2, price="10", **changes):
    values = dict(
        instrument_id="security-1",
        timeframe="1d",
        bar_start=at(day, 1),
        bar_end=at(day),
        trading_date=at(day).date(),
        session_id=f"cn:{day}",
        open=price,
        high=price,
        low=price,
        close=price,
        vwap=price,
        volume="100",
        quantity_unit="share",
        quote_currency="CNY",
        turnover="1000",
        price_basis="raw",
        available_at=at(day),
        ingested_at=at(day),
        source_id="synthetic",
        revision_id="1",
    )
    values.update(changes)
    return Bar(**values)


def factor(day=1, value="2", **changes):
    values = dict(
        factor_id=f"factor-{day}",
        instrument_id="security-1",
        effective_at=at(day, 0),
        available_at=at(day, 0),
        value=value,
        definition_id="cumulative-v1",
        factor_kind="cumulative_forward",
        baseline_id="listing",
        source_id="synthetic",
        revision_id="1",
    )
    values.update(changes)
    return AdjustmentFactor(**values)


def spec(mode, anchor=1):
    return PriceViewSpec(
        provider_id=f"tivelo.{mode}",
        mode=mode,
        **(
            {}
            if mode == "raw"
            else dict(
                anchor_at=at(anchor, 0),
                factor_definition_id="cumulative-v1",
                factor_baseline_id="listing",
            )
        ),
    )


@pytest.mark.parametrize(
    "mode,anchor,expected",
    [
        ("raw", 1, ("10", "5")),
        ("qfq", 3, ("5", "5")),
        ("hfq", 1, ("10", "10")),
    ],
)
def test_free_views_hand_calculated_split_prices_and_unchanged_quantities(mode, anchor, expected):
    provider = StaticPriceView(mode)
    assert isinstance(provider, PriceViewProvider)
    original = (bar(), bar(6, "5"))
    result = provider.project(
        original, (factor(), factor(3, "4")), spec(mode, anchor), decision_at=at(6)
    )
    assert tuple(record.close for record in result) == tuple(map(Decimal, expected))
    assert all(record.price_basis == mode for record in result)
    assert tuple(record.volume for record in result) == (Decimal(100), Decimal(100))
    assert tuple(record.turnover for record in result) == (Decimal(1000), Decimal(1000))
    assert all(record.vwap == record.close for record in result)
    numeric = tuple(
        provider.iter_prices(
            original, (factor(), factor(3, "4")), spec(mode, anchor), decision_at=at(6)
        )
    )
    assert tuple(
        tuple(row[key] for key in ("open", "high", "low", "close")) for row in numeric
    ) == tuple((row.open, row.high, row.low, row.close) for row in result)
    assert original[0].close == Decimal(10) and original[0].price_basis == "raw"
    price = provider.price(result[-1].close, instrument_id="security-1", decision_at=at(6))
    assert provider.to_raw(price, decision_at=at(6)) == Decimal(5)


@pytest.mark.parametrize(
    "mode,anchor,expected",
    [("raw", 1, [10.0, 5.0]), ("qfq", 3, [5.0, 5.0]), ("hfq", 1, [10.0, 10.0])],
)
def test_legacy_daily_panel_preserves_source_dates_raw_prices_and_lot_units(mode, anchor, expected):
    previous = bar(2, volume="1250")
    following = bar(6, "5", volume="12500", turnover=None)
    reader = SimpleNamespace(
        bars=lambda _: (following, previous),
        factors=lambda _: (factor(), factor(3, "4")),
    )
    request = BarQuery(
        instrument_ids=("security-1",),
        timeframe="1d",
        start=at(1),
        end=at(7),
        as_of=at(7),
    )
    panel = read_legacy_daily_panel(
        reader, request, symbols={"security-1": "600000.SH"}, price_view=spec(mode, anchor)
    )
    assert panel["date"].to_list() == [previous.trading_date, following.trading_date]
    assert panel["close"].to_list() == expected
    assert panel["raw_close"].to_list() == [10.0, 5.0]
    assert panel["volume"].to_list() == [12.5, 125.0]
    assert panel["amount"].to_list() == [1000.0, None]

    reader.bars = lambda _: (previous, previous)
    with pytest.raises(TiveloError) as error:
        read_legacy_daily_panel(reader, request)
    assert error.value.code == ErrorCode.DATA_CONFLICT


def test_b05_future_anchor_rejected_without_trusting_strategy_claims():
    with pytest.raises(TiveloError) as error:
        StaticPriceView("qfq").project(
            (bar(),), (factor(), factor(3, "4")), spec("qfq", 3), decision_at=at(2)
        )
    assert error.value.code == ErrorCode.PRICE_BASIS_UNPROVEN
    with pytest.raises(TiveloError) as numeric:
        tuple(
            StaticPriceView("qfq").iter_prices(
                (bar(),), (factor(), factor(3, "4")), spec("qfq", 3), decision_at=at(2)
            )
        )
    assert numeric.value.code == ErrorCode.PRICE_BASIS_UNPROVEN


def test_b06_fixed_hfq_baseline_does_not_follow_warmup_start():
    provider = StaticPriceView("hfq")
    factors = (factor(), factor(3, "4"))
    full = provider.project((bar(), bar(6, "5")), factors, spec("hfq"), decision_at=at(6))
    short = provider.project((bar(6, "5"),), factors, spec("hfq"), decision_at=at(6))
    assert short == full[1:]
    assert short[0].close == Decimal(10)
    with pytest.raises(TiveloError) as error:
        provider.project((bar(6, "5"),), (factor(3, "4"),), spec("hfq"), decision_at=at(6))
    assert error.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT


def test_b07_future_events_and_later_revisions_cannot_change_fixed_history():
    provider = StaticPriceView("qfq")
    bars = (bar(), bar(6, "5"))
    factors = (factor(), factor(3, "4"))
    expected = provider.project(bars, factors, spec("qfq", 3), decision_at=at(6))
    future = (
        factor(10, "8", available_at=at(5)),
        factor(1, "999", available_at=at(10), revision_id="2"),
    )
    assert provider.project(bars, factors + future, spec("qfq", 3), decision_at=at(6)) == expected


def test_later_correction_carries_its_knowledge_time_and_derived_provenance():
    provider = StaticPriceView("qfq")
    old, corrected = factor(), factor(value="3", available_at=at(5), revision_id="2")
    (result,) = provider.project(
        (bar(),), (old, corrected, factor(3, "4")), spec("qfq", 3), decision_at=at(6)
    )
    assert result.close == Decimal("7.5")
    assert result.available_at == result.ingested_at == at(5)
    assert result.source_id == "tivelo.qfq"
    assert result.revision_id.startswith("view:")


@pytest.mark.parametrize(
    "correction",
    [
        dict(cancelled=True),
        dict(effective_at=at(10)),
    ],
)
def test_cancelled_or_rescheduled_revision_does_not_resurrect_old_factor(correction):
    with pytest.raises(TiveloError) as error:
        StaticPriceView("hfq").project(
            (bar(),),
            (factor(), factor(available_at=at(4), revision_id="2", **correction)),
            spec("hfq"),
            decision_at=at(6),
        )
    assert error.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT


@pytest.mark.parametrize(
    "changes,code",
    [
        (dict(price_basis="qfq"), ErrorCode.RAW_PRICE_REQUIRED),
        (dict(available_at=at(7), ingested_at=at(7)), ErrorCode.FUTURE_INFORMATION),
        (dict(is_final=False), ErrorCode.FUTURE_INFORMATION),
    ],
)
def test_unusable_bars_rejected(changes, code):
    with pytest.raises(TiveloError) as error:
        StaticPriceView("raw").project((bar(**changes),), (), spec("raw"), decision_at=at(6))
    assert error.value.code == code


@pytest.mark.parametrize(
    "changes",
    [
        dict(factor_kind="event_ratio"),
        dict(definition_id="unrelated"),
        dict(baseline_id="other"),
    ],
)
def test_incompatible_factor_definitions_are_not_guessed(changes):
    with pytest.raises(TiveloError) as error:
        StaticPriceView("hfq").project(
            (bar(),), (factor(**changes),), spec("hfq"), decision_at=at(6)
        )
    assert error.value.code == ErrorCode.FACTOR_INCOMPATIBLE


@pytest.mark.parametrize(
    "factors",
    [
        (factor(), factor(value="3")),
        (factor(), factor(value="3", factor_id="another")),
    ],
)
def test_conflicting_revisions_or_overlapping_factors_fail(factors):
    with pytest.raises(TiveloError) as error:
        StaticPriceView("hfq").project((bar(),), factors, spec("hfq"), decision_at=at(6))
    assert error.value.code == ErrorCode.DATA_CONFLICT


def test_change_inside_a_bar_cannot_be_applied_to_entire_ohlc():
    with pytest.raises(TiveloError) as error:
        StaticPriceView("hfq").project(
            (bar(),),
            (factor(), factor(2, "4", effective_at=at(2, 3))),
            spec("hfq"),
            decision_at=at(6),
        )
    assert error.value.code == ErrorCode.FACTOR_INCOMPATIBLE


def test_duplicate_bars_are_not_silently_resolved():
    with pytest.raises(TiveloError) as error:
        StaticPriceView("raw").project(
            (bar(), bar(revision_id="2")), (), spec("raw"), decision_at=at(6)
        )
    assert error.value.code == ErrorCode.DATA_CONFLICT


def test_conversion_rejects_fabricated_stale_and_failed_projection_scales():
    provider = StaticPriceView("hfq")
    provider.project((bar(),), (factor(),), spec("hfq"), decision_at=at(6))
    price = provider.price("10", instrument_id="security-1", decision_at=at(6))
    fabricated = PriceValue(value="10", scale={**price.scale.model_dump(), "raw_multiplier": "999"})
    for value, decision in ((fabricated, at(6)), (price, at(7))):
        with pytest.raises(TiveloError) as error:
            provider.to_raw(value, decision_at=decision)
        assert error.value.code == ErrorCode.PRICE_BASIS_UNPROVEN
    with pytest.raises(TiveloError):
        provider.project((bar(price_basis="hfq"),), (factor(),), spec("hfq"), decision_at=at(6))
    with pytest.raises(TiveloError):
        provider.to_raw(price, decision_at=at(6))


def test_decimal_rounding_and_conversion_ignore_ambient_precision():
    with localcontext() as context:
        context.prec = 4
        context.traps[Inexact] = True
        provider = StaticPriceView("qfq")
        (projected,) = provider.project(
            (bar(price="1"),),
            (factor(value="1"), factor(3, "3")),
            spec("qfq", 3),
            decision_at=at(6),
        )
        assert projected.close == Decimal("0.333333333333")
        provider = StaticPriceView("hfq")
        provider.project(
            (bar(6, "1"),), (factor(value="1"), factor(3, "3")), spec("hfq"), decision_at=at(6)
        )
        price = provider.price("3", instrument_id="security-1", decision_at=at(6))
        assert price.scale.raw_multiplier == Decimal("0.333333333333")
        assert provider.to_raw(price, decision_at=at(6)) == Decimal(1)


@pytest.mark.parametrize(
    "price,baseline,later",
    [
        ("99999999999999999999999999", "1", "2"),
        ("0.000000000001", "100", "1"),
    ],
)
def test_overflow_and_underflow_are_explicit(price, baseline, later):
    with pytest.raises(TiveloError) as error:
        StaticPriceView("hfq").project(
            (bar(6, price),),
            (factor(value=baseline), factor(3, later)),
            spec("hfq"),
            decision_at=at(6),
        )
    assert error.value.code == ErrorCode.CONTRACT_INVALID


def test_dynamic_mode_is_not_provided_by_free_package():
    with pytest.raises(TiveloError) as error:
        StaticPriceView("dynamic")
    assert error.value.code == ErrorCode.UNSUPPORTED_CAPABILITY
