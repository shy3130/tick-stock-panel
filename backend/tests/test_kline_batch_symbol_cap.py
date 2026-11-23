"""批量 K 线端点 symbols 单次上限: 挡住旧客户端/异常调用的一次性全量请求。

大自选 (千只级) 整表直发会在服务端构造百万行级 Python dict 峰值, 是内存暴涨
主因之一。前端已按视口/分块收敛; 上限是后端侧的兜底防线, 值现在由用户偏好
配置 (默认 400, 见 test_kline_batch_limits_config.py), 前端分块读同一偏好。
"""
import json
import datetime as _dt

import polars as pl
import pytest
from fastapi import HTTPException

from app.services import preferences
from app.tickflow.capabilities import Cap, CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository

_BJ_DATE = _dt.date(2026, 10, 23)  # 周五交易日, 钉死不随跑测试当天漂移
_BJ_NOW = _dt.datetime(2026, 10, 23, 11, 0)


@pytest.fixture(autouse=True)
def _isolated_prefs(tmp_path, monkeypatch):
    """上限来自用户偏好: 固定到临时文件, 不读真实 preferences.json。"""
    path = tmp_path / "preferences.json"
    monkeypatch.setattr(preferences, "_path", lambda: path)
    preferences._invalidate_cache()
    yield
    preferences._invalidate_cache()


@pytest.fixture()
def repo(tmp_path):
    return KlineRepository(DataStore(tmp_path))


def _fake_req(repo, *, with_minute_cap=False):
    state_attrs = {"repo": repo}
    if with_minute_cap:
        capset = CapabilitySet()
        capset.grant(Cap.KLINE_MINUTE_BATCH)
        state_attrs["capabilities"] = capset
    state = type("S", (), state_attrs)()
    return type("R", (), {"app": type("A", (), {"state": state})()})()


# ---------- daily-batch ----------

def test_daily_batch_rejects_over_cap(repo):
    from app.api import kline as kline_api

    req = _fake_req(repo)
    body = {"symbols": [f"{600000 + i}.SH" for i in range(401)], "days": 12}
    with pytest.raises(HTTPException) as exc:
        kline_api.get_daily_batch(req, body)
    assert exc.value.status_code == 400
    assert "400" in exc.value.detail


def test_daily_batch_accepts_cap_boundary(repo, monkeypatch):
    from app.api import kline as kline_api

    monkeypatch.setattr(repo, "get_daily_batch", lambda *a, **kw: pl.DataFrame())
    monkeypatch.setattr(repo, "resolve_asset_type", lambda s: "stock")
    req = _fake_req(repo)
    body = {"symbols": [f"{600000 + i}.SH" for i in range(400)], "days": 12}
    out = json.loads(kline_api.get_daily_batch(req, body).body)
    assert out["data"] == {}


# ---------- minute-batch ----------

def test_minute_batch_rejects_over_cap(repo, monkeypatch):
    from app.api import kline as kline_api

    monkeypatch.setattr(kline_api, "cn_today", lambda: _BJ_DATE)
    monkeypatch.setattr(kline_api, "cn_now", lambda: _BJ_NOW)
    monkeypatch.setattr(kline_api.trading_day, "is_trading_day", lambda: True)
    monkeypatch.setattr(repo, "get_minute_batch", lambda *a, **kw: pl.DataFrame())
    monkeypatch.setattr(repo, "get_etf_symbol_set", lambda: set())
    monkeypatch.setattr(kline_api.kline_sync, "sync_minute_batch", lambda *a, **kw: pl.DataFrame())

    req = _fake_req(repo, with_minute_cap=True)
    body = {"symbols": [f"{600000 + i}.SH" for i in range(401)]}
    with pytest.raises(HTTPException) as exc:
        kline_api.get_minute_batch(req, body)
    assert exc.value.status_code == 400


def test_minute_batch_accepts_cap_boundary(repo, monkeypatch):
    from app.api import kline as kline_api

    monkeypatch.setattr(kline_api, "cn_today", lambda: _BJ_DATE)
    monkeypatch.setattr(kline_api, "cn_now", lambda: _BJ_NOW)
    monkeypatch.setattr(kline_api.trading_day, "is_trading_day", lambda: True)
    monkeypatch.setattr(repo, "get_minute_batch", lambda *a, **kw: pl.DataFrame())
    monkeypatch.setattr(repo, "get_etf_symbol_set", lambda: set())
    monkeypatch.setattr(kline_api.kline_sync, "sync_minute_batch", lambda *a, **kw: pl.DataFrame())

    req = _fake_req(repo, with_minute_cap=True)
    body = {"symbols": [f"{600000 + i}.SH" for i in range(400)]}
    out = json.loads(kline_api.get_minute_batch(req, body).body)
    assert out["data"] == {}
