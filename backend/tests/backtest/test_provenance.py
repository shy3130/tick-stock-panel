from datetime import date
from types import SimpleNamespace

from app.api.backtest import _attach_run_provenance
from app.backtest.metrics import MetricContext
from app.backtest.provenance import build_data_snapshot, build_run_provenance


class FakeRepo:
    _external_enriched_root = None

    @staticmethod
    def local_enriched_latest_date():
        return date(2026, 8, 18)


def _published():
    return (
        {
            "generation": "generation-a",
            "start_date": "2020-01-01",
            "end_date": "2026-08-18",
            "source_generations": {"tdx": "tdx-a", "fstore": "fstore-a"},
        },
        SimpleNamespace(),
    )


def test_explicit_universe_snapshot_is_stable(monkeypatch):
    monkeypatch.setattr("app.services.canonical_history.resolve_published_history", lambda _root=None: _published())
    kwargs = {
        "repo": FakeRepo(),
        "start": date(2026, 1, 1),
        "end": date(2026, 6, 30),
        "symbols": ["600000.SH", "000001.SZ", "600000.SH"],
    }
    first, warnings = build_data_snapshot(**kwargs)
    second, _ = build_data_snapshot(**kwargs)
    assert first == second
    assert warnings == []
    assert first["canonical_generation"] == "generation-a"
    assert first["adjustment_generation"] == "tdx-a"
    assert first["local_overlay_latest_date"] == "2026-08-18"
    assert first["universe_definition"]["count"] == 2
    assert len(first["snapshot_hash"]) == 64


def test_current_provider_universe_is_flagged(monkeypatch):
    monkeypatch.setattr("app.services.canonical_history.resolve_published_history", lambda _root=None: _published())
    provenance = build_run_provenance(
        FakeRepo(),
        start=date(2026, 1, 1),
        end=date(2026, 6, 30),
        symbols=None,
        metric_context=MetricContext("weekly"),
        random_seed=7,
    )
    assert provenance["random_seed"] == 7
    assert provenance["metric_context"]["periods_per_year"] == 52
    assert provenance["data_snapshot"]["universe_as_of"] is None
    assert any(warning.startswith("survivorship_bias:") for warning in provenance["warnings"])


def _fake_request(repo) -> SimpleNamespace:
    """_attach_run_provenance 只读 request.app.state.repo, 无需真实 FastAPI Request。"""
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))


def test_strategy_provenance_uses_equity_curve_coverage(monkeypatch):
    monkeypatch.setattr("app.services.canonical_history.resolve_published_history", lambda _root=None: _published())
    payload = _attach_run_provenance(
        {
            "config": {"start": "2026-05-19", "end": "2026-08-19", "risk_free_rate": 0.02},
            "equity_curve": [
                {"date": "2026-05-18", "value": 1.0},  # 早于请求 start → 钳制回边界
                {"date": "not-a-date", "value": 1.1},   # 非法日期 → 忽略
                {"date": "2026-08-17", "value": 1.2},
            ],
        },
        _fake_request(FakeRepo()),
        start=date(2026, 5, 19),
        end=date(2026, 8, 19),
        symbols=["600000.SH"],
    )
    snapshot = payload["data_snapshot"]
    assert snapshot["data_start"] == "2026-05-19"
    assert snapshot["data_cutoff"] == "2026-08-17"  # 请求 end=08-19, 曲线实际止于 08-17
    # 请求区间仍保留在 config, 不因实际覆盖收窄而改写
    assert payload["config"]["start"] == "2026-05-19"
    assert payload["config"]["end"] == "2026-08-19"



def test_candidate_execution_omits_daily_metric_context_and_warns(monkeypatch):
    monkeypatch.setattr("app.services.canonical_history.resolve_published_history", lambda _root=None: _published())
    payload = _attach_run_provenance(
        {
            "stats": {"full_kind": "candidate_execution"},
            "equity_curve": [{"date": "2026-08-17", "value": 1.1}],
        },
        _fake_request(FakeRepo()),
        start=date(2026, 5, 19),
        end=date(2026, 8, 19),
        symbols=["600000.SH"],
    )

    assert "metric_context" not in payload
    assert "candidate_return_curve" in payload["warnings"]

def test_factor_provenance_prefers_group_nav_over_ic_series(monkeypatch):
    monkeypatch.setattr("app.services.canonical_history.resolve_published_history", lambda _root=None: _published())
    payload = _attach_run_provenance(
        {
            "config": {"start": "2026-05-19", "end": "2026-08-19"},
            "group_nav": [{"date": "2026-06-30", "G1": 1.0}, {"date": "2026-08-15", "G1": 1.1}],
            "ic_series": [{"date": "2026-06-30", "ic": 0.1}, {"date": "2026-08-17", "ic": 0.2}],
        },
        _fake_request(FakeRepo()),
        start=date(2026, 5, 19),
        end=date(2026, 8, 19),
        symbols=["600000.SH"],
        return_frequency="monthly",
    )
    assert payload["data_snapshot"]["data_cutoff"] == "2026-08-15"


def test_factor_provenance_uses_long_short_nav_before_ic_series(monkeypatch):
    monkeypatch.setattr("app.services.canonical_history.resolve_published_history", lambda _root=None: _published())
    payload = _attach_run_provenance(
        {
            "config": {"start": "2026-05-19", "end": "2026-08-19"},
            "long_short_nav": [{"date": "2026-08-14", "value": 1.05}],
            "ic_series": [{"date": "2026-08-17", "ic": 0.2}],
        },
        _fake_request(FakeRepo()),
        start=date(2026, 5, 19),
        end=date(2026, 8, 19),
        symbols=["600000.SH"],
    )
    assert payload["data_snapshot"]["data_cutoff"] == "2026-08-14"


def test_factor_ic_only_provenance_uses_ic_series(monkeypatch):
    monkeypatch.setattr("app.services.canonical_history.resolve_published_history", lambda _root=None: _published())
    payload = _attach_run_provenance(
        {
            "config": {"start": "2026-05-19", "end": "2026-08-19"},
            "ic_series": [{"date": "2026-05-20", "ic": 0.1}, {"date": "2026-08-16", "ic": 0.2}],
        },
        _fake_request(FakeRepo()),
        start=date(2026, 5, 19),
        end=date(2026, 8, 19),
        symbols=["600000.SH"],
    )
    assert payload["data_snapshot"]["data_start"] == "2026-05-20"
    assert payload["data_snapshot"]["data_cutoff"] == "2026-08-16"


def test_provenance_falls_back_to_request_range_without_series(monkeypatch):
    """错误结果/旧 payload 无可用序列时, 观测覆盖回退请求区间。"""
    monkeypatch.setattr("app.services.canonical_history.resolve_published_history", lambda _root=None: _published())
    payload = _attach_run_provenance(
        {"config": {"start": "2026-05-19", "end": "2026-08-19"}, "error": "no data"},
        _fake_request(FakeRepo()),
        start=date(2026, 5, 19),
        end=date(2026, 8, 19),
        symbols=["600000.SH"],
    )
    assert payload["data_snapshot"]["data_start"] == "2026-05-19"
    assert payload["data_snapshot"]["data_cutoff"] == "2026-08-19"


def test_snapshot_hash_tracks_actual_coverage(monkeypatch):
    """snapshot_hash 必须随修正后的实际覆盖一起计算, 而非请求区间。"""
    monkeypatch.setattr("app.services.canonical_history.resolve_published_history", lambda _root=None: _published())
    kwargs = dict(
        request=_fake_request(FakeRepo()),
        start=date(2026, 5, 19),
        end=date(2026, 8, 19),
        symbols=["600000.SH"],
    )
    truncated = _attach_run_provenance(
        {"equity_curve": [{"date": "2026-05-19", "value": 1.0}, {"date": "2026-08-17", "value": 1.2}]},
        **kwargs,
    )
    full = _attach_run_provenance(
        {"equity_curve": [{"date": "2026-05-19", "value": 1.0}, {"date": "2026-08-19", "value": 1.3}]},
        **kwargs,
    )
    assert truncated["data_snapshot"]["snapshot_hash"] != full["data_snapshot"]["snapshot_hash"]


def test_explicit_coverage_used_when_payload_has_no_series(monkeypatch):
    """robustness 响应无观测序列: 显式 coverage 写入 data_snapshot, 不回退请求区间。"""
    monkeypatch.setattr("app.services.canonical_history.resolve_published_history", lambda _root=None: _published())
    payload = _attach_run_provenance(
        {"run_id": "robrun0001", "full_stats": {"sharpe": None}},
        _fake_request(FakeRepo()),
        start=date(2026, 5, 19),
        end=date(2026, 8, 19),
        symbols=["600000.SH"],
        coverage=(date(2026, 5, 20), date(2026, 8, 17)),
    )
    assert payload["data_snapshot"]["data_start"] == "2026-05-20"
    assert payload["data_snapshot"]["data_cutoff"] == "2026-08-17"
