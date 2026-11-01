from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.backtest.evidence import (
    audit_frame,
    audit_matrix,
    audit_trade_timing,
    fingerprint_frame,
    load_evidence,
    save_evidence,
)
from app.backtest.matrix import build_market_data_matrix, make_signal_matrix, rolling_mean


@pytest.fixture
def service_factory(tmp_path, monkeypatch):
    from app.backtest.engine import BacktestEngine
    from app.backtest.strategy import StrategyBacktestService
    from app.config import settings
    from app.strategy.engine import StrategyEngine
    from app.tickflow.repository import DataStore, KlineRepository
    from tests.signal_seeds import install_pipeline_caches

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    install_pipeline_caches(monkeypatch)
    frame = _panel().with_columns(
        pl.col("close").alias("raw_close"), pl.col("high").alias("raw_high"),
        pl.col("low").alias("raw_low"), (pl.col("close") * pl.col("volume")).alias("amount"),
        pl.lit(1.0).alias("turnover_rate"),
        pl.lit(0).alias("consecutive_limit_ups"), pl.lit(0).alias("consecutive_limit_downs"),
    )
    for part in frame.partition_by("date"):
        path = tmp_path / "kline_daily_enriched" / f"date={part['date'][0]}" / "part.parquet"
        path.parent.mkdir(parents=True)
        part.write_parquet(path)
    instruments = tmp_path / "instruments" / "part.parquet"
    instruments.parent.mkdir()
    pl.DataFrame({
        "symbol": ["600000.SH", "000001.SZ"], "name": ["A", "B"],
        "float_shares": [1e8, 2e8], "total_shares": [2e8, 4e8],
    }).write_parquet(instruments)
    strategies = tmp_path / "strategies" / "custom"
    strategies.mkdir(parents=True)
    store = DataStore(tmp_path)
    repo = KlineRepository(store)

    def build(backend="matrix_native", leak=False):
        if backend == "matrix_native":
            source = '''
import numpy as np
from app.backtest.matrix import make_signal_matrix
META = {"id": "audit_test", "name": "audit", "params": [{"id": "offset", "type": "int", "default": 0}], "scoring": {}}
EXECUTION_BACKEND = "matrix_native"
class Strategy:
    def required_fields(self):
        return frozenset({"open", "high", "low", "close", "volume"})
    def required_warmup_bars(self, params):
        return 1
    def compute_signals(self, market, params):
        score = SCORE + params.get("offset", 0)
        return make_signal_matrix(market.shape, entry=np.ones(market.shape), score=score)
MATRIX_STRATEGY = Strategy()
'''.replace("SCORE", "np.broadcast_to(market.close[-1], market.shape)" if leak else "market.close")
        else:
            source = '''
import polars as pl
META = {"id": "audit_test", "name": "audit", "scoring": {}, "params": []}
EXECUTION_BACKEND = "polars_expr"
def filter(df, params):
    return EXPR
'''.replace("EXPR", 'pl.col("close") > pl.col("close").mean().over("symbol")' if leak else 'pl.col("close") > 0')
        (strategies / "audit_test.py").write_text(source, encoding="utf-8")
        return StrategyBacktestService(BacktestEngine(repo), StrategyEngine(strategy_dirs=[strategies]))

    yield build
    store.db.close()


def _config(**kwargs):
    from app.backtest.strategy import StrategyBacktestConfig
    return StrategyBacktestConfig(
        strategy_id="audit_test", symbols=None, start=date(2024, 1, 4), end=date(2024, 1, 12),
        overrides={"basic_filter": {"enabled": False}}, **kwargs,
    )


def _panel() -> pl.DataFrame:
    return pl.DataFrame([
        {
            "symbol": symbol,
            "date": date(2024, 1, 1) + timedelta(days=i),
            "open": float(i + offset), "high": float(i + offset + 1),
            "low": float(i + offset - 1), "close": float(i + offset),
            "volume": float(100 + i),
        }
        for symbol, offset in [("600000.SH", 10), ("000001.SZ", 20)]
        for i in range(12)
    ]).sort(["symbol", "date"])


def test_matrix_causal_rolling_passes_without_modifying_inputs():
    market = build_market_data_matrix(_panel())
    original = market.close.copy()

    def evaluate(data):
        score = rolling_mean(data.close, 3)
        return make_signal_matrix(data.shape, entry=data.close > score, score=np.nan_to_num(score))

    report = audit_matrix(market, evaluate(market), evaluate, start="2024-01-04", end="2024-01-12")
    assert report["status"] == "passed"
    assert {c["kind"] for c in report["checks"]} == {"truncate", "perturb"}
    assert len(report["checks"]) == 6
    assert all(c["compared_values"] > 0 for c in report["checks"])
    np.testing.assert_equal(market.close, original)


@pytest.mark.parametrize("leak", ["next_bar", "global_mean", "last_bar"])
def test_matrix_detects_future_dependency_with_location(leak):
    market = build_market_data_matrix(_panel())

    def evaluate(data):
        if leak == "next_bar":
            score = np.roll(data.close, -1, axis=0)
        elif leak == "global_mean":
            score = np.broadcast_to(np.mean(data.close, axis=0), data.shape)
        else:
            score = np.broadcast_to(data.close[-1], data.shape)
        return make_signal_matrix(data.shape, entry=np.ones(data.shape), score=score)

    report = audit_matrix(market, evaluate(market), evaluate, start="2024-01-04", end="2024-01-12")
    assert report["status"] == "failed"
    assert report["differences"] > 0
    example = next(c for c in report["checks"] if c["examples"])["examples"][0]
    assert example["field"] == "score"
    assert example["symbol"] in market.symbols
    assert example["date"] <= example["cutoff"]


def test_frame_recomputes_features_on_prefix_and_perturbed_future():
    raw = _panel()

    def evaluate(frame):
        return frame.with_columns(
            pl.col("close").shift(-1).over("symbol").alias("future_price"),
            pl.lit(True).alias("entry"),
        ).select("symbol", "date", "future_price", "entry")

    report = audit_frame(raw, evaluate(raw), evaluate, start="2024-01-04", end="2024-01-12")
    assert report["status"] == "failed"
    assert any(e["field"] == "future_price" for c in report["checks"] for e in c["examples"])
    assert raw.equals(_panel())


def test_audit_insufficient_or_failed_recompute_never_passes():
    market = build_market_data_matrix(_panel().filter(pl.col("date") == date(2024, 1, 1)))
    signals = make_signal_matrix(market.shape, entry=np.ones(market.shape))
    report = audit_matrix(market, signals, lambda _: signals, start="2024-01-01", end="2024-01-01")
    assert report["status"] == "insufficient"

    market = build_market_data_matrix(_panel())

    def broken(_):
        raise ValueError("bad prefix")

    report = audit_matrix(market, make_signal_matrix(market.shape), broken, start="2024-01-04", end="2024-01-12")
    assert report["status"] == "error"
    assert all(c["status"] == "error" for c in report["checks"])


def test_fingerprint_stable_across_row_and_column_order_but_changes_with_values():
    frame = _panel()
    first = fingerprint_frame(frame)
    assert first["sha256"] == fingerprint_frame(frame.reverse().select(reversed(frame.columns)))["sha256"]
    changed = frame.with_columns((pl.col("volume") + 1).alias("volume"))
    second = fingerprint_frame(changed)
    assert first["sha256"] != second["sha256"]
    assert first["fields"]["close"] == second["fields"]["close"]
    assert first["fields"]["volume"] != second["fields"]["volume"]


def test_announcement_fingerprint_ignores_physical_order_of_same_symbol_rows():
    frame = pl.DataFrame({
        "symbol": ["600000.SH", "600000.SH"],
        "announce_date": ["2024-01-04", "2024-01-05"], "value": [1.0, 2.0],
    })
    assert fingerprint_frame(frame)["sha256"] == fingerprint_frame(frame.reverse())["sha256"]


def test_manifest_persists_and_rejects_unsafe_ids_and_overwrites(tmp_path):
    evidence = {"schema_version": 1, "run_id": "012345abcd", "data": {"sha256": "a" * 64}}
    save_evidence(tmp_path, evidence)
    assert load_evidence(tmp_path, "012345abcd") == evidence
    with pytest.raises(FileExistsError):
        save_evidence(tmp_path, evidence)
    with pytest.raises(ValueError):
        load_evidence(tmp_path, "../../secrets")
    with pytest.raises(FileNotFoundError):
        load_evidence(tmp_path, "0000000000")


@pytest.mark.parametrize("backend", ["matrix_native", "polars_expr"])
@pytest.mark.parametrize("leak", [False, True])
def test_real_service_checks_and_persists_exact_result_inputs(service_factory, tmp_path, backend, leak):
    service = service_factory(backend, leak)
    result = service.run_with_evidence(_config())
    assert result.error is None
    assert result.evidence["lookahead"]["status"] == ("failed" if leak else "passed")
    assert result.evidence["persistence"]["status"] == "saved"
    assert load_evidence(tmp_path, result.run_id) == result.evidence
    assert result.evidence["data"]["datasets"]
    assert result.evidence["config"]["asset_type"] == "stock"
    assert service.engine._evidence is None
    assert result.evidence["execution_timing"]["status"] == "passed"


def test_versions_separate_data_parameters_and_strategy_changes(service_factory, tmp_path):
    service = service_factory()
    first = service.run_with_evidence(_config()).evidence
    same = service.run_with_evidence(_config()).evidence
    assert first["reproducibility_sha256"] == same["reproducibility_sha256"]
    params = service.run_with_evidence(_config(params={"offset": 2})).evidence
    assert params["data"]["sha256"] == first["data"]["sha256"]
    assert params["config_sha256"] != first["config_sha256"]
    assert params["strategy"]["sha256"] == first["strategy"]["sha256"]

    from app.enriched_generation import bump_enriched_generation
    path = tmp_path / "kline_daily_enriched" / "date=2024-01-08" / "part.parquet"
    pl.read_parquet(path).with_columns((pl.col("volume") + 5).alias("volume")).write_parquet(path)
    bump_enriched_generation(tmp_path, "stock")
    data = service.run_with_evidence(_config()).evidence
    assert data["data"]["sha256"] != first["data"]["sha256"]
    assert data["config_sha256"] == first["config_sha256"]
    assert data["strategy"]["sha256"] == first["strategy"]["sha256"]
    assert load_evidence(tmp_path, first["run_id"]) == first

    changed_service = service_factory(leak=True)
    strategy = changed_service.run_with_evidence(_config()).evidence
    assert strategy["data"]["sha256"] == data["data"]["sha256"]
    assert strategy["strategy"]["sha256"] != data["strategy"]["sha256"]


def test_unwritable_manifest_is_visible_and_normal_trials_do_not_audit(service_factory, monkeypatch):
    service = service_factory()
    assert service.run(_config()).evidence is None

    def denied(*_args):
        raise PermissionError("synthetic")

    monkeypatch.setattr("app.backtest.evidence.save_evidence", denied)
    result = service.run_with_evidence(_config())
    assert result.error is None
    assert result.evidence["persistence"]["status"] == "error"
    assert service.engine._evidence is None


def test_nondeterministic_strategy_is_inconclusive_not_a_future_leak_verdict():
    market = build_market_data_matrix(_panel())
    calls = 0

    def evaluate(data):
        nonlocal calls
        calls += 1
        return make_signal_matrix(data.shape, score=np.full(data.shape, calls))

    result = audit_matrix(market, evaluate(market), evaluate, start="2024-01-04", end="2024-01-12")
    assert result["status"] == "error"
    assert result["checks"][-1]["kind"] == "repeat"


def test_evidence_api_reads_saved_manifest_and_handles_missing_invalid_and_corrupt(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.backtest import router
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    evidence = {"schema_version": 1, "run_id": "012345abcd"}
    save_evidence(tmp_path, evidence)
    assert client.get("/api/backtest/strategy/runs/012345abcd/evidence").json() == evidence
    assert client.get("/api/backtest/strategy/runs/0000000000/evidence").status_code == 404
    assert client.get("/api/backtest/strategy/runs/invalid/evidence").status_code == 400
    (tmp_path / "backtest_runs" / "012345abcd.json").write_text("{", encoding="utf-8")
    assert client.get("/api/backtest/strategy/runs/012345abcd/evidence").status_code == 400


def test_real_post_api_returns_same_persisted_report_as_worker(service_factory, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.backtest import router

    service = service_factory()
    app = FastAPI()
    app.state.repo = service.engine.repo
    app.state.strategy_engine = service.strategy_engine
    app.include_router(router)
    client = TestClient(app)
    response = client.post("/api/backtest/strategy/run", json={
        "strategy_id": "audit_test", "start": "2024-01-04", "end": "2024-01-12",
        "overrides": {"basic_filter": {"enabled": False}},
    })
    assert response.status_code == 200
    result = response.json()
    assert result["error"] is None
    assert result["evidence"]["lookahead"]["status"] == "passed"
    assert load_evidence(tmp_path, result["run_id"]) == result["evidence"]
    assert client.get(f"/api/backtest/strategy/runs/{result['run_id']}/evidence").json() == result["evidence"]


def test_panel_audit_freezes_external_timeseries_values(service_factory, monkeypatch):
    from app.factors import ext_factors

    service = service_factory("polars_expr")
    calls = 0

    def attach(frame, **_kwargs):
        nonlocal calls
        calls += 1
        return frame.with_columns(pl.lit(float(calls)).alias("ext_test_value"))

    monkeypatch.setattr(ext_factors, "attach_ext_columns", attach)
    result = service.run_with_evidence(_config())
    assert result.error is None
    assert result.evidence["lookahead"]["status"] == "passed"
    assert calls == 1
    assert "ext_test_value" in result.evidence["data"]["datasets"]["daily_base"]["fields"]


def test_report_path_rejects_symlink_even_inside_data_directory(tmp_path):
    target = tmp_path / "other"
    target.mkdir()
    (tmp_path / "backtest_runs").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="目录无效"):
        save_evidence(tmp_path, {"schema_version": 1, "run_id": "012345abcd"})


def test_concurrent_manifest_writes_never_replace_an_existing_version(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    def write(value):
        try:
            save_evidence(tmp_path, {"schema_version": 1, "run_id": "012345abcd", "value": value})
            return value
        except FileExistsError:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        written = [value for value in pool.map(write, range(8)) if value is not None]
    assert len(written) == 1
    assert load_evidence(tmp_path, "012345abcd")["value"] == written[0]


def test_trade_timing_checks_future_dates_and_next_open_but_respects_minute_entries():
    trade = {"symbol": "600000.SH", "entry_signal_date": "2024-01-05", "entry_date": "2024-01-04"}
    assert audit_trade_timing([trade], {}, "matrix_native")["status"] == "failed"
    trade["entry_date"] = "2024-01-05"
    assert audit_trade_timing([trade], {"entry_fill": "open_t+1"}, "matrix_native")["status"] == "failed"
    assert audit_trade_timing([trade], {"entry_fill": "close_t"}, "matrix_native")["status"] == "passed"
    assert audit_trade_timing([trade], {"entry_fill": "open_t+1"}, "minute_filter")["status"] == "passed"
    trade.update(exit_signal_date="2024-01-08", exit_date="2024-01-08", exit_reason="signal")
    assert audit_trade_timing([trade], {"exit_fill": "open_t+1"}, "matrix_native")["status"] == "failed"
    trade["exit_reason"] = "forced_close"
    assert audit_trade_timing([trade], {"exit_fill": "open_t+1"}, "matrix_native")["status"] == "passed"


def test_unknown_strategy_keeps_legacy_error_contract(service_factory):
    from dataclasses import replace

    result = service_factory().run_with_evidence(replace(_config(), strategy_id="missing"))
    assert result.error
    assert result.evidence is None


def test_empty_minute_signal_comparisons_are_insufficient_instead_of_passed():
    raw = _panel()
    empty = raw.head(0)
    result = audit_frame(raw, empty, lambda _: empty, start=date(2024, 1, 4), end=date(2024, 1, 12))
    assert result["status"] == "insufficient"


def test_perturbation_also_exercises_zero_valued_future_fields():
    market = build_market_data_matrix(_panel().with_columns(pl.lit(0.0).alias("volume")))

    def evaluate(data):
        return make_signal_matrix(data.shape, score=np.broadcast_to(data.volume.mean(axis=0), data.shape))

    result = audit_matrix(market, evaluate(market), evaluate, start="2024-01-04", end="2024-01-12")
    assert result["status"] == "failed"
    assert any(check["kind"] == "perturb" and check["differences"] > 0 for check in result["checks"])


def test_cancelled_checks_are_not_reported_as_passed():
    import threading

    event = threading.Event()
    event.set()
    market = build_market_data_matrix(_panel())
    result = audit_matrix(market, make_signal_matrix(market.shape), lambda _: None,
                          start="2024-01-04", end="2024-01-12", cancel_event=event)
    assert result["status"] == "cancelled"


@pytest.mark.parametrize("leak", [False, True])
def test_minute_replay_checks_intraday_future_and_tracks_minute_inputs(tmp_path, monkeypatch, leak):
    from types import SimpleNamespace

    from app.config import settings
    from tests.backtest.test_minute_backtest import (
        TEST_STRATEGY_SOURCE,
        _daily_panel,
        _make_service,
        _minute_frame,
        _trading_days,
    )
    from tests.backtest.test_minute_backtest import (
        _config as minute_config,
    )

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    days = _trading_days(10)
    frame = _daily_panel(days, ["600000.SH"])
    minutes = {
        day: _minute_frame(day, [("600000.SH", f"09:{31 + i}", 10.7 + i * 0.01) for i in range(6)])
        for day in days[5:9]
    }
    service = _make_service(tmp_path, frame, minutes)
    service.engine.repo.store = SimpleNamespace(data_dir=tmp_path)
    if leak:
        source = TEST_STRATEGY_SOURCE.replace(
            'joined = df.join(prev, on="symbol", how="inner")',
            'df = df.with_columns(pl.col("close").shift(-1).over("symbol").alias("close"))\n    joined = df.join(prev, on="symbol", how="inner")',
        )
        (tmp_path / "strategies" / "test_minute_ping.py").write_text(source, encoding="utf-8")
        service.strategy_engine.reload()
    result = service.run_with_evidence(minute_config(days[5], days[8]))
    assert result.error is None
    assert result.evidence["lookahead"]["status"] == ("failed" if leak else "passed")
    assert len([key for key in result.evidence["data"]["datasets"] if key.startswith("minute_replay/")]) == 4
