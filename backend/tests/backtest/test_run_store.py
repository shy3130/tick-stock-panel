"""BacktestRunStore 单元测试 — 不可变持久化、白名单、原子写、迁移、比较、导出。

全部使用 tmp_path, 不触碰真实 data/ 目录。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from app.backtest.run_store import (
    LEGACY_RUN_CARD_WARNING,
    MAX_CONFIG_DIFF_ENTRIES,
    BacktestRun,
    BacktestRunStore,
    LegacyRunCardReadOnly,
    RunIdError,
    RunTooLargeError,
    RunSubject,
    check_run_id,
    compare_runs,
    export_csv,
    summarize,
)

from app.services.research_registry import ResearchStore


def _make_run(run_id: str = "abc123def0", **overrides) -> BacktestRun:
    defaults = dict(
        run_id=run_id,
        kind="strategy",
        created_at="2026-08-19T00:00:00+00:00",
        subject=RunSubject(id="macd", name="MACD", hash="h1"),
        config={
            "strategy_id": "macd",
            "symbols": ["600000.SH"],
            "start": "2026-01-01",
            "end": "2026-06-30",
            "fees_pct": 0.0002,
            "slippage_bps": 5.0,
        },
        data_snapshot={
            "snapshot_hash": "snap-1",
            "canonical_generation": "g1",
            "universe_definition": {"type": "explicit_symbols", "hash": "u1"},
        },
        benchmark={"symbol": "000001.INDEX"},
        cost_model={"fees_pct": 0.0002, "slippage_bps": 5.0},
        metric_context={"version": "1", "return_frequency": "daily"},
        engine_version="polars-numpy-v1",
        stats={"sharpe": 1.5, "total_return": 0.2, "max_drawdown": -0.1, "win_rate": 0.55},
        equity_curve=[{"date": "2026-01-01", "equity": 1.0}, {"date": "2026-01-02", "equity": 1.01}],
        drawdown_curve=[{"date": "2026-01-01", "value": 0.0}],
        benchmark_curve=[{"date": "2026-01-01", "close": 3000.0}],
        trades=[{"symbol": "600000.SH", "entry_date": "2026-01-05", "pnl": 120.5}],
        warnings=[],
    )
    defaults.update(overrides)
    return BacktestRun(**defaults)


# ── run_id 白名单 / 路径穿越 ──────────────────────────────


@pytest.mark.parametrize("bad", ["../etc/passwd", "..%2f", "a/b", ".hidden", "", "x" * 65, "run id", "-lead"])
def test_check_run_id_rejects_dangerous(bad: str):
    with pytest.raises(RunIdError):
        check_run_id(bad)


@pytest.mark.parametrize("good", ["abc123def0", "A-b_2", "9startok"])
def test_check_run_id_accepts_safe(good: str):
    assert check_run_id(good) == good


def test_dangerous_run_id_does_not_escape_dir(tmp_path: Path):
    store = BacktestRunStore(tmp_path)
    with pytest.raises(RunIdError):
        store.get("../evil")
    with pytest.raises(RunIdError):
        store.delete("..")
    with pytest.raises(RunIdError):
        store.patch("..", favorite=True)
    # 目录外没有产生任何文件
    assert not (tmp_path.parent / "evil.json").exists()
    assert list(tmp_path.rglob("*.json")) == []


# ── 不可变性 ──────────────────────────────────────────────


def test_save_and_read_roundtrip(tmp_path: Path):
    store = BacktestRunStore(tmp_path)
    run = _make_run()
    store.save(run)
    assert (tmp_path / "research" / "backtest_runs" / "abc123def0.json").exists()
    loaded = store.get("abc123def0")
    assert loaded.run_id == run.run_id
    assert loaded.equity_curve == run.equity_curve
    assert loaded.trades == run.trades
    assert loaded.stats == run.stats


def test_save_refuses_to_overwrite_existing_immutable_run(tmp_path: Path):
    store = BacktestRunStore(tmp_path)
    store.save(_make_run(stats={"sharpe": 1.5}))

    with pytest.raises(FileExistsError):
        store.save(_make_run(stats={"sharpe": 99.0}))

    assert store.get("abc123def0").stats["sharpe"] == 1.5


def test_non_finite_values_become_null(tmp_path: Path):
    store = BacktestRunStore(tmp_path)
    run = _make_run(stats={"sharpe": math.inf, "nan": math.nan, "ok": 1.25})
    store.save(run)
    raw = json.loads((tmp_path / "research" / "backtest_runs" / "abc123def0.json").read_text())
    assert raw["stats"]["sharpe"] is None
    assert raw["stats"]["nan"] is None
    assert raw["stats"]["ok"] == 1.25


def test_patch_only_changes_favorite_and_label(tmp_path: Path):
    store = BacktestRunStore(tmp_path)
    store.save(_make_run())
    updated = store.patch("abc123def0", favorite=True, label="我的实验")
    assert updated.favorite is True
    assert updated.label == "我的实验"
    # 核心字段不变
    again = store.get("abc123def0")
    assert again.trades == _make_run().trades
    assert again.stats["sharpe"] == 1.5


def test_patch_missing_run_raises_keyerror(tmp_path: Path):
    with pytest.raises(KeyError):
        BacktestRunStore(tmp_path).patch("nonexist1", favorite=True)


def test_corrupt_new_contract_run_is_treated_as_missing(tmp_path: Path):
    """单个损坏文件不能穿透存储边界变成 API 500。"""
    run_dir = tmp_path / "research" / "backtest_runs"
    run_dir.mkdir(parents=True)
    (run_dir / "badjson001.json").write_text("{not-json", encoding="utf-8")

    store = BacktestRunStore(tmp_path)
    with pytest.raises(KeyError):
        store.get("badjson001")
    assert store.list_runs()["total"] == 0


# ── 20 MiB 上限 + 原子写 ──────────────────────────────────


def test_oversized_run_rejected_no_partial_file(tmp_path: Path, monkeypatch):
    import app.backtest.run_store as rs

    monkeypatch.setattr(rs, "MAX_RUN_FILE_BYTES", 1024)
    store = BacktestRunStore(tmp_path)
    big = _make_run(equity_curve=[{"date": "2026-01-01", "equity": 1.0}] * 10_000)
    with pytest.raises(RunTooLargeError):
        store.save(big)
    run_dir = tmp_path / "research" / "backtest_runs"
    assert not (run_dir / "abc123def0.json").exists()
    # 原子写: 失败不留半文件、不留 .tmp
    assert not list(run_dir.glob("*.tmp"))
    assert list(run_dir.glob("*.json")) == []


def test_atomic_write_leaves_no_tmp_on_success(tmp_path: Path):
    store = BacktestRunStore(tmp_path)
    store.save(_make_run())
    run_dir = tmp_path / "research" / "backtest_runs"
    assert not list(run_dir.glob("*.tmp"))


# ── 删除 ──────────────────────────────────────────────────


def test_delete_only_removes_explicit_run(tmp_path: Path):
    store = BacktestRunStore(tmp_path)
    store.save(_make_run())
    assert store.delete("abc123def0") is True
    assert not store.exists("abc123def0")
    assert store.delete("abc123def0") is False  # 再删 → False, 不误删


# ── 列表过滤/分页 ─────────────────────────────────────────


def test_list_filters_and_pagination(tmp_path: Path):
    store = BacktestRunStore(tmp_path)
    store.save(_make_run("run0000001", kind="strategy", created_at="2026-08-01T00:00:00+00:00"))
    store.save(_make_run("run0000002", kind="factor", created_at="2026-08-02T00:00:00+00:00", trades=[], stats={}))
    store.save(_make_run("run0000003", kind="strategy", created_at="2026-08-03T00:00:00+00:00"))

    out = store.list_runs()
    assert out["total"] == 3
    assert [i["run_id"] for i in out["items"]] == ["run0000003", "run0000002", "run0000001"]

    only_factor = store.list_runs(kind="factor")
    assert only_factor["total"] == 1
    assert only_factor["items"][0]["kind"] == "factor"

    # query 命中 label / subject
    store.patch("run0000001", label="动量实验")
    assert store.list_runs(query="动量")["total"] == 1

    # favorite 过滤
    store.patch("run0000003", favorite=True)
    assert store.list_runs(favorite=True)["total"] == 1

    # 分页
    page = store.list_runs(limit=2, offset=1)
    assert page["total"] == 3
    assert [i["run_id"] for i in page["items"]] == ["run0000002", "run0000001"]


def test_list_summary_is_lightweight(tmp_path: Path):
    store = BacktestRunStore(tmp_path)
    store.save(_make_run())
    item = store.list_runs()["items"][0]
    assert "equity_curve" not in item
    assert "trades" not in item
    assert item["n_trades"] == 1
    assert item["n_points"] == 2
    assert item["stats"]["sharpe"] == 1.5


# ── 比较器 ────────────────────────────────────────────────


def test_compare_two_runs_matrix_and_curves():
    a = _make_run("runaaaaaa1", stats={"sharpe": 1.5, "total_return": 0.2})
    b = _make_run(
        "runbbbbbb2",
        stats={"sharpe": 0.8, "total_return": 0.05},
        config={"strategy_id": "macd", "start": "2026-02-01", "end": "2026-07-31"},
        data_snapshot={
            "snapshot_hash": "snap-2",
            "canonical_generation": "g2",
            "universe_definition": {"type": "explicit_symbols", "hash": "u2"},
        },
        benchmark={"symbol": "399001.INDEX"},
        metric_context={"version": "2"},
    )
    out = compare_runs([a, b])
    assert out["metric_matrix"]["sharpe"] == {"runaaaaaa1": 1.5, "runbbbbbb2": 0.8}
    assert {c["run_id"] for c in out["curves"]} == {"runaaaaaa1", "runbbbbbb2"}
    warns = " ".join(out["warnings"])
    assert "compare.interval_mismatch" in warns
    assert "compare.universe_mismatch" in warns
    assert "compare.benchmark_mismatch" in warns
    assert "compare.canonical_generation_mismatch" in warns
    assert "compare.metric_version_mismatch" in warns


def test_compare_identical_contexts_no_warnings():
    a = _make_run("runaaaaaa1")
    b = _make_run("runbbbbbb2")
    out = compare_runs([a, b])
    assert out["warnings"] == []


def test_compare_warns_for_metric_cost_engine_and_curve_semantics():
    a = _make_run("runaaaaaa1")
    b = _make_run(
        "runbbbbbb2",
        config={
            **a.config,
            "mode": "full",
        },
        stats={
            **a.stats,
            "full_kind": "candidate_execution",
        },
        metric_context={
            **a.metric_context,
            "risk_free_rate": 0.03,
        },
        cost_model={"fees_pct": 0.0005, "slippage_bps": 8.0},
        engine_version="polars-numpy-v2",
    )

    warnings = " ".join(compare_runs([a, b])["warnings"])

    assert "compare.metric_context_mismatch" in warnings
    assert "compare.cost_model_mismatch" in warnings
    assert "compare.engine_version_mismatch" in warnings
    assert "compare.curve_semantics_mismatch" in warnings


# ── 配置差异 / 交易变化 (相对 baseline 的 diff) ─────────────


def _trade(symbol: str, entry: str, exit_: str, **kw) -> dict:
    row = {"symbol": symbol, "entry_date": entry, "exit_date": exit_}
    row.update(kw)
    return row


def test_compare_output_additive_schema_keeps_existing_keys():
    out = compare_runs([_make_run("runaaaaaa1"), _make_run("runbbbbbb2")])
    assert set(out) >= {"runs", "metric_matrix", "curves", "warnings", "config_diff", "trade_summary"}


def test_compare_config_diff_recursive_nested_and_stable():
    a = _make_run("runaaaaaa1", config={
        "strategy_id": "macd",
        "symbols": ["600000.SH", "600519.SH"],
        "start": "2026-01-01",
        "end": "2026-06-30",
        "params": {"fast": 12, "slow": 26, "nested": {"x": 1, "keep": 2}},
    })
    b = _make_run("runbbbbbb2", config={
        "strategy_id": "macd",
        "symbols": ["600519.SH", "000001.SZ", "600000.SH"],  # 顺序无关, 仅 000001.SZ 新增
        "start": "2026-01-01",
        "end": "2026-06-30",
        "params": {"fast": 12, "slow": 30, "nested": {"x": 1, "keep": 2, "new": 5}},
    })
    diff = compare_runs([a, b])["config_diff"]
    assert diff["baseline_run_id"] == "runaaaaaa1"
    assert [c["run_id"] for c in diff["candidates"]] == ["runbbbbbb2"]
    cand = diff["candidates"][0]
    entries = cand["entries"]
    by_key = {(e["path"], e["op"]): e for e in entries}
    assert by_key[("params.slow", "changed")] == {"path": "params.slow", "op": "changed", "before": 26, "after": 30}
    assert by_key[("params.nested.new", "added")]["after"] == 5
    # 标量 list 按多重集比较: 顺序无关, 仅元素增删, 不产生 reordered 噪音
    assert [e for e in entries if e["path"] == "symbols"] == [
        {"path": "symbols", "op": "added", "before": None, "after": "000001.SZ"},
    ]
    # 稳定排序: path 递增
    paths = [e["path"] for e in entries]
    assert paths == sorted(paths)
    assert cand["total"] == len(entries)
    assert cand["truncated"] is False


def test_compare_config_diff_structured_list_by_index():
    a = _make_run("runaaaaaa1", config={
        "children": [{"strategy_id": "x", "weight": 0.5}, {"strategy_id": "y", "weight": 0.5}],
    })
    b = _make_run("runbbbbbb2", config={
        "children": [{"strategy_id": "x", "weight": 0.6}],
    })
    entries = compare_runs([a, b])["config_diff"]["candidates"][0]["entries"]
    by_key = {(e["path"], e["op"]): e for e in entries}
    assert by_key[("children[0].weight", "changed")]["after"] == 0.6
    assert by_key[("children[1]", "removed")]["before"]["strategy_id"] == "y"


def test_compare_config_diff_identical_configs_empty():
    cand = compare_runs([_make_run("runaaaaaa1"), _make_run("runbbbbbb2")])["config_diff"]["candidates"][0]
    assert cand["total"] == 0
    assert cand["entries"] == []
    assert cand["truncated"] is False


def test_compare_config_diff_caps_entries_total_complete():
    a = _make_run("runaaaaaa1", config={"symbols": []})
    b = _make_run("runbbbbbb2", config={"symbols": [f"S{i:04d}" for i in range(250)]})
    cand = compare_runs([a, b])["config_diff"]["candidates"][0]
    assert cand["total"] == 250  # 总数完整
    assert cand["truncated"] is True
    assert len(cand["entries"]) == MAX_CONFIG_DIFF_ENTRIES  # 样本受限


def test_compare_config_diff_caps_long_list_values():
    a = _make_run("runaaaaaa1", config={"watchlist": [{"s": f"S{i}"} for i in range(80)]})
    b = _make_run("runbbbbbb2", config={})
    entry = compare_runs([a, b])["config_diff"]["candidates"][0]["entries"][0]
    assert entry["op"] == "removed"
    assert len(entry["before"]) == 51  # 50 个元素 + 1 个 "…(+30)" 截断标记
    assert entry["after"] is None


def test_compare_trade_summary_common_added_removed():
    a = _make_run("runaaaaaa1", trades=[
        _trade("600000.SH", "2026-01-05", "2026-01-10", shares=100, entry_value=1000.0, pnl_pct=0.05),
        _trade("600519.SH", "2026-02-01", "2026-02-15", shares=10, entry_value=18000.0, pnl_pct=-0.02),
    ])
    b = _make_run("runbbbbbb2", trades=[
        _trade("600000.SH", "2026-01-05", "2026-01-10", shares=100, entry_value=1000.0, pnl_pct=0.05),
        _trade("000001.SZ", "2026-03-01", "2026-03-05", shares=500, entry_value=4500.0, pnl_pct=0.01),
    ])
    summary = compare_runs([a, b])["trade_summary"]
    assert summary["baseline_run_id"] == "runaaaaaa1"
    assert summary["baseline_n_trades"] == 2
    cand = summary["candidates"][0]
    assert cand["n_trades"] == 2
    assert cand["common"] == 1
    assert cand["common_value_diff"] == 0
    assert cand["added"] == 1
    assert cand["removed"] == 1
    assert cand["samples"]["added"][0]["symbol"] == "000001.SZ"
    assert cand["samples"]["removed"][0]["symbol"] == "600519.SH"
    assert cand["samples"]["common"][0]["value_differs"] is False


def test_compare_trade_same_identity_different_shares_counts_common():
    a = _make_run("runaaaaaa1", trades=[
        _trade("600000.SH", "2026-01-05", "2026-01-10", shares=100, entry_value=1000.0, exit_value=1050.0, pnl_pct=0.05),
    ])
    b = _make_run("runbbbbbb2", trades=[
        _trade("600000.SH", "2026-01-05", "2026-01-10", shares=200, entry_value=2000.0, exit_value=2100.0, pnl_pct=0.05),
    ])
    cand = compare_runs([a, b])["trade_summary"]["candidates"][0]
    # shares/金额不同仍是共同交易, 但必须在概要中可辨认
    assert cand["common"] == 1
    assert cand["added"] == 0
    assert cand["removed"] == 0
    assert cand["common_value_diff"] == 1
    row = cand["samples"]["common"][0]
    assert row["value_differs"] is True
    assert row["baseline"]["shares"] == 100
    assert row["candidate"]["shares"] == 200
    assert row["baseline"]["entry_value"] == 1000.0
    assert row["candidate"]["entry_value"] == 2000.0
    assert row["baseline"]["exit_value"] == 1050.0
    assert row["candidate"]["exit_value"] == 2100.0


def test_compare_trade_duplicate_identity_pairs_by_count():
    a = _make_run("runaaaaaa1", trades=[
        _trade("600000.SH", "2026-01-05", "2026-01-10"),
        _trade("600000.SH", "2026-01-05", "2026-01-10"),
    ])
    b = _make_run("runbbbbbb2", trades=[
        _trade("600000.SH", "2026-01-05", "2026-01-10"),
    ])
    cand = compare_runs([a, b])["trade_summary"]["candidates"][0]
    assert (cand["common"], cand["added"], cand["removed"]) == (1, 0, 1)


def test_compare_trade_summary_non_finite_and_missing_values_null():
    a = _make_run("runaaaaaa1", trades=[
        _trade("600000.SH", "2026-01-05", "2026-01-10", shares=math.nan, pnl_pct=0.1),
    ])
    b = _make_run("runbbbbbb2", trades=[
        _trade("600000.SH", "2026-01-05", "2026-01-10"),  # 旧 run 常见: 无 shares 字段
    ])
    cand = compare_runs([a, b])["trade_summary"]["candidates"][0]
    row = cand["samples"]["common"][0]
    assert row["baseline"]["shares"] is None  # NaN → null, 不伪装成 0
    assert row["candidate"]["shares"] is None  # 缺字段 → null
    assert row["baseline"]["exit_value"] is None
    assert row["candidate"]["exit_value"] is None
    assert row["value_differs"] is False  # 双方均未知, 不算数值口径不同


def test_compare_trade_summary_empty_trades_safe():
    a = _make_run("runaaaaaa1", trades=[])
    b = _make_run("runbbbbbb2", trades=[], factor_result={"group_stats": [{"group": "Q1"}], "ic_mean": 0.02})
    cand = compare_runs([a, b])["trade_summary"]["candidates"][0]
    assert (cand["common"], cand["added"], cand["removed"]) == (0, 0, 0)
    assert cand["samples"]["common"] == []


def test_compare_diff_sections_support_multiple_candidates():
    a = _make_run("runaaaaaa1", config={"fees_pct": 0.1}, trades=[_trade("600000.SH", "2026-01-05", "2026-01-10")])
    b = _make_run("runbbbbbb2", config={"fees_pct": 0.2}, trades=[])
    c = _make_run("runcccccc3", config={"fees_pct": 0.3}, trades=[_trade("000001.SZ", "2026-02-01", "2026-02-10")])
    out = compare_runs([a, b, c])
    assert [x["run_id"] for x in out["config_diff"]["candidates"]] == ["runbbbbbb2", "runcccccc3"]
    assert all(x["total"] == 1 for x in out["config_diff"]["candidates"])  # 每个候选都相对同一 baseline
    ts = out["trade_summary"]["candidates"]
    assert (ts[0]["common"], ts[0]["added"], ts[0]["removed"]) == (0, 0, 1)  # b 无交易: 基线 1 笔全消失
    assert (ts[1]["common"], ts[1]["added"], ts[1]["removed"]) == (0, 1, 1)


def test_list_runs_reuses_cache_and_invalidates_after_patch(tmp_path: Path, monkeypatch):
    import app.backtest.run_store as rs

    store = BacktestRunStore(tmp_path)
    store.save(_make_run("runaaaaaa1"))
    store.save(_make_run("runbbbbbb2"))
    original_loads = rs.json.loads
    calls = 0

    def counting_loads(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_loads(*args, **kwargs)

    monkeypatch.setattr(rs.json, "loads", counting_loads)
    assert store.list_runs()["total"] == 2
    first_calls = calls
    assert first_calls >= 2
    assert store.list_runs()["total"] == 2
    assert calls == first_calls

    store.patch("runaaaaaa1", label="更新标签")
    calls_after_patch = calls
    assert store.list_runs()["total"] == 2
    assert calls > calls_after_patch


# ── 导出 ──────────────────────────────────────────────────


def test_export_csv_trades():
    name, data = export_csv(_make_run())
    assert name == "trades"
    text = data.decode("utf-8")
    assert "symbol" in text
    assert "600000.SH" in text


def test_export_csv_factor_falls_back_to_group_stats():
    run = _make_run(
        "runfactor1",
        kind="factor",
        trades=[],
        factor_result={"group_stats": [{"group": 1, "label": "Q1", "ann_return": 0.12}]},
    )
    name, data = export_csv(run)
    assert name == "group_stats"
    assert "ann_return" in data.decode("utf-8")


def test_export_csv_empty_raises():
    with pytest.raises(ValueError):
        export_csv(_make_run("runempty1", trades=[], factor_result=None))


# ── 旧 run_card 只读迁移 ──────────────────────────────────


def _write_legacy_card(tmp_path: Path, run_id: str = "legacycard1") -> None:
    ResearchStore(tmp_path).save_run_card(
        run_id=run_id,
        kind="strategy",
        config={"strategy_id": "macd", "start": "2026-01-01", "end": "2026-06-30", "fees_pct": 0.0002},
        stats={"sharpe": 2.0},
    )


def test_legacy_card_readable_with_warning(tmp_path: Path):
    _write_legacy_card(tmp_path)
    store = BacktestRunStore(tmp_path)
    run = store.get("legacycard1")
    assert run.kind == "strategy"
    assert run.status == "legacy"
    assert run.stats["sharpe"] == 2.0
    assert LEGACY_RUN_CARD_WARNING in run.warnings
    # 缺曲线/交易 → 显式标注
    assert run.equity_curve == []
    assert run.trades == []


def test_legacy_card_listed_and_queryable(tmp_path: Path):
    _write_legacy_card(tmp_path)
    out = BacktestRunStore(tmp_path).list_runs()
    assert out["total"] == 1
    assert out["items"][0]["status"] == "legacy"


def test_legacy_card_uses_validated_filename_as_authoritative_run_id(tmp_path: Path):
    card_dir = tmp_path / "research" / "run_cards"
    card_dir.mkdir(parents=True)
    (card_dir / "legacyfile1.json").write_text(
        json.dumps(
            {
                "run_id": "wrongcard1",
                "kind": "strategy",
                "config": {"strategy_id": "macd"},
                "config_hash": "h1",
                "strategy_hash": "h2",
                "stats": {"sharpe": 2.0},
                "created_at": "2026-08-19T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    store = BacktestRunStore(tmp_path)
    assert store.get("legacyfile1").run_id == "legacyfile1"
    assert [item["run_id"] for item in store.list_runs()["items"]] == ["legacyfile1"]
    store.patch("legacyfile1", favorite=True)
    assert (tmp_path / "research" / "backtest_runs" / "legacyfile1.json").exists()
    assert not (tmp_path / "research" / "backtest_runs" / "wrongcard1.json").exists()


def test_non_backtest_legacy_card_is_not_listed(tmp_path: Path):
    ResearchStore(tmp_path).save_run_card(
        run_id="scheduled-report-1",
        kind="scheduled_research",
        config={"schedule_id": "weekly"},
        stats={"summary": "not a backtest"},
    )
    store = BacktestRunStore(tmp_path)
    assert store.list_runs()["total"] == 0
    with pytest.raises(KeyError):
        store.get("scheduled-report-1")


def test_legacy_card_patch_persists_new_contract_without_touching_original(tmp_path: Path):
    _write_legacy_card(tmp_path)
    store = BacktestRunStore(tmp_path)
    updated = store.patch("legacycard1", favorite=True)
    assert updated.favorite is True
    # 新契约文件落盘
    assert (tmp_path / "research" / "backtest_runs" / "legacycard1.json").exists()
    # 原旧文件不动
    original = ResearchStore(tmp_path).get_run_card("legacycard1")
    assert original.stats == {"sharpe": 2.0}


def test_legacy_card_delete_refused(tmp_path: Path):
    _write_legacy_card(tmp_path)
    with pytest.raises(LegacyRunCardReadOnly):
        BacktestRunStore(tmp_path).delete("legacycard1")
    # 原文件仍在
    assert (tmp_path / "research" / "run_cards" / "legacycard1.json").exists()


def test_new_contract_wins_over_legacy_same_id(tmp_path: Path):
    _write_legacy_card(tmp_path)
    store = BacktestRunStore(tmp_path)
    store.save(_make_run("legacycard1"))
    run = store.get("legacycard1")
    assert run.status == "completed"
    assert LEGACY_RUN_CARD_WARNING not in run.warnings


# ── summarize ─────────────────────────────────────────────


def test_summarize_includes_headline_and_factor_metrics():
    run = _make_run(
        "runfactor2",
        kind="factor",
        factor_result={"ic_mean": 0.05, "ir": 1.2},
        stats={},
    )
    s = summarize(run)
    assert s["stats"]["ic_mean"] == 0.05
    assert s["has_factor_result"] is True
