from __future__ import annotations

import pytest

from app.services import strategy_cache


def _result(*symbols: str) -> dict:
    return {
        "total": len(symbols),
        "as_of": "2026-07-20",
        "rows": [{"symbol": symbol, "close": index + 1.0} for index, symbol in enumerate(symbols)],
    }


def test_strict_publication_reports_write_failure_and_preserves_old_snapshot(tmp_path, monkeypatch):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"strategy_a": _result("000001.SZ")})

    def fail(*args):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(strategy_cache.os, "replace", fail)
    with pytest.raises(OSError):
        strategy_cache.write_cache(tmp_path, "2026-07-20", {"strategy_a": _result("000002.SZ")}, strict=True)
    assert strategy_cache.read_cache(tmp_path)["results"]["strategy_a"]["rows"][0]["symbol"] == "000001.SZ"
    # Existing best-effort writers remain backward compatible.
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"strategy_a": _result("000003.SZ")})


def test_same_day_partial_writes_merge_strategy_results(tmp_path):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"strategy_a": _result("000001.SZ")})
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"strategy_b": _result("600000.SH")})

    cached = strategy_cache.read_cache(tmp_path)

    assert set(cached["results"]) == {"strategy_a", "strategy_b"}
    assert cached["results"]["strategy_a"]["rows"][0]["symbol"] == "000001.SZ"
    assert cached["results"]["strategy_b"]["rows"][0]["symbol"] == "600000.SH"


def test_same_day_update_replaces_only_target_strategy_and_keeps_ever_rows(tmp_path):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {
        "strategy_a": _result("000001.SZ"),
        "strategy_b": _result("600000.SH"),
    })
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"strategy_a": _result("000002.SZ")})

    cached = strategy_cache.read_cache(tmp_path)

    assert [row["symbol"] for row in cached["results"]["strategy_a"]["rows"]] == ["000002.SZ"]
    assert [row["symbol"] for row in cached["results"]["strategy_b"]["rows"]] == ["600000.SH"]
    assert set(cached["today_ever_rows"]["strategy_a"]) == {"000001.SZ", "000002.SZ"}


def test_new_date_resets_results_and_ever_rows(tmp_path):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"strategy_a": _result("000001.SZ")})
    next_day = _result("600000.SH")
    next_day["as_of"] = "2026-07-21"

    strategy_cache.write_cache(tmp_path, "2026-07-21", {"strategy_b": next_day})
    cached = strategy_cache.read_cache(tmp_path)

    assert cached["as_of"] == "2026-07-21"
    assert set(cached["results"]) == {"strategy_b"}
    assert set(cached["today_ever_rows"]) == {"strategy_b"}
