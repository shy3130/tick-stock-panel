from datetime import date, datetime

import pytest
from pydantic import ValidationError

from app.services import weak_to_strong
from app.services.weak_to_strong import (
    WeakToStrongEvaluateRequest,
    evaluate_weak_to_strong_v1,
    validate_evidence_keys,
)


def test_missing_reader_is_unavailable():
    request = WeakToStrongEvaluateRequest(signal_date=date(2026, 1, 2), symbols=["600000.SH"])
    result = evaluate_weak_to_strong_v1(request)
    assert result.manifest.status == "unavailable"
    assert result.evaluations[0].core_status == "unavailable"


def test_duplicate_or_unknown_symbols_rejected():
    with pytest.raises(ValidationError):
        WeakToStrongEvaluateRequest(signal_date=date(2026, 1, 2), symbols=["600000.SH", "sh600000"])
    with pytest.raises(ValidationError):
        WeakToStrongEvaluateRequest(signal_date=date(2026, 1, 2), symbols=["600000.XX"])


def test_extra_fields_and_trading_evidence_keys_rejected():
    with pytest.raises(ValidationError):
        WeakToStrongEvaluateRequest(signal_date=date(2026, 1, 2), symbols=["600000.SH"], target_price=1)
    with pytest.raises(ValueError):
        validate_evidence_keys(["target_price"])


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("600000.SH", "600000"),
        ("sh.600000", "600000"),
        ("SZ000001", "000001"),
        ("300001.sz", "300001"),
        ("sh.600000.SH", "600000"),
    ],
)
def test_exchange_qualifier_must_match_code_market(raw, canonical):
    assert weak_to_strong.canonicalize_symbol(raw) == canonical


@pytest.mark.parametrize("raw", ["600000.SZ", "000001.SH", "BJ600000", "sh.600000.sz"])
def test_exchange_code_mismatch_is_rejected(raw):
    with pytest.raises(ValueError):
        weak_to_strong.canonicalize_symbol(raw)


@pytest.fixture()
def isolated_reader_registry():
    original = list(weak_to_strong._READER_FACTORIES)
    weak_to_strong._READER_FACTORIES.clear()
    yield
    weak_to_strong._READER_FACTORIES[:] = original


class _FullCapabilityReader:
    def capabilities(self):
        return frozenset(weak_to_strong.REQUIRED_CAPABILITIES)


def _raising_reader_factory():
    raise RuntimeError("snapshot unavailable")


def test_reader_factory_failure_does_not_mask_later_candidate(isolated_reader_registry):
    weak_to_strong.register_reader_factory(_raising_reader_factory)
    weak_to_strong.register_reader_factory(_FullCapabilityReader)

    assert isinstance(weak_to_strong.resolve_weak_to_strong_reader(), _FullCapabilityReader)


def test_all_reader_factory_failures_return_reader_missing(
    isolated_reader_registry,
):
    weak_to_strong.register_reader_factory(_raising_reader_factory)
    request = WeakToStrongEvaluateRequest(signal_date=date(2026, 1, 2), symbols=["600000.SH"])

    result = evaluate_weak_to_strong_v1(request)

    assert result.manifest.status == "unavailable"
    assert result.evaluations[0].status_reason == "reader_missing"


class _CompleteReader:
    def __init__(self, *, scenario="reseal", order_book=True, pit=True, forward=True):
        self.scenario, self.order_book, self.pit, self.forward = scenario, order_book, pit, forward
        self.signal = date(2026, 1, 9)

    def capabilities(self):
        return frozenset(weak_to_strong.REQUIRED_CAPABILITIES)

    def run_manifest(self):
        return {"generation": "fake-generation", "sha256": "fake-sha"}

    def daily_bars(self, symbol, start, end):
        def bar(day, close=10.0, volume=10_000_000.0, open_=None, high=None, low=None):
            return {"trade_date": day, "open": close if open_ is None else open_, "high": close if high is None else high, "low": close if low is None else low, "close": close, "volume": volume}
        days = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
        bars = [bar(days[0]), bar(days[1]), bar(days[2]), bar(days[3], 11.0, 30_000_000, 10.2, 11.0, 10.1)]
        if self.scenario == "one_word":
            bars.append(bar(self.signal, 12.1, 2_000_000, 12.1, 12.1, 12.1))
        elif self.scenario == "broken":
            bars.append(bar(self.signal, 11.5, 25_000_000, 11.5, 12.1, 11.4))
        elif self.scenario == "low_open":
            bars.append(bar(self.signal, 10.8, 20_000_000, 10.8, 11.0, 10.7))
        elif self.scenario == "no_touch":
            bars.append(bar(self.signal, 11.8, 20_000_000, 11.6, 11.9, 11.4))
        else:
            bars.append(bar(self.signal, 12.1, 25_000_000, 11.5, 12.1, 11.4))
        if self.forward:
            bars.append(bar(date(2026, 1, 12), 12.5, 20_000_000, 12.2, 12.6, 12.0))
        return [b for b in bars if start <= b["trade_date"] <= end]

    def suspended_dates(self, symbol, start, end):
        return []

    def minute_bars(self, symbol, trade_date):
        if self.scenario == "one_word":
            return [{"timestamp": datetime(2026, 1, 9, 9, 31), "open": 12.1, "high": 12.1, "low": 12.1, "close": 12.1, "volume": 1.0}]
        if self.scenario == "low_open":
            return [{"timestamp": datetime(2026, 1, 9, 9, 31), "open": 10.8, "high": 11.0, "low": 10.7, "close": 10.8, "volume": 1.0}]
        if self.scenario == "no_touch":
            return [{"timestamp": datetime(2026, 1, 9, 9, 31), "open": 11.6, "high": 11.9, "low": 11.4, "close": 11.8, "volume": 1.0}]
        return [{"timestamp": datetime(2026, 1, 9, 9, 31), "open": 11.5, "high": 11.6, "low": 11.4, "close": 11.5, "volume": 1.0}, {"timestamp": datetime(2026, 1, 9, 9, 45), "open": 12.0, "high": 12.1, "low": 11.9, "close": 12.1, "volume": 1.0}, {"timestamp": datetime(2026, 1, 9, 10, 30), "open": 11.9, "high": 12.0, "low": 11.7, "close": 11.8, "volume": 1.0}]

    def auction_snapshot(self, symbol, trade_date):
        return {"open_price": 12.1 if self.scenario == "one_word" else 11.5, "matched_volume": 100.0}

    def ticks(self, symbol, trade_date):
        if self.scenario in {"low_open", "no_touch"}:
            return [{"timestamp": datetime(2026, 1, 9, 9, 31), "seq": 1, "price": 10.8, "volume": 1.0}]
        if self.scenario == "one_word":
            return [{"timestamp": datetime(2026, 1, 9, 9, 31), "seq": 1, "price": 12.1, "volume": 1.0}]
        if self.scenario == "broken":
            return [{"timestamp": datetime(2026, 1, 9, 9, 45), "seq": 1, "price": 12.1, "volume": 1.0}, {"timestamp": datetime(2026, 1, 9, 10, 0), "seq": 2, "price": 11.8, "volume": 1.0}]
        return [{"timestamp": datetime(2026, 1, 9, 9, 45), "seq": 1, "price": 12.1, "volume": 1.0}, {"timestamp": datetime(2026, 1, 9, 10, 30), "seq": 2, "price": 11.8, "volume": 1.0}, {"timestamp": datetime(2026, 1, 9, 14, 0), "seq": 3, "price": 12.1, "volume": 1.0}]

    def order_book_snapshots(self, symbol, trade_date):
        if not self.order_book:
            return []
        return [{"timestamp": datetime(2026, 1, 9, 9, 45, 1), "bid1_price": 12.1, "bid1_volume": 100.0, "ask1_price": None, "ask1_volume": 0.0}]

    def pit_snapshot(self, symbol, as_of):
        if not self.pit:
            return None
        return {"effective_at": datetime(2020, 1, 1), "available_at": datetime(2020, 1, 1), "limit_up_pct": 0.1, "limit_down_pct": 0.1, "is_st": False, "float_shares": 1_000_000.0}


def test_complete_reader_produces_structured_event(isolated_reader_registry):
    weak_to_strong.register_reader_factory(lambda: _CompleteReader())
    result = evaluate_weak_to_strong_v1(WeakToStrongEvaluateRequest(signal_date=date(2026, 1, 9), symbols=["600000.SH"]))
    assert result.manifest.status == "available"
    assert result.evaluations[0].status_reason != "event_path_not_implemented"
    assert result.evaluations[0].event_label == "broken_resealed"


@pytest.mark.parametrize(("scenario", "label"), [("one_word", "one_word_limit"), ("broken", "broken_not_resealed"), ("low_open", "no_gap_up"), ("no_touch", "gap_up_no_touch")])
def test_event_classification_scenarios(isolated_reader_registry, scenario, label):
    weak_to_strong.register_reader_factory(lambda: _CompleteReader(scenario=scenario))
    result = evaluate_weak_to_strong_v1(WeakToStrongEvaluateRequest(signal_date=date(2026, 1, 9), symbols=["600000.SH"]))
    assert result.evaluations[0].event_label == label


def test_missing_order_book_downgrades_to_bar_touched(isolated_reader_registry):
    weak_to_strong.register_reader_factory(lambda: _CompleteReader(order_book=False))
    result = evaluate_weak_to_strong_v1(WeakToStrongEvaluateRequest(signal_date=date(2026, 1, 9), symbols=["600000.SH"]))
    evaluation = result.evaluations[0]
    assert evaluation.event_label == "bar_touched"
    assert evaluation.status == "censored"
    assert "missing_order_book_evidence" in evaluation.censoring


def test_missing_pit_is_unavailable(isolated_reader_registry):
    weak_to_strong.register_reader_factory(lambda: _CompleteReader(pit=False))
    result = evaluate_weak_to_strong_v1(WeakToStrongEvaluateRequest(signal_date=date(2026, 1, 9), symbols=["600000.SH"]))
    assert result.evaluations[0].status == "unavailable"
    assert result.evaluations[0].status_reason == "pit_incomplete"


def test_forward_shortage_is_censored_and_cost_is_diagnostic(isolated_reader_registry):
    weak_to_strong.register_reader_factory(lambda: _CompleteReader(forward=False))
    result = evaluate_weak_to_strong_v1(WeakToStrongEvaluateRequest(signal_date=date(2026, 1, 9), symbols=["600000.SH"], cost_bps=15))
    assert "forward_insufficient" in result.evaluations[0].censoring
    assert result.evaluations[0].forward.status == "censored"
    assert result.summary.unspecified_forward.forward_censored == 1
