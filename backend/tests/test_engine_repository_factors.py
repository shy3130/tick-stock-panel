import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tivelo_backtest.legacy.simulator import LegacySimulator, MatcherConfig
from tivelo_backtest.price_views import StaticPriceView
from tivelo_backtest.snapshot import PortableSnapshotReader
from tivelo_contracts.data import BarQuery, HistoryQuery
from tivelo_contracts.execution import PriceViewSpec
from tivelo_data.cli import main as data_cli

from app.api.engine_jobs import router
from app.indicators.pipeline import _apply_adj_factor
from app.services.engine_jobs.manager import EngineJobManager
from app.services.kline_sync import _merge_adj_factor_store
from app.tickflow.repository import DataStore, KlineRepository


def test_repository_writers_to_research_worker_and_both_free_backtest_views(
    tmp_path, monkeypatch, capsys
):
    import app.services.heavy_job_limiter as limiter

    monkeypatch.setattr(limiter, "_memory_admission_ok", lambda: True)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    dates = [date(2025, 7, n) for n in (1, 2, 3)]
    raw = pl.DataFrame(
        {
            "date": dates,
            "symbol": ["600000.SH"] * 3,
            **{key: [10.0, 5.5, 4.8] for key in ("open", "high", "low", "close")},
            "volume": [10.0, 20.0, 30.0],
            "amount": [10000.0, 11000.0, 14400.0],
        }
    )
    factors = pl.DataFrame(
        {"symbol": ["600000.SH"] * 2, "trade_date": dates[1:], "ex_factor": [2.0, 1.25]}
    )
    repo = KlineRepository(DataStore(tmp_path))
    try:
        repo.append_daily(raw)
        assert _merge_adj_factor_store(tmp_path / "adj_factor/all.parquet", factors) == (
            2,
            ["600000.SH"],
        )
    finally:
        repo.db.close()
    symbols = [{"instrument_id": "security-A", "symbol": "600000.SH", "venue_id": "XSHG"}]
    common = dict(
        market_id="cn.equity",
        asset_class="equity",
        instrument_ids=["security-A"],
        start="2025-07-01T00:00:00+08:00",
        end="2025-07-04T00:00:00+08:00",
    )
    inputs = [
        dict(
            kind="tsp_repository_factors",
            layout="event_factors_v1",
            representation="cumulative_forward",
            baseline_date="2025-07-01",
            expected_provider="archive",
            symbols=symbols,
            request=dict(common, dataset_id="factors", record_type="adjustment_factor"),
        ),
        dict(
            kind="tsp_repository_daily",
            layout="daily_raw_v1",
            calendar_version="2026.1.0",
            expected_provider="archive",
            symbols=symbols,
            request=dict(
                common, dataset_id="raw", record_type="bar", timeframe="1d", price_basis="raw"
            ),
        ),
    ]
    manager = EngineJobManager(tmp_path, autostart=False)
    app = FastAPI()
    app.state.engine_job_manager = manager
    app.include_router(router)
    endpoint = "/api/engine-jobs"
    job = {
        "idempotency_key": "archive-prices",
        "nodes": [{"node_id": "prices", "capture": {"datasets": inputs}}],
    }
    try:
        with TestClient(app) as api:
            strict = dict(
                datasets=[inputs[0]],
                definition="strategy",
                run={
                    "universe": ["security-A"],
                    "start": "2025-07-01",
                    "end": "2025-07-03",
                    "initial_capital": "10000",
                    "account": {"currency": "CNY"},
                    "market": {"adapter_id": "cn.equity", "distribution": "tivelo-market-cn"},
                    "price_view": {"mode": "raw", "provider_id": "builtin"},
                },
            )
            rejected = api.post(
                endpoint + "/preview",
                json={
                    "idempotency_key": "strict",
                    "nodes": [{"node_id": "strict", "prepare": strict}],
                },
            )
            assert rejected.status_code != 200 and "FUTURE_INFORMATION" in rejected.text
            before = datetime.now(UTC)
            preview = api.post(endpoint + "/preview", json=job)
            assert preview.status_code == 200, preview.text
            plan = preview.json()
            capture = tmp_path / "factors.json"
            capture.write_text(
                json.dumps({key: value for key, value in inputs[0].items() if key != "kind"})
            )
            assert (
                data_cli(
                    [
                        "--root",
                        str(tmp_path / "standalone"),
                        "plan-repository",
                        "--repository",
                        str(tmp_path),
                        "--capture",
                        str(capture),
                    ]
                )
                == 0
            )
            assert (
                json.loads(capsys.readouterr().out)
                == plan["plan"]["inputs"][0]["preparation"]["sources"][0]["plan"]
            )
            response = api.post(endpoint, params={"expected_plan_id": plan["plan_id"]}, json=job)
            assert response.status_code == 200, response.text
            job_url = f"{endpoint}/{response.json()['job_id']}"
            for _ in range(8):
                manager.run_once()
                state = api.get(job_url).json()
                if state["status"] in ("failed", "succeeded"):
                    break
            assert state["status"] == "succeeded", state
            summary = api.get(job_url + "/snapshots/prices").json()
            assert {v["knowledge_basis"] for v in summary["datasets"]} == {"latest_reconstruction"}
            assert {v["coverage"]["status"] for v in summary["datasets"]} == {"complete"}
            with PortableSnapshotReader(
                manager.store.path(summary["path"]), snapshot_id=summary["snapshot_id"]
            ) as reader:
                reader.verify()
                observed = datetime.now(UTC)
                history = HistoryQuery(
                    instrument_ids=("security-A",), effective_at=common["end"], as_of=observed
                )
                assert not tuple(reader.factors(history.model_copy(update={"as_of": before})))
                checkpoints = tuple(reader.factors(history))
                assert [v.value for v in checkpoints] == [1, 2, Decimal("2.5")]
                bars = tuple(
                    reader.bars(
                        BarQuery(
                            instrument_ids=("security-A",),
                            timeframe="1d",
                            price_basis="raw",
                            start=common["start"],
                            end=common["end"],
                            as_of=observed,
                        )
                    )
                )
                assert [bar.close for bar in bars] == [10, Decimal("5.5"), Decimal("4.8")]
                legacy = _apply_adj_factor(raw, factors)
                for mode, anchor, closes in (
                    ("qfq", bars[-1].bar_start, ["4", "4.4", "4.8"]),
                    ("hfq", bars[0].bar_start, ["10", "11", "12"]),
                ):
                    view = StaticPriceView(mode)
                    spec = PriceViewSpec(
                        mode=mode,
                        provider_id=view.provider_id,
                        anchor_at=anchor,
                        factor_definition_id=checkpoints[0].definition_id,
                        factor_baseline_id=checkpoints[0].baseline_id,
                    )
                    projected = view.project(bars, checkpoints, spec, decision_at=observed)
                    assert [bar.close for bar in projected] == list(map(Decimal, closes))
                    for name in ("open", "high", "low", "close"):
                        if mode == "qfq":
                            assert [float(getattr(bar, name)) for bar in projected] == legacy[
                                name
                            ].to_list()
                    assert [bar.volume for bar in projected] == [1000, 2000, 3000]
                    assert [bar.turnover for bar in projected] == [10000, 11000, 14400]
                    # An explicit retrospective v1 close-to-close simulation, with no costs.
                    panel = pl.DataFrame(
                        {
                            "symbol": ["600000.SH"] * len(dates),
                            "date": dates,
                            **{
                                name: [float(getattr(bar, name)) for bar in projected]
                                for name in ("open", "close")
                            },
                        }
                    )
                    simulation = LegacySimulator().simulate(
                        panel,
                        pl.Series([True, False, False]),
                        pl.Series([False, False, True]),
                        MatcherConfig(fees_pct=0.0, slippage_bps=0.0),
                    )
                    (trade,) = simulation.trades
                    assert (trade.entry_price, trade.exit_price, trade.pnl_pct) == (
                        float(closes[0]),
                        float(closes[-1]),
                        0.2,
                    )
    finally:
        assert manager.shutdown()


def test_raw_factor_failure_reports_the_same_source_row_in_cli_and_workbench(
    tmp_path, monkeypatch, capsys
):
    from app.services import heavy_job_limiter
    from app.services.engine_jobs.storage import JobStore

    monkeypatch.setattr(heavy_job_limiter, "_memory_admission_ok", lambda: True)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    path = tmp_path / "adj_factor/all.parquet"
    path.parent.mkdir()
    day = date(2025, 7, 2)
    pl.DataFrame(
        {
            "symbol": ["000001.SZ", "600000.SH"],
            "trade_date": [day, day],
            "ex_factor": [2.0, 1.123456789012345],
        }
    ).write_parquet(path)
    capture = {
        "layout": "event_factors_v1",
        "representation": "event_ratio",
        "expected_provider": "archive",
        "symbols": [{"instrument_id": "A", "symbol": "600000.SH", "venue_id": "XSHG"}],
        "request": {
            "dataset_id": "factors",
            "record_type": "adjustment_factor",
            "market_id": "cn.equity",
            "asset_class": "equity",
            "instrument_ids": ["A"],
            "start": "2025-07-01T00:00:00+08:00",
            "end": "2025-07-03T00:00:00+08:00",
        },
    }
    declaration = tmp_path / "capture.json"
    declaration.write_text(json.dumps(capture))
    assert (
        data_cli(
            [
                "--root",
                str(tmp_path / "cli"),
                "import-repository",
                "--repository",
                str(tmp_path),
                "--capture",
                str(declaration),
            ]
        )
        == 1
    )
    public = json.loads(capsys.readouterr().err)
    expected = {"code": public["code"], "source_issues": public["source_issues"]}
    assert expected["source_issues"] == [
        {
            "layout": "event_factors_v1",
            "field": "ex_factor",
            "trading_date": day.isoformat(),
            "symbol": "600000.SH",
            "row_number": 2,
            "reason": "fractional_precision",
            "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    ]
    manager = EngineJobManager(tmp_path, autostart=False)
    app = FastAPI()
    app.state.engine_job_manager = manager
    app.include_router(router)
    endpoint = "/api/engine-jobs"
    body = {
        "idempotency_key": "raw-factor-precision",
        "nodes": [
            {
                "node_id": "prices",
                "capture": {
                    "datasets": [
                        {"kind": "tsp_repository_factors", **capture},
                    ]
                },
            }
        ],
    }
    try:
        with TestClient(app) as api:
            submitted = api.post(endpoint, json=body)
            assert submitted.is_success, submitted.text
            job = submitted.json()["job_id"]
            manager.run_once()
            state = api.get(f"{endpoint}/{job}").json()
            assert state["status"] == "failed", state
            attempt = state["nodes"]["prices.sync_00"]["attempts"][-1]
            assert attempt["artifact"] is None and attempt["error"] == expected
            assert (
                JobStore(tmp_path).get(job)["nodes"]["prices.sync_00"]["attempts"][-1]["error"]
                == expected
            )
    finally:
        assert manager.shutdown()
