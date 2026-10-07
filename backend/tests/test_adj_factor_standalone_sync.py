"""独立除权因子同步 (run_adj_factor_sync) 的联动语义回归。

覆盖: 事件窗口按本地日K起点钳制 / 受影响个股 enriched 局部重算 /
明细回填 (无因子变化) 不触发重算 / enriched 未构建时跳过 / 无能力时跳过。
纯逻辑, 不触网。
"""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

from app.jobs import daily_pipeline
from app.services import kline_sync


def _repo(tmp_path, earliest_daily=None):
    return SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        refresh_cache=lambda: None,
        earliest_daily_date=lambda: earliest_daily,
    )


def _capset(has_adj=True):
    return SimpleNamespace(has=lambda key: has_adj)


def _patch_common(monkeypatch, tmp_path, *, written=2, affected=("600519.SH",),
                  with_enriched=True, provider="tickflow"):
    """公共桩: 标的池/失效/视图刷新/因子同步/enriched 重算全部可观测。"""
    monkeypatch.setattr(daily_pipeline, "_resolve_universe",
                        lambda capset, repo=None: ["600519.SH", "000001.SZ"])
    invalidated: list[str | None] = []
    monkeypatch.setattr(daily_pipeline, "_invalidate", invalidated.append)
    refreshed: list[str] = []
    monkeypatch.setattr(daily_pipeline, "_refresh_single_view",
                        lambda repo, name: refreshed.append(name))

    captured_sync: dict = {}

    def fake_sync(symbols, repo, capset, start_time=None, end_time=None, on_chunk_done=None):
        captured_sync["symbols"] = symbols
        captured_sync["start_time"] = start_time
        return written, list(affected)

    monkeypatch.setattr(kline_sync, "sync_adj_factor", fake_sync)

    captured_recompute: dict = {}

    def fake_run_pipeline(data_dir=None, symbols=None, new_dates_only=False, on_batch_done=None):
        captured_recompute["symbols"] = symbols
        return 7

    monkeypatch.setattr(daily_pipeline, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(daily_pipeline._prefs, "get_adj_factor_provider", lambda: provider)

    if with_enriched:
        (tmp_path / "kline_daily_enriched" / "date=2026-01-05").mkdir(parents=True)
    return captured_sync, captured_recompute, invalidated, refreshed


def test_full_history_window_and_partial_recompute(monkeypatch, tmp_path):
    """无本地日K时全历史拉取 + 因子变更个股的 enriched 全日期局部重算 + 双表联动。"""
    sync, recompute, invalidated, refreshed = _patch_common(monkeypatch, tmp_path)

    events: list[tuple[str, int, str]] = []

    def progress(stage, pct, msg, **kw):
        events.append((stage, pct, msg))

    result = daily_pipeline.run_adj_factor_sync(_repo(tmp_path), _capset(), on_progress=progress)

    assert sync["start_time"] is None, "无本地日K起点时不钳制, 仍拉全历史"
    assert sync["symbols"] == ["600519.SH", "000001.SZ"]
    assert recompute["symbols"] == ["600519.SH"], "只重算因子变化个股 (与管道 adj 增量分支同语义)"
    assert invalidated == ["adj_factor", "enriched"]
    assert refreshed == ["adj_factor", "kline_enriched"]
    assert result["adj_factor_symbols"] == 1
    assert result["enriched_days"] == 7
    assert result["skipped_stages"] == []
    assert events[-1][0] == "done" and events[-1][1] == 100


def test_window_clamped_to_local_daily_horizon(monkeypatch, tmp_path):
    """有本地日K时事件窗口钳到其起点 — 更早事件对已存价格零影响 (前复权 cum/total 抵消),
    拉它们只会为远古事件逐只回补历史K线。"""
    sync, _, _, _ = _patch_common(monkeypatch, tmp_path)

    repo = _repo(tmp_path, earliest_daily=date(2016, 5, 10))
    daily_pipeline.run_adj_factor_sync(repo, _capset())

    assert sync["start_time"] == datetime(2016, 5, 10, 0, 0)


def test_detail_backfill_does_not_recompute_enriched(monkeypatch, tmp_path):
    """扩表后重拉全历史但因子值全未变 (仅明细回填) → 不触发 enriched 重算。"""
    _sync, recompute, invalidated, _ = _patch_common(
        monkeypatch, tmp_path, written=0, affected=())

    result = daily_pipeline.run_adj_factor_sync(_repo(tmp_path), _capset())

    assert "symbols" not in recompute, "无因子变化时不得调 run_pipeline"
    assert invalidated == ["adj_factor"], "enriched 未动则不失效"
    assert result["adj_factor_symbols"] == 0
    assert result["enriched_days"] == 0


def test_skips_recompute_when_enriched_not_built(monkeypatch, tmp_path):
    """enriched 尚未构建 (首次使用前) → 跳过局部重算, 交由盘后管道全量计算。"""
    _sync, recompute, invalidated, _ = _patch_common(
        monkeypatch, tmp_path, with_enriched=False)

    result = daily_pipeline.run_adj_factor_sync(_repo(tmp_path), _capset())

    assert "symbols" not in recompute
    assert invalidated == ["adj_factor"]
    assert result["adj_factor_symbols"] == 1  # 因子本身已同步入库


def test_no_capability_returns_skipped(monkeypatch, tmp_path):
    """TickFlow 无除权能力且未路由到其他源 → 跳过, 不发起任何同步。"""
    def boom(*a, **k):  # pragma: no cover - 触发即失败
        raise AssertionError("无能力时不得触网")

    monkeypatch.setattr(kline_sync, "sync_adj_factor", boom)
    monkeypatch.setattr(daily_pipeline._prefs, "get_adj_factor_provider", lambda: "tickflow")
    monkeypatch.setattr(daily_pipeline, "_invalidate", lambda table=None: None)
    monkeypatch.setattr(daily_pipeline, "_resolve_universe",
                        lambda capset, repo=None: ["600519.SH"])

    result = daily_pipeline.run_adj_factor_sync(_repo(tmp_path), _capset(has_adj=False))

    assert result["skipped_stages"] == ["sync_adj"]
    assert result["adj_factor_symbols"] == 0
