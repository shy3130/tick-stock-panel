"""账户模型读写校验测试。"""
from __future__ import annotations

import pytest

from app.services.trading.accounts import read_accounts, write_accounts

TS = "2026-08-04 14:30"


def _acct(**over):
    base = {
        "id": "default",
        "currency": "CNY",
        "capital": 500000,
        "horizonFundMonths": 12,
        "maxSingleRatio": 0.25,
        "changes": [],
    }
    base.update(over)
    return base


# ── 读取默认 ─────────────────────────────────────────────
def test_read_accounts_returns_default_when_absent(tmp_path):
    data = read_accounts(tmp_path)
    assert data["schemaVersion"] == 1
    assert len(data["accounts"]) == 1
    acc = data["accounts"][0]
    assert acc["id"] == "default"
    assert acc["capital"] == 0
    assert acc["changes"] == []


def test_read_accounts_defaults_on_corrupt(tmp_path):
    p = tmp_path / "user_data" / "trading" / "accounts.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    data = read_accounts(tmp_path)
    assert data["accounts"][0]["capital"] == 0


# ── 写入校验 ─────────────────────────────────────────────
def test_write_roundtrip(tmp_path):
    payload = {"schemaVersion": 1, "accounts": [_acct()]}
    out = write_accounts(tmp_path, payload)
    assert out["accounts"][0]["capital"] == 500000
    again = read_accounts(tmp_path)
    assert again["accounts"][0]["capital"] == 500000


def test_write_rejects_negative_capital(tmp_path):
    with pytest.raises(ValueError, match="capital"):
        write_accounts(tmp_path, {"accounts": [_acct(capital=-1)]})


def test_write_rejects_zero_max_single_ratio(tmp_path):
    with pytest.raises(ValueError, match="maxSingleRatio"):
        write_accounts(tmp_path, {"accounts": [_acct(maxSingleRatio=0)]})


def test_write_rejects_max_single_ratio_over_one(tmp_path):
    with pytest.raises(ValueError, match="maxSingleRatio"):
        write_accounts(tmp_path, {"accounts": [_acct(maxSingleRatio=1.5)]})


def test_write_rejects_nonpositive_horizon(tmp_path):
    with pytest.raises(ValueError, match="horizonFundMonths"):
        write_accounts(tmp_path, {"accounts": [_acct(horizonFundMonths=0)]})


# ── changes 只允许追加 ───────────────────────────────────
def test_changes_append_allowed(tmp_path):
    write_accounts(tmp_path, {"accounts": [_acct(changes=[
        {"ts": TS, "amount": 50000, "reason": "增资"},
    ])]})
    out = write_accounts(tmp_path, {"accounts": [_acct(changes=[
        {"ts": TS, "amount": 50000, "reason": "增资"},
        {"ts": "2026-08-05 10:00", "amount": 20000, "reason": "追加"},
    ])]})
    assert len(out["accounts"][0]["changes"]) == 2


def test_changes_shrink_rejected(tmp_path):
    write_accounts(tmp_path, {"accounts": [_acct(changes=[
        {"ts": TS, "amount": 50000, "reason": "增资"},
    ])]})
    with pytest.raises(ValueError, match="changes 只允许追加"):
        write_accounts(tmp_path, {"accounts": [_acct(changes=[])]})


def test_changes_history_rewrite_rejected(tmp_path):
    write_accounts(tmp_path, {"accounts": [_acct(changes=[
        {"ts": TS, "amount": 50000, "reason": "增资"},
    ])]})
    with pytest.raises(ValueError, match="changes 历史记录不可改写"):
        write_accounts(tmp_path, {"accounts": [_acct(changes=[
            {"ts": TS, "amount": 99999, "reason": "篡改"},
            {"ts": "2026-08-05 10:00", "amount": 20000, "reason": "追加"},
        ])]})


def test_new_account_no_changes_constraint(tmp_path):
    write_accounts(tmp_path, {"accounts": [_acct(id="a", changes=[
        {"ts": TS, "amount": 10, "reason": "x"},
    ])]})
    # 新增第二个账户,changes 无历史约束
    out = write_accounts(tmp_path, {"accounts": [
        _acct(id="a", changes=[{"ts": TS, "amount": 10, "reason": "x"}]),
        _acct(id="b", changes=[]),
    ]})
    assert {a["id"] for a in out["accounts"]} == {"a", "b"}
