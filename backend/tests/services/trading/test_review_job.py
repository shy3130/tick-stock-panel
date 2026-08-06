"""盘后状态驱动 AI 归因 (review_job) 测试 — L0/L1/去重/AI 未配置降级/fail-soft。

核心验收点:
- L0 路径零 AI 调用 (mock autopsy.run_autopsy 计数断言 0)。
- L1 候选归因; 去重 (已归因且事件数未变 skip); AI 未配置降级; 单笔失败不影响其他。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.services.trading import store
from app.services.trading.lifecycle import apply_event, new_trade, now_str
from app.services.trading.models import (
    KIND_CLOSE,
    KIND_FILL,
    KIND_PREPARE,
    STATUS_CLOSED,
)
from app.services.trading.review_job import _collect_candidates, run_state_driven_autopsy

NOW = datetime(2026, 8, 4, 16, 45)
TS_OPEN = "2026-07-20 10:00"
TS_FILL = "2026-07-20 14:30"
TS_ADJUST = "2026-08-04 14:30"  # 近 1 日的放宽止损 → 新红旗


def _make_trade_files(data_dir: Path, trade_id: str, *, recent_flag: bool = False,
                      closed_recent: bool = False) -> None:
    """在 data_dir 下落盘一个 trade + 事件流 + 审计流。

    - recent_flag: True 时构造一个近期(今天)的放宽止损红旗。
    - closed_recent: True 时构造一个近期平仓 (closedAt=今天)。
    """
    symbol = "600519.SH"
    trade = new_trade(trade_id, symbol, {"name": "茅台",
                                         "thesis": {"text": "x", "invalidation": "y"},
                                         "stopLoss": 1600.0}, TS_OPEN)
    trade = apply_event(trade, KIND_PREPARE, {"plannedQty": 100, "plannedPrice": 1680, "stopLoss": 1600.0}, TS_OPEN)
    trade = apply_event(trade, KIND_FILL, {"qty": 100, "price": 1680.0}, TS_FILL)

    store.write_trade(data_dir, trade)
    # 事件流
    for kind, ts, payload in [
        ("open", TS_OPEN, {}),
        (KIND_PREPARE, TS_OPEN, {}),
        (KIND_FILL, TS_FILL, {"qty": 100, "price": 1680.0}),
    ]:
        store.append_event(data_dir, {"schemaVersion": 1, "tradeId": trade_id,
                                      "kind": kind, "ts": ts, "payload": payload,
                                      "note": "", "gateBypassed": False})
    # 审计流
    store.append_audit(data_dir, {"ts": TS_OPEN, "mode": "buy_new", "tradeId": trade_id, "passed": True})
    store.append_audit(data_dir, {"ts": TS_FILL, "mode": "fill", "tradeId": trade_id, "passed": True})

    if recent_flag:
        store.append_event(data_dir, {"schemaVersion": 1, "tradeId": trade_id,
                                      "kind": "adjust", "ts": TS_ADJUST,
                                      "payload": {"oldStopLoss": 1600.0, "newStopLoss": 1500.0},
                                      "note": "", "gateBypassed": False})
        store.append_audit(data_dir, {"ts": TS_ADJUST, "mode": "adjust", "tradeId": trade_id, "passed": True})

    if closed_recent:
        close_ts = now_str()  # 今天
        store.append_event(data_dir, {"schemaVersion": 1, "tradeId": trade_id,
                                      "kind": KIND_CLOSE, "ts": close_ts,
                                      "payload": {"price": 1700.0}, "note": "", "gateBypassed": False})
        store.append_audit(data_dir, {"ts": close_ts, "mode": "close", "tradeId": trade_id, "passed": True})
        # 更新单笔投影为已平仓
        trade = store.read_trade(data_dir, trade_id) or {}
        trade["status"] = STATUS_CLOSED
        trade["closedAt"] = close_ts
        store.write_trade(data_dir, trade)


# ── L0: 无候选 → 零 AI 调用 ──────────────────────────────
@pytest.mark.asyncio
async def test_l0_no_candidates_zero_ai_calls(tmp_path):
    """空数据目录: 无候选 → L0, run_autopsy 调用计数 0 (零 AI 调用)。"""
    mock_autopsy = AsyncMock(return_value={"classification": "A", "reasoning": "x", "fix": "y"})
    with patch("app.services.trading.review_job.run_autopsy", mock_autopsy), \
         patch("app.services.trading.review_job.ai_configured", return_value=True):
        result = await run_state_driven_autopsy(tmp_path)

    assert result == {"level": "L0", "candidates": 0, "autopsied": 0, "skipped": 0}
    assert mock_autopsy.call_count == 0  # 零 AI 调用


# ── L1: 候选归因 ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_l1_candidates_autopsied(tmp_path):
    """近期新红旗的 trade → L1 归因。"""
    _make_trade_files(tmp_path, "600519.SH_20260720_1", recent_flag=True)

    mock_autopsy = AsyncMock(return_value={"tradeId": "600519.SH_20260720_1",
                                           "classification": "B", "reasoning": "r", "fix": "f"})
    with patch("app.services.trading.review_job.run_autopsy", mock_autopsy), \
         patch("app.services.trading.review_job.ai_configured", return_value=True):
        result = await run_state_driven_autopsy(tmp_path)

    assert result["level"] == "L1"
    assert result["candidates"] == 1
    assert result["autopsied"] == 1
    assert result["skipped"] == 0
    assert result["errors"] == []
    assert mock_autopsy.call_count == 1
    # eventCount 被补写
    saved = store.trading_dir(tmp_path) / "autopsies" / "600519.SH_20260720_1.json"
    assert saved.exists()
    assert json.loads(saved.read_text("utf-8"))["eventCount"] == 4  # open+prepare+fill+adjust


# ── L1: 近期平仓也是候选 ─────────────────────────────────
@pytest.mark.asyncio
async def test_l1_recent_close_is_candidate(tmp_path):
    """近期平仓 (无红旗) → L1 归因。"""
    _make_trade_files(tmp_path, "600519.SH_20260720_1", closed_recent=True)

    mock_autopsy = AsyncMock(return_value={"tradeId": "600519.SH_20260720_1",
                                           "classification": "A", "reasoning": "r", "fix": "f"})
    with patch("app.services.trading.review_job.run_autopsy", mock_autopsy), \
         patch("app.services.trading.review_job.ai_configured", return_value=True):
        result = await run_state_driven_autopsy(tmp_path)

    assert result["level"] == "L1"
    assert result["candidates"] == 1
    assert result["autopsied"] == 1


# ── 去重: 已归因且事件数未变 → skip ──────────────────────
@pytest.mark.asyncio
async def test_dedup_skip_when_event_count_unchanged(tmp_path):
    """已落盘归因且 eventCount == 当前事件数 → skip, 不调 AI。"""
    trade_id = "600519.SH_20260720_1"
    _make_trade_files(tmp_path, trade_id, recent_flag=True)

    # 预置归因记录, eventCount 与当前事件数一致 (4)
    autopsies_dir = store.trading_dir(tmp_path) / "autopsies"
    autopsies_dir.mkdir(parents=True, exist_ok=True)
    (autopsies_dir / f"{trade_id}.json").write_text(
        json.dumps({"tradeId": trade_id, "classification": "B",
                    "reasoning": "old", "fix": "old", "eventCount": 4}, ensure_ascii=False),
        encoding="utf-8")

    mock_autopsy = AsyncMock()
    with patch("app.services.trading.review_job.run_autopsy", mock_autopsy), \
         patch("app.services.trading.review_job.ai_configured", return_value=True):
        result = await run_state_driven_autopsy(tmp_path)

    assert result["level"] == "L1"
    assert result["candidates"] == 1
    assert result["autopsied"] == 0
    assert result["skipped"] == 1
    assert mock_autopsy.call_count == 0


@pytest.mark.asyncio
async def test_dedup_rerun_when_event_count_changed(tmp_path):
    """已落盘归因但 eventCount 不匹配 → 重新归因。"""
    trade_id = "600519.SH_20260720_1"
    _make_trade_files(tmp_path, trade_id, recent_flag=True)

    autopsies_dir = store.trading_dir(tmp_path) / "autopsies"
    autopsies_dir.mkdir(parents=True, exist_ok=True)
    (autopsies_dir / f"{trade_id}.json").write_text(
        json.dumps({"tradeId": trade_id, "classification": "B",
                    "reasoning": "old", "fix": "old", "eventCount": 3}, ensure_ascii=False),  # 旧值, 需重跑
        encoding="utf-8")

    mock_autopsy = AsyncMock(return_value={"tradeId": trade_id,
                                           "classification": "C", "reasoning": "new", "fix": "new"})
    with patch("app.services.trading.review_job.run_autopsy", mock_autopsy), \
         patch("app.services.trading.review_job.ai_configured", return_value=True):
        result = await run_state_driven_autopsy(tmp_path)

    assert result["autopsied"] == 1
    assert result["skipped"] == 0
    assert mock_autopsy.call_count == 1


@pytest.mark.asyncio
async def test_old_autopsy_without_event_count_reruns(tmp_path):
    """无 eventCount 的旧归因记录 → 视为需重跑。"""
    trade_id = "600519.SH_20260720_1"
    _make_trade_files(tmp_path, trade_id, recent_flag=True)

    autopsies_dir = store.trading_dir(tmp_path) / "autopsies"
    autopsies_dir.mkdir(parents=True, exist_ok=True)
    (autopsies_dir / f"{trade_id}.json").write_text(
        json.dumps({"tradeId": trade_id, "classification": "B", "reasoning": "old", "fix": "old"},
                   ensure_ascii=False),
        encoding="utf-8")

    mock_autopsy = AsyncMock(return_value={"tradeId": trade_id,
                                           "classification": "C", "reasoning": "new", "fix": "new"})
    with patch("app.services.trading.review_job.run_autopsy", mock_autopsy), \
         patch("app.services.trading.review_job.ai_configured", return_value=True):
        result = await run_state_driven_autopsy(tmp_path)

    assert result["autopsied"] == 1
    assert mock_autopsy.call_count == 1


# ── AI 未配置 → 降级 ─────────────────────────────────────
@pytest.mark.asyncio
async def test_l1_ai_not_configured_degrades(tmp_path):
    """有候选但 AI 未配置 → 返回 blocked_by_dependency 语义, 不抛异常。"""
    _make_trade_files(tmp_path, "600519.SH_20260720_1", recent_flag=True)

    mock_autopsy = AsyncMock()
    with patch("app.services.trading.review_job.run_autopsy", mock_autopsy), \
         patch("app.services.trading.review_job.ai_configured", return_value=False):
        result = await run_state_driven_autopsy(tmp_path)

    assert result["level"] == "L1"
    assert result["candidates"] == 1
    assert result["autopsied"] == 0
    assert result["code"] == "blocked_by_dependency"
    assert "detail" in result
    assert mock_autopsy.call_count == 0


# ── fail-soft: 单笔失败不影响其他 ─────────────────────────
@pytest.mark.asyncio
async def test_fail_soft_one_failure_doesnt_block_others(tmp_path):
    """一笔归因抛异常 → 记 errors, 另一笔正常完成。"""
    _make_trade_files(tmp_path, "600519.SH_20260720_1", recent_flag=True)
    _make_trade_files(tmp_path, "000001.SZ_20260720_1", recent_flag=True)

    call_log = []

    async def _fake_run(data_dir, trade_id):
        call_log.append(trade_id)
        if trade_id == "000001.SZ_20260720_1":
            raise RuntimeError("boom")
        return {"tradeId": trade_id, "classification": "A", "reasoning": "r", "fix": "f"}

    with patch("app.services.trading.review_job.run_autopsy", side_effect=_fake_run), \
         patch("app.services.trading.review_job.ai_configured", return_value=True):
        result = await run_state_driven_autopsy(tmp_path)

    assert result["level"] == "L1"
    assert result["candidates"] == 2
    assert result["autopsied"] == 1
    assert len(result["errors"]) == 1
    assert result["errors"][0]["tradeId"] == "000001.SZ_20260720_1"
    assert "boom" in result["errors"][0]["error"]
    assert len(call_log) == 2  # 两笔都被尝试


# ── 候选收集纯函数 ───────────────────────────────────────
def test_collect_candidates_recent_flag_only(tmp_path):
    """红旗 ts 在窗口外 → 不是候选; 在窗口内 → 是候选。"""
    _make_trade_files(tmp_path, "600519.SH_20260720_1", recent_flag=False)
    cands = _collect_candidates(tmp_path, now=NOW)
    assert cands == set()  # 无近期红旗/平仓

    _make_trade_files(tmp_path, "000001.SZ_20260720_1", recent_flag=True)
    cands = _collect_candidates(tmp_path, now=NOW)
    assert "000001.SZ_20260720_1" in cands
    assert "600519.SH_20260720_1" not in cands


def test_collect_candidates_old_close_excluded(tmp_path):
    """平仓时间在窗口外 (30 天前) → 不是候选。"""
    _make_trade_files(tmp_path, "600519.SH_20260720_1", closed_recent=False)
    # 手动写一个 30 天前的平仓
    old_close = (NOW - timedelta(days=30)).strftime("%Y-%m-%d %H:%M")
    trade = store.read_trade(tmp_path, "600519.SH_20260720_1") or {}
    trade["status"] = STATUS_CLOSED
    trade["closedAt"] = old_close
    store.write_trade(tmp_path, trade)
    cands = _collect_candidates(tmp_path, now=NOW)
    assert cands == set()
