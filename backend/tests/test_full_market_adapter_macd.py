from datetime import date
from types import SimpleNamespace

from app.services.full_market_adapters.macd import MacdArmsAdapter
from app.services.full_market_research import RunnerContext
from app.services.macd_stages import ARMS_SCHEMA
from app.services.volume_breakout import DEFAULT_OOS_START

START = date(2024, 1, 1)
END = date(2025, 1, 31)


class _ClosableIndexReader:
    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


def test_build_request_keeps_complete_cohort_in_one_request():
    adapter = MacdArmsAdapter()
    cohort = ["600000.SH", "000001.SZ", "300750.SZ"]

    request = adapter.build_request(
        START,
        END,
        cohort,
        oos_start=None,
        cost_bps=12.5,
    )

    assert request.symbols == cohort
    assert request.symbols is not cohort
    assert request.oos_start == DEFAULT_OOS_START
    assert request.start == START
    assert request.end == END


def test_evaluate_calls_arms_evaluator_once_with_full_cohort(monkeypatch):
    adapter = MacdArmsAdapter()
    cohort = ["600000.SH", "000001.SZ", "300750.SZ"]
    request = adapter.build_request(
        START,
        END,
        cohort,
        oos_start=date(2025, 7, 1),
        cost_bps=None,
    )
    canonical_reader = object()
    context = RunnerContext(repo=SimpleNamespace(), reader=canonical_reader)
    calls = []
    expected = {"schema": ARMS_SCHEMA, "status": "ok", "arms": {}}

    def fake_evaluate_macd_arms(received_reader, *, start, end, symbols, oos_start, index_reader):
        calls.append((received_reader, start, end, symbols, oos_start, index_reader))
        return expected

    def fail_evaluate_macd_stages(*args, **kwargs):
        raise AssertionError("legacy evaluate_macd_stages must not be called")

    monkeypatch.setattr(
        "app.services.full_market_adapters.macd.evaluate_macd_arms",
        fake_evaluate_macd_arms,
    )
    monkeypatch.setattr(
        "app.services.full_market_adapters.macd.evaluate_macd_stages",
        fail_evaluate_macd_stages,
        raising=False,
    )

    result = adapter.evaluate(context, request)

    assert result is expected
    assert len(calls) == 1
    received_reader, start, end, symbols, oos_start, _ = calls[0]
    assert received_reader is canonical_reader
    assert symbols == cohort
    assert symbols is request.symbols
    assert (start, end, oos_start) == (request.start, request.end, request.oos_start)


def test_index_reader_passthrough_when_repo_provides(monkeypatch):
    adapter = MacdArmsAdapter()
    request = adapter.build_request(
        START,
        END,
        ["600000.SH"],
        oos_start=date(2025, 7, 1),
        cost_bps=None,
    )
    index_reader = _ClosableIndexReader()
    repo = SimpleNamespace(index_daily_research_reader=index_reader)
    context = RunnerContext(repo=repo, reader=object())
    received = {}

    def fake_evaluate_macd_arms(received_reader, *, start, end, symbols, oos_start, index_reader):
        received["index_reader"] = index_reader
        return {"schema": ARMS_SCHEMA, "status": "ok"}

    monkeypatch.setattr(
        "app.services.full_market_adapters.macd.evaluate_macd_arms",
        fake_evaluate_macd_arms,
    )

    result = adapter.evaluate(context, request)

    assert result["status"] == "ok"
    assert received["index_reader"] is index_reader
    assert index_reader.close_calls == 1


def test_index_reader_closed_once_when_evaluator_raises(monkeypatch):
    adapter = MacdArmsAdapter()
    request = adapter.build_request(
        START,
        END,
        ["600000.SH"],
        oos_start=date(2025, 7, 1),
        cost_bps=None,
    )
    index_reader = _ClosableIndexReader()
    repo = SimpleNamespace(index_daily_research_reader=index_reader)
    context = RunnerContext(repo=repo, reader=object())

    def failing_evaluate_macd_arms(
        received_reader, *, start, end, symbols, oos_start, index_reader
    ):
        raise RuntimeError("evaluator boom")

    monkeypatch.setattr(
        "app.services.full_market_adapters.macd.evaluate_macd_arms",
        failing_evaluate_macd_arms,
    )

    try:
        adapter.evaluate(context, request)
    except RuntimeError as exc:
        assert str(exc) == "evaluator boom"
    else:
        raise AssertionError("evaluator error must propagate")

    assert index_reader.close_calls == 1


def test_missing_index_reader_is_passed_as_none_without_fallback(monkeypatch):
    adapter = MacdArmsAdapter()
    request = adapter.build_request(
        START,
        END,
        ["600000.SH"],
        oos_start=date(2025, 7, 1),
        cost_bps=None,
    )
    context = RunnerContext(repo=SimpleNamespace(), reader=object())
    received = {}

    def fake_evaluate_macd_arms(received_reader, *, start, end, symbols, oos_start, index_reader):
        received["index_reader"] = index_reader
        return {"schema": ARMS_SCHEMA, "status": "ok"}

    monkeypatch.setattr(
        "app.services.full_market_adapters.macd.evaluate_macd_arms",
        fake_evaluate_macd_arms,
    )

    adapter.evaluate(context, request)

    assert received["index_reader"] is None


def test_missing_canonical_reader_returns_unavailable_without_fallback():
    adapter = MacdArmsAdapter()
    request = adapter.build_request(
        START,
        END,
        ["600000.SH", "000001.SZ"],
        oos_start=None,
        cost_bps=None,
    )

    result = adapter.evaluate(RunnerContext(repo=SimpleNamespace(), reader=None), request)

    assert result["schema"] == ARMS_SCHEMA
    assert result["status"] == "unavailable"
    assert result["unavailable_reasons"] == ["generation_pinned_reader_missing"]
    serialized = adapter.serialize_verdict(result)
    assert serialized is result
    assert adapter.extract_coverage(serialized) == {"is": None, "oos": None}


def test_extract_coverage_reads_segment_coverage_from_arms_verdict():
    adapter = MacdArmsAdapter()
    verdict = {
        "schema": ARMS_SCHEMA,
        "status": "ok",
        "segments": {
            "is": {"coverage": {"symbols": 3}, "arms": {}},
            "oos": {"coverage": {"symbols": 3}, "arms": {}},
        },
    }

    coverage = adapter.extract_coverage(verdict)

    assert coverage == {"is": {"symbols": 3}, "oos": {"symbols": 3}}
