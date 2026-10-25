"""守护档案存储测试: 幂等基线、复验滚动上限、状态转换与路径防护。"""
from __future__ import annotations

import json

import pytest

from app.services.guardian_store import (
    GuardianStore,
    GuardianStoreError,
    GuardianStoreValidationError,
)


def _baseline_kwargs(**overrides):
    params = dict(
        kind="mining_gate",
        data_as_of="2026-01-09",
        objective="sharpe",
        metrics={"score": 0.9, "n_trades": 70, "max_drawdown": -0.12},
        provenance={"origin_run_id": "run-1"},
    )
    params.update(overrides)
    return params


def test_create_and_get_roundtrip(tmp_path):
    store = GuardianStore(tmp_path)
    profile = store.create_baseline("mined_factor_abc", **_baseline_kwargs())

    fetched = store.get("mined_factor_abc")
    assert fetched is not None
    assert fetched["status"] == "pending"
    assert fetched["baseline"]["kind"] == "mining_gate"
    assert fetched["baseline"]["data_as_of"] == "2026-01-09"
    assert fetched["baseline"]["objective"] == "sharpe"
    assert fetched["baseline"]["metrics"]["score"] == 0.9
    assert profile["strategy_id"] == "mined_factor_abc"


def test_create_baseline_idempotent_keeps_history(tmp_path):
    store = GuardianStore(tmp_path)
    store.create_baseline("mined_factor_abc", **_baseline_kwargs())
    store.append_check("mined_factor_abc", {"verdict": "healthy"})
    store.set_status("mined_factor_abc", "healthy")

    again = store.create_baseline(
        "mined_factor_abc", **_baseline_kwargs(metrics={"score": 9.9})
    )
    assert again["baseline"]["metrics"]["score"] == 0.9  # 未覆盖
    assert again["status"] == "healthy"
    assert len(again["checks"]) == 1


def test_create_baseline_validates(tmp_path):
    store = GuardianStore(tmp_path)
    with pytest.raises(GuardianStoreValidationError):
        store.create_baseline("s", kind="bogus", data_as_of="2026-01-09",
                              objective="sharpe", metrics={})
    with pytest.raises(GuardianStoreValidationError):
        store.create_baseline("s", kind="mining_gate", data_as_of="not-a-date",
                              objective="sharpe", metrics={})


def test_unsafe_strategy_id_rejected(tmp_path):
    store = GuardianStore(tmp_path)
    with pytest.raises(GuardianStoreValidationError):
        store.get("../../etc/passwd")
    with pytest.raises(GuardianStoreValidationError):
        store.drop("../escape")


def test_append_check_caps_at_52(tmp_path):
    from app.backtest.guardian import MAX_CHECKS

    store = GuardianStore(tmp_path)
    store.create_baseline("mined_factor_abc", **_baseline_kwargs())
    for i in range(MAX_CHECKS + 10):
        store.append_check("mined_factor_abc", {"verdict": "healthy", "i": i})
    checks = store.get("mined_factor_abc")["checks"]
    assert len(checks) == MAX_CHECKS
    assert checks[0]["i"] == 10  # 最旧的被滚动裁掉


def test_set_status_transitions_and_reason(tmp_path):
    store = GuardianStore(tmp_path)
    store.create_baseline("mined_factor_abc", **_baseline_kwargs())
    profile = store.set_status("mined_factor_abc", "watch", reason="退化 30%")
    assert profile["status"] == "watch"
    assert profile["status_reason"] == "退化 30%"
    assert profile["status_since"] is not None

    with pytest.raises(GuardianStoreValidationError):
        store.set_status("mined_factor_abc", "exploded")


def test_set_baseline_metrics_only_for_trailing(tmp_path):
    store = GuardianStore(tmp_path)
    store.create_baseline("mined_factor_abc", **_baseline_kwargs())
    with pytest.raises(GuardianStoreValidationError):
        store.set_baseline_metrics("mined_factor_abc", {"score": 1.0})

    store.create_baseline(
        "my_manual_strategy",
        **_baseline_kwargs(kind="trailing_reference", metrics={}),
    )
    profile = store.set_baseline_metrics("my_manual_strategy", {"score": 1.2})
    assert profile["baseline"]["metrics"]["score"] == 1.2
    assert profile["baseline"]["calibrated_at"]


def test_drop_removes_only_our_file(tmp_path):
    store = GuardianStore(tmp_path)
    store.create_baseline("mined_factor_abc", **_baseline_kwargs())
    assert store.drop("mined_factor_abc") is True
    assert store.get("mined_factor_abc") is None
    assert store.drop("mined_factor_abc") is False


def test_append_missing_profile_raises_key_error(tmp_path):
    store = GuardianStore(tmp_path)
    with pytest.raises(KeyError):
        store.append_check("ghost", {"verdict": "healthy"})


def test_list_profiles_skips_unreadable(tmp_path):
    store = GuardianStore(tmp_path)
    store.create_baseline("mined_factor_abc", **_baseline_kwargs())
    (tmp_path / "user_data" / "strategy_guardian" / "broken.json").write_text(
        "{not json", encoding="utf-8"
    )
    profiles = store.list_profiles()
    assert [p["strategy_id"] for p in profiles] == ["mined_factor_abc"]


def test_corrupt_profile_raises_on_direct_get(tmp_path):
    store = GuardianStore(tmp_path)
    path = tmp_path / "user_data" / "strategy_guardian" / "bad.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"status": "weird"}), encoding="utf-8")
    with pytest.raises(GuardianStoreError):
        store.get("bad")


def test_list_profiles_excludes_combinations_file(tmp_path):
    """combinations.json 是组合建议数据, 不能被当成策略档案列出 (P2d)。"""
    from app.services.guardian_store import write_combinations

    store = GuardianStore(tmp_path)
    store.create_baseline("mined_factor_abc", **_baseline_kwargs())
    write_combinations(
        tmp_path,
        window_start="2026-04-01",
        window_end="2026-10-01",
        pairs_evaluated=1,
        suggestions=[
            {
                "a": "mined_factor_abc",
                "b": "mined_factor_def",
                "correlation": -0.1,
                "common_days": 40,
            }
        ],
    )
    profiles = store.list_profiles()
    assert [p["strategy_id"] for p in profiles] == ["mined_factor_abc"]
