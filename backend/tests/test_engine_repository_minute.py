import json
from datetime import UTC, date, datetime, time, timedelta

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tivelo_backtest.legacy.simulator import LegacySimulator, MatcherConfig
from tivelo_backtest.legacy.snapshot_minutes import LegacySnapshotMinutes
from tivelo_backtest.snapshot import PortableSnapshotReader
from tivelo_contracts.data import BarQuery
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_data.cli import main as data_cli
from tivelo_market_cn.calendar import CNCalendar
from tivelo_market_cn.presets import compile_calendar, load_calendar_preset

from app.api.engine_jobs import router
from app.services.engine_jobs.manager import EngineJobManager
from app.services.kline_sync import _write_minute_partition
from app.services.minute_adjust import mark_minute_basis_raw
from app.tickflow.repository import DataStore, KlineRepository


@pytest.mark.parametrize("marked", [False, True])
def test_v1_minute_store_to_worker_and_standalone_minute_fills(
    tmp_path, monkeypatch, capsys, marked
):
    import app.services.heavy_job_limiter as limiter

    monkeypatch.setattr(limiter, "_memory_admission_ok", lambda: True)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    symbol = "600000.SH"
    days = [date(2025, 7, n) for n in (1, 2, 3, 4)]
    if marked:
        mark_minute_basis_raw(tmp_path)
    repo = KlineRepository(DataStore(tmp_path))
    try:
        for d, morning in [(days[1], 10), (days[3], 11)]:
            rows = []
            for begin, raw_price in ((time(9, 31), morning), (time(13, 1), morning + 1)):
                stored_price = float(raw_price) if marked else raw_price / 2
                for offset in range(120):
                    rows.append(
                        dict(
                            symbol=symbol,
                            datetime=datetime.combine(d, begin) + timedelta(minutes=offset),
                            **{col: stored_price for col in ("open", "high", "low", "close")},
                            volume=2.5,
                            amount=raw_price * 250.0,
                        )
                    )
            assert _write_minute_partition(pl.DataFrame(rows), tmp_path / "kline_minute") == 240
            repo.append_daily(
                pl.DataFrame(
                    [
                        dict(
                            date=d,
                            symbol=symbol,
                            open=float(morning),
                            low=float(morning),
                            high=float(morning + 1),
                            close=float(morning + 1),
                            volume=600.0,
                            amount=(morning + 0.5) * 60000,
                        )
                    ]
                )
            )
        old = repo.get_minute_for_backtest([symbol], [days[1], days[3]], asset_type="stock").sort(
            "datetime"
        )
        assert old.height == 480
    finally:
        repo.db.close()
    capture = dict(
        kind="tsp_repository_minute",
        layout="minute_v1",
        minute_label="end",
        calendar_version="2026.1.0",
        price_evidence="marked_raw" if marked else "legacy_close_anchor",
        expected_provider="local_archive",
        symbols=[dict(symbol=symbol, instrument_id="security-A", venue_id="XSHG")],
        request=dict(
            dataset_id="stock-minute",
            record_type="bar",
            timeframe="1m",
            price_basis="raw",
            market_id="cn.equity",
            asset_class="equity",
            instrument_ids=["security-A"],
            start="2025-07-02T09:30:00+08:00",
            end="2025-07-04T15:00:00+08:00",
        ),
    )
    service = EngineJobManager(tmp_path, autostart=False)
    app = FastAPI()
    app.state.engine_job_manager = service
    app.include_router(router)
    root = "/api/engine-jobs"
    request = dict(
        idempotency_key="stock-minute-archive",
        nodes=[dict(node_id="prices", capture=dict(datasets=[capture]))],
    )
    try:
        with TestClient(app) as api:
            strict = {
                "datasets": [capture],
                "definition": "strategy",
                "run": {
                    "universe": ["security-A"],
                    "start": "2025-07-01",
                    "end": "2025-07-04",
                    "initial_capital": "10000",
                    "account": {"currency": "CNY"},
                    "market": {"adapter_id": "cn.equity", "distribution": "tivelo-market-cn"},
                    "price_view": {"mode": "raw", "provider_id": "builtin"},
                },
            }
            refused = api.post(
                root + "/preview",
                json={
                    "idempotency_key": "strict-minute",
                    "nodes": [{"node_id": "strict", "prepare": strict}],
                },
            )
            assert refused.status_code != 200 and "FUTURE_INFORMATION" in refused.text
            preview = api.post(root + "/preview", json=request)
            assert preview.status_code == 200, preview.text
            prepared = preview.json()
            config = tmp_path / "capture.json"
            config.write_text(json.dumps({k: v for k, v in capture.items() if k != "kind"}))
            assert (
                data_cli(
                    [
                        "--root",
                        str(tmp_path / "standalone"),
                        "plan-repository",
                        "--repository",
                        str(tmp_path),
                        "--capture",
                        str(config),
                    ]
                )
                == 0
            )
            assert (
                json.loads(capsys.readouterr().out)
                == prepared["plan"]["inputs"][0]["preparation"]["sources"][0]["plan"]
            )
            submitted = api.post(
                root, params={"expected_plan_id": prepared["plan_id"]}, json=request
            )
            assert submitted.status_code == 200, submitted.text
            address = root + "/" + submitted.json()["job_id"]
            for _ in range(5):
                service.run_once()
                state = api.get(address).json()
                if state["status"] in {"failed", "succeeded"}:
                    break
            assert state["status"] == "succeeded", state
            report = api.get(address + "/snapshots/prices").json()
            (dataset,) = report["datasets"]
            assert (
                dataset["knowledge_basis"],
                dataset["coverage"]["status"],
                dataset["coverage"]["completed_units"],
                dataset["coverage"]["requested_units"],
            ) == ("latest_reconstruction", "partial", 480, 720)
            with PortableSnapshotReader(
                service.store.path(report["path"]), snapshot_id=report["snapshot_id"]
            ) as reader:
                observed = datetime.now(UTC)
                query = BarQuery(
                    instrument_ids=("security-A",),
                    price_basis="raw",
                    timeframe="1m",
                    start=capture["request"]["start"],
                    end=capture["request"]["end"],
                    as_of=observed,
                )
                bars = tuple(reader.bars(query))
                for v2, v1 in zip(bars, old.iter_rows(named=True), strict=True):
                    assert (
                        float(v2.close),
                        float(v2.open),
                        float(v2.volume),
                        float(v2.turnover),
                    ) == (v1["close"], v1["open"], v1["volume"] * 100, v1["amount"])
                q_past = BarQuery.model_validate(
                    {**query.model_dump(), "as_of": capture["request"]["end"]}
                )
                assert not tuple(reader.bars(q_past))
                cal = CNCalendar(
                    compile_calendar(
                        load_calendar_preset(version="2026.1.0"),
                        venue_id="XSHG",
                        start=days[0],
                        end=days[-1],
                    )
                )
                loader = LegacySnapshotMinutes(
                    reader, {symbol: ("security-A", cal)}, as_of=observed
                )
                with pytest.raises(TiveloError) as error:
                    loader([symbol], {str(days[2])}, "stock")
                assert error.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT
                panel = pl.DataFrame(
                    dict(
                        date=days,
                        symbol=[symbol] * 4,
                        score=[1.0] * 4,
                        volume=[600.0] * 4,
                        open=[10.0, 10.0, 11.0, 11.0],
                        high=[10.0, 11.0, 11.0, 12.0],
                        low=[10.0, 10.0, 11.0, 11.0],
                        close=[10.0, 11.0, 11.0, 12.0],
                        signal_limit_up=[False] * 4,
                        signal_limit_down=[False] * 4,
                    )
                )
                result = LegacySimulator(minute_loader=loader).simulate_portfolio(
                    panel,
                    pl.Series([True, False, False, False]),
                    pl.Series([False, False, True, False]),
                    MatcherConfig(
                        matching="open_t+1",
                        minute_fill=True,
                        initial_capital=10000,
                        max_positions=1,
                        commission_pct=0,
                        stamp_tax_pct=0,
                        slippage_bps=0,
                    ),
                )
                (trade,) = result.trades
                assert (
                    str(trade.entry_date),
                    str(trade.exit_date),
                    trade.entry_price,
                    trade.exit_price,
                    trade.shares,
                ) == (str(days[1]), str(days[3]), 10.5, 11.5, 900)
                assert result.equity_curve[-1]["value"] == 10900
    finally:
        assert service.shutdown()
