"""#303: 空库/薄库数据不足时的策略可见化。

增量管道只拉到 1 个交易日时, 指标暖机不足, 策略静默全 0。修复后:
- enriched_history_days 按 date=* 分区目录计数 (不读 parquet 内容);
- ScreenerService.coverage_warnings 生成人话警告, run/run_preset/run_all
  响应附带 warnings 字段;
- 盘后管道在 enriched 覆盖过薄时打 WARN (warn_if_enriched_too_thin)。
"""
from __future__ import annotations

import logging
from datetime import date
from types import SimpleNamespace

from app.api import screener as screener_api
from app.jobs import daily_pipeline
from app.services.screener import (
    MIN_INDICATOR_WARMUP_DAYS,
    ScreenerService,
    enriched_history_days,
)


def _make_partitions(root, dirname: str, dates: list[str]) -> None:
    for ds in dates:
        (root / dirname / f"date={ds}").mkdir(parents=True, exist_ok=True)


def test_enriched_history_days_counts_and_filters_as_of(tmp_path):
    _make_partitions(tmp_path, "kline_daily_enriched",
                     ["2026-09-01", "2026-09-02", "2026-09-03"])

    assert enriched_history_days(tmp_path) == 3
    assert enriched_history_days(tmp_path, as_of=date(2026, 9, 2)) == 2
    assert enriched_history_days(tmp_path / "nonexistent") == 0


def test_enriched_history_days_routes_etf_directory(tmp_path):
    _make_partitions(tmp_path, "kline_etf_enriched", ["2026-09-01"])

    assert enriched_history_days(tmp_path, asset_type="etf") == 1
    assert enriched_history_days(tmp_path, asset_type="stock") == 0


def _svc(tmp_path) -> ScreenerService:
    return ScreenerService(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))


def test_coverage_warnings_fire_on_thin_history(tmp_path):
    _make_partitions(tmp_path, "kline_daily_enriched", ["2026-09-01"])

    warnings = _svc(tmp_path).coverage_warnings(date(2026, 9, 1))

    assert len(warnings) == 1
    assert "1 个交易日" in warnings[0]


def test_coverage_warnings_respect_strategy_required_bars(tmp_path):
    # 36 天: 通用窗口 (30) 足够, 策略精确需求 60 天不够
    _make_partitions(
        tmp_path, "kline_daily_enriched",
        [f"2026-{m:02d}-{d:02d}" for m in range(1, 10) for d in (1, 15)]
        + [f"2026-10-{d:02d}" for d in range(1, 19)],
    )

    assert _svc(tmp_path).coverage_warnings(date(2026, 10, 18)) == []
    thin = _svc(tmp_path).coverage_warnings(date(2026, 10, 18), required_bars=60)
    assert len(thin) == 1 and "60" in thin[0]


def test_coverage_warnings_silent_when_sufficient(tmp_path):
    # 18 天 (1~9 月每月两日) + 12 天 (10 月) = 30, 恰好达标
    _make_partitions(
        tmp_path, "kline_daily_enriched",
        [f"2026-{m:02d}-{d:02d}" for m in range(1, 10) for d in (1, 15)]
        + [f"2026-10-{d:02d}" for d in range(1, 13)],
    )

    assert _svc(tmp_path).coverage_warnings(date(2026, 10, 12)) == []


class _CoverageSvc:
    """run_preset 接线测试用: 只实现 coverage_warnings/latest_date/build_strategy_context。"""

    def __init__(self, warnings):
        self._warnings = warnings

    def latest_date(self):
        return date(2026, 9, 10)

    def coverage_warnings(self, as_of, *, required_bars=None):
        return self._warnings

    def build_strategy_context(self, engine, as_of, strategy_ids, **kwargs):
        return SimpleNamespace(as_of=as_of)


class _MinimalEngine:
    def __init__(self):
        self.run_called = False

    def has(self, strategy_id):
        return strategy_id == "s1"

    def get(self, strategy_id):
        return SimpleNamespace(meta={"id": strategy_id})

    def run(self, strategy_id, context, **kwargs):
        self.run_called = True
        from app.services.screener import ScreenerResult
        return ScreenerResult(as_of=context.as_of, strategy=strategy_id)


def test_run_preset_response_carries_warnings(monkeypatch, tmp_path):
    svc = _CoverageSvc(["数据不足提示"])
    engine = _MinimalEngine()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
        strategy_engine=engine,
    )))
    monkeypatch.setattr(screener_api, "ScreenerService", lambda *a, **k: svc)
    monkeypatch.setattr(screener_api, "_load_ext_value_maps", lambda *a, **k: {})
    monkeypatch.setattr(screener_api, "_update_cache_strategy", lambda *a, **k: None)
    monkeypatch.setattr(screener_api.strategy_config, "load_override", lambda *a: {})

    resp = screener_api.run_preset(
        screener_api.PresetRequest(strategy_id="s1", as_of=date(2026, 9, 10)),
        request,
    )

    assert engine.run_called
    assert resp["warnings"] == ["数据不足提示"]


def test_run_preset_no_warnings_key_when_sufficient(monkeypatch, tmp_path):
    svc = _CoverageSvc([])
    engine = _MinimalEngine()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
        strategy_engine=engine,
    )))
    monkeypatch.setattr(screener_api, "ScreenerService", lambda *a, **k: svc)
    monkeypatch.setattr(screener_api, "_load_ext_value_maps", lambda *a, **k: {})
    monkeypatch.setattr(screener_api, "_update_cache_strategy", lambda *a, **k: None)
    monkeypatch.setattr(screener_api.strategy_config, "load_override", lambda *a: {})

    resp = screener_api.run_preset(
        screener_api.PresetRequest(strategy_id="s1", as_of=date(2026, 9, 10)),
        request,
    )

    assert "warnings" not in resp


def test_pipeline_warns_when_enriched_too_thin(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="app.jobs.daily_pipeline")
    _make_partitions(tmp_path, "kline_daily_enriched", ["2026-09-01", "2026-09-02"])

    days = daily_pipeline.warn_if_enriched_too_thin(tmp_path)

    assert days == 2
    assert any("指标暖机不足" in r.message for r in caplog.records)


def test_pipeline_silent_when_coverage_sufficient(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="app.jobs.daily_pipeline")
    _make_partitions(
        tmp_path, "kline_daily_enriched",
        [f"2026-{m:02d}-15" for m in range(1, 10)] + [f"2026-10-{d:02d}" for d in range(1, 25)],
    )

    days = daily_pipeline.warn_if_enriched_too_thin(tmp_path)

    assert days >= MIN_INDICATOR_WARMUP_DAYS
    assert not any("指标暖机不足" in r.message for r in caplog.records)


def test_update_cache_strategy_keeps_warnings(tmp_path):
    """单跑刷新缓存不得丢掉数据不足提示 (#303 复审回归)。

    _update_cache_strategy 重建缓存条目时曾只保留 total/as_of/rows 白名单,
    run_preset 单跑会把 run_all 写入的 warnings 冲掉。
    """
    from app.services import strategy_cache

    strategy_cache.write_cache(
        tmp_path, "2026-09-10",
        {"other": {"total": 1, "as_of": "2026-09-10", "rows": []}},
    )

    screener_api._update_cache_strategy(
        tmp_path, "2026-09-10", "s1",
        {"total": 0, "as_of": "2026-09-10", "rows": [], "warnings": ["数据不足"]},
    )

    cached = strategy_cache.read_cache(tmp_path)
    assert cached["results"]["s1"]["warnings"] == ["数据不足"]
    assert cached["results"]["other"]["total"] == 1  # 同日其余策略不受影响
