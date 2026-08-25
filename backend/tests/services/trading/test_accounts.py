"""账户模型读写校验测试。"""
from __future__ import annotations

import pytest

from app.services.trading.accounts import read_accounts, settle_trade, write_accounts

TS = "2026-08-04 14:30"


def _acct(**over):
    base = {
        "id": "default",
        "currency": "CNY",
        "capital": 500000,
        "horizonFundMonths": 12,
        "maxSingleRatio": 0.25,
        "changes": [],
        "settlements": [],
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
    # 首次写入: capital 作为基线(即使与 changes 合计不等也允许)
    write_accounts(tmp_path, {"accounts": [_acct(changes=[
        {"ts": TS, "amount": 50000, "reason": "增资"},
    ])]})
    # 追加时: capital 必须 = 旧 capital + 本次新增 changes amount 合计
    out = write_accounts(tmp_path, {"accounts": [_acct(capital=520000, changes=[
        {"ts": TS, "amount": 50000, "reason": "增资"},
        {"ts": "2026-08-05 10:00", "amount": 20000, "reason": "追加"},
    ])]})
    assert len(out["accounts"][0]["changes"]) == 2
    assert out["accounts"][0]["capital"] == 520000


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


def test_close_settlement_updates_capital_once(tmp_path):
    write_accounts(tmp_path, {"accounts": [_acct()]})
    trade = {
        "tradeId": "600519.SH_1",
        "accountId": "default",
        "symbol": "600519.SH",
        "status": "已平仓",
        "realizedPnl": 1200.5,
        "closedAt": TS,
    }
    first = settle_trade(tmp_path, trade, TS)
    second = settle_trade(tmp_path, trade, "2026-08-05 10:00")
    assert second == first
    account = read_accounts(tmp_path)["accounts"][0]
    assert account["capital"] == 501200.5
    assert len(account["settlements"]) == 1
    assert len(account["changes"]) == 1
    assert account["changes"][0]["kind"] == "settlement"


def test_settlement_history_rewrite_rejected(tmp_path):
    write_accounts(tmp_path, {"accounts": [_acct()]})
    trade = {
        "tradeId": "t1",
        "status": "已平仓",
        "realizedPnl": 100,
    }
    settle_trade(tmp_path, trade, TS)
    account = read_accounts(tmp_path)["accounts"][0]
    tampered = {**account, "settlements": [{**account["settlements"][0], "realizedPnl": 999}]}
    with pytest.raises(ValueError, match="settlements 历史记录不可改写"):
        write_accounts(tmp_path, {"accounts": [tampered]})


# ── append-only capital 增量与账户保留契约 ─────────────────────
def test_capital_must_not_change_without_new_changes(tmp_path):
    """无新增流水时改 capital 必须被拒绝(delta != 0 但 appended sum=0)。"""
    write_accounts(tmp_path, {"accounts": [_acct(capital=500000)]})
    with pytest.raises(ValueError, match=r"capital 增量 .* 不一致"):
        write_accounts(tmp_path, {"accounts": [_acct(capital=600000)]})


def test_capital_delta_must_equal_appended_changes_sum(tmp_path):
    """capital 增量必须精确匹配本次新增 changes 的 amount 总和(容忍浮点噪声)。"""
    write_accounts(tmp_path, {"accounts": [_acct()]})
    # delta=10000 但新增 changes 合计 30000 → 拒绝
    with pytest.raises(ValueError, match=r"capital 增量 .* 与本次新增 changes.*不一致"):
        write_accounts(tmp_path, {"accounts": [_acct(
            capital=510000,
            changes=[
                {"ts": TS, "amount": 30000, "reason": "不匹配增资"},
            ],
        )]})


def test_large_capital_delta_rejects_absolute_mismatch(tmp_path):
    """大额变更也不得用相对误差吞掉实际金额差异。"""
    write_accounts(tmp_path, {"accounts": [_acct(capital=2_000_000_000)]})
    with pytest.raises(ValueError, match=r"capital 增量 .* 与本次新增 changes.*不一致"):
        write_accounts(tmp_path, {"accounts": [_acct(
            capital=3_000_000_000.01,
            changes=[
                {"ts": TS, "amount": 1_000_000_000, "reason": "大额增资"},
            ],
        )]})


def test_missing_persisted_account_id_rejected(tmp_path):
    """已有持久化账户 id 不得从请求中消失。"""
    write_accounts(tmp_path, {"accounts": [
        _acct(id="acc1", capital=100000),
        _acct(id="acc2", capital=200000),
    ]})
    with pytest.raises(ValueError, match=r"已有账户.*缺失|不允许删除"):
        # 只提交 acc1, 遗漏 acc2
        write_accounts(tmp_path, {"accounts": [
            _acct(id="acc1", capital=100000),
        ]})


def test_new_change_amount_rejects_non_numeric_and_bool_and_nan_inf(tmp_path):
    """新增 change.amount 必须是有限数值; bool/NaN/inf 应返回清晰 ValueError。"""
    write_accounts(tmp_path, {"accounts": [_acct()]})
    bad_cases = [
        {"ts": TS, "amount": "123", "reason": "str"},
        {"ts": TS, "amount": True, "reason": "bool"},
        {"ts": TS, "amount": float("nan"), "reason": "nan"},
        {"ts": TS, "amount": float("inf"), "reason": "inf"},
    ]
    for bad in bad_cases:
        with pytest.raises(ValueError, match="amount 必须是数值"):
            write_accounts(tmp_path, {"accounts": [_acct(changes=[bad]) ]})


def test_first_write_and_new_account_capital_are_baseline_free(tmp_path):
    """首次写入或新增账户的 capital 是任意基线, 不要求匹配其 changes 合计。"""
    # 首次: capital=999 与 changes 合计 100 仍允许(基线)
    out1 = write_accounts(tmp_path, {"accounts": [_acct(id="first", capital=999, changes=[
        {"ts": TS, "amount": 100, "reason": "基线不匹配也行"},
    ])]})
    assert out1["accounts"][0]["capital"] == 999

    # 已有 first 后, 新增 second 账户, second capital 任意
    out2 = write_accounts(tmp_path, {"accounts": [
        _acct(id="first", capital=999, changes=[{"ts": TS, "amount": 100, "reason": "基线不匹配也行"}]),
        _acct(id="second", capital=12345, changes=[]),
    ]})
    ids = {a["id"] for a in out2["accounts"]}
    assert ids == {"first", "second"}
    assert out2["accounts"][1]["capital"] == 12345
