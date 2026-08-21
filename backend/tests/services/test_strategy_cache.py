"""write_cache 同日 results 合并回归测试。

覆盖:
- 同一天子集写入不清掉其他策略的当日命中 (results 按策略 id 合并, 新写入覆盖同 id)
- 日期变更时 results 只保留本次写入, today_ever_matched / today_ever_rows 重置
- 同一天 today_ever_rows 对 symbol 取并集
"""
from __future__ import annotations

from pathlib import Path

from app.services.strategy_cache import read_cache, write_cache


def _res(as_of: str, symbols: list[str]) -> dict:
    return {
        "total": len(symbols),
        "as_of": as_of,
        "rows": [{"symbol": s, "close": 1.0} for s in symbols],
    }


def _symbols(cached: dict, sid: str) -> list[str]:
    return [r["symbol"] for r in cached["results"][sid]["rows"]]


def test_same_day_subset_write_keeps_other_strategies(tmp_path: Path) -> None:
    write_cache(tmp_path, "2026-08-20", {"A": _res("2026-08-20", ["a1"]), "B": _res("2026-08-20", ["b1"])})
    # 子集运行只写 A: B 的当日命中必须保留, A 被新结果覆盖
    write_cache(tmp_path, "2026-08-20", {"A": _res("2026-08-20", ["a2"])})

    cached = read_cache(tmp_path)
    assert cached is not None
    assert set(cached["results"]) == {"A", "B"}
    assert _symbols(cached, "A") == ["a2"]
    assert _symbols(cached, "B") == ["b1"]


def test_new_day_write_resets_results_and_today_ever(tmp_path: Path) -> None:
    write_cache(tmp_path, "2026-08-20", {"A": _res("2026-08-20", ["a1"])})
    # 新的一天只写 C: results 只剩 C, 旧策略 A 的曾命中集合被重置
    write_cache(tmp_path, "2026-08-21", {"C": _res("2026-08-21", ["c1"])})

    cached = read_cache(tmp_path)
    assert cached is not None
    assert cached["as_of"] == "2026-08-21"
    assert set(cached["results"]) == {"C"}
    assert _symbols(cached, "C") == ["c1"]
    assert set(cached["today_ever_matched"]) == {"C"}
    assert set(cached["today_ever_rows"]) == {"C"}


def test_today_ever_unions_same_day_symbols(tmp_path: Path) -> None:
    write_cache(tmp_path, "2026-08-20", {"A": _res("2026-08-20", ["a1", "a2"])})
    write_cache(tmp_path, "2026-08-20", {"A": _res("2026-08-20", ["a2", "a3"])})

    cached = read_cache(tmp_path)
    assert cached is not None
    assert cached["today_ever_matched"]["A"] == ["a1", "a2", "a3"]
    assert set(cached["today_ever_rows"]["A"]) == {"a1", "a2", "a3"}
    # results 本身仍是最新一次写入
    assert _symbols(cached, "A") == ["a2", "a3"]
