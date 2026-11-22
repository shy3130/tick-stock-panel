import json
import os
import threading
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from tivelo_contracts.data import BarQuery
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_data import DataEngine
from tivelo_data.providers import FixedSourceResolver, SyncContext, local_context

from app.api.engine_jobs import router
from app.config import settings
from app.data_providers.custom import loader
from app.data_providers.custom.config import (
    CN_RAW_DAILY_PROFILE,
    AuthConfig,
    CustomSourceConfig,
    DatasetConfig,
)
from app.data_providers.custom.provider import GenericHTTPProvider
from app.services import preferences
from app.services.engine_jobs.manager import EngineJobManager
from app.services.engine_jobs.models import JobRequest, ResearchNode
from app.services.engine_jobs.preparation_models import (
    CaptureRequest,
    DailySymbol,
    RoutedDailyInput,
)
from app.services.engine_jobs.routed_daily import open_daily_source
from app.services.engine_jobs.storage import JobStore

FIELDS = {
    key: key for key in ("symbol", "date", "open", "high", "low", "close", "volume", "amount")
}
ROW = dict(
    symbol="600000.SH",
    date="2025-07-01",
    open="10.01",
    high="11",
    low="10",
    close="10.5",
    volume="12.5",
    amount="13125",
)


def daily_config(url="http://test.invalid/daily", **kwargs):
    return DatasetConfig(url=url, response_path="data.rows", field_map=FIELDS, **kwargs)


@pytest.fixture
def checked_source():
    providers = []

    def build(handler, auth=None):
        source = GenericHTTPProvider(
            CustomSourceConfig("desk", "Desk", auth or AuthConfig(), {"daily": daily_config()})
        )
        source._client.close()
        source._client = httpx.Client(transport=httpx.MockTransport(handler))
        providers.append(source)
        return source

    yield build
    for source in providers:
        source.close()


def fetch(source):
    return source.get_daily_checked(
        ["600000.SH"], datetime(2025, 7, 1), datetime(2025, 7, 1, 23, 59, 59)
    )


@pytest.mark.parametrize("rows", [[], [ROW]])
def test_checked_daily_distinguishes_successful_empty(checked_source, rows):
    source = checked_source(lambda request: httpx.Response(200, json={"data": {"rows": rows}}))
    assert fetch(source).height == len(rows)


@pytest.mark.parametrize(
    ("status", "payload", "code"),
    [
        (401, {}, ErrorCode.AUTHENTICATION_FAILED),
        (403, {}, ErrorCode.AUTHENTICATION_FAILED),
        (429, {}, ErrorCode.RATE_LIMITED),
        (502, {}, ErrorCode.SOURCE_UNAVAILABLE),
        (200, {"error": "secret"}, ErrorCode.CONTRACT_INVALID),
        (200, {"data": {"rows": [ROW, 4]}}, ErrorCode.CONTRACT_INVALID),
        (200, {"data": {"rows": [dict(ROW, volume=None)]}}, ErrorCode.CONTRACT_INVALID),
        (200, {"data": {"rows": [dict(ROW, open="oops")]}}, ErrorCode.CONTRACT_INVALID),
    ],
)
def test_checked_daily_errors_are_not_empty(checked_source, status, payload, code):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, json=payload, headers={"retry-after": "11"})

    with pytest.raises(TiveloError) as error:
        fetch(checked_source(respond))
    assert error.value.code == code and len(calls) == 1
    assert error.value.retry_after == (11 if status == 429 else None)
    assert "secret" not in str(error.value) and "http" not in str(error.value)


def test_checked_daily_missing_credentials_fails_before_network(checked_source, monkeypatch):
    monkeypatch.setattr("app.data_providers.custom.provider._token_from_env", lambda name: None)
    source = checked_source(
        lambda request: pytest.fail("uncredentialed request"),
        AuthConfig("bearer", "SAMPLE_TEST_KEY"),
    )
    with pytest.raises(TiveloError) as error:
        fetch(source)
    assert error.value.code == ErrorCode.AUTHENTICATION_FAILED


def test_checked_daily_query_token_stays_out_of_logs(checked_source, monkeypatch, caplog):
    marker = "fixture-only-provider-credential"
    monkeypatch.setattr("app.data_providers.custom.provider._token_from_env", lambda name: marker)
    seen = []

    def respond(request):
        seen.append(request.url.params["token"])
        return httpx.Response(401, json={"message": marker})

    caplog.set_level("INFO", logger="httpx")
    with pytest.raises(TiveloError) as error:
        fetch(checked_source(respond, AuthConfig("query", "SAMPLE_TEST_KEY")))
    assert seen == [marker] and marker not in str(error.value) and marker not in caplog.text


@pytest.fixture
def feed(tmp_path, monkeypatch):
    state = SimpleNamespace(status=200, rows=[ROW], calls=[])

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state.calls.append(parse_qs(urlsplit(self.path).query))
            status, rows = (
                state.respond(state.calls[-1])
                if hasattr(state, "respond")
                else (state.status, state.rows)
            )
            body = {"data": getattr(state, "sdk_data", {"rows": rows})}
            if hasattr(state, "reply"):
                status, body = state.reply(urlsplit(self.path).path, state.calls[-1], self.headers)
            if urlsplit(self.path).path.startswith("/api/a-share/"):
                body["code"] = 0
            payload = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(loader, "_PROVIDERS", {})
    monkeypatch.setattr(loader, "_PLUGIN_STATUS", {})
    monkeypatch.setattr(loader, "_LOAD_ERRORS", [])
    monkeypatch.setattr(preferences, "_cache", None)
    monkeypatch.setattr(preferences, "_cache_sig", None)
    state.cfg = asdict(
        daily_config(
            f"http://127.0.0.1:{server.server_port}/bars",
            batch=1,
            params={"stream": "test-only-source-option"},
            research_profile=CN_RAW_DAILY_PROFILE,
        )
    )
    state.path = tmp_path / "data_sources" / "desk.yaml"
    state.path.parent.mkdir()

    def install():
        state.path.write_text(yaml.safe_dump({"name": "desk", "datasets": {"daily": state.cfg}}))
        loader.load_all(include_plugins=False)

    state.install = install
    install()
    preferences.save({"daily_data_provider": "desk"})
    state.store = JobStore(tmp_path)
    try:
        yield state
    finally:
        for handle in loader._PROVIDERS.values():
            handle.close()
        server.shutdown()
        server.server_close()
        thread.join()


def routed(symbols=("600000.SH",), start="2025-06-30T16:00:00Z", end="2025-07-02T16:00:00Z"):
    bindings = tuple(
        DailySymbol(
            instrument_id=f"security-{n}",
            symbol=sym,
            venue_id="XSHG" if sym.endswith(".SH") else "XSHE",
        )
        for n, sym in enumerate(symbols)
    )
    return RoutedDailyInput(
        kind="tsp_daily",
        expected_provider="desk",
        calendar_version="2026.1.0",
        symbols=bindings,
        request={
            "dataset_id": "raw-daily",
            "record_type": "bar",
            "market_id": "cn.equity",
            "asset_class": "equity",
            "timeframe": "1d",
            "price_basis": "raw",
            "instrument_ids": [item.instrument_id for item in bindings],
            "start": start,
            "end": end,
        },
    )


def data_plan(store, spec):
    provider = open_daily_source(store, spec)
    engine = DataEngine(
        store.path("data"), FixedSourceResolver(provider, provider.configuration_digest)
    )
    return provider, engine, engine.plan_sync(spec.request)


def factor_input(bars):
    from app.services.engine_jobs.preparation_models import RoutedFactorInput

    request = bars.request.model_dump(exclude={"timeframe", "price_basis"})
    request.update(dataset_id="factors", record_type="adjustment_factor")
    return RoutedFactorInput(
        kind="tsp_factors",
        expected_provider="tickflow",
        symbols=bars.symbols,
        representation="cumulative_forward",
        baseline_date="2025-07-01",
        request=request,
    )


def test_routed_factor_permission_and_source_change(feed):
    from app.services.engine_jobs.preparation import source_provider
    from app.tickflow import policy
    from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet

    spec = factor_input(routed())
    policy._persist(CapabilitySet({Cap.KLINE_DAILY_BATCH: CapabilityLimits(batch=10)}), "Free")
    with pytest.raises(TiveloError) as denied:
        source_provider(feed.store, spec)
    assert denied.value.code == ErrorCode.UNSUPPORTED_CAPABILITY
    preferences.save({"adj_factor_provider": "missing_plugin"})
    assert preferences.get_adj_factor_provider() == "tickflow"
    with pytest.raises(TiveloError) as changed:
        source_provider(feed.store, spec)
    assert changed.value.code == ErrorCode.DATA_CONFLICT
    with pytest.raises(TiveloError) as missing:
        source_provider(feed.store, spec.model_copy(update={"expected_provider": "missing_plugin"}))
    assert missing.value.code == ErrorCode.SOURCE_UNAVAILABLE
    assert not feed.calls


@pytest.mark.parametrize(
    "name, module, variable",
    [
        ("fuyao", "tivelo_data.fuyao_factors", "FUYAO_API_KEY"),
        ("tdxquant", "tivelo_data.tdx_factors", "TDXQUANT_API_KEY"),
    ],
)
def test_prior_data_release_keeps_bundled_daily_if_factor_adapter_is_missing(
    feed, monkeypatch, name, module, variable
):
    import sys

    from app.data_providers.custom.loader import load_bundled_manifest
    from app.services.engine_jobs.routed_daily import open_daily_source, open_factor_source

    monkeypatch.setenv(variable, "offline-compatibility")
    load_bundled_manifest(name)
    preferences.save({"adj_factor_provider": name, "daily_data_provider": name})
    bars = routed().model_copy(update={"expected_provider": name})
    factors = factor_input(bars).model_copy(update={"expected_provider": name})
    monkeypatch.setitem(sys.modules, module, None)
    with pytest.raises(TiveloError) as missing:
        open_factor_source(feed.store, factors)
    assert missing.value.code == ErrorCode.UNSUPPORTED_CAPABILITY
    daily = open_daily_source(feed.store, bars)
    try:
        assert daily.describe().provider_id == name and not feed.calls
    finally:
        daily.close()


@pytest.mark.parametrize("factor_source", ["tickflow", "dividends", "fuyao", "tdxquant"])
def test_different_daily_and_factor_sources_reach_both_free_views_through_workers(
    feed, monkeypatch, factor_source
):
    from tivelo_backtest.price_views import StaticPriceView
    from tivelo_backtest.snapshot import PortableSnapshotReader
    from tivelo_contracts.data import HistoryQuery
    from tivelo_contracts.execution import PriceViewSpec

    from app import secrets_store
    from app.services import heavy_job_limiter
    from app.tickflow import policy
    from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet

    monkeypatch.setattr(heavy_job_limiter, "_memory_admission_ok", lambda: True)
    bars = routed(end="2025-07-03T16:00:00Z")
    factors = factor_input(bars).model_copy(update={"expected_provider": factor_source})
    endpoint = feed.cfg["url"].removesuffix("/bars")
    paths_expected = ["/bars"]
    if factor_source in ("fuyao", "tdxquant"):
        from app.data_providers.custom.loader import load_bundled_manifest
        from app.services.engine_jobs.preparation import source_provider

        monkeypatch.syspath_prepend(
            str(Path(__file__).resolve().parents[2] / "packages/tivelo-data/tests")
        )
        from stock_factor_fixture import (
            DUMP,
            OBJECT,
            RAW,
            STOCK,
            parquet_bytes,
            raw_bars,
            tdx_replies,
        )

        if factor_source == "fuyao":
            from tivelo_data.fuyao_daily import FuyaoDailyConfig as Config
            from tivelo_data.fuyao_factors import FuyaoFactorProvider as Provider

            from app.backtest import worker

            monkeypatch.setenv("FUYAO_API_KEY", "local-fuyao-test")
            monkeypatch.setenv("TIVELO_TEST_FUYAO_LOOPBACK", endpoint)
            monkeypatch.setattr(worker, "_worker_entry", _loopback_fuyao_worker)
            paths_expected.extend((DUMP, OBJECT, RAW))
        else:
            from stock_history_fixture import install_tdx_transport
            from tivelo_data.tdx_daily import TdxQuantDailyConfig as Config
            from tivelo_data.tdx_factors import TdxQuantFactorProvider as Provider

            monkeypatch.setenv("TDXQUANT_API_KEY", "local-tdx-a")
            log, data = install_tdx_transport(feed.store.data_dir, monkeypatch, {})
            data.write_text(json.dumps(tdx_replies()))
        load_bundled_manifest(factor_source)
    elif factor_source == "tickflow":
        # An explicitly granted capability also works below the standard tier label.
        policy._persist(
            CapabilitySet({Cap.ADJ_FACTOR: CapabilityLimits(rpm=30, batch=50)}), "Free + adj_factor"
        )
        secrets_store.save(
            {"tickflow_base_url": endpoint, "tickflow_api_key": "fixture-factor-key"}
        )
        paths_expected.append("/v1/klines/ex-factors")
    else:
        from app.api.settings import CustomSourceIn

        factor_path = "/events"
        paths_expected.append(factor_path)
        path = feed.path.with_name("dividends.yaml")
        dataset = {
            "url": endpoint + factor_path,
            "field_map": {
                "code": "symbol",
                "day": "trade_date",
                "ratio": "ex_factor",
                "state": "event_status",
            },
            "response_path": "rows",
            "research_profile": "cn.equity.ex-factor.events.v1",
        }
        path.write_text(
            yaml.safe_dump({"name": factor_source, "datasets": {"adj_factor": dataset}})
        )
        loader.load_all(include_plugins=False)
        public = loader.get_config_dict(factor_source)
        loader.save_config(factor_source, CustomSourceIn.model_validate(public).model_dump())
        assert (
            yaml.safe_load(path.read_text())["datasets"]["adj_factor"]["research_profile"]
            == dataset["research_profile"]
        )
        loader.load_all(include_plugins=False)
    preferences.save({"adj_factor_provider": factor_source})
    if factor_source in ("fuyao", "tdxquant"):
        hosted = source_provider(feed.store, factors)
        standalone = Provider(factors, Config())
        assert hosted.describe() == standalone.describe()
        assert hosted.configuration_digest == standalone.configuration_digest
        hosted.close()
        standalone.close()
    paths = []

    def reply(path, params, headers):
        paths.append(path)
        if factor_source == "fuyao":
            if path == DUMP:
                assert headers.get("x-api-key") == "local-fuyao-test"
                return 200, {
                    "code": 0,
                    "data": {"presigned_url": endpoint + OBJECT + "?signature=fixture-signature"},
                }
            if path == OBJECT:
                assert not headers.get("x-api-key")
                return 200, parquet_bytes()
            if path == RAW:
                assert params["thscode"] == [STOCK] and params["adjust"] == ["none"]
                return 200, {
                    "code": 0,
                    "data": {
                        "thscode": STOCK,
                        "interval": "1d",
                        "adjust": "none",
                        "item": raw_bars(),
                    },
                }
        if path == "/v1/klines/ex-factors":
            assert headers.get("x-api-key") == "fixture-factor-key"
            return 200, {
                "data": {
                    "600000.SH": [
                        {"timestamp": 1751385600000, "ex_factor": 2},
                        {"timestamp": 1751472000000, "ex_factor": 1.25},
                    ]
                }
            }
        if path == "/events":
            assert not headers.get("x-api-key")
            return 200, {
                "rows": [
                    {"code": "600000.SH", "day": "2025-07-02", "ratio": 2},
                    {"code": "600000.SH", "day": "2025-07-03", "ratio": 1.25},
                ]
            }
        assert path == "/bars" and not headers.get("x-api-key")
        return 200, {
            "data": {
                "rows": [
                    dict(
                        ROW,
                        date=f"2025-07-0{day}",
                        **dict.fromkeys(("open", "high", "low", "close"), price),
                    )
                    for day, price in [(1, "10"), (2, "5.5"), (3, "4.8")]
                ]
            }
        }

    feed.reply = reply
    job = JobRequest(
        idempotency_key="two-routed-sources",
        nodes=(ResearchNode(node_id="prices", capture=CaptureRequest(datasets=(factors, bars))),),
    ).model_dump(mode="json")
    manager = EngineJobManager(feed.store.data_dir, autostart=False)
    app = FastAPI()
    app.include_router(router)
    app.state.engine_job_manager = manager
    root = "/api/engine-jobs"
    try:
        with TestClient(app) as api:
            preview = api.post(root + "/preview", json=job)
            assert preview.status_code == 200, preview.text
            assert (
                not paths
                and "fixture-factor-key" not in preview.text
                and "127.0.0.1" not in preview.text
            )
            result = api.post(
                root, params={"expected_plan_id": preview.json()["plan_id"]}, json=job
            )
            assert result.status_code == 200, result.text
            url = root + "/" + result.json()["job_id"]
            for _ in range(10):
                manager.run_once()
                state = api.get(url).json()
                if state["status"] in ("failed", "succeeded"):
                    break
            assert state["status"] == "succeeded", state
            assert all(
                n["attempts"][0]["worker"]["pid"] != os.getpid() for n in state["nodes"].values()
            )
            result = api.get(url + "/snapshots/prices")
            assert result.status_code == 200, result.text
            info = result.json()
            assert sorted(paths) == sorted(paths_expected)
            if factor_source == "tdxquant":
                calls = [json.loads(line) for line in log.read_text().splitlines()]
                assert len(calls) == 2 and calls[1][2][-2:] == ["none", 3]
                assert calls[0][0] == calls[1][0] and not Path(calls[0][1]).exists()
            assert all(ds["coverage"]["status"] == "complete" for ds in info["datasets"])
            with PortableSnapshotReader(
                feed.store.path(info["path"]), snapshot_id=info["snapshot_id"]
            ) as reader:
                reader.verify()
                now = datetime.now(UTC)
                scope = bars.request
                prices = tuple(
                    reader.bars(
                        BarQuery(
                            instrument_ids=scope.instrument_ids,
                            timeframe="1d",
                            start=scope.start,
                            end=scope.end,
                            as_of=now,
                        )
                    )
                )
                q = HistoryQuery(
                    instrument_ids=scope.instrument_ids, effective_at=scope.end, as_of=now
                )
                points = tuple(reader.factors(q))
                assert not reader.factors(q.model_copy(update={"as_of": scope.end}))
                assert [p.value for p in points] == [1, 2, Decimal("2.5")]
                assert {p.source_id for p in points} == {factor_source}
                assert {bar.source_id for bar in prices} == {"desk"}
                for mode, index, closes in (
                    ("qfq", -1, ["4", "4.4", "4.8"]),
                    ("hfq", 0, ["10", "11", "12"]),
                ):
                    view = StaticPriceView(mode)
                    spec = PriceViewSpec(
                        mode=mode,
                        provider_id=view.provider_id,
                        anchor_at=prices[index].bar_start,
                        factor_definition_id=points[0].definition_id,
                        factor_baseline_id=points[0].baseline_id,
                    )
                    projected = view.project(prices, points, spec, decision_at=now)
                    assert [p.close for p in projected] == list(map(Decimal, closes))
                    assert [(p.volume, p.turnover) for p in projected] == [
                        (p.volume, p.turnover) for p in prices
                    ]
            if factor_source == "dividends":
                legacy = loader.get_provider("dividends").get_adj_factors(
                    ["600000.SH"], datetime(2025, 7, 1), datetime(2025, 7, 3), asset_type="stock"
                )
                assert legacy["ex_factor"].to_list() == [2, 1.25]
    finally:
        assert manager.shutdown()


def test_missing_plugin_cannot_be_replaced_by_tickflow_in_a_frozen_capture(feed):
    preferences.save({"daily_data_provider": "temporarily_missing_plugin"})
    assert preferences.get_daily_data_provider() == "tickflow"
    for expected, code in (
        ("tickflow", ErrorCode.DATA_CONFLICT),
        ("temporarily_missing_plugin", ErrorCode.SOURCE_UNAVAILABLE),
    ):
        with pytest.raises(TiveloError) as error:
            open_daily_source(
                feed.store, routed().model_copy(update={"expected_provider": expected})
            )
        assert error.value.code == code
    assert not feed.calls


def test_standalone_and_workbench_lock_the_same_source_declaration(feed, tmp_path, capsys):
    from tivelo_contracts.base import canonical_bytes
    from tivelo_data.cli import main

    spec = routed()
    provider, _, hosted_plan = data_plan(feed.store, spec)
    provider.close()
    capture = tmp_path / "capture.json"
    capture.write_text(spec.model_dump_json(exclude={"kind"}))
    root = tmp_path / "standalone"
    assert (
        main(
            [
                "--root",
                str(root),
                "plan-http",
                "--capture",
                str(capture),
                "--source",
                str(feed.path),
            ]
        )
        == 0
    )
    assert capsys.readouterr().out.rstrip().encode() == canonical_bytes(hosted_plan)
    assert not root.exists() and not feed.calls


def test_routed_daily_preserves_market_units_visibility_and_gaps(feed):
    spec = routed()
    _, engine, plan = data_plan(feed.store, spec)
    before = datetime.now(UTC)
    candidate = engine.sync(plan)
    after = datetime.now(UTC)
    version = engine.publish(candidate)
    coverage = version.contents.coverage
    assert (coverage.status, coverage.requested_units, coverage.completed_units) == (
        "partial",
        2,
        1,
    )
    assert coverage.issues[0].reason == "missing"
    assert "2025-07-02" in coverage.issues[0].detail
    assert version.contents.knowledge_basis == "latest_reconstruction"
    assert len(feed.calls) == 1 and feed.calls[0]["symbols"] == ["600000.SH"]
    assert feed.calls[0]["start_time"] == ["2025-07-01T00:00:00"]
    assert feed.calls[0]["end_time"] == ["2025-07-02T23:59:59.999999"]
    snapshot = engine.create_snapshot((version.version_id,))
    with engine.open_reader(snapshot.snapshot_id) as reader:
        query = BarQuery(
            instrument_ids=spec.request.instrument_ids,
            timeframe="1d",
            start=spec.request.start,
            end=spec.request.end,
            as_of=after,
        )
        (bar,) = reader.bars(query)
        assert reader.bars(query.model_copy(update={"as_of": spec.request.end})) == ()
    assert (bar.instrument_id, bar.open, bar.volume, bar.turnover, bar.quote_currency) == (
        "security-0",
        Decimal("10.01"),
        Decimal("1250"),
        Decimal("13125"),
        "CNY",
    )
    assert bar.available_at == bar.ingested_at and before <= bar.ingested_at <= after
    assert (bar.bar_start.hour, bar.bar_start.minute, bar.bar_end.hour, bar.bar_end.minute) == (
        1,
        30,
        7,
        0,
    )


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [ROW, ROW],
        [dict(ROW, symbol="000001.SZ")],
        [dict(ROW, date="2025-07-06")],
        [dict(ROW, low="0")],
    ],
)
def test_routed_daily_empty_and_invalid_observations(feed, rows):
    feed.rows = rows
    _, engine, plan = data_plan(feed.store, routed())
    if rows:
        with pytest.raises(TiveloError):
            engine.sync(plan)
    else:
        coverage = engine.sync(plan).version.contents.coverage
        assert coverage.status == "empty" and coverage.issues[0].reason == "missing"


def test_source_change_and_cancellation_before_network(feed):
    spec = routed()
    provider, _, original = data_plan(feed.store, spec)
    provider.close()
    feed.cfg["params"]["stream"] = "updated"
    feed.install()
    provider, engine, changed = data_plan(feed.store, spec)
    try:
        assert original.plan_id != changed.plan_id
        with pytest.raises(TiveloError) as error:
            engine.sync(original)
        assert error.value.code == ErrorCode.DATA_CONFLICT
        with pytest.raises(TiveloError) as error:
            engine.sync(
                changed,
                SyncContext(local_context().resolve_credentials, lambda: True, lambda *_: None),
            )
        assert error.value.code == ErrorCode.CANCELLED
    finally:
        provider.close()
    feed.cfg.pop("research_profile")
    feed.install()
    with pytest.raises(TiveloError) as error:
        open_daily_source(feed.store, spec)
    assert error.value.code == ErrorCode.PRICE_BASIS_UNPROVEN
    preferences.save({"daily_data_provider": "tickflow"})
    with pytest.raises(TiveloError) as error:
        open_daily_source(feed.store, spec)
    assert error.value.code == ErrorCode.DATA_CONFLICT
    assert not feed.calls


def _loopback_fuyao_worker(task, events, cancel):
    from unittest.mock import patch

    from tivelo_data.legacy.fuyao_history import BASE_URL

    from app.backtest.worker import _worker_entry

    client = httpx.Client

    def local_client(*args, **kwargs):
        if kwargs.get("base_url") == BASE_URL:
            kwargs["base_url"] = os.environ["TIVELO_TEST_FUYAO_LOOPBACK"]
        return client(*args, **kwargs)

    with patch.object(httpx, "Client", local_client):
        _worker_entry(task, events, cancel)


@pytest.mark.parametrize("source_name", ["desk", "tickflow", "fuyao", "tdxquant"])
def test_capture_through_api_spawn_and_export(feed, monkeypatch, source_name):
    import app.services.heavy_job_limiter as limiter

    monkeypatch.setattr(limiter, "_memory_admission_ok", lambda: True)
    manager = EngineJobManager(feed.store.data_dir, autostart=False)
    root = "/api/engine-jobs"
    app = FastAPI()
    app.include_router(router)
    app.state.engine_job_manager = manager
    spec = routed()
    expected_volume = Decimal("1250")
    if source_name == "tickflow":
        from test_source_packages import CODE, bundle, install
        from tivelo_data.tickflow_daily import TickFlowDailyConfig, TickFlowDailyProvider

        from app.data_providers.custom import packages
        from app.services.engine_jobs.preparation import source_provider

        preferences.save({"daily_data_provider": source_name})
        endpoint = feed.cfg["url"].removesuffix("/bars")
        monkeypatch.setattr(settings, "tickflow_api_key", "")
        monkeypatch.setenv("TICKFLOW_API_KEY", "")
        monkeypatch.setenv("TICKFLOW_FREE_BASE_URL", endpoint)
        spec = spec.model_copy(update={"expected_provider": source_name})
        standalone = TickFlowDailyProvider(spec, TickFlowDailyConfig(endpoint=endpoint))
        hosted = source_provider(feed.store, spec)
        assert standalone.configuration_digest == hosted.configuration_digest
        assert standalone.describe() == hosted.describe()
        feed.sdk_data = {
            "600000.SH": {
                "timestamp": [1751299200000],
                "open": [10.01],
                "high": [11],
                "low": [10],
                "close": [10.5],
                "volume": [12],
                "amount": [12600],
            }
        }
        expected_volume = Decimal("1200")
        # A separate, already-installed v1 plugin has an upgrade pending a TSP restart.
        load_log = feed.store.data_dir / "provider-imports.txt"
        tracked = (
            "from pathlib import Path\n"
            f"with Path({str(load_log)!r}).open('a') as f: f.write('loaded\\n')\n"
        ) + CODE
        install(bundle(code=tracked))
        install(bundle("2.0.0", code=tracked))
        previous_loads = load_log.read_text()
        installed = packages.read_state("demo_source")
        assert installed["active"] == "1.0.0" and installed["pending"] == "2.0.0"
    elif source_name in ("fuyao", "tdxquant"):
        from app.data_providers.custom.loader import load_bundled_manifest
        from app.services.engine_jobs.preparation import source_provider

        if source_name == "fuyao":
            from tivelo_data.fuyao_daily import FuyaoDailyConfig as Config
            from tivelo_data.fuyao_daily import FuyaoDailyProvider as Provider

            from app.backtest import worker

            monkeypatch.setenv("FUYAO_API_KEY", "local-fuyao-test")
            monkeypatch.setenv("TIVELO_TEST_FUYAO_LOOPBACK", feed.cfg["url"].removesuffix("/bars"))
            monkeypatch.setattr(worker, "_worker_entry", _loopback_fuyao_worker)
            feed.sdk_data = {
                "interval": "1d",
                "adjust": "none",
                "item": [
                    {
                        "thscode": "600000.SH",
                        "item": [
                            {
                                "date_ms": 1751299200000,
                                "open_price": 10,
                                "high_price": 11,
                                "low_price": 9,
                                "close_price": 10.5,
                                "volume": 1207,
                                "turnover": 12673.5001,
                            }
                        ],
                    }
                ],
            }
        else:
            from tivelo_data.tdx_daily import TdxQuantDailyConfig as Config
            from tivelo_data.tdx_daily import TdxQuantDailyProvider as Provider

            monkeypatch.syspath_prepend(
                str(Path(__file__).resolve().parents[2] / "packages/tivelo-data/tests")
            )
            from stock_history_fixture import install_tdx_transport

            monkeypatch.setenv("TDXQUANT_API_KEY", "local-tdx-a")
            columns = {
                "Date": [20250701],
                "Open": [10],
                "High": [11],
                "Low": [9],
                "Close": [10.5],
                "Volume": [1207],
                "Amount": [1.26735001],
            }
            tdx_log, _ = install_tdx_transport(feed.store.data_dir, monkeypatch, columns)
        load_bundled_manifest(source_name)
        preferences.save({"daily_data_provider": source_name})
        spec = spec.model_copy(update={"expected_provider": source_name})
        hosted = source_provider(feed.store, spec)
        standalone = Provider(spec, Config())
        assert standalone.configuration_digest == hosted.configuration_digest
        assert standalone.describe() == hosted.describe()
        expected_volume = Decimal("1207")
    job = JobRequest(
        idempotency_key="observation-1",
        nodes=(ResearchNode(node_id="prices", capture=CaptureRequest(datasets=(spec,))),),
    ).model_dump(mode="json")
    try:
        with TestClient(app) as api:
            preview = api.post(root + "/preview", json=job)
            assert preview.status_code == 200, preview.text
            assert not feed.calls and "test-only-source-option" not in preview.text
            plan = preview.json()
            assert plan["plan"]["plan_version"] == "1.2"
            submitted = api.post(root, params={"expected_plan_id": plan["plan_id"]}, json=job)
            assert submitted.status_code == 200, submitted.text
            job_url = root + "/" + submitted.json()["job_id"]
            for _ in range(5):
                manager.run_once()
                state = api.get(job_url).json()
                if state["status"] in ("failed", "succeeded"):
                    break
            assert state["status"] == "succeeded", state
            assert all(
                item["attempts"][0]["worker"]["pid"] != os.getpid()
                for item in state["nodes"].values()
            )
            result = api.get(job_url + "/snapshots/prices")
            assert result.status_code == 200, result.text
            info = result.json()
            assert info["datasets"][0]["knowledge_basis"] == "latest_reconstruction"
            assert info["datasets"][0]["coverage"]["status"] == "partial"
            assert info["snapshot_id"] != info["bundle_id"]
            from tivelo_backtest.snapshot import PortableSnapshotReader

            with PortableSnapshotReader(
                feed.store.path(info["path"]), snapshot_id=info["snapshot_id"]
            ) as reader:
                reader.verify()
                prices = reader.bars(
                    BarQuery(
                        instrument_ids=spec.request.instrument_ids,
                        timeframe="1d",
                        start=spec.request.start,
                        end=spec.request.end,
                        as_of=datetime.now(UTC),
                    )
                )
                assert len(prices) == 1 and prices[0].volume == expected_volume
                assert prices[0].source_id == source_name
                if source_name in ("fuyao", "tdxquant"):
                    assert prices[0].turnover == Decimal("12673.5001")
            assert (
                json.loads((feed.store.path(info["path"]) / "bundle.json").read_bytes())[
                    "snapshot"
                ]["snapshot_id"]
                == info["snapshot_id"]
            )
            assert api.get(job_url + "/reports/prices").status_code == 404
            if source_name == "tickflow":
                assert packages.read_state("demo_source") == installed
                assert load_log.read_text() == previous_loads
            assert api.get(job_url + "/snapshots/impostor").status_code == 404
            assert api.post(root, json=job).json()["job_id"] == plan["plan_id"]
            if source_name == "tdxquant":
                entries = tdx_log.read_text().splitlines()
                assert len(entries) == 1 and not feed.calls
                native_pid, sdk_dir, args = json.loads(entries[0])
                assert native_pid not in {
                    os.getpid(),
                    *(n["attempts"][0]["worker"]["pid"] for n in state["nodes"].values()),
                }
                assert not Path(sdk_dir).exists() and args == [
                    "600000.SH",
                    "",
                    "2025-07-02 00:00:00",
                    "1d",
                    "none",
                    2,
                ]
            else:
                assert len(feed.calls) == 1
    finally:
        assert manager.shutdown()


def test_capture_rejects_conflicting_modes_and_symbol_venue():
    spec = routed()
    with pytest.raises(ValidationError):
        ResearchNode(
            node_id="prices", capture=CaptureRequest(datasets=(spec,)), snapshot="old", run="old"
        )
    with pytest.raises(ValidationError):
        DailySymbol(instrument_id="id", symbol="000001.SZ", venue_id="XSHG")


def test_capture_does_not_publish_a_failed_second_page_and_cancels_between_pages(feed):
    from threading import Event

    spec = routed(symbols=("600000.SH", "600001.SH"), end="2025-07-01T16:00:00Z")
    feed.respond = lambda query: (200, [ROW]) if query["symbols"] == ["600000.SH"] else (502, [])
    _, engine, plan = data_plan(feed.store, spec)
    with pytest.raises(TiveloError) as error:
        engine.sync(plan)
    assert error.value.code == ErrorCode.SOURCE_UNAVAILABLE and len(feed.calls) == 2
    assert not list(engine.store.path("dataset_versions").glob("*/manifest.json"))
    feed.calls.clear()
    cancelled = Event()
    _, engine, plan = data_plan(feed.store, spec)
    context = SyncContext(
        local_context().resolve_credentials,
        cancelled.is_set,
        lambda count, phase: cancelled.set() if phase == "staging" else None,
    )
    with pytest.raises(TiveloError) as error:
        engine.sync(plan, context)
    assert error.value.code == ErrorCode.CANCELLED and len(feed.calls) == 1


def test_capture_pages_consume_source_rpm_and_count_every_symbol_session(feed, monkeypatch):
    from app.data_providers.minute_stream import RequestPacer

    waits = []
    monkeypatch.setattr(
        RequestPacer, "wait", lambda self, scope, rpm, checkpoint: waits.append((scope, rpm))
    )
    feed.cfg["rpm"] = 20
    feed.install()
    feed.respond = lambda query: (200, [dict(ROW, symbol=query["symbols"][0])])
    _, engine, plan = data_plan(
        feed.store, routed(symbols=("600000.SH", "000001.SZ"), end="2025-07-01T16:00:00Z")
    )
    result = engine.sync(plan).version.contents
    assert result.coverage.status == "complete" and result.coverage.completed_units == 2
    assert waits == [("daily", 20), ("daily", 20)]
    assert {request["symbols"][0] for request in feed.calls} == {"600000.SH", "000001.SZ"}


def test_weekend_source_coverage_and_unfinished_session(feed, monkeypatch):
    _, engine, plan = data_plan(
        feed.store, routed(start="2025-07-04T16:00:00Z", end="2025-07-06T16:00:00Z")
    )
    coverage = engine.sync(plan).version.contents.coverage
    assert coverage.status == "empty" and coverage.issues[0].reason == "no_trades"

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2025, 7, 1, 4, tzinfo=UTC)

    monkeypatch.setattr("app.services.engine_jobs.routed_daily.datetime", Clock)
    _, engine, plan = data_plan(feed.store, routed(end="2025-07-01T16:00:00Z"))
    with pytest.raises(TiveloError) as error:
        engine.sync(plan)
    assert error.value.code == ErrorCode.FUTURE_INFORMATION and not feed.calls


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2025-07-01T01:31:00Z", "2025-07-01T07:00:00Z"),
        ("2025-07-01T01:30:00Z", "2025-07-01T06:59:59Z"),
    ],
)
def test_intraday_window_cannot_be_published_as_a_complete_daily_session(feed, start, end):
    _, engine, plan = data_plan(feed.store, routed(start=start, end=end))
    with pytest.raises(TiveloError) as error:
        engine.sync(plan)
    assert error.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT and not feed.calls


@pytest.mark.parametrize("factors", [False, True])
def test_client_cannot_upgrade_source_provenance_or_start_strict_run(feed, factors):
    from app.services.engine_jobs.artifacts import freeze_request
    from app.services.engine_jobs.preparation_models import PreparationRequest
    from app.services.engine_jobs.storage import JobError

    source = factor_input(routed()) if factors else routed()
    with pytest.raises(ValidationError):
        type(source).model_validate(
            {**source.model_dump(), "knowledge_basis": "historical_versions"}
        )
    prepared = PreparationRequest(
        datasets=(source,),
        definition="strategy",
        run={
            "universe": ["security-0"],
            "start": "2025-07-01",
            "end": "2025-07-02",
            "initial_capital": "10000",
            "account": {"currency": "CNY"},
            "market": {"adapter_id": "cn.equity", "distribution": "tivelo-market-cn"},
            "price_view": {"mode": "raw", "provider_id": "builtin"},
        },
    )
    request = JobRequest(
        idempotency_key="strict", nodes=(ResearchNode(node_id="research", prepare=prepared),)
    )
    with pytest.raises(JobError) as error:
        freeze_request(feed.store, request)
    assert error.value.code == "FUTURE_INFORMATION" and not feed.calls


def test_profile_edit_round_trips_through_settings_without_disclosing_static_request_data(feed):
    from app.api.settings import CustomSourceIn

    public = loader.get_config_dict("desk")
    assert "test-only-source-option" not in json.dumps(public)
    public["datasets"]["daily"]["research_profile"] = None
    loader.save_config("desk", CustomSourceIn.model_validate(public).model_dump())
    saved = yaml.safe_load(feed.path.read_text())["datasets"]["daily"]
    assert "research_profile" not in saved and saved["params"] == feed.cfg["params"]
    public["datasets"]["daily"]["research_profile"] = CN_RAW_DAILY_PROFILE
    loader.save_config("desk", CustomSourceIn.model_validate(public).model_dump())
    loader.load_all(include_plugins=False)
    provider, _, _ = data_plan(feed.store, routed())
    provider.close()


def test_new_observation_needed_only_for_unsealed_live_sync(feed):
    from test_engine_jobs import drain, inline_worker

    from app.services.engine_jobs.storage import JobError

    source = routed()
    request = JobRequest(
        idempotency_key="failed-observation",
        nodes=(ResearchNode(node_id="daily", capture=CaptureRequest(datasets=(source,))),),
    )
    feed.status = 401
    manager = EngineJobManager(feed.store.data_dir, worker_runner=inline_worker, autostart=False)
    try:
        job_id = manager.submit(request)["job_id"]
        assert drain(manager, job_id)["status"] == "failed"
        feed.status = 200
        with pytest.raises(JobError) as error:
            manager.retry(job_id)
        assert error.value.code == "DATA_CONFLICT" and len(feed.calls) == 1
        new = manager.submit(request.model_copy(update={"idempotency_key": "new-observation"}))
        assert drain(manager, new["job_id"])["status"] == "succeeded" and len(feed.calls) == 2
    finally:
        assert manager.shutdown()


def test_retry_after_publication_failure_reuses_sealed_facts(feed):
    from test_engine_jobs import drain, inline_worker

    from app.backtest.worker import BacktestWorkerError
    from app.services.engine_jobs.models import stages

    fail = True

    def runner(task, *args, **kwargs):
        nonlocal fail
        if task["node_id"] == "prices.publish_00" and fail:
            fail = False
            raise BacktestWorkerError("test publication interruption", code="RESOURCE_LIMIT")
        return inline_worker(task, *args, **kwargs)

    capture = CaptureRequest(datasets=(routed(),))
    request = JobRequest(
        idempotency_key="reuse-capture", nodes=(ResearchNode(node_id="prices", capture=capture),)
    )
    manager = EngineJobManager(feed.store.data_dir, worker_runner=runner, autostart=False)
    try:
        job_id = manager.submit(request)["job_id"]
        first = drain(manager, job_id)
        assert first["nodes"]["prices.sync_00"]["status"] == "succeeded"
        assert first["status"] == "failed" and len(feed.calls) == 1
        preferences.save({"daily_data_provider": "tickflow"})
        manager.retry(job_id)
        done = drain(manager, job_id)
        assert done["status"] == "succeeded" and len(feed.calls) == 1
        assert (
            first["nodes"]["prices.sync_00"]["artifact"]
            == done["nodes"]["prices.sync_00"]["artifact"]
        )
        locked = manager.store.plan(job_id)
        downstream = ResearchNode(
            node_id="research", run="run", snapshot="snapshot", depends_on=("prices",)
        )
        from app.services.engine_jobs.models import LockedInput

        archived = LockedInput(
            request=downstream,
            run_archive_id="0" * 64,
            lock_id="1" * 64,
            snapshot_id="2" * 64,
            bundle_id="3" * 64,
        )
        graph = locked.model_copy(
            update={
                "request": request.model_copy(update={"nodes": (*request.nodes, downstream)}),
                "inputs": (*locked.inputs, archived),
            }
        )
        assert stages(graph)["research.validate"][2] == ("prices.snapshot",)
    finally:
        assert manager.shutdown()
