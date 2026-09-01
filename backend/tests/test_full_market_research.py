from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, ClassVar

import pytest

from app.services import full_market_research as runner


class Reader:
    def __init__(self, symbols=("600000.SH",), manifest="a" * 64):
        self.symbols = list(symbols)
        self.manifest = manifest
        self.close_calls = 0

    def generation(self):
        return "generation-1"

    def manifest_sha256(self):
        return self.manifest

    def provider_id(self):
        return "fake.provider"

    def source_provenance(self):
        return {"markets": {"generation": "g", "manifest_sha256": "b" * 64}}

    def universe(self, start, end):
        return self.symbols

    def close(self):
        self.close_calls += 1


class Repo:
    def __init__(self, reader):
        self.n_shape_research_reader = reader


class RecordingAdapter:
    name = "test-recording"
    calls: ClassVar[list[tuple[Any, dict[str, Any]]]] = []

    def build_request(self, start, end, cohort, *, oos_start, cost_bps):
        return {"start": start.isoformat(), "end": end.isoformat(), "symbols": cohort}

    def evaluate(self, context, request):
        self.calls.append((context, request))
        return {
            "status": "ok",
            "request": request,
            "coverage": {"symbols": len(request["symbols"])},
        }

    def serialize_verdict(self, verdict):
        return verdict

    def extract_coverage(self, verdict):
        return verdict["coverage"]


@dataclass(frozen=True)
class DataclassRequest:
    start: date
    end: date
    symbols: list[str]


class UnavailableAdapter:
    name = "test-unavailable"

    def build_request(self, start, end, cohort, *, oos_start, cost_bps):
        return DataclassRequest(start=start, end=end, symbols=cohort)

    def evaluate(self, context, request):
        return {
            "status": "unavailable",
            "unavailable_reasons": ["sealed_input_missing"],
        }

    def serialize_verdict(self, verdict):
        return verdict

    def extract_coverage(self, verdict):
        return None


class RaisingAdapter(UnavailableAdapter):
    name = "test-raising"

    def evaluate(self, context, request):
        raise RuntimeError("evaluator failed")


@pytest.fixture
def recording():
    adapter = RecordingAdapter()
    adapter.calls = []
    runner.register_adapter(adapter, overwrite=True)
    return adapter


def test_empty_universe_fails_closed(recording):
    with pytest.raises(runner.FullMarketRunnerError, match="universe_empty"):
        runner.run_full_market_research(
            "test-recording", Repo(Reader(())), date(2025, 1, 1), date(2025, 1, 2)
        )
    assert recording.calls == []


def test_missing_provenance_fails_closed(recording):
    with pytest.raises(runner.FullMarketRunnerError, match="provenance"):
        runner.run_full_market_research(
            "test-recording", Repo(Reader(("A",), "bad")), date(2025, 1, 1), date(2025, 1, 2)
        )
    assert recording.calls == []


def test_complete_cohort_passed_once(recording):
    payload = runner.run_full_market_research(
        "test-recording", Repo(Reader((" b ", "A", "b", "c"))), date(2025, 1, 1), date(2025, 1, 2)
    )
    assert len(recording.calls) == 1
    assert isinstance(recording.calls[0][0], runner.RunnerContext)
    assert recording.calls[0][1]["symbols"] == ["A", "B", "C"]
    assert payload["cohort"]["count"] == 3
    assert payload["cohort"]["hash"] == hashlib.sha256(b"A\nB\nC").hexdigest()


def test_no_output_path_writes_nothing(tmp_path, recording, monkeypatch):
    monkeypatch.chdir(tmp_path)
    payload = runner.run_full_market_research(
        "test-recording", Repo(Reader()), date(2025, 1, 1), date(2025, 1, 2)
    )
    assert payload["research_id"].startswith("fm-")
    assert list(tmp_path.iterdir()) == []


def test_output_is_atomic(tmp_path, recording, monkeypatch):
    payload = runner.run_full_market_research(
        "test-recording", Repo(Reader()), date(2025, 1, 1), date(2025, 1, 2)
    )
    output = tmp_path / "result.json"
    replacements = []
    original = runner.os.replace
    monkeypatch.setattr(
        runner.os,
        "replace",
        lambda source, target: (replacements.append((source, target)), original(source, target))[1],
    )
    runner.write_payload_json(payload, output)
    assert output.exists()
    assert json.loads(output.read_text()) == json.loads(json.dumps(payload, default=str))
    assert len(replacements) == 1
    source, target = replacements[0]
    assert Path(source).parent == tmp_path
    assert str(source).endswith(".tmp")
    assert target == output


def test_builtin_registry_contains_all_full_market_factors():
    assert set(runner.registered_factor_names()) >= {
        "macd-arms",
        "weekly-flagpole",
        "single-yang-no-break",
        "dugu-trend",
        "pre-surge",
        "hold-firm",
        "mera",
        "n-depth",
        "negative-v5",
        "escape-risk",
        "doji-patterns",
    }
    assert "macd-stages" not in runner.registered_factor_names()


def test_unavailable_verdict_is_auditable_payload_and_reader_closes(tmp_path):
    runner.register_adapter(UnavailableAdapter(), overwrite=True)
    reader = Reader()
    payload = runner.run_full_market_research(
        "test-unavailable",
        Repo(reader),
        date(2025, 1, 1),
        date(2025, 1, 2),
    )

    assert payload["verdict"]["status"] == "unavailable"
    assert payload["verdict"]["unavailable_reasons"] == ["sealed_input_missing"]
    assert payload["request"]["symbols"] == ["600000.SH"]
    assert reader.close_calls == 1
    output = tmp_path / "unavailable.json"
    runner.write_payload_json(payload, output)
    assert json.loads(output.read_text())["verdict"]["status"] == "unavailable"


def test_reader_closes_when_evaluator_raises():
    runner.register_adapter(RaisingAdapter(), overwrite=True)
    reader = Reader()

    with pytest.raises(RuntimeError, match="evaluator failed"):
        runner.run_full_market_research(
            "test-raising",
            Repo(reader),
            date(2025, 1, 1),
            date(2025, 1, 2),
        )

    assert reader.close_calls == 1
