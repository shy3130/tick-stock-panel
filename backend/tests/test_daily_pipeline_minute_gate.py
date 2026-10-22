"""盘后管道分钟K阶段门控: 自定义 minute 源无需 TickFlow KLINE_MINUTE_BATCH 能力。

门控口径与 api/kline._minute_allowed 一致 — minute_data_provider 解析为自定义源
(声明 minute 数据集) 即放行, TickFlow 档位无能力时才跳过。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.jobs import daily_pipeline
from app.services import data_integrity, preferences
from app.tickflow.capabilities import CapabilitySet


def _run_pipeline(tmp_path: Path, monkeypatch, minute_provider: str) -> tuple[dict, list[dict]]:
    """跑 run_now 到分钟阶段, 返回 (result, sync_and_persist_minute 调用记录)。"""
    monkeypatch.setattr(daily_pipeline.settings, "data_dir", tmp_path)
    monkeypatch.setattr(preferences, "load", lambda: {
        "pipeline_pull_a_share": False, "pipeline_pull_etf": False,
        "pipeline_regime_enabled": False, "minute_sync_enabled": True,
        "minute_sync_days": 5, "minute_data_provider": minute_provider,
        "adj_factor_provider": "tickflow",
    })
    monkeypatch.setattr(daily_pipeline.instrument_sync, "sync_instruments", lambda *_: 0)
    monkeypatch.setattr(daily_pipeline, "_resolve_universe", lambda *_: [])
    monkeypatch.setattr(daily_pipeline, "_invalidate", lambda *_: None)
    monkeypatch.setattr(daily_pipeline, "_refresh_views", lambda *_: None)
    monkeypatch.setattr(daily_pipeline, "_refresh_single_view", lambda *_: None)
    monkeypatch.setattr(data_integrity, "scan_recent_integrity", lambda *_, **__: [])
    if minute_provider == "tickflow":
        monkeypatch.setattr(
            daily_pipeline.kline_sync, "_resolve_minute_provider",
            lambda name: (None, True, None),
        )
    else:
        # 真 resolver + monkeypatch 自定义源注册表: 该源声明 minute 数据集
        from app.data_providers import custom as custom_sources

        # 绕开 provider 白名单过滤, 让门控拿到的就是被测的源名
        monkeypatch.setattr(
            preferences, "get_minute_data_provider", lambda: minute_provider,
        )

        monkeypatch.setattr(
            custom_sources, "provider_has_dataset",
            lambda name, ds: name == minute_provider and ds == "minute",
        )
        monkeypatch.setattr(custom_sources, "get_provider", lambda name: object())

    calls: list[dict] = []

    def fake_sync_minute(symbols, *args, **kwargs):
        calls.append({"symbols": symbols, **kwargs})
        return 1

    monkeypatch.setattr(
        daily_pipeline.kline_sync, "sync_and_persist_minute", fake_sync_minute,
    )
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        latest_daily_date=lambda: date(2026, 9, 28),
    )
    return daily_pipeline.run_now(repo, CapabilitySet(set())), calls


@pytest.mark.parametrize("provider,expect_called", [
    ("customsrc", True),  # 自定义源声明 minute 数据集 → 放行 (本次修复的行为)
    ("tickflow", False),  # TickFlow 无 KLINE_MINUTE_BATCH → 跳过
])
def test_minute_stage_gate_routes_by_provider(tmp_path, monkeypatch, provider, expect_called):
    result, calls = _run_pipeline(tmp_path, monkeypatch, provider)
    assert bool(calls) is expect_called
    if expect_called:
        assert calls[0]["days"] == 5
        assert "sync_minute" not in result["skipped_stages"]
    else:
        assert "sync_minute" in result["skipped_stages"]
