"""Public SDK integration against its actual HTTP protocol on loopback."""

import json
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest
from tivelo_contracts.data import BarQuery, HistoryQuery

from tivelo_data import DataEngine
from tivelo_data.cli import main

pytest.importorskip("tickflow")
pytest.importorskip("tivelo_market_cn")

SYMBOL = "600000.SH"
STAMP = 1751299200000  # 2025-07-01 00:00 in Shanghai
BAR = {
    "timestamp": [STAMP],
    "open": [10],
    "high": [11],
    "low": [9.9],
    "close": [10.5],
    "volume": [12],
    "amount": [12600],
}


@pytest.fixture
def feed(tmp_path, monkeypatch):
    state = SimpleNamespace(requests=[], status=200, body={"data": {SYMBOL: BAR}})

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path)
            state.requests.append((path.path, parse_qs(path.query), self.headers.get("x-api-key")))
            status, reply = state.respond(state.requests[-1][1]) if hasattr(state, "respond") else (state.status, state.body)
            body = json.dumps(reply).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    state.config = {"endpoint": f"http://127.0.0.1:{server.server_port}"}
    state.capture = {
        "calendar_version": "2026.1.0",
        "expected_provider": "tickflow",
        "symbols": [{"instrument_id": "id-1", "symbol": SYMBOL, "venue_id": "XSHG"}],
        "request": {
            "dataset_id": "daily",
            "market_id": "cn.equity",
            "asset_class": "equity",
            "record_type": "bar",
            "timeframe": "1d",
            "price_basis": "raw",
            "instrument_ids": ["id-1"],
            "start": "2025-06-30T16:00:00Z",
            "end": "2025-07-02T16:00:00Z",
        },
    }
    capture, source = tmp_path / "capture.json", tmp_path / "source.json"
    state.root = tmp_path / "store"
    monkeypatch.setenv("TICKFLOW_API_KEY", "ambient-synthetic-value")
    monkeypatch.setenv("TICKFLOW_BASE_URL", "http://127.0.0.1:1")

    def run(operation, *extra):
        capture.write_text(json.dumps(state.capture))
        source.write_text(json.dumps(state.config))
        return main(
            [
                "--root",
                str(state.root),
                operation,
                "--capture",
                str(capture),
                "--source",
                str(source),
                *extra,
            ]
        )

    state.run = run
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_sdk_cli_plan_snapshot_and_exact_units(feed, capsys, tmp_path):
    assert feed.run("plan-tickflow") == 0
    plan = capsys.readouterr().out
    assert "tickflow" in plan and "127.0.0.1" not in plan
    assert not feed.requests and not feed.root.exists()
    locked = tmp_path / "plan.json"
    locked.write_text(plan)
    assert feed.run("import-tickflow", "--locked-plan", str(locked)) == 0
    snapshot = json.loads(capsys.readouterr().out)
    assert len(feed.requests) == 1
    path, query, key = feed.requests[0]
    assert path == "/v1/klines/batch" and key is None
    assert query == {
        "symbols": [SYMBOL],
        "start_time": [str(STAMP)],
        "end_time": [str(STAMP + 172800000 - 1)],
        "adjust": ["none"],
        "count": ["2"],
        "period": ["1d"],
    }
    with DataEngine(feed.root).open_reader(snapshot["snapshot_id"]) as reader:
        (dataset,) = reader.datasets
        ds = dataset.contents
        assert ds.knowledge_basis == "latest_reconstruction" and ds.source_ids == ("tickflow",)
        assert ds.coverage.status == "partial" and ds.coverage.completed_units == 1
        assert ds.coverage.requested_units == 2
        start, end = feed.capture["request"]["start"], feed.capture["request"]["end"]
        query = BarQuery(
            instrument_ids=("id-1",), timeframe="1d", start=start, end=end, as_of=datetime.now(UTC)
        )
        (bar,) = reader.bars(query)
        assert bar.trading_date.isoformat() == "2025-07-01"
        assert bar.bar_start == datetime(2025, 6, 30, 16, tzinfo=UTC)
        assert bar.bar_end == datetime(2025, 7, 1, 16, tzinfo=UTC)
        assert (bar.close, bar.volume, bar.turnover) == (
            Decimal("10.5"),
            Decimal(1200),
            Decimal(12600),
        )
        assert (bar.price_basis, bar.quantity_unit, bar.quote_currency) == ("raw", "share", "CNY")
        assert bar.available_at == bar.ingested_at > bar.bar_end
        assert not reader.bars(query.model_copy(update={"as_of": bar.bar_end}))
    feed.config["batch"] = 1
    assert feed.run("sync-tickflow", "--locked-plan", str(locked)) == 1
    assert (
        json.loads(capsys.readouterr().err)["code"] == "DATA_CONFLICT" and len(feed.requests) == 1
    )


def test_paid_credentials_resolved_only_for_requested_feed(feed, monkeypatch, capsys):
    feed.config.update(mode="paid", credential_ref="TIVELO_TEST_VENDOR_REF")
    monkeypatch.delenv("TIVELO_TEST_VENDOR_REF", raising=False)
    assert feed.run("plan-tickflow") == 0
    plan = capsys.readouterr().out
    assert "VENDOR_REF" not in plan and not feed.requests
    assert feed.run("sync-tickflow") == 1
    assert (
        json.loads(capsys.readouterr().err)["code"] == "AUTHENTICATION_FAILED" and not feed.requests
    )
    monkeypatch.setenv("TIVELO_TEST_VENDOR_REF", "requested-synthetic-token")
    feed.body = {"data": {}}
    assert feed.run("sync-tickflow") == 0
    output = capsys.readouterr().out
    assert "requested-synthetic-token" not in output
    assert json.loads(output)["version"]["contents"]["coverage"]["status"] == "empty"
    assert feed.requests[0][2] == "requested-synthetic-token"


@pytest.mark.parametrize(
    ("status", "payload", "code"),
    [
        (401, {"message": "synthetic-private-text"}, "AUTHENTICATION_FAILED"),
        (403, {}, "AUTHENTICATION_FAILED"),
        (429, {}, "RATE_LIMITED"),
        (502, {}, "SOURCE_UNAVAILABLE"),
        (200, {"error": "synthetic-private-text"}, "CONTRACT_INVALID"),
        (200, {"data": {SYMBOL: {}}}, "CONTRACT_INVALID"),
        (200, {"data": {SYMBOL: {**BAR, "amount": []}}}, "CONTRACT_INVALID"),
        (200, {"data": {SYMBOL: {**BAR, "volume": [True]}}}, "CONTRACT_INVALID"),
        (200, {"data": {SYMBOL: {**BAR, "low": [100]}}}, "CONTRACT_INVALID"),
        (200, {"data": {"000001.SZ": BAR}}, "DATA_CONFLICT"),
        (200, {"data": {SYMBOL: {**BAR, "timestamp": [STAMP - 86400000]}}}, "DATA_CONFLICT"),
        (200, {"data": {SYMBOL: {k: v * 2 for k, v in BAR.items()}}}, "DATA_CONFLICT"),
    ],
)
def test_sdk_failure_never_publishes_or_exposes_provider_message(
    feed, capsys, status, payload, code
):
    feed.status, feed.body = status, payload
    assert feed.run("import-tickflow") == 1
    error = json.loads(capsys.readouterr().err)
    assert error["code"] == code
    assert error["retryable"] == (code in {"RATE_LIMITED", "SOURCE_UNAVAILABLE"})
    assert "synthetic-private-text" not in str(error) and len(feed.requests) == 1
    assert not list((feed.root / "dataset_versions").glob("*/manifest.json"))


def test_sdk_halt_and_weekend_are_distinct_from_transport_failure(feed, capsys):
    feed.body = {"data": {SYMBOL: {**BAR, "volume": [0], "amount": [0]}}}
    assert feed.run("sync-tickflow") == 0
    coverage = json.loads(capsys.readouterr().out)["version"]["contents"]["coverage"]
    assert coverage["status"] == "empty" and coverage["issues"][0]["reason"] == "missing"
    feed.capture["request"].update(start="2025-07-04T16:00:00Z", end="2025-07-06T16:00:00Z")
    assert feed.run("sync-tickflow") == 0
    coverage = json.loads(capsys.readouterr().out)["version"]["contents"]["coverage"]
    assert coverage["status"] == "empty" and coverage["issues"][0]["reason"] == "no_trades"
    assert len(feed.requests) == 1


@pytest.fixture
def factors(feed, monkeypatch):
    feed.capture.pop("calendar_version")
    feed.capture.update(representation="event_ratio")
    feed.capture["request"].update(dataset_id="factors", record_type="adjustment_factor")
    for field in ("timeframe", "price_basis"):
        feed.capture["request"].pop(field)
    feed.capture["symbols"].extend(
        [
            {"instrument_id": "id-2", "symbol": "000001.SZ", "venue_id": "XSHE"},
            {"instrument_id": "id-3", "symbol": "600001.SH", "venue_id": "XSHG"},
        ]
    )
    feed.capture["request"]["instrument_ids"] = ["id-1", "id-2", "id-3"]
    feed.config.update(mode="paid", credential_ref="TEST_FACTORS_KEY", rpm=60000)
    monkeypatch.setenv("TEST_FACTORS_KEY", "synthetic-factor-token")
    feed.body = {
        "data": {
            SYMBOL: [
                {"timestamp": STAMP + 86400000, "ex_factor": 1.25},
                {"timestamp": STAMP, "ex_factor": 2},
            ],
            "000001.SZ": [],
        }
    }
    return feed


@pytest.mark.parametrize("kind", ["event_ratio", "cumulative_forward"])
def test_sdk_factor_plan_capture_coverage_and_historical_visibility(
    factors, capsys, tmp_path, kind
):
    f = factors
    if kind == "cumulative_forward":
        f.capture.update(representation=kind, baseline_date="2025-06-30")
        f.body["data"][SYMBOL].append({"timestamp": STAMP - 86400000, "ex_factor": 9})
    assert f.run("plan-tickflow-factors") == 0
    plan = capsys.readouterr().out
    assert not f.requests and not f.root.exists()
    assert "latest_reconstruction" in plan and "synthetic-factor-token" not in plan
    assert "TEST_FACTORS_KEY" not in plan and "127.0.0.1" not in plan
    locked = tmp_path / "locked.json"
    locked.write_text(plan)
    before = datetime.now(UTC)
    assert f.run("import-tickflow-factors", "--locked-plan", str(locked)) == 0
    snapshot = json.loads(capsys.readouterr().out)["snapshot_id"]
    after = datetime.now(UTC)
    ((path, params, key),) = f.requests
    assert (path, key) == ("/v1/klines/ex-factors", "synthetic-factor-token")
    assert params == {
        "symbols": [SYMBOL + ",000001.SZ,600001.SH"],
        "start_time": [str(STAMP - (86400000 if kind == "cumulative_forward" else 0))],
        "end_time": [str(STAMP + 172800000 - 1)],
    }
    with DataEngine(f.root).open_reader(snapshot) as reader:
        ds = reader.datasets[0].contents
        assert (ds.knowledge_basis, ds.source_ids) == ("latest_reconstruction", ("tickflow",))
        cov = ds.coverage
        assert (cov.status, cov.completed_units, cov.requested_units) == ("partial", 2, 3)
        assert [(issue.instrument_id, issue.reason) for issue in cov.issues] == [
            ("id-3", "missing")
        ]
        q = HistoryQuery(
            instrument_ids=("id-1", "id-2", "id-3"),
            effective_at=f.capture["request"]["end"],
            as_of=before,
        )
        assert not reader.factors(q)
        rows = reader.factors(q.model_copy(update={"as_of": after}))
        assert all(before <= r.available_at <= after and r.factor_kind == kind for r in rows)
        values = [(r.instrument_id, r.effective_at.isoformat()[:10], r.value) for r in rows]
        assert ("id-1", "2025-06-30", Decimal(2)) in values
        if kind == "cumulative_forward":
            assert ("id-1", "2025-07-01", Decimal("2.5")) in values
            assert ("id-2", "2025-06-30", Decimal(1)) in values
        else:
            assert ("id-1", "2025-07-01", Decimal("1.25")) in values
        assert cov.row_count == len(values) == (3 if kind == "cumulative_forward" else 2)
    f.config["batch"] = 1
    assert f.run("sync-tickflow-factors", "--locked-plan", str(locked)) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "DATA_CONFLICT" and len(f.requests) == 1


@pytest.mark.parametrize(
    ("status", "data", "code"),
    [
        (429, {}, "RATE_LIMITED"),
        (403, {}, "AUTHENTICATION_FAILED"),
        (503, {}, "SOURCE_UNAVAILABLE"),
        (200, [], "CONTRACT_INVALID"),
        (200, {SYMBOL: {}}, "CONTRACT_INVALID"),
        (200, {SYMBOL: [{"timestamp": STAMP, "ex_factor": 0}]}, "CONTRACT_INVALID"),
        (200, {SYMBOL: [{"timestamp": True, "ex_factor": 1}]}, "CONTRACT_INVALID"),
        (200, {SYMBOL: [{"timestamp": STAMP + 172800000, "ex_factor": 1}]}, "DATA_CONFLICT"),
        (200, {"other": []}, "DATA_CONFLICT"),
        (
            200,
            {
                SYMBOL: [
                    {"timestamp": STAMP, "ex_factor": 1},
                    {"timestamp": STAMP + 1, "ex_factor": 2},
                ]
            },
            "DATA_CONFLICT",
        ),
    ],
)
def test_failed_factor_observation_never_publishes(factors, capsys, status, data, code):
    factors.status = status
    factors.body = {"data": data, "message": "private-provider-detail"}
    assert factors.run("import-tickflow-factors") == 1
    error = json.loads(capsys.readouterr().err)
    assert error["code"] == code and "private-provider-detail" not in str(error)
    assert not list((factors.root / "dataset_versions").glob("*/manifest.json"))


def test_multiwindow_factor_pages_and_missing_warmup(factors, capsys):
    f = factors
    f.capture["symbols"] = f.capture["symbols"][:1]
    f.capture["request"]["instrument_ids"] = ["id-1"]
    start = datetime.fromisoformat(f.capture["request"]["end"]) - timedelta(days=2051)
    f.capture["request"]["start"] = start.isoformat()
    f.respond = lambda p: (
        200,
        {
            "data": {
                SYMBOL: [
                    {"timestamp": day, "ex_factor": 1}
                    for day in range(int(p["start_time"][0]), int(p["end_time"][0]) + 1, 86400000)
                ]
            }
        },
    )
    assert f.run("import-tickflow-factors") == 0
    snapshot = json.loads(capsys.readouterr().out)["snapshot_id"]
    with DataEngine(f.root).open_reader(snapshot) as reader:
        cov = reader.datasets[0].contents.coverage
        assert (cov.status, cov.completed_units, cov.row_count) == ("complete", 1, 2051)
    assert len(f.requests) == 2
    windows = [req[1] for req in f.requests]
    assert int(windows[0]["end_time"][0]) + 1 == int(windows[1]["start_time"][0])
    f.capture.update(
        representation="cumulative_forward",
        baseline_date=start.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat(),
    )
    f.capture["request"]["start"] = "2025-06-30T16:00:00Z"
    f.respond = lambda p: (200, {"data": {} if p == windows[0] else {SYMBOL: []}})
    assert f.run("sync-tickflow-factors") == 0
    cov = json.loads(capsys.readouterr().out)["version"]["contents"]["coverage"]
    assert (cov["status"], cov["completed_units"], cov["row_count"]) == ("empty", 0, 0)
    assert [i["reason"] for i in cov["issues"]] == ["missing"]


def test_explicit_no_events_free_grant_and_missing_credential(factors, monkeypatch, capsys):
    f = factors
    f.body = {"data": {s["symbol"]: [] for s in f.capture["symbols"]}}
    assert f.run("sync-tickflow-factors") == 0
    cov = json.loads(capsys.readouterr().out)["version"]["contents"]["coverage"]
    assert (cov["status"], cov["completed_units"], cov["row_count"]) == ("empty", 3, 0)
    assert [i["reason"] for i in cov["issues"]] == ["no_events"]
    monkeypatch.delenv(f.config["credential_ref"])
    assert f.run("sync-tickflow-factors") == 1
    assert json.loads(capsys.readouterr().err)["code"] == "AUTHENTICATION_FAILED"
    f.config.pop("credential_ref")
    f.config.update(mode="free")
    assert f.run("plan-tickflow-factors") == 1
    assert (
        json.loads(capsys.readouterr().err)["code"] == "UNSUPPORTED_CAPABILITY"
        and len(f.requests) == 1
    )


def test_regressing_clock_cannot_backdate_a_live_factor(factors):
    from tivelo_contracts.errors import ErrorCode, TiveloError

    from tivelo_data.adapters.cn_daily import CNFactorRequest
    from tivelo_data.providers import FixedSourceResolver
    from tivelo_data.tickflow_connection import TickFlowDailyConfig
    from tivelo_data.tickflow_factors import TickFlowFactorProvider

    source = CNFactorRequest.model_validate(factors.capture)
    current = datetime.now(UTC)
    clock = iter((current, current - timedelta(days=1)))
    provider = TickFlowFactorProvider(
        source,
        TickFlowDailyConfig(**factors.config),
        resolve_token=lambda _: "test-only",
        now=clock.__next__,
    )
    engine = DataEngine(factors.root, FixedSourceResolver(provider, provider.configuration_digest))
    with pytest.raises(TiveloError) as error:
        engine.sync(engine.plan_sync(source.request))
    assert error.value.code == ErrorCode.DATA_CONFLICT
    assert not list((factors.root / "staging").iterdir())


def test_cancelled_between_factor_batches_rolls_back_and_same_plan_can_retry(factors):
    from tivelo_contracts.errors import ErrorCode, TiveloError

    from tivelo_data.adapters.cn_daily import CNFactorRequest
    from tivelo_data.providers import FixedSourceResolver, SyncContext
    from tivelo_data.tickflow_connection import TickFlowDailyConfig
    from tivelo_data.tickflow_factors import TickFlowFactorProvider

    f = factors
    source = CNFactorRequest.model_validate(f.capture)
    provider = TickFlowFactorProvider(source, TickFlowDailyConfig(**{**f.config, "batch": 1}))
    f.respond = lambda p: (200, {"data": {p["symbols"][0]: [{"timestamp": STAMP, "ex_factor": 2}]}})
    engine = DataEngine(f.root, FixedSourceResolver(provider, provider.configuration_digest))
    plan = engine.plan_sync(source.request)

    def token(_):
        return {"token": "temporary-fixture-token"}

    with pytest.raises(TiveloError) as error:
        engine.sync(plan, SyncContext(token, lambda: bool(f.requests), lambda *_: None))
    assert error.value.code == ErrorCode.CANCELLED and len(f.requests) == 1
    assert not list((f.root / "staging").iterdir())
    result = engine.publish(engine.sync(plan, SyncContext(token, lambda: False, lambda *_: None)))
    assert result.contents.coverage.row_count == 3 and len(f.requests) == 4
