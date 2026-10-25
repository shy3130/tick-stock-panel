"""批量端点单请求上限: 可配置 + 与前端分块同源。

背景: daily-batch / minute-batch 的 symbols 上限原先是三处硬编码 (后端 400、
日K分块 300、分时块 300), 改任一处不一致就全站 400。现在上限是用户偏好,
后端校验与前端分块读同一个值。
"""
from __future__ import annotations

import json

import pytest

from app.services import preferences


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    path = tmp_path / "preferences.json"
    monkeypatch.setattr(preferences, "_path", lambda: path)
    preferences._invalidate_cache()
    yield path
    preferences._invalidate_cache()


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    preferences._invalidate_cache()


# ── 偏好读写 ─────────────────────────────────────────────

def test_defaults_when_unset():
    assert preferences.get_daily_batch_symbols() == 400
    assert preferences.get_minute_batch_symbols() == 400


def test_setter_roundtrip(_isolated):
    assert preferences.set_daily_batch_symbols(1500) == 1500
    assert preferences.get_daily_batch_symbols() == 1500
    assert preferences.set_minute_batch_symbols(600) == 600
    assert preferences.get_minute_batch_symbols() == 600


@pytest.mark.parametrize("raw,expected", [(0, 50), (1, 50), (-10, 50)])
def test_daily_lower_bound_clamps(_isolated, raw, expected):
    assert preferences.set_daily_batch_symbols(raw) == expected


@pytest.mark.parametrize("raw,expected", [(99999, 5000), (5001, 5000)])
def test_daily_upper_bound_clamps(_isolated, raw, expected):
    assert preferences.set_daily_batch_symbols(raw) == expected


@pytest.mark.parametrize("raw,expected", [(0, 50), (2001, 2000), (99999, 2000)])
def test_minute_bounds_clamp(_isolated, raw, expected):
    assert preferences.set_minute_batch_symbols(raw) == expected


def test_minute_default_kept_below_daily_range(_isolated):
    """分时上限区间比日K窄: 单只 240 根, 同标的数成本高一个量级。"""
    assert preferences.set_minute_batch_symbols(3000) == 2000
    assert preferences.set_daily_batch_symbols(3000) == 3000


def test_malformed_stored_value_falls_back_to_default(_isolated):
    _write(_isolated, {"daily_batch_symbols": "not-a-number", "minute_batch_symbols": None})
    assert preferences.get_daily_batch_symbols() == 400
    assert preferences.get_minute_batch_symbols() == 400


def test_out_of_range_stored_value_is_clamped_on_read(_isolated):
    """手改 preferences.json 越界时按区间收敛, 不把非法值直接用于校验。"""
    _write(_isolated, {"daily_batch_symbols": 999999, "minute_batch_symbols": 1})
    assert preferences.get_daily_batch_symbols() == 5000
    assert preferences.get_minute_batch_symbols() == 50


def test_setter_keeps_other_preferences(_isolated):
    _write(_isolated, {"realtime_quotes_enabled": True})
    preferences.set_daily_batch_symbols(800)
    data = preferences.load()
    assert data["realtime_quotes_enabled"] is True
    assert data["daily_batch_symbols"] == 800


# ── 端点校验 (与偏好联动) ─────────────────────────────────

def _fake_req(repo=None):
    from types import SimpleNamespace

    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(repo=repo, capabilities=None),
        )
    )


def test_daily_endpoint_rejects_over_configured_limit(_isolated, monkeypatch):
    from fastapi import HTTPException

    from app.api import kline as kline_api

    preferences.set_daily_batch_symbols(100)
    body = {"symbols": [f"6000{i:02d}.SH" for i in range(101)], "days": 12}
    with pytest.raises(HTTPException) as exc:
        kline_api.get_daily_batch(_fake_req(), body)
    assert exc.value.status_code == 400
    assert "100" in exc.value.detail
    assert "系统设置" in exc.value.detail


def test_daily_endpoint_accepts_at_configured_limit(_isolated, monkeypatch):
    import polars as pl

    from app.api import kline as kline_api

    preferences.set_daily_batch_symbols(120)

    class _Repo:
        def resolve_asset_type(self, _s):
            return "stock"

        def get_daily_batch(self, *_a, **_kw):
            return pl.DataFrame()

    body = {"symbols": [f"6000{i:02d}.SH" for i in range(120)], "days": 12}
    out = json.loads(kline_api.get_daily_batch(_fake_req(_Repo()), body).body)
    assert out["data"] == {}


def test_minute_endpoint_rejects_over_configured_limit(_isolated):
    from fastapi import HTTPException

    from app.api import kline as kline_api

    preferences.set_minute_batch_symbols(80)
    body = {"symbols": [f"6000{i:02d}.SH" for i in range(81)]}
    with pytest.raises(HTTPException) as exc:
        kline_api.get_minute_batch(_fake_req(), body)
    assert exc.value.status_code == 400
    assert "80" in exc.value.detail


def test_limits_are_independent(_isolated):
    """日K 调高不影响分时上限 (两档独立, 不共用同一个数)。"""
    preferences.set_daily_batch_symbols(1800)
    preferences.set_minute_batch_symbols(120)
    assert preferences.get_daily_batch_symbols() == 1800
    assert preferences.get_minute_batch_symbols() == 120
