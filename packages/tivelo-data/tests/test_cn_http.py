"""The optional mainland daily profile runs against a real, local-only HTTP feed."""

import importlib.util
import json
import sqlite3
import subprocess
import sys
import threading
from datetime import UTC, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from tivelo_contracts.data import BarQuery, HistoryQuery

from tivelo_data import DataEngine
from tivelo_data.cli import main

pytestmark = pytest.mark.skipif(
    any(importlib.util.find_spec(name) is None for name in ("httpx", "yaml", "tivelo_market_cn")),
    reason="requires the cn-http extra",
)

TOKEN_REF = "TIVELO_TEST_DAILY_BEARER"
TOKEN = "synthetic-provider-token"
ROW = {
    "code": "600000.SH",
    "date": "2025-07-01",
    "open": "10.01",
    "high": "11",
    "low": "10",
    "close": "10.5",
    "volume": "12.5",
    "amount": "13125",
}


@pytest.fixture
def feed(tmp_path, monkeypatch):
    state = SimpleNamespace(calls=[], responses=[(200, {"rows": [ROW]})])

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state.calls.append(
                (self.headers.get("Authorization"), parse_qs(urlsplit(self.path).query))
            )
            status, payload = state.responses[min(len(state.calls), len(state.responses)) - 1]
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            if status == 429:
                self.send_header("Retry-After", "7")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    monkeypatch.setenv(TOKEN_REF, TOKEN)
    state.root = tmp_path / "market-store"
    state.source = tmp_path / "feed.json"
    state.capture = tmp_path / "daily.json"
    state.dataset = {
        "url": f"http://127.0.0.1:{server.server_port}/bars",
        "research_profile": "cn.equity.regular.raw.lot100.v1",
        "response_path": "rows",
        "field_map": {name: "symbol" if name == "code" else name for name in ROW},
        "batch": 1,
    }
    state.declaration = {
        "name": "example",
        "auth": {"type": "bearer", "token_env": TOKEN_REF},
        "datasets": {"daily": state.dataset},
    }
    state.scope = {
        "expected_provider": "example",
        "calendar_version": "2026.1.0",
        "symbols": [{"instrument_id": "security-1", "symbol": ROW["code"], "venue_id": "XSHG"}],
        "request": {
            "dataset_id": "daily",
            "record_type": "bar",
            "market_id": "cn.equity",
            "asset_class": "equity",
            "instrument_ids": ["security-1"],
            "start": "2025-06-30T16:00:00Z",
            "end": "2025-07-02T16:00:00Z",
            "timeframe": "1d",
            "price_basis": "raw",
        },
    }
    state.source.write_text(json.dumps(state.declaration))
    state.capture.write_text(json.dumps(state.scope))
    state.options = ["--capture", str(state.capture), "--source", str(state.source)]
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_standalone_plan_import_and_historical_visibility(feed, tmp_path):
    def run(*args):
        result = subprocess.run(
            [sys.executable, "-I", "-m", "tivelo_data.cli", "--root", str(feed.root), *args],
            cwd=tmp_path,
            text=True,
            capture_output=True,
            check=True,
        )
        assert TOKEN not in result.stdout + result.stderr
        return json.loads(result.stdout)

    plan = run("plan-http", *feed.options)
    assert run("plan-http", *feed.options) == plan
    assert not feed.calls and not feed.root.exists()
    assert plan["provider"]["provider_id"] == "example"
    assert "127.0.0.1" not in json.dumps(plan) and TOKEN_REF not in json.dumps(plan)
    locked = tmp_path / "plan.json"
    locked.write_text(json.dumps(plan))
    snapshot = run("import-http", *feed.options, "--locked-plan", str(locked))
    assert feed.calls == [
        (
            f"Bearer {TOKEN}",
            {
                "symbols": ["600000.SH"],
                "start_time": ["2025-07-01T00:00:00"],
                "end_time": ["2025-07-02T23:59:59.999999"],
            },
        )
    ]
    with DataEngine(feed.root).open_reader(snapshot["snapshot_id"]) as reader:
        (dataset,) = reader.datasets
        assert dataset.contents.knowledge_basis == "latest_reconstruction"
        coverage = dataset.contents.coverage
        assert (coverage.status, coverage.completed_units, coverage.requested_units) == (
            "partial",
            1,
            2,
        )
        assert (
            coverage.issues[0].instrument_id == "security-1"
            and "2025-07-02" in coverage.issues[0].detail
        )
        request = feed.scope["request"]
        query = BarQuery(
            instrument_ids=("security-1",),
            timeframe="1d",
            start=request["start"],
            end=request["end"],
            as_of=datetime.now(UTC),
        )
        (bar,) = reader.bars(query)
        assert (bar.open, bar.volume, bar.turnover) == (
            Decimal("10.01"),
            Decimal("1250"),
            Decimal("13125"),
        )
        assert (bar.price_basis, bar.quote_currency, bar.quantity_unit) == ("raw", "CNY", "share")
        assert bar.available_at == bar.ingested_at
        assert not reader.bars(query.model_copy(update={"as_of": query.end}))
    assert all(
        TOKEN.encode() not in file.read_bytes() for file in feed.root.rglob("*") if file.is_file()
    )


def test_sync_respects_locked_selection_and_stages_before_publication(feed, tmp_path, capsys):
    args = ["--root", str(feed.root)]
    assert main([*args, "plan-http", *feed.options]) == 0
    plan = tmp_path / "plan.json"
    plan.write_text(capsys.readouterr().out)
    feed.dataset["params"] = {"series": "changed"}
    feed.source.write_text(json.dumps(feed.declaration))
    opts = [*feed.options, "--locked-plan", str(plan)]
    assert main([*args, "sync-http", *opts]) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "DATA_CONFLICT"
    assert not feed.calls and not feed.root.exists()
    del feed.dataset["params"]
    feed.source.write_text(json.dumps(feed.declaration))
    assert main([*args, "sync-http", *opts]) == 0
    candidate = tmp_path / "candidate.json"
    candidate.write_text(capsys.readouterr().out)
    with sqlite3.connect(feed.root / "index.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM versions").fetchone() == (0,)
    assert main([*args, "validate", "--candidate", str(candidate)]) == 0
    validated = json.loads(capsys.readouterr().out)
    assert main([*args, "publish", "--candidate", str(candidate)]) == 0
    version = json.loads(capsys.readouterr().out)
    assert validated == version
    assert main([*args, "snapshot", "--version", version["version_id"]]) == 0
    assert (
        json.loads(capsys.readouterr().out)["contents"]["datasets"][0]["version_id"]
        == version["version_id"]
    )
    assert len(feed.calls) == 1


@pytest.fixture
def factor_feed(feed):
    feed.dataset.update(
        research_profile="cn.equity.ex-factor.events.v1",
        batch=100,
        field_map={
            "code": "symbol",
            "day": "trade_date",
            "ratio": "ex_factor",
            "status": "event_status",
        },
    )
    feed.declaration["datasets"] = {"adj_factor": feed.dataset}
    feed.scope.pop("calendar_version")
    feed.scope.update(representation="cumulative_forward", baseline_date="2025-07-01")
    feed.scope["request"].update(dataset_id="factors", record_type="adjustment_factor")
    for key in ("price_basis", "timeframe"):
        feed.scope["request"].pop(key)
    for i in (2, 3):
        feed.scope["symbols"].append(
            {"instrument_id": f"security-{i}", "symbol": f"60000{i}.SH", "venue_id": "XSHG"}
        )
        feed.scope["request"]["instrument_ids"].append(f"security-{i}")
    feed.source.write_text(json.dumps(feed.declaration))
    feed.capture.write_text(json.dumps(feed.scope))
    return feed


def test_cli_declared_factor_store_precise_dates_empty_and_missing(factor_feed, capsys, tmp_path):
    f = factor_feed
    event = {"code": ROW["code"], "day": "2025-07-02", "ratio": "1.500000000001"}
    f.responses = [
        (
            200,
            {
                "rows": [
                    event,
                    {**event, "status": "event"},
                    {"code": "600002.SH", "status": "no_events"},
                ]
            },
        )
    ]
    prefix = ["--root", str(f.root)]
    assert main([*prefix, "plan-http-factors", *f.options]) == 0
    plan = capsys.readouterr().out
    assert not f.calls and TOKEN not in plan and not f.root.exists()
    locked = tmp_path / "plan.json"
    locked.write_text(plan)
    opts = [*f.options, "--locked-plan", str(locked)]
    assert main([*prefix, "import-http-factors", *opts]) == 0
    snapshot = json.loads(capsys.readouterr().out)["snapshot_id"]
    assert f.calls == [
        (
            "Bearer " + TOKEN,
            {
                "symbols": ["600000.SH,600002.SH,600003.SH"],
                "start_time": ["2025-07-01T00:00:00"],
                "end_time": ["2025-07-02T23:59:59.999999"],
            },
        )
    ]
    with DataEngine(f.root).open_reader(snapshot) as reader:
        contents = reader.datasets[0].contents
        assert (contents.source_ids, contents.knowledge_basis) == (
            ("example",),
            "latest_reconstruction",
        )
        cov = contents.coverage
        assert (cov.status, cov.completed_units, cov.requested_units, cov.row_count) == (
            "partial",
            2,
            3,
            3,
        )
        assert [(i.instrument_id, i.reason) for i in cov.issues] == [("security-3", "missing")]
        q = HistoryQuery(
            instrument_ids=tuple(f.scope["request"]["instrument_ids"]),
            effective_at=f.scope["request"]["end"],
            as_of=datetime.now(UTC),
        )
        assert {(p.instrument_id, p.value) for p in reader.factors(q)} == {
            ("security-1", Decimal(1)),
            ("security-1", Decimal("1.500000000001")),
            ("security-2", Decimal(1)),
        }
    f.dataset["batch"] = 2
    f.source.write_text(json.dumps(f.declaration))
    assert main([*prefix, "sync-http-factors", *opts]) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "DATA_CONFLICT" and len(f.calls) == 1


@pytest.mark.parametrize(
    ("rows", "code"),
    [
        ([{"code": "unexpected", "status": "no_events"}], "DATA_CONFLICT"),
        ([{"code": ROW["code"], "day": "20250702", "ratio": "2"}], "CONTRACT_INVALID"),
        ([{"code": ROW["code"], "day": "2025-07-02", "ratio": True}], "CONTRACT_INVALID"),
        ([{"code": ROW["code"], "day": "2025-07-02", "ratio": "NaN"}], "CONTRACT_INVALID"),
        ([{"code": ROW["code"], "status": "no_events", "ratio": "2"}], "CONTRACT_INVALID"),
        (
            [
                {"code": ROW["code"], "status": "no_events"},
                {"code": ROW["code"], "day": "2025-07-02", "ratio": 2},
            ],
            "DATA_CONFLICT",
        ),
        ([{"code": ROW["code"], "day": "2025-07-03", "ratio": 2}], "DATA_CONFLICT"),
    ],
)
def test_bad_http_factor_events_leave_no_version(factor_feed, capsys, rows, code):
    f = factor_feed
    f.responses = [(200, {"rows": rows})]
    assert main(["--root", str(f.root), "import-http-factors", *f.options]) == 1
    assert json.loads(capsys.readouterr().err)["code"] == code
    assert not list((f.root / "staging").iterdir())


def test_factor_source_correction_creates_new_snapshot_and_preserves_old_reader(
    factor_feed, capsys
):
    f = factor_feed
    row = {"code": ROW["code"], "day": "2025-07-02", "ratio": "2"}
    f.responses = [(200, {"rows": [row]}), (200, {"rows": [{**row, "ratio": "1.75"}]})]

    def capture():
        assert main(["--root", str(f.root), "import-http-factors", *f.options]) == 0
        return json.loads(capsys.readouterr().out)["snapshot_id"]

    engine = DataEngine(f.root)
    old = capture()
    with engine.open_reader(old) as first:
        new = capture()
        assert new != old
        with engine.open_reader(new) as second:
            q = HistoryQuery(
                instrument_ids=("security-1",),
                effective_at=f.scope["request"]["end"],
                as_of=datetime.now(UTC),
            )
            assert [v.value for v in first.factors(q)] == [1, 2]
            assert [v.value for v in second.factors(q)] == [1, Decimal("1.75")]
            assert not second.factors(
                q.model_copy(update={"as_of": first.factors(q)[-1].available_at})
            )


def test_http_factor_rate_limit_and_missing_profile(factor_feed, capsys):
    f = factor_feed
    f.responses = [(429, {"message": TOKEN})]
    assert main(["--root", str(f.root), "sync-http-factors", *f.options]) == 1
    err = capsys.readouterr().err
    assert TOKEN not in err and json.loads(err)["retry_after"] == 7
    del f.dataset["research_profile"]
    f.source.write_text(json.dumps(f.declaration))
    assert main(["--root", str(f.root), "plan-http-factors", *f.options]) == 1
    assert (
        json.loads(capsys.readouterr().err)["code"] == "UNSUPPORTED_CAPABILITY"
        and len(f.calls) == 1
    )


@pytest.mark.parametrize(
    "status, code",
    [
        (401, "AUTHENTICATION_FAILED"),
        (429, "RATE_LIMITED"),
        (502, "SOURCE_UNAVAILABLE"),
        (200, "CONTRACT_INVALID"),
    ],
)
def test_failed_second_page_leaves_no_candidate_or_snapshot(feed, capsys, status, code):
    feed.scope["symbols"].append(
        {"instrument_id": "security-2", "symbol": "600001.SH", "venue_id": "XSHG"}
    )
    feed.scope["request"]["instrument_ids"].append("security-2")
    feed.capture.write_text(json.dumps(feed.scope))
    feed.responses.append((status, {"error": TOKEN}))
    assert main(["--root", str(feed.root), "import-http", *feed.options]) == 1
    captured = capsys.readouterr()
    assert not captured.out and TOKEN not in captured.err
    error = json.loads(captured.err)
    assert error["code"] == code and error.get("retry_after") == (7 if status == 429 else None)
    assert len(feed.calls) == 2 and not list((feed.root / "staging").iterdir())
    with sqlite3.connect(feed.root / "index.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM snapshots").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM versions").fetchone() == (0,)


def test_credentials_and_provenance_fail_before_sending_request(feed, capsys, monkeypatch):
    monkeypatch.delenv(TOKEN_REF)
    args = ["--root", str(feed.root), "import-http", *feed.options]
    assert main(args) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "AUTHENTICATION_FAILED"
    feed.declaration["name"] = "another"
    feed.source.write_text(json.dumps(feed.declaration))
    assert main(args) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "DATA_CONFLICT"
    feed.declaration["name"] = "example"
    del feed.dataset["research_profile"]
    feed.source.write_text(json.dumps(feed.declaration))
    assert main(args) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "PRICE_BASIS_UNPROVEN"
    assert not feed.calls


def test_library_cancellation_releases_stage_and_can_retry_with_new_credentials(feed):
    from tivelo_contracts.errors import ErrorCode, TiveloError

    from tivelo_data.adapters.cn_daily import CNRawDailyRequest
    from tivelo_data.http.cn_daily import CNDailyProvider
    from tivelo_data.http.config import load_config
    from tivelo_data.providers import FixedSourceResolver, SyncContext

    feed.scope["symbols"].append(
        {"instrument_id": "security-2", "symbol": "600001.SH", "venue_id": "XSHG"}
    )
    feed.scope["request"]["instrument_ids"].append("security-2")
    capture = CNRawDailyRequest.model_validate(feed.scope)
    config = load_config(feed.source)
    source = CNDailyProvider(capture, config)
    engine = DataEngine(feed.root, FixedSourceResolver(source, source.configuration_digest))
    plan = engine.plan_sync(capture.request)
    # The caller can reload its mutable source declaration after locking the observation.
    config.datasets["daily"].field_map.clear()
    phases = []
    references = []

    def credentials(name):
        references.append(name)
        return {"token": "first" if len(references) == 1 else "second"}

    with pytest.raises(TiveloError) as error:
        engine.sync(
            plan,
            SyncContext(
                credentials, lambda: bool(phases), lambda n, phase: phases.append((n, phase))
            ),
        )
    assert error.value.code == ErrorCode.CANCELLED
    assert phases == [(1, "staging")] and not list((feed.root / "staging").iterdir())
    feed.responses.extend([(200, {"rows": [ROW]}), (200, {"rows": []})])
    candidate = engine.sync(plan, SyncContext(credentials, lambda: False, lambda *_: None))
    coverage = candidate.version.contents.coverage
    assert (coverage.status, coverage.requested_units, coverage.row_count) == ("partial", 4, 1)
    assert references == [TOKEN_REF] * 3
    assert [auth for auth, _ in feed.calls] == ["Bearer first", "Bearer second", "Bearer second"]


@pytest.mark.parametrize(
    "source",
    [
        "bad: [synthetic-provider-token",  # invalid YAML
        "name: example\ndatasets: []\nauth: [synthetic-provider-token]",  # invalid declaration
    ],
)
def test_malformed_declarations_only_return_public_errors(feed, source, capsys):
    feed.source.write_text(source)
    assert main(["--root", str(feed.root), "plan-http", *feed.options]) == 1
    captured = capsys.readouterr()
    assert not captured.out and TOKEN not in captured.err
    assert json.loads(captured.err)["code"] == "CONTRACT_INVALID"
    assert not feed.calls and not feed.root.exists()
