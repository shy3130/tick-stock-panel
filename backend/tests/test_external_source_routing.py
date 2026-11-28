"""Exercise all seven workbench data routes through a real v1 ZIP across process restarts."""

import io
import json
import multiprocessing
import threading
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from zipfile import ZipFile

import yaml

NAME = "example_market_routes"
SYMBOL = "600000.SH"
ENTRY = """from pathlib import Path
from app.data_providers.custom.config import load_config
from app.data_providers.custom.provider import GenericHTTPProvider

def create():
    return GenericHTTPProvider(load_config(Path(__file__).with_name("source.yaml")))
"""


def _sample(release, day):
    price = {1: 10, 2: 12}[release]
    quote = dict(symbol=SYMBOL, open=price, high=price, low=price, volume=15)
    bar = {**quote, "close": price, "amount": price * 1500}
    minute = {**bar, "datetime": f"{day} 09:35:00"}
    return {
        "daily": {**bar, "date": day},
        "adj_factor": dict(symbol=SYMBOL, trade_date=day, ex_factor=1 + release / 8),
        "realtime": {**quote, "last_price": price, "prev_close": price - 1},
        "minute": minute,
        "full_minute": minute,
        "financial": dict(symbol=SYMBOL, eps=release / 2),
        "depth5": dict(
            symbol=SYMBOL,
            asks=[[price + level / 100, level * 10] for level in range(1, 6)],
            bids=[[price - level / 100, level * 20] for level in range(1, 6)],
        ),
    }


def _package(release, endpoint, day, *, invalid=False):
    samples = _sample(1, day)
    manifest = dict(
        name=NAME,
        version=f"{release}.0.0",
        plugin_api_version=1,
        runtime="none",
        entry="provider:create",
        datasets=list(samples),
    )
    datasets = {
        dataset: dict(
            url=f"{endpoint}/{release}/{dataset}",
            response_path="rows",
            field_map={field: field for field in row},
        )
        for dataset, row in samples.items()
    }
    out = io.BytesIO()
    with ZipFile(out, "w") as archive:
        archive.writestr("plugin.yaml", yaml.safe_dump(manifest))
        archive.writestr("source.yaml", yaml.safe_dump(dict(name=NAME, datasets=datasets)))
        archive.writestr(
            "provider.py",
            "def create():\n    raise ValueError('sample initialization failure')\n"
            if invalid
            else ENTRY,
        )
    return out.getvalue()


def _json(response):
    assert response.status_code == 200, response.text
    return response.json()


def _install(client, package):
    files = {"file": ("market-source.zip", package)}
    base = "/api/custom/source-packages"
    checked = _json(client.post(base + "/inspect", files=files))
    assert not checked["errors"]
    _json(
        client.post(
            base + "/install",
            files=files,
            data={
                "digest": checked["digest"],
                "revision": checked["revision"],
            },
        )
    )


def _verify(client, release, day):
    from app.data_providers.capabilities import resolve_capability_sources
    from app.data_providers.custom import loader

    samples = _sample(release, day)
    capabilities = _json(client.get("/api/settings/capability-matrix"))["capabilities"]
    assert {c["id"] for c in capabilities} == samples.keys()
    assert all(c["usable"] and c["effective"] == NAME for c in capabilities)
    routes = resolve_capability_sources()
    assert {row["id"]: row["source"] for row in routes if row["usable"]} == dict.fromkeys(
        samples, NAME
    )
    for dataset, expected in samples.items():
        preview = _json(
            client.post(
                "/api/settings/data-sources/test",
                json={
                    "provider": NAME,
                    "dataset": dataset,
                    "symbols": [SYMBOL],
                },
            )
        )
        assert preview["provider"] == NAME and preview["dataset"] == dataset
        assert preview["rows"] == 1 and preview["preview"] == [expected]
    provider = loader.get_provider(NAME)
    start = datetime.combine(date.fromisoformat(day), time.min)
    end = datetime.combine(start.date(), time.max)
    assert provider.get_daily([SYMBOL], start, end)["close"].item() == samples["daily"]["close"]
    assert (
        provider.get_adj_factors([SYMBOL], start, end)["ex_factor"].item()
        == samples["adj_factor"]["ex_factor"]
    )
    assert provider.get_minute([SYMBOL], start, end)["close"].item() == samples["minute"]["close"]
    assert provider.get_intraday_batch([SYMBOL])["close"].item() == samples["full_minute"]["close"]
    assert provider.get_realtime()[0]["last_price"] == samples["realtime"]["last_price"]
    assert provider.get_depth_batch([SYMBOL])[SYMBOL]["bids"] == samples["depth5"]["bids"]
    assert provider.get_financials("metrics", [SYMBOL])["eps"].item() == samples["financial"]["eps"]


def _session(stage, endpoint, day):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.settings import router as settings_router
    from app.custom.source_packages.routes import router as packages_router
    from app.data_providers.custom import packages

    packages.discover()
    app = FastAPI()
    app.include_router(settings_router)
    app.include_router(packages_router)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 40000)) as client:
        route_url = "/api/settings/preferences/data-providers"
        if stage == "install":
            _install(client, _package(1, endpoint, day))
            caps = _json(client.get("/api/settings/capability-matrix"))["capabilities"]
            choices = {c["field"]: NAME for c in caps}
            assert _json(client.put(route_url, json=choices)) == choices
            separate = {**choices, "financial_data_provider": "tickflow"}
            assert (
                _json(client.put(route_url, json={"financial_data_provider": "tickflow"}))
                == separate
            )
            assert _json(client.put(route_url, json={"financial_data_provider": NAME})) == choices
            _verify(client, 1, day)
            _install(client, _package(2, endpoint, day))
            assert packages.read_state(NAME)["pending"] == "2.0.0"
            _verify(client, 1, day)
        elif stage == "upgrade":
            _verify(client, 2, day)
            _json(
                client.post(
                    f"/api/custom/source-packages/{NAME}/manage", json={"action": "rollback"}
                )
            )
            _verify(client, 2, day)
        elif stage == "rollback":
            _verify(client, 1, day)
            _install(client, _package(3, endpoint, day, invalid=True))
        else:
            assert stage == "failed_upgrade" and packages.read_state(NAME)["error"]
            _verify(client, 1, day)


def test_all_declared_routes_survive_cold_upgrades_and_rollback(tmp_path, monkeypatch):
    from zoneinfo import ZoneInfo

    day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    calls = Counter()

    class Source(BaseHTTPRequestHandler):
        def do_GET(self):
            _, version, dataset = urlsplit(self.path).path.split("/")
            response = json.dumps({"rows": [_sample(int(version), day)[dataset]]}).encode()
            calls[version, dataset] += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *_):
            pass

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "workbench"))
    monkeypatch.setenv("TICKFLOW_API_KEY", "")
    with ThreadingHTTPServer(("127.0.0.1", 0), Source) as source:
        worker = threading.Thread(target=source.serve_forever, daemon=True)
        worker.start()
        try:
            endpoint = f"http://127.0.0.1:{source.server_port}"
            for stage in ("install", "upgrade", "rollback", "failed_upgrade"):
                with ProcessPoolExecutor(
                    1, mp_context=multiprocessing.get_context("spawn")
                ) as pool:
                    pool.submit(_session, stage, endpoint, day).result(timeout=60)
        finally:
            source.shutdown()
            worker.join()
    # Four checks against release 1, two against release 2, for every dataset.
    # Each check makes one settings probe and one request through the provider method.
    assert calls == {(v, ds): n for v, n in (("1", 8), ("2", 4)) for ds in _sample(1, day)}
