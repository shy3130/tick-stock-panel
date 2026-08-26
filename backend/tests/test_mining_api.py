from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.mining import router
from app.backtest.mining import compute_candidate_signature
from app.enriched_generation import EnrichedGenerationUnavailableError
from app.services.mining_jobs import MiningRunStore
from app.strategy.engine import StrategyEngine

_FACTOR_DEFINITION = {
    "kind": "factor_rank",
    "factor_names": ["turnover_rate"],
    "scoring": {"turnover_rate": 1.0},
    "directions": {"turnover_rate": "high"},
}
_FACTOR_SIGNATURE = compute_candidate_signature(_FACTOR_DEFINITION)


class _Repo:
    def __init__(self, data_dir) -> None:
        self.store = SimpleNamespace(data_dir=data_dir)

    @staticmethod
    def get_matrix_data_generation(asset_type="stock"):
        return f"generation-{asset_type}"

    @staticmethod
    def latest_enriched_date(asset_type="stock"):
        del asset_type
        return date(2026, 1, 9)

    @staticmethod
    def get_instruments_asset(asset_type):
        del asset_type
        return pl.DataFrame({"symbol": ["000001.SZ"], "name": ["测试"]})


class _Manager:
    def __init__(self, data_dir) -> None:
        self.store = MiningRunStore(data_dir)

    def start(self, request, fingerprint, force=False, source="manual", run_id=None):
        del force
        manifest = self.store.create(request, fingerprint, run_id=run_id)
        self.store.append_event(
            manifest["run_id"],
            "queued",
            {"status": "queued", "source": source},
        )
        return manifest

    def cancel(self, run_id):
        manifest = self.store.get(run_id)
        if manifest is None:
            raise KeyError(run_id)
        if manifest["status"] == "queued":
            manifest = self.store.transition_status(run_id, "cancelled")
            self.store.append_event(run_id, "cancelled", {"status": "cancelled"})
        return manifest


def _write_enriched_dates(
    data_dir: Path,
    count: int,
    *,
    asset_type: str = "stock",
    first: date = date(2020, 1, 1),
) -> list[date]:
    dirname = "kline_etf_enriched" if asset_type == "etf" else "kline_daily_enriched"
    values = [first + timedelta(days=index) for index in range(count)]
    for value in values:
        partition = data_dir / dirname / f"date={value.isoformat()}"
        partition.mkdir(parents=True, exist_ok=True)
        (partition / "part.parquet").touch()
    return values


def _client(tmp_path):
    _write_enriched_dates(tmp_path, 219, first=date(2022, 8, 15))
    app = FastAPI()
    app.include_router(router)
    app.state.repo = _Repo(tmp_path)
    app.state.mining_manager = _Manager(tmp_path)
    app.state.strategy_engine = SimpleNamespace()
    return TestClient(app), app.state.mining_manager.store


def _successful_run(store: MiningRunStore, run_id: str = "result-run"):
    manifest = store.create(
        {
            "factor_names": ["turnover_rate"],
            "strategy_ids": [],
            "asset_type": "stock",
            "budget_profile": "exploratory",
            "correlation_threshold": 0.75,
        },
        {"generation": "test"},
        run_id=run_id,
    )
    store.append_event(run_id, "queued", {"status": "queued", "source": "manual"})
    store.transition_status(run_id, "running")
    store.append_event(run_id, "running", {"status": "running"})

    frames = {
        "factors": pl.DataFrame({
            "factor_name": ["turnover_rate"],
            "label": ["换手率"],
            "direction": [1],
            "score": [1.2],
            "ic_mean": [0.1],
            "ir": [0.8],
            "coverage": [1.0],
            "turnover": [0.2],
            "spread_return": [None],
            "spread_sharpe": [None],
            "selected": [True],
            "excluded_reason": [None],
        }),
        "correlation": pl.DataFrame({
            "factor_x": ["turnover_rate"],
            "factor_y": ["turnover_rate"],
            "rho": [1.0],
            "pair_count": [3],
        }),
        "candidates": pl.DataFrame({
            "signature": [_FACTOR_SIGNATURE],
            "name": ["因子组合 · 换手率"],
            "kind": ["factor_combination"],
            "factor_names_json": ['["turnover_rate"]'],
            "strategy_id": [None],
            "definition_json": [json.dumps(_FACTOR_DEFINITION, sort_keys=True)],
            "regime_state": ["overall"],
            "score": [0.9],
            "oos_return": [0.08],
            "oos_sharpe": [0.9],
            "oos_max_drawdown": [-0.12],
            "oos_positive_fold_ratio": [1.0],
            "oos_n_trades": [70],
            "confidence": ["standard"],
            "valid_folds": [3],
            "skipped_folds": [0],
            "promoted_candidate_id": [None],
            "published_strategy_id": [None],
        }, schema_overrides={
            "strategy_id": pl.String,
            "promoted_candidate_id": pl.String,
            "published_strategy_id": pl.String,
        }),
        "folds": pl.DataFrame({
            "candidate_signature": [_FACTOR_SIGNATURE, _FACTOR_SIGNATURE],
            "fold": [0, 0],
            "label": ["OOS 1", "OOS 1"],
            "regime_state": ["overall", "strong"],
            "n_dates": [3, 2],
            "train_start": ["2025-01-01", "2025-01-01"],
            "train_end": ["2025-12-31", "2025-12-31"],
            "test_start": ["2026-01-05", "2026-01-05"],
            "test_end": ["2026-01-09", "2026-01-09"],
            "selected_factors_json": ['["turnover_rate"]'] * 2,
            "total_return": [0.08, 0.1],
            "sharpe": [0.9, 1.1],
            "max_drawdown": [-0.12, -0.08],
            "n_trades": [70, 40],
            "skipped": [False, False],
            "reason": [None, None],
        }),
    }
    for name, frame in frames.items():
        frame.write_parquet(store.artifact_path(run_id, name))
        store.register_artifact(run_id, name)
    store.write_summary(run_id, {
        "status": "succeeded",
        "factor_count": 1,
        "selected_factor_count": 1,
        "candidate_count": 1,
        "valid_fold_count": 1,
        "skipped_fold_count": 0,
        "confidence": "low",
        "budget_exhausted": False,
        "elapsed_ms": 123.4,
        "data_as_of": "2026-01-09",
        "methodology_version": "factor_v2",
        "algorithm_version": "mining-v1",
        "panel_scans": 1,
        "matrix_bytes": 4096,
        "phase_ms": {"panel": 1.0, "total": 123.4},
        "worker": {"peak_rss_bytes": 1000, "serialized_result_bytes": 2000},
    })
    store.transition_status(run_id, "succeeded")
    store.append_event(run_id, "succeeded", {"status": "succeeded"})
    return manifest


def test_start_is_strict_and_projects_server_signature(tmp_path):
    client, store = _client(tmp_path)
    payload = {
        "factor_names": ["turnover_rate"],
        "budget_profile": "exploratory",
        "force": False,
    }

    response = client.post("/api/backtest/mining/runs", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["signature"] == store.get(body["run_id"])["run_signature"]
    assert body["request"]["factor_names"] == ["turnover_rate"]
    assert "force" not in body["request"]
    assert body["source"] == "manual"
    assert client.post(
        "/api/backtest/mining/runs",
        json={**payload, "matching": "close_t"},
    ).status_code == 422
    assert client.post(
        "/api/backtest/mining/runs",
        json={"factor_names": ["unknown_factor"]},
    ).status_code == 422


def test_start_accepts_iso_dates_and_persists_json_safe_request(tmp_path):
    client, store = _client(tmp_path)

    response = client.post(
        "/api/backtest/mining/runs",
        json={
            "factor_names": ["turnover_rate"],
            "budget_profile": "exploratory",
            "start": "2022-08-15",
            "end": "2026-08-15",
        },
    )

    assert response.status_code == 200
    run_id = response.json()["run_id"]
    manifest = store.get(run_id)
    assert manifest is not None
    assert manifest["request"]["start"] == "2022-08-15"
    assert manifest["request"]["end"] == "2026-08-15"
    assert client.post(
        "/api/backtest/mining/runs",
        json={
            "factor_names": ["turnover_rate"],
            "start": "2022/08/15",
        },
    ).status_code == 422


@pytest.mark.parametrize(
    ("profile", "required_bars"),
    [("exploratory", 219), ("balanced", 786), ("strict", 1164)],
)
def test_availability_enforces_exact_profile_boundaries(
    tmp_path,
    profile,
    required_bars,
):
    client, _store = _client(tmp_path)
    dates = _write_enriched_dates(
        tmp_path,
        required_bars,
        first=date(2015, 1, 1),
    )

    eligible = client.get(
        "/api/backtest/mining/availability",
        params={
            "asset_type": "stock",
            "budget_profile": profile,
            "start": dates[0].isoformat(),
            "end": dates[-1].isoformat(),
        },
    )
    insufficient = client.get(
        "/api/backtest/mining/availability",
        params={
            "asset_type": "stock",
            "budget_profile": profile,
            "start": dates[1].isoformat(),
            "end": dates[-1].isoformat(),
        },
    )

    assert eligible.status_code == 200
    assert eligible.json() == {
        "asset_type": "stock",
        "budget_profile": profile,
        "trading_bars": required_bars,
        "required_bars": required_bars,
        "outer_folds": 1 if profile == "exploratory" else 3,
        "required_outer_folds": 1 if profile == "exploratory" else 3,
        "eligible": True,
        "available_start": dates[0].isoformat(),
        "available_end": "2023-03-21",
        "effective_start": dates[0].isoformat(),
        "effective_end": dates[-1].isoformat(),
        "suggested_start": dates[0].isoformat(),
    }
    assert insufficient.status_code == 200
    assert insufficient.json()["trading_bars"] == required_bars - 1
    assert insufficient.json()["eligible"] is False
    assert insufficient.json()["suggested_start"] == dates[0].isoformat()


def test_start_rejects_balanced_625_bar_range_before_creating_run(tmp_path):
    client, store = _client(tmp_path)
    dates = _write_enriched_dates(tmp_path, 625, first=date(2024, 1, 15))

    response = client.post(
        "/api/backtest/mining/runs",
        json={
            "factor_names": ["turnover_rate"],
            "budget_profile": "balanced",
            "start": dates[0].isoformat(),
            "end": dates[-1].isoformat(),
            "force": True,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "balanced mining requires at least 786 enriched trading bars for 3 outer "
        "folds; effective range 2024-01-15 to 2025-09-30 has 625"
    )
    assert store.list_runs() == []


def test_availability_uses_asset_specific_valid_partitions(tmp_path):
    client, _store = _client(tmp_path)
    etf_dates = _write_enriched_dates(
        tmp_path,
        219,
        asset_type="etf",
        first=date(2024, 1, 1),
    )
    malformed = tmp_path / "kline_etf_enriched" / "date=not-a-date"
    malformed.mkdir(parents=True)
    (malformed / "part.parquet").touch()
    (tmp_path / "kline_etf_enriched" / "date=2023-12-31").mkdir(parents=True)

    response = client.get(
        "/api/backtest/mining/availability",
        params={
            "asset_type": "etf",
            "budget_profile": "exploratory",
            "start": etf_dates[0].isoformat(),
            "end": etf_dates[-1].isoformat(),
        },
    )

    assert response.status_code == 200
    assert response.json()["trading_bars"] == 219
    assert response.json()["eligible"] is True


def test_result_reconstructs_artifacts_without_exposing_definition(tmp_path):
    client, store = _client(tmp_path)
    _successful_run(store)

    response = client.get("/api/backtest/mining/runs/result-run/result")

    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["peak_rss_bytes"] == 1000
    assert body["correlation"]["matrix"] == [[1.0]]
    assert body["candidates"][0]["factor_names"] == ["turnover_rate"]
    assert "definition_json" not in body["candidates"][0]
    assert body["candidates"][0]["folds"][0]["selected_factors"] == ["turnover_rate"]
    assert body["candidates"][0]["gate"] == {"qualified": True, "reasons": []}
    assert body["request_summary"] == {
        "asset_type": "stock",
        "budget_profile": "exploratory",
        "start": None,
        "end": None,
        "factor_count": 1,
        "strategy_count": 0,
        "commission_pct": None,
        "stamp_tax_pct": None,
        "slippage_bps": None,
        "correlation_threshold": 0.75,
    }
    regimes = {row["state"]: row for row in body["regimes"]}
    assert regimes["overall"]["n_dates"] == 3
    assert regimes["strong"]["n_dates"] == 2
    assert regimes["range"]["total_return"] is None
    assert body["telemetry"]["panel_scans"] == 1


def test_result_marks_legacy_fold_rows_without_evaluation_kind(tmp_path):
    client, store = _client(tmp_path)
    _successful_run(store)

    response = client.get("/api/backtest/mining/runs/result-run/result")

    assert response.status_code == 200
    fold = response.json()["candidates"][0]["folds"][0]
    assert fold["evaluation_kind"] is None


def test_publish_endpoint_rejects_candidate_below_evidence_gate(tmp_path):
    client, store = _client(tmp_path)
    _successful_run(store)
    path = store.artifact_path("result-run", "candidates")
    pl.read_parquet(path).with_columns(
        pl.lit("low").alias("confidence"),
    ).write_parquet(path)

    response = client.post(
        f"/api/backtest/mining/runs/result-run/candidates/"
        f"{quote(_FACTOR_SIGNATURE, safe='')}/publish",
    )

    assert response.status_code == 400
    assert "exploratory results can only be saved" in response.json()["detail"]


def test_promote_endpoint_uses_persisted_definition_only(tmp_path):
    client, store = _client(tmp_path)
    _successful_run(store)

    response = client.post(
        f"/api/backtest/mining/runs/result-run/candidates/"
        f"{quote(_FACTOR_SIGNATURE, safe='')}/promote"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending"
    assert body["config"]["origin_run_id"] == "result-run"
    assert body["config"]["candidate_signature"] == _FACTOR_SIGNATURE
    assert body["config"]["factor_names"] == ["turnover_rate"]
    assert "definition_json" not in body
    persisted = pl.read_parquet(store.artifact_path("result-run", "candidates"))
    assert persisted["promoted_candidate_id"][0] == body["id"]


def test_publish_endpoint_uses_persisted_definition_and_invalidates_runtime(
    tmp_path,
) -> None:
    client, store = _client(tmp_path)
    _successful_run(store)
    builtin_dir = Path(__file__).resolve().parents[1] / "app" / "strategy" / "builtin"
    custom_dir = tmp_path / "strategies" / "custom"
    engine = StrategyEngine(strategy_dirs=[builtin_dir, custom_dir])
    invalidations: list[str] = []
    client.app.state.strategy_engine = engine
    client.app.state.monitor_engine = SimpleNamespace(
        invalidate_strategy_state=lambda: invalidations.append("monitor")
    )

    response = client.post(
        f"/api/backtest/mining/runs/result-run/candidates/"
        f"{quote(_FACTOR_SIGNATURE, safe='')}/publish",
        json={
            "strategy_id": "caller-selected",
            "scoring": {"rsi_14": 999.0},
        },
    )

    assert response.status_code == 200
    strategy_id = response.json()["strategy_id"]
    strategy = engine.get(strategy_id)
    assert strategy.meta["origin_run_id"] == "result-run"
    assert strategy.meta["candidate_signature"] == _FACTOR_SIGNATURE
    assert strategy.matrix_strategy._scoring == {"turnover_rate": 1.0}
    assert strategy.matrix_strategy._directions == {"turnover_rate": "high"}
    assert invalidations == ["monitor"]
    persisted = pl.read_parquet(store.artifact_path("result-run", "candidates"))
    assert persisted["published_strategy_id"][0] == strategy_id


def test_result_fails_closed_when_artifact_is_missing(tmp_path):
    client, store = _client(tmp_path)
    _successful_run(store)
    store.artifact_path("result-run", "folds").unlink()

    response = client.get("/api/backtest/mining/runs/result-run/result")

    assert response.status_code == 500
    assert response.json()["detail"] == "mining result artifacts are unavailable"


def test_sse_maps_failed_event_and_honors_last_event_id(tmp_path):
    client, store = _client(tmp_path)
    store.create(
        {"factor_names": ["turnover_rate"]},
        {"generation": "test"},
        run_id="failed-run",
    )
    queued = store.append_event(
        "failed-run", "queued", {"status": "queued", "source": "manual"}
    )
    store.transition_status("failed-run", "failed", error="worker failed")
    failed = store.append_event(
        "failed-run", "error", {"status": "failed", "message": "worker failed"}
    )

    response = client.get(
        "/api/backtest/mining/runs/failed-run/events",
        headers={"Last-Event-ID": str(queued["id"])},
    )

    assert response.status_code == 200
    assert f"id: {failed['id']}" in response.text
    assert "event: failed" in response.text
    assert "event: error" not in response.text
    assert "worker failed" in response.text


def test_sse_recovers_progress_snapshot_when_history_is_truncated(tmp_path):
    client, store = _client(tmp_path)
    store.create(
        {"factor_names": ["turnover_rate"]},
        {"generation": "test"},
        run_id="truncated-run",
    )
    store.write_summary(
        "truncated-run",
        {"progress": {"phase": "search", "done": 7, "total": 10}},
    )
    for index in range(260):
        store.append_event(
            "truncated-run",
            "progress",
            {"phase": "search", "done": index, "total": 260},
        )
    store.transition_status("truncated-run", "failed", error="worker failed")
    store.append_event(
        "truncated-run",
        "error",
        {"status": "failed", "message": "worker failed"},
    )

    response = client.get("/api/backtest/mining/runs/truncated-run/events")

    assert response.status_code == 200
    assert '"done": 7' in response.text
    assert "event: failed" in response.text


def test_start_rejects_incompatible_strategy_before_creating_run(tmp_path):
    client, store = _client(tmp_path)
    client.app.state.strategy_engine = SimpleNamespace(
        get=lambda _strategy_id: SimpleNamespace(
            meta={
                "research_only": True,
                "asset_types": ["stock"],
                "timeframes": ["1d"],
            },
            execution_backend="matrix_native",
        )
    )

    response = client.post(
        "/api/backtest/mining/runs",
        json={
            "factor_names": ["turnover_rate"],
            "strategy_ids": ["factor_rank_research"],
            "budget_profile": "exploratory",
        },
    )

    assert response.status_code == 400
    assert store.list_runs() == []


def test_config_patch_merges_current_values(tmp_path, monkeypatch):
    client, _store = _client(tmp_path)
    current = {
        "mining_schedule_enabled": False,
        "mining_schedule_weekday": 4,
        "mining_budget_profile": "balanced",
    }
    saved = []
    monkeypatch.setattr(
        "app.api.mining.preferences.get_mining_schedule",
        lambda: dict(current),
    )

    def set_schedule(enabled, weekday, profile):
        saved.append((enabled, weekday, profile))
        return {
            "mining_schedule_enabled": enabled,
            "mining_schedule_weekday": weekday,
            "mining_budget_profile": profile,
        }

    monkeypatch.setattr("app.api.mining.preferences.set_mining_schedule", set_schedule)

    response = client.patch(
        "/api/backtest/mining/config",
        json={"mining_schedule_enabled": True},
    )

    assert response.status_code == 200
    assert saved == [(True, 4, "balanced")]
    assert client.patch("/api/backtest/mining/config", json={}).status_code == 400


class _PublishingRepo(_Repo):
    """模拟 enriched 发布进行中: 世代读取抛 EnrichedGenerationUnavailableError。"""

    @staticmethod
    def get_matrix_data_generation(asset_type="stock"):
        raise EnrichedGenerationUnavailableError(
            "enriched data is being published; retry after the update finishes"
        )


def test_start_returns_400_with_guidance_while_enriched_publication_active(
    tmp_path,
):
    _write_enriched_dates(tmp_path, 219, first=date(2022, 8, 15))
    app = FastAPI()
    app.include_router(router)
    app.state.repo = _PublishingRepo(tmp_path)
    app.state.mining_manager = _Manager(tmp_path)
    app.state.strategy_engine = SimpleNamespace()
    client = TestClient(app)

    response = client.post(
        "/api/backtest/mining/runs",
        json={"factor_names": ["turnover_rate"], "budget_profile": "exploratory"},
    )

    assert response.status_code == 400
    assert "数据更新" in response.json()["detail"]
