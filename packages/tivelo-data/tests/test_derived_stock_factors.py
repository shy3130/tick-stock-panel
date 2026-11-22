"""Observed cash, bonus and rights terms from the two original stock suppliers."""

from datetime import date
from decimal import Decimal

import pytest
from tivelo_contracts.errors import ErrorCode, TiveloError

pytest.importorskip("tivelo_market_cn")


def test_cash_bonus_rights_and_half_cent_are_derived_before_ratio_rounding():
    from tivelo_data.adapters.cn_ex_rights import event_ratios, terms

    first, second, third = map(date.fromisoformat, ("2025-07-01", "2025-07-02", "2025-07-03"))
    actions = {
        second: [terms("0.005", 0, 0, 0)],
        third: [terms(0, "0.1", 0, 0), terms(0, 0, "0.1", 4)],
    }
    assert event_ratios("600000.SH", actions, {first: 10, second: 10, third: 9}) == {
        second: Decimal(1),
        third: Decimal("1.153402537486"),
    }


@pytest.mark.parametrize("missing", ["before", "effective"])
def test_known_event_without_observed_raw_price_has_location(missing):
    from tivelo_data.adapters.cn_ex_rights import event_ratios, terms

    a, b = date(2025, 7, 1), date(2025, 7, 2)
    with pytest.raises(TiveloError) as exc:
        event_ratios(
            "600000.SH", {b: [terms(1, 0, 0, 0)]}, {b: 9} if missing == "before" else {a: 10}
        )
    assert exc.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT
    assert "600000.SH" in str(exc.value) and b.isoformat() in str(exc.value)


def factor_capture(vendor, symbols):
    return {
        "expected_provider": vendor,
        "representation": "cumulative_forward",
        "baseline_date": "2025-07-01",
        "symbols": [
            {
                "symbol": s,
                "instrument_id": f"stock-{i}",
                "venue_id": "XSHG" if s.endswith(".SH") else "XSHE",
            }
            for i, s in enumerate(symbols)
        ],
        "request": {
            "dataset_id": "factors",
            "market_id": "cn.equity",
            "asset_class": "equity",
            "record_type": "adjustment_factor",
            "instrument_ids": [f"stock-{i}" for i in range(len(symbols))],
            "start": "2025-06-30T16:00:00Z",
            "end": "2025-07-03T16:00:00Z",
        },
    }


@pytest.fixture
def wire(request, tmp_path, monkeypatch):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace
    from urllib.parse import parse_qs, urlsplit

    from stock_factor_fixture import (
        DUMP,
        EMPTY,
        EVENTS,
        MISSING,
        OBJECT,
        RAW,
        STOCK,
        parquet_bytes,
        raw_bars,
        tdx_replies,
    )
    from test_stock_history_sources import run

    vendor = request.param
    config = {"rpm": 60000}
    scope = factor_capture(vendor, (STOCK, EMPTY, MISSING))
    root = tmp_path / "data"
    state = SimpleNamespace(
        calls=[],
        root=root,
        scope=scope,
        config=config,
        vendor=vendor,
        key="local-tdx-a" if vendor == "tdxquant" else "local-fuyao-test",
    )
    monkeypatch.setenv("TDXQUANT_API_KEY" if vendor == "tdxquant" else "FUYAO_API_KEY", state.key)
    state.run = lambda op, *rest: run(root, f"{op}-{vendor}-factors", config, scope, *rest)
    if vendor == "tdxquant":
        pytest.importorskip("tdxaidata")
        from stock_history_fixture import install_tdx_transport

        state.log, state.data = install_tdx_transport(tmp_path, monkeypatch, {})
        state.replies = tdx_replies()
        state.data.write_text(json.dumps(state.replies))
        yield state
        return
    pytest.importorskip("httpx")
    state.archive, state.bars = parquet_bytes(), raw_bars()
    state.release = OBJECT
    state.dump_status = 200
    state.object_status = 200
    state.missing = {"code": 1002, "message": "Unknown thscode: " + MISSING}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            u = urlsplit(self.path)
            params = parse_qs(u.query)
            state.calls.append((u.path, params, self.headers.get("X-api-key")))
            status = 200
            if u.path == DUMP:
                status = state.dump_status
                body = {
                    "code": 0,
                    "data": {
                        "presigned_url": config["endpoint"]
                        + state.release
                        + "?signature=synthetic-object-marker"
                    },
                }
            elif u.path == state.release:
                status = state.object_status
                body = state.archive
            elif u.path == RAW:
                symbol = params["thscode"][0]
                bars = [
                    b
                    for b in state.bars
                    if int(params["start"][0]) <= b["date_ms"] <= int(params["end"][0])
                ]
                body = {
                    "code": 0,
                    "data": {
                        "thscode": symbol,
                        "adjust": getattr(state, "adjust", "none"),
                        "interval": "1d",
                        "item": bars,
                    },
                }
            elif u.path == EVENTS:
                body = state.missing
            else:
                status, body = 404, {}
            payload = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    config["endpoint"] = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("wire", ["fuyao", "tdxquant"], indirect=True)
@pytest.mark.parametrize("representation", ["event_ratio", "cumulative_forward"])
def test_independent_plan_import_and_observed_coverage(
    wire, tmp_path, capsys, caplog, representation
):
    import json
    from datetime import UTC, datetime
    from pathlib import Path

    from stock_factor_fixture import DUMP, EVENTS, OBJECT, RAW, STOCK
    from tivelo_contracts.data import HistoryQuery

    from tivelo_data import DataEngine

    w = wire
    w.scope["representation"] = representation
    if representation == "event_ratio":
        w.scope.pop("baseline_date")
    caplog.set_level("INFO", logger="httpx")
    assert w.run("plan") == 0
    plan = capsys.readouterr().out
    assert not w.calls and not w.root.exists() and (w.vendor != "tdxquant" or not w.log.exists())
    assert w.key not in plan and "ex-terms.cent-halfup.ratio-dec12" in plan
    lock = tmp_path / "plan.json"
    lock.write_text(plan)
    before = datetime.now(UTC)
    assert w.run("import", "--locked-plan", str(lock)) == 0
    snapshot = json.loads(capsys.readouterr().out)["snapshot_id"]
    after = datetime.now(UTC)
    with DataEngine(w.root).open_reader(snapshot) as reader:
        ds = reader.datasets[0].contents
        assert ds.knowledge_basis == "latest_reconstruction"
        assert (ds.coverage.status, ds.coverage.completed_units, ds.coverage.requested_units) == (
            "partial",
            2,
            3,
        )
        assert [(i.instrument_id, i.reason) for i in ds.coverage.issues] == [("stock-2", "missing")]
        query = HistoryQuery(
            instrument_ids=("stock-0", "stock-1", "stock-2"),
            effective_at=w.scope["request"]["end"],
            as_of=after,
        )
        assert not reader.factors(query.model_copy(update={"as_of": before}))
        facts = reader.factors(query)
        expected = (
            [2, Decimal("1.25")] if representation == "event_ratio" else [1, 2, Decimal("2.5")]
        )
        assert [f.value for f in facts if f.instrument_id == "stock-0"] == expected
        assert [f.value for f in facts if f.instrument_id == "stock-1"] == (
            [] if representation == "event_ratio" else [1]
        )
        assert all(before <= f.available_at <= after and f.source_id == w.vendor for f in facts)
        if w.vendor == "fuyao":
            import hashlib

            assert all(hashlib.sha256(w.archive).hexdigest() in f.revision_id for f in facts)
    if w.vendor == "fuyao":
        assert [p for p, _, _ in w.calls] == [DUMP, OBJECT, RAW, EVENTS]
        assert [key for _, _, key in w.calls] == [w.key, None, w.key, w.key]
        assert w.calls[2][1]["adjust"] == ["none"]
        assert "synthetic-object-marker" not in caplog.text
    else:
        calls = [json.loads(line) for line in w.log.read_text().splitlines()]
        assert len({p for p, _, _ in calls}) == 1 and all(not Path(d).exists() for _, d, _ in calls)
        assert calls[0][2] == [STOCK, "2025-07-01 00:00:00", "2025-07-03 00:00:00"]
        assert calls[1][2] == [STOCK, "", "2025-07-03 00:00:00", "1d", "none", 3]
        assert len(calls) == 4


@pytest.mark.parametrize("wire", ["fuyao", "tdxquant"], indirect=True)
@pytest.mark.parametrize(
    "failure",
    ["credentials", "rate", "price_gap", "wrong_close", "bad_rights", "duplicate", "bad_wire"],
)
def test_source_failure_never_publishes_a_partial_factor_chain(wire, capsys, monkeypatch, failure):
    import json

    from stock_factor_fixture import STOCK, archive_rows, parquet_bytes

    w = wire
    if failure == "credentials":
        monkeypatch.delenv("TDXQUANT_API_KEY" if w.vendor == "tdxquant" else "FUYAO_API_KEY")
    if w.vendor == "fuyao":
        if failure == "rate":
            w.object_status = 429
        if failure == "price_gap":
            w.bars = w.bars[1:]
        if failure == "wrong_close":
            w.bars[1].update(open_price=11, high_price=11, close_price=11)
        if failure in ("bad_rights", "duplicate"):
            rows = archive_rows()
            if failure == "bad_rights":
                rows[1]["allotment_price"] = 0
            else:
                rows.append(rows[0])
            w.archive = parquet_bytes(rows)
        if failure == "bad_wire":
            w.adjust = "forward"
    else:
        events = w.replies["replies"]["events"]
        data = w.replies["replies"]["history"][STOCK]["Value"]
        if failure == "rate":
            events[STOCK] = {"ErrorId": 429, "Error": "too many requests, local-tdx-a"}
        if failure == "price_gap":
            for k in data:
                data[k] = data[k][1:]
        if failure == "wrong_close":
            for key in ("Open", "High", "Close"):
                data[key][1] = 11
        if failure == "bad_rights":
            events[STOCK]["Value"][-1][1] = 0
        if failure == "duplicate":
            for field in ("Date", "Type", "Value"):
                events[STOCK][field].append(events[STOCK][field][-1])
        if failure == "bad_wire":
            events[STOCK]["Value"] = []
        w.data.write_text(json.dumps(w.replies))
    assert w.run("import") == 1
    error = json.loads(capsys.readouterr().err)
    assert (
        error["code"]
        == {
            "credentials": "AUTHENTICATION_FAILED",
            "rate": "RATE_LIMITED",
            "price_gap": "DATA_COVERAGE_INSUFFICIENT",
            "wrong_close": "DATA_CONFLICT",
            "bad_rights": "CONTRACT_INVALID",
            "duplicate": "DATA_CONFLICT",
            "bad_wire": "CONTRACT_INVALID",
        }[failure]
    )
    if failure == "price_gap":
        assert "2025-07-02" in error["message"] and STOCK in error["message"]
    assert w.key not in str(error) and "synthetic-object-marker" not in str(error)
    assert not list((w.root / "snapshots").glob("*.json"))
    if failure == "credentials":
        assert not w.calls and (w.vendor != "tdxquant" or not w.log.exists())


@pytest.mark.parametrize("wire", ["fuyao"], indirect=True)
@pytest.mark.parametrize(
    "case",
    [
        "old_release",
        "unknown_release",
        "truncated_dump",
        "empty_file",
        "wrong_currency",
        "empty_confirmed",
        "conflicting_sources",
    ],
)
def test_fuyao_full_market_release_and_per_security_confirmation(wire, capsys, case):
    import json

    from stock_factor_fixture import MISSING, archive_rows, parquet_bytes

    from tivelo_data import DataEngine

    w = wire
    if case == "old_release":
        w.release = "/releases/20250702/events.parquet"
    elif case == "unknown_release":
        w.release = "/events.parquet"
    elif case == "truncated_dump":
        w.archive = w.archive[:-4]
    elif case == "empty_file":
        w.archive = parquet_bytes([])
    elif case == "wrong_currency":
        rows = archive_rows()
        rows[0]["currency"] = "USD"
        w.archive = parquet_bytes(rows)
    else:
        w.missing = {
            "code": 0,
            "data": {
                "thscode": MISSING,
                "item": [] if case == "empty_confirmed" else [{"dividend_per_share": 0.2}],
            },
        }
    if case == "empty_confirmed":
        assert w.run("import") == 0
        with DataEngine(w.root).open_reader(
            json.loads(capsys.readouterr().out)["snapshot_id"]
        ) as reader:
            assert reader.datasets[0].contents.coverage.completed_units == 3
    else:
        assert w.run("import") == 1
        code = json.loads(capsys.readouterr().err)["code"]
        assert code == {
            "old_release": "DATA_COVERAGE_INSUFFICIENT",
            "wrong_currency": "DATA_CONFLICT",
            "conflicting_sources": "DATA_CONFLICT",
        }.get(case, "CONTRACT_INVALID")
        if case in ("old_release", "unknown_release"):
            assert len(w.calls) == 1


def test_reconstructed_ratio_is_independent_of_decimal_context_and_allows_consolidation():
    from datetime import date
    from decimal import localcontext

    from tivelo_data.adapters.cn_ex_rights import event_ratios, terms

    a, b = date(2025, 7, 1), date(2025, 7, 2)
    with localcontext() as ctx:
        ctx.prec = 3
        assert event_ratios("600000.SH", {b: [terms("0.025", "-0.5", 0, 0)]}, {a: 10, b: 20})[
            b
        ] == Decimal("0.501253132832")


@pytest.mark.parametrize("wire", ["fuyao", "tdxquant"], indirect=True)
def test_first_ex_date_can_follow_a_long_halt(wire, capsys):
    import copy
    import json

    from stock_factor_fixture import DAY_MS, STOCK

    from tivelo_data import DataEngine

    w = wire
    if w.vendor == "fuyao":
        w.bars[0]["date_ms"] -= 90 * DAY_MS
    else:
        history = w.replies["replies"]["history"]
        current = history[STOCK]
        earlier = copy.deepcopy(current)
        for key, column in earlier["Value"].items():
            earlier["Value"][key] = [20250401] if key == "Date" else column[:1]
        # The one earlier row in the initial count is a suspended placeholder.
        for key in ("Open", "High", "Volume", "Amount"):
            current["Value"][key][0] = 0
        history[STOCK] = {"by_end": {"2025-07-03": current, "2025-07-01": earlier}}
        w.data.write_text(json.dumps(w.replies))
    assert w.run("import") == 0
    with DataEngine(w.root).open_reader(
        json.loads(capsys.readouterr().out)["snapshot_id"]
    ) as reader:
        assert reader.datasets[0].contents.coverage.row_count == 4
    if w.vendor == "tdxquant":
        calls = [json.loads(line) for line in w.log.read_text().splitlines()]
        assert calls[2][2][-2:] == ["none", 2600]
    else:
        from stock_factor_fixture import RAW

        assert len([call for call in w.calls if call[0] == RAW]) == 2
