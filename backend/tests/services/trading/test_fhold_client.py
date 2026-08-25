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


def test_cli_prefers_explicit_override_over_workspace(monkeypatch):
    monkeypatch.setenv("FHOLD_CLI", "/configured/fhold-cli")
    monkeypatch.setattr(
        fhold_client.shutil,
        "which",
        lambda path: path if path == "/configured/fhold-cli" else None,
    )
    monkeypatch.setattr(fhold_client, "_workspace_cli", lambda: "/workspace/fhold-cli")

    assert fhold_client._cli() == "/configured/fhold-cli"


def test_cli_prefers_workspace_binary_when_unconfigured(monkeypatch):
    monkeypatch.delenv("FHOLD_CLI", raising=False)
    monkeypatch.setattr(fhold_client, "_workspace_cli", lambda: "/workspace/fhold-cli")
    monkeypatch.setattr(fhold_client.shutil, "which", lambda _path: "/path/fhold-cli")

    assert fhold_client._cli() == "/workspace/fhold-cli"


def test_cli_uses_path_only_when_workspace_binary_is_absent(monkeypatch):
    monkeypatch.delenv("FHOLD_CLI", raising=False)
    monkeypatch.setattr(fhold_client, "_workspace_cli", lambda: None)
    monkeypatch.setattr(fhold_client.shutil, "which", lambda _path: "/path/fhold-cli")

    assert fhold_client._cli() == "/path/fhold-cli"


def test_invalid_explicit_cli_does_not_fall_back(monkeypatch):
    monkeypatch.setenv("FHOLD_CLI", "/missing/fhold-cli")
    monkeypatch.setattr(fhold_client.shutil, "which", lambda _path: None)
    monkeypatch.setattr(fhold_client, "_workspace_cli", lambda: "/workspace/fhold-cli")

    assert fhold_client._cli() is None


def test_fetch_holdings_maps_positions(monkeypatch):
    accounts = [
        {"id": 1, "name": "银河证券", "broker": "银河", "is_default": True, "is_active": True}
    ]
    positions = [
        {
            "account_id": 1,
            "code": "000988",
            "name": "华工科技",
            "quantity": 1600,
            "amount": 156192,
            "cost_price": 124.868,
            "current_price": 97.62,
            "holding_pnl": -43597.44,
            "holding_pnl_ratio": -0.2182,
            "source_date": "2026-08-04T00:00:00Z",
            "updated_at": "2026-08-04T02:23:29Z",
        }
    ]

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
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout="not json", stderr=""),
    )
    assert fhold_client._run(["position", "list"]) is None


def _consistent_snapshot(items, *, accounts=None):
    return {
        "items": items,
        "accounts": [] if accounts is None else accounts,
        "total": len(items),
        "count": len(items),
        "consistent": True,
        "mode": "local",
    }


def test_fetch_transactions_uses_single_read_only_consistent_snapshot(monkeypatch):
    calls = []
    accounts = [
        {"id": 7, "name": "银河证券", "broker": "银河", "is_default": True, "is_active": True}
    ]
    transactions = _consistent_snapshot(
        [
            {
                "id": 101,
                "account_id": 7,
                "trade_date": "2024-02-05",
                "trade_type": "buy",
            }
        ],
        accounts=accounts,
    )

    def fake_run(args):
        calls.append(args)
        return transactions

    monkeypatch.setattr(fhold_client, "_run", fake_run)

    result = fhold_client.fetch_transactions()

    assert calls == [["tx", "snapshot"]]
    assert result["available"] is True
    assert result["accounts"][0]["name"] == "银河证券"
    assert result["transactions"] == transactions["items"]


def test_fetch_transactions_is_unavailable_when_snapshot_command_fails(monkeypatch):
    monkeypatch.setattr(fhold_client, "_run", lambda _args: None)

    assert fhold_client.fetch_transactions() == {
        "available": False,
        "accounts": [],
        "transactions": [],
    }


def test_fetch_transactions_fails_closed_without_local_consistency_proof(monkeypatch):
    snapshot = _consistent_snapshot([{"id": 101}])
    snapshot["mode"] = "http"
    monkeypatch.setattr(fhold_client, "_run", lambda _args: snapshot)

    assert fhold_client.fetch_transactions() == {
        "available": False,
        "accounts": [],
        "transactions": [],
    }


def test_fetch_transactions_fails_closed_on_snapshot_count_or_ids(monkeypatch):
    snapshot = _consistent_snapshot([{"id": 101}, {"id": 101}])
    monkeypatch.setattr(fhold_client, "_run", lambda _args: snapshot)

    assert fhold_client.fetch_transactions() == {
        "available": False,
        "accounts": [],
        "transactions": [],
    }


def test_fetch_transactions_fails_closed_when_snapshot_omits_accounts(monkeypatch):
    snapshot = _consistent_snapshot([{"id": 101}])
    del snapshot["accounts"]
    monkeypatch.setattr(fhold_client, "_run", lambda _args: snapshot)

    assert fhold_client.fetch_transactions() == {
        "available": False,
        "accounts": [],
        "transactions": [],
    }


def test_run_does_not_log_cli_payload_on_failure(monkeypatch, caplog):
    secret = "account=银河证券,amount=12345.67"
    monkeypatch.setattr(fhold_client, "_cli", lambda: "/fake/fhold-cli")
    monkeypatch.setattr(
        fhold_client.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "", secret),
    )

    assert fhold_client._run(["tx", "list"]) is None

    assert secret not in caplog.text
