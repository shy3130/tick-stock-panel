"""fhold 持仓接入测试 — mock subprocess,不依赖真实 fhold-cli。"""
from __future__ import annotations

import subprocess

from app.services.trading import fhold_client


def test_symbol_mapping():
    assert fhold_client.to_symbol("600519") == "600519.SH"
    assert fhold_client.to_symbol("000988") == "000988.SZ"
    assert fhold_client.to_symbol("300750") == "300750.SZ"
    assert fhold_client.to_symbol("430047") == "430047.BJ"
    assert fhold_client.to_symbol("06088") == "06088.HK"
    assert fhold_client.to_symbol("00700") == "00700.HK"
    assert fhold_client.to_symbol("ABC") is None
    assert fhold_client.to_symbol("") is None
    assert fhold_client.to_symbol("12") is None


def test_fetch_holdings_unavailable_when_cli_missing(monkeypatch):
    monkeypatch.setattr(fhold_client, "_cli", lambda: None)
    out = fhold_client.fetch_holdings()
    assert out == {"available": False, "accounts": [], "positions": []}


def test_fetch_holdings_maps_positions(monkeypatch):
    accounts = [{"id": 1, "name": "银河证券", "broker": "银河", "is_default": True, "is_active": True}]
    positions = [{
        "account_id": 1, "code": "000988", "name": "华工科技",
        "quantity": 1600, "amount": 156192, "cost_price": 124.868,
        "current_price": 97.62, "holding_pnl": -43597.44, "holding_pnl_ratio": -0.2182,
        "source_date": "2026-08-04T00:00:00Z", "updated_at": "2026-08-04T02:23:29Z",
    }]

    def fake_run(args):
        return accounts if args[0] == "account" else positions

    monkeypatch.setattr(fhold_client, "_run", fake_run)
    out = fhold_client.fetch_holdings()
    assert out["available"] is True
    assert out["accounts"][0]["name"] == "银河证券"
    pos = out["positions"][0]
    assert pos["symbol"] == "000988.SZ"
    assert pos["qty"] == 1600
    assert pos["costPrice"] == 124.868
    assert pos["holdingPnl"] == -43597.44


def test_fetch_holdings_partial_failure_still_available(monkeypatch):
    monkeypatch.setattr(fhold_client, "_run", lambda args: None if args[0] == "account" else [])
    out = fhold_client.fetch_holdings()
    assert out["available"] is True
    assert out["accounts"] == [] and out["positions"] == []


def test_run_fail_soft_on_timeout(monkeypatch):
    monkeypatch.setattr(fhold_client, "_cli", lambda: "/fake/fhold-cli")

    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="fhold-cli", timeout=10)

    monkeypatch.setattr(subprocess, "run", boom)
    assert fhold_client._run(["position", "list"]) is None


def test_run_fail_soft_on_bad_json(monkeypatch):
    monkeypatch.setattr(fhold_client, "_cli", lambda: "/fake/fhold-cli")
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout="not json", stderr=""),
    )
    assert fhold_client._run(["position", "list"]) is None
