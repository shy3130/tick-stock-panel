"""Public CLI tests for two original TSP suppliers with no live vendor accounts."""

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from tivelo_contracts.data import BarQuery
from tivelo_contracts.errors import ErrorCode, TiveloError

from tivelo_data import DataEngine
from tivelo_data.cli import main

pytest.importorskip("tivelo_market_cn")

SYMBOL = "600000.SH"
STAMP = 1751299200000
START = "2025-06-30T16:00:00Z"
END = "2025-07-02T16:00:00Z"
OHLC = {"open_price": 10, "high_price": 11, "low_price": 9, "close_price": 10.5}
BAR = {**OHLC, "date_ms": STAMP, "volume": 1207, "turnover": 12673.5001}


def capture(vendor, symbols=(SYMBOL,)):
    ids = [f"stock-{i}" for i in range(len(symbols))]
    return {
        "calendar_version": "2026.1.0",
        "expected_provider": vendor,
        "symbols": [
            {"instrument_id": i, "symbol": s, "venue_id": "XSHG"}
            for i, s in zip(ids, symbols, strict=True)
        ],
        "request": {
            "dataset_id": "history",
            "market_id": "cn.equity",
            "asset_class": "equity",
            "record_type": "bar",
            "timeframe": "1d",
            "price_basis": "raw",
            "instrument_ids": ids,
            "start": START,
            "end": END,
        },
    }


def run(root, command, source, scope, *extra):
    request = root.parent / (root.name + "-capture.json")
    declaration = root.parent / (root.name + "-source.json")
    request.write_text(json.dumps(scope))
    declaration.write_text(json.dumps(source))
    return main(
        [
            "--root",
            str(root),
            command,
            "--capture",
            str(request),
            "--source",
            str(declaration),
            *extra,
        ]
    )


def read(root, snapshot):
    with DataEngine(root).open_reader(snapshot["snapshot_id"]) as reader:
        (dataset,) = reader.datasets
        assert dataset.contents.knowledge_basis == "latest_reconstruction"
        q = BarQuery(
            instrument_ids=("stock-0",),
            timeframe="1d",
            start=START,
            end=END,
            as_of=datetime.now(UTC),
        )
        assert not reader.bars(q.model_copy(update={"as_of": datetime(2025, 7, 2, tzinfo=UTC)}))
        (bar,) = reader.bars(q)
        assert (
            bar.price_basis == "raw"
            and bar.quantity_unit == "share"
            and bar.quote_currency == "CNY"
        )
        assert bar.available_at == bar.ingested_at > bar.bar_end
        return dataset.contents.coverage, bar


@pytest.fixture
def fuyao(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    state = SimpleNamespace(
        calls=[],
        root=tmp_path / "data",
        scope=capture("fuyao"),
        batch=(
            200,
            {
                "code": 0,
                "data": {
                    "interval": "1d",
                    "adjust": "none",
                    "item": [{"thscode": SYMBOL, "item": [BAR]}],
                },
            },
        ),
        single=(200, {"code": 1002, "message": "Unknown thscode: 600001.SH"}),
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            state.calls.append((url.path, parse_qs(url.query), self.headers.get("X-api-key")))
            status, body = state.batch if url.path.endswith("-batch") else state.single
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Retry-After", "7")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    state.config = {
        "endpoint": f"http://127.0.0.1:{server.server_port}",
        "credential_ref": "FIXTURE_FUYAO",
        "rpm": 60000,
    }
    monkeypatch.setenv("FIXTURE_FUYAO", "local-test")
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_fuyao_locked_plan_replay_and_absent_group(fuyao, tmp_path, capsys):
    f = fuyao
    f.scope = capture("fuyao", (SYMBOL, "600001.SH"))
    assert run(f.root, "plan-fuyao", f.config, f.scope) == 0
    plan = capsys.readouterr().out
    assert not f.calls and not f.root.exists() and "local-test" not in plan
    lock = tmp_path / "plan.json"
    lock.write_text(plan)
    assert run(f.root, "import-fuyao", f.config, f.scope, "--locked-plan", str(lock)) == 0
    snapshot = json.loads(capsys.readouterr().out)
    coverage, bar = read(f.root, snapshot)
    assert bar.source_id == "fuyao" and bar.volume == 1207 and bar.turnover == Decimal("12673.5001")
    assert (coverage.status, coverage.requested_units, coverage.completed_units) == (
        "partial",
        4,
        1,
    )
    assert [x[0] for x in f.calls] == [
        "/api/a-share/prices/historical-batch",
        "/api/a-share/prices/historical",
    ]
    assert all(
        q["adjust"] == ["none"]
        and q["start"] == [str(STAMP)]
        and q["end"] == [str(STAMP + 86400000)]
        and key == "local-test"
        for _, q, key in f.calls
    )
    assert f.calls[1][1]["thscode"] == ["600001.SH"]
    assert (
        run(f.root, "sync-fuyao", {**f.config, "rpm": 1}, f.scope, "--locked-plan", str(lock)) == 1
    )
    assert json.loads(capsys.readouterr().err)["code"] == "DATA_CONFLICT" and len(f.calls) == 2
    assert read(f.root, snapshot)[1] == bar


def test_replaced_adapter_release_rejects_saved_plan_before_fetch(
    fuyao, capsys, tmp_path, monkeypatch
):
    from tivelo_data.adapters import stock_history

    f = fuyao
    assert run(f.root, "plan-fuyao", f.config, f.scope) == 0
    lock = tmp_path / "release-plan.json"
    lock.write_text(capsys.readouterr().out)
    installed = stock_history.version
    monkeypatch.setattr(
        stock_history,
        "version",
        lambda name: "0.1.0a2" if name == "tivelo-data" else installed(name),
    )
    assert run(f.root, "import-fuyao", f.config, f.scope, "--locked-plan", str(lock)) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "DATA_CONFLICT" and not f.calls


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (401, {}, "AUTHENTICATION_FAILED"),
        (429, {}, "RATE_LIMITED"),
        (503, {}, "SOURCE_UNAVAILABLE"),
        (200, {"code": 4001}, "RATE_LIMITED"),
        (200, {"data": {}}, "CONTRACT_INVALID"),
        (
            200,
            {"code": 0, "data": {"interval": "1d", "adjust": "forward", "item": []}},
            "CONTRACT_INVALID",
        ),
        (200, {"code": 1002, "message": "Invalid start"}, "CONTRACT_INVALID"),
    ],
)
def test_fuyao_failure_does_not_publish_or_retry(fuyao, capsys, status, body, code):
    f = fuyao
    f.batch = status, body
    assert run(f.root, "import-fuyao", f.config, f.scope) == 1
    report = json.loads(capsys.readouterr().err)
    assert report["code"] == code and len(f.calls) == 1 and not list(f.root.rglob("manifest.json"))


def test_fuyao_single_endpoint_evidence_and_unknown_groups(fuyao, capsys):
    f = fuyao
    f.batch = 404, {}
    f.single = (
        200,
        {"code": 0, "data": {"thscode": SYMBOL, "adjust": "none", "interval": "1d", "item": [BAR]}},
    )
    assert run(f.root, "import-fuyao", f.config, f.scope) == 0
    assert read(f.root, json.loads(capsys.readouterr().out))[1].volume == 1207
    f.single[1]["data"].pop("adjust")
    assert run(f.root, "import-fuyao", f.config, f.scope) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "CONTRACT_INVALID"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"volume": 1.5}, "CONTRACT_INVALID"),
        ({"date_ms": STAMP + 1}, "DATA_CONFLICT"),
        ({"high_price": 2}, "CONTRACT_INVALID"),
        ({"thscode": "600099.SH"}, "DATA_CONFLICT"),
    ],
)
def test_fuyao_raw_facts_require_whole_shares_ohlc_and_requested_session(
    fuyao, capsys, change, code
):
    f = fuyao
    f.batch[1]["data"]["item"][0]["item"] = [{**BAR, **change}]
    assert run(f.root, "import-fuyao", f.config, f.scope) == 1
    assert json.loads(capsys.readouterr().err)["code"] == code


def test_missing_credentials_and_no_session_never_fetch(fuyao, capsys, monkeypatch):
    f = fuyao
    monkeypatch.delenv("FIXTURE_FUYAO")
    assert run(f.root, "import-fuyao", f.config, f.scope) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "AUTHENTICATION_FAILED"
    f.scope["request"].update(start="2025-07-04T16:00:00Z", end="2025-07-06T16:00:00Z")
    assert run(f.root, "import-fuyao", f.config, f.scope) == 0
    assert not f.calls


COLUMNS = {
    "Date": [20250701],
    "Open": [10],
    "High": [11],
    "Low": [9],
    "Close": [10.5],
    "Volume": [1207],
    "Amount": [1.26735001],
}


@pytest.fixture
def tdx(tmp_path, monkeypatch):
    pytest.importorskip("tdxaidata")
    from stock_history_fixture import install_tdx_transport

    request_log, data = install_tdx_transport(tmp_path, monkeypatch, COLUMNS)
    monkeypatch.setenv("UNRELATED_VENDOR_TEST_SECRET", "out-of-scope")
    monkeypatch.setenv("TDX_AI_DATA_LIB", "ambient-path-must-stay")
    monkeypatch.setenv("FIXTURE_TDX", "local-tdx-a")
    return SimpleNamespace(
        root=tmp_path / "data",
        log=request_log,
        data=data,
        scope=capture("tdxquant"),
        config={"credential_ref": "FIXTURE_TDX", "rpm": 60000, "timeout_seconds": 15},
    )


def test_tdx_real_sdk_arguments_isolated_and_exact_replay(tdx, tmp_path, capsys):
    t = tdx
    assert run(t.root, "plan-tdxquant", t.config, t.scope) == 0
    lock = tmp_path / "plan.json"
    lock.write_text(capsys.readouterr().out)
    assert not t.log.exists() and not t.root.exists() and "local-tdx-a" not in lock.read_text()
    assert run(t.root, "import-tdxquant", t.config, t.scope, "--locked-plan", str(lock)) == 0
    snapshot = json.loads(capsys.readouterr().out)
    coverage, bar = read(t.root, snapshot)
    assert (bar.source_id, bar.volume, bar.turnover) == (
        "tdxquant",
        1207,
        Decimal("12673.50010000"),
    )
    assert (coverage.status, coverage.requested_units, coverage.completed_units) == (
        "partial",
        2,
        1,
    )
    pid, library_root, args = json.loads(t.log.read_text())
    assert pid != os.getpid() and not Path(library_root).exists()
    assert args == [SYMBOL, "", "2025-07-02 00:00:00", "1d", "none", 2]
    assert os.environ["TDX_AI_DATA_LIB"] == "ambient-path-must-stay"
    assert (
        run(t.root, "sync-tdxquant", {**t.config, "rpm": 1}, t.scope, "--locked-plan", str(lock))
        == 1
    )
    assert json.loads(capsys.readouterr().err)["code"] == "DATA_CONFLICT"
    assert len(t.log.read_text().splitlines()) == 1 and read(t.root, snapshot)[1] == bar


@pytest.mark.parametrize(
    ("response", "code"),
    [
        ({"ErrorId": 7, "Error": "tokenKey invalid local-tdx-a"}, "AUTHENTICATION_FAILED"),
        ({"ErrorId": 9, "Error": "service unavailable"}, "SOURCE_UNAVAILABLE"),
        ({"ErrorId": 3, "Error": "Too many requests"}, "RATE_LIMITED"),
        ({"ErrorId": 0, "Value": {}}, "CONTRACT_INVALID"),
        ({"ErrorId": 0, "Value": {**COLUMNS, "Amount": []}}, "CONTRACT_INVALID"),
        ({"ErrorId": 0, "Value": {**COLUMNS, "Date": [20250703]}}, "DATA_CONFLICT"),
    ],
)
def test_tdx_errors_never_appear_as_empty(tdx, capsys, response, code):
    t = tdx
    t.data.write_text(json.dumps(response))
    assert run(t.root, "import-tdxquant", t.config, t.scope) == 1
    report = capsys.readouterr().err
    assert json.loads(report)["code"] == code and "local-tdx-a" not in report
    assert not Path(json.loads(t.log.read_text())[1]).exists()


def test_tdx_concurrent_credentials_have_separate_native_singletons(tdx, tmp_path):
    from tivelo_data.adapters.cn_daily import CNRawDailyRequest
    from tivelo_data.providers import local_context
    from tivelo_data.tdx_daily import TdxQuantDailyConfig, TdxQuantDailyProvider

    def observe(key):
        source = CNRawDailyRequest.model_validate(tdx.scope)
        provider = TdxQuantDailyProvider(
            source, TdxQuantDailyConfig(**tdx.config), resolve_token=lambda _: key
        )
        try:
            provider.check_ready(local_context())
            return provider.fetch(source.request, None, local_context()).records[0].volume
        finally:
            provider.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(observe, ("local-tdx-a", "local-tdx-b"))) == [1207, 1319]
    log = [json.loads(line) for line in tdx.log.read_text().splitlines()]
    assert len({pid for pid, _, _ in log}) == len({path for _, path, _ in log}) == 2
    assert all(not Path(path).exists() for _, path, _ in log)


def test_tdx_cancels_active_native_request(tdx):
    from tivelo_data.adapters.cn_daily import CNRawDailyRequest
    from tivelo_data.providers import SyncContext, local_context
    from tivelo_data.tdx_daily import TdxQuantDailyConfig, TdxQuantDailyProvider

    tdx.data.write_text('{"block": true}')
    source = CNRawDailyRequest.model_validate(tdx.scope)
    provider = TdxQuantDailyProvider(
        source, TdxQuantDailyConfig(**tdx.config), resolve_token=lambda _: "local-tdx-a"
    )
    context = SyncContext(lambda _: {}, tdx.log.exists, lambda *_: None)
    try:
        provider.check_ready(local_context())
        with pytest.raises(TiveloError) as caught:
            provider.fetch(source.request, None, context)
        assert caught.value.code == ErrorCode.CANCELLED
    finally:
        provider.close()
    assert not Path(json.loads(tdx.log.read_text())[1]).exists()


def test_tdx_count_mode_discards_older_trades_and_accepts_explicit_empty():
    from tivelo_data.tdx_daily import _daily_rows

    days = [SimpleNamespace(trading_date=date(2025, 7, 1))]
    assert _daily_rows(json.dumps({**COLUMNS, "Date": [20250630]}), SYMBOL, days) == []
    assert _daily_rows(json.dumps({k: [] for k in COLUMNS}), SYMBOL, days) == []


def test_supplier_wheel_identity_includes_modules_inside_frozen_python_archive(monkeypatch):
    from tivelo_data.adapters import stock_history

    entries = [
        "sdk/__init__.py,sha256=bundled-python,10",
        "sdk/lib/vendor.so,sha256=bundled-native,15",
        "sdk/token.ini,sha256=private-settings,3",
        "sdk-1.dist-info/RECORD,,",
    ]
    release = SimpleNamespace(
        files=(), read_text=lambda name: "\n".join(entries) if name == "RECORD" else None
    )
    monkeypatch.setattr(stock_history, "distribution", lambda name: release)
    assert stock_history._supplier_artifacts("sdk") == [
        ("sdk/__init__.py", "sha256", "bundled-python"),
        ("sdk/lib/vendor.so", "sha256", "bundled-native"),
    ]
    entries[0] = "sdk/__init__.py,sha256=replaced,10"
    assert stock_history._supplier_artifacts("sdk")[0][-1] == "replaced"
    entries[0] = "sdk/__init__.py,,10"
    with pytest.raises(TiveloError) as exc:
        stock_history._supplier_artifacts("sdk")
    assert exc.value.code == ErrorCode.RUNTIME_MISMATCH
