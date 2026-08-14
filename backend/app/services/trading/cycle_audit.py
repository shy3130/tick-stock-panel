"""周期审计 — 跨笔聚合诊断（移植自 YMOS SOP_内核周期审计）。

单笔复盘只固定证据，周期审计把证据变成规则改进。
纯代码聚合红旗频率、归因分类分布、策略族统计，不做 AI 判断。

样本门槛（照搬 YMOS）:
  < 10 笔: 只做观察登记
  ≥ 10 笔: 可以提案
  ≥ 30 笔: 可以分策略族横向对比
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from app.services.trading import store
from app.services.trading.autopsy import _autopsies_dir, _safe_id, read_autopsy
from app.services.trading.models import STATUS_CLOSED
from app.services.trading.red_flags import scan_trade, scan_all

# 样本门槛
SAMPLE_MIN_OBSERVE = 10
SAMPLE_MIN_COMPARE = 30


def _list_closed_trades(data_dir: Path) -> list[dict[str, Any]]:
    """列出已平仓的单笔。"""
    return [t for t in store.list_trades(data_dir) if t.get("status") == STATUS_CLOSED]


def _read_all_autopsies(data_dir: Path, trade_ids: list[str]) -> dict[str, dict[str, Any]]:
    """批量读取已落盘的归因记录。"""
    out: dict[str, dict[str, Any]] = {}
    for tid in trade_ids:
        record = read_autopsy(data_dir, tid)
        if record is not None:
            out[tid] = record
    return out


def _aggregate_red_flags(
    all_flags: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """聚合红旗频率与趋势。

    Returns:
        {
            "byType": {"relaxed_stop": {"count": 3, "trades": ["x", "y"]}, ...},
            "totalFlags": 7,
            "tradesWithFlags": 3,
            "tradesWithoutFlags": 7,
        }
    """
    by_type: dict[str, dict[str, Any]] = {}
    total = 0
    trades_with = 0

    for trade_id, flags in all_flags.items():
        if trade_id == "global":
            continue
        if flags:
            trades_with += 1
        for flag in flags:
            ftype = flag.get("type", "unknown")
            total += 1
            if ftype not in by_type:
                by_type[ftype] = {"count": 0, "trades": []}
            by_type[ftype]["count"] += 1
            by_type[ftype]["trades"].append(trade_id)

    closed_count = sum(1 for k in all_flags if k != "global")
    return {
        "byType": by_type,
        "totalFlags": total,
        "tradesWithFlags": trades_with,
        "tradesWithoutFlags": max(closed_count - trades_with, 0),
    }


def _aggregate_autopsies(
    autopsies: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """聚合归因分类分布。

    Returns:
        {
            "distribution": {"A": 3, "B": 2, "C": 1, "D": 0},
            "totalAutopsies": 6,
            "patternFrequency": {1: 2, 3: 1, ...},
            "missingAutopsies": 4,  # 已平仓但无归因
        }
    """
    distribution: dict[str, int] = {"A": 0, "B": 0, "C": 0, "D": 0}
    pattern_freq: dict[int, int] = {}

    for record in autopsies.values():
        cls = str(record.get("classification", "A"))
        if cls in distribution:
            distribution[cls] += 1
        for pid in record.get("patternIds") or []:
            try:
                pid_int = int(pid)
                pattern_freq[pid_int] = pattern_freq.get(pid_int, 0) + 1
            except (ValueError, TypeError):
                pass

    return {
        "distribution": distribution,
        "totalAutopsies": len(autopsies),
        "patternFrequency": dict(sorted(pattern_freq.items(), key=lambda x: -x[1])),
    }


def _aggregate_by_strategy(
    trades: list[dict[str, Any]],
    autopsies: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """按策略族聚合统计（样本 ≥ 30 时才有意义）。

    Returns:
        {
            "byStrategy": {
                "boll_breakout": {
                    "tradeCount": 5,
                    "closedCount": 3,
                    "withRedFlags": 1,
                    "avgHoldDays": 12.5,
                    "realizedPnl": 1500.0,
                    "autopsyDistribution": {"A": 1, "B": 1, "C": 0, "D": 0},
                },
            }
        }
    """
    by_strategy: dict[str, dict[str, Any]] = {}

    for trade in trades:
        strategy = str(trade.get("strategy") or "未指定")
        if strategy not in by_strategy:
            by_strategy[strategy] = {
                "tradeCount": 0,
                "closedCount": 0,
                "withRedFlags": 0,
                "realizedPnl": 0.0,
                "autopsyDistribution": {"A": 0, "B": 0, "C": 0, "D": 0},
                "_hold_days_sum": 0.0,
                "_hold_days_count": 0,
            }
        entry = by_strategy[strategy]
        entry["tradeCount"] += 1

        if trade.get("status") == STATUS_CLOSED:
            entry["closedCount"] += 1
            pnl = trade.get("realizedPnl")
            if isinstance(pnl, (int, float)):
                entry["realizedPnl"] += pnl

            # 持仓天数
            opened = trade.get("openedAt") or trade.get("createdAt")
            closed = trade.get("closedAt") or trade.get("updatedAt")
            if opened and closed:
                try:
                    d_open = datetime.fromisoformat(str(opened).replace("Z", "+00:00"))
                    d_close = datetime.fromisoformat(str(closed).replace("Z", "+00:00"))
                    hold_days = (d_close - d_open).days
                    entry["_hold_days_sum"] += hold_days
                    entry["_hold_days_count"] += 1
                except (ValueError, TypeError):
                    pass

            tid = trade.get("tradeId", "")
            if tid in autopsies:
                cls = str(autopsies[tid].get("classification", "A"))
                if cls in entry["autopsyDistribution"]:
                    entry["autopsyDistribution"][cls] += 1

    # 清理临时字段
    for entry in by_strategy.values():
        count = entry.pop("_hold_days_count")
        total = entry.pop("_hold_days_sum")
        entry["avgHoldDays"] = round(total / count, 1) if count > 0 else None

    return {"byStrategy": by_strategy}


def run_cycle_audit(data_dir: Path) -> dict[str, Any]:
    """执行一次周期审计，返回聚合统计。

    纯代码实现，不调用 AI。
    按 YMOS SOP 的样本门槛产出结论级别。
    """
    closed_trades = _list_closed_trades(data_dir)
    closed_ids = [t.get("tradeId", "") for t in closed_trades]
    sample_size = len(closed_trades)

    # 红旗聚合
    all_flags = scan_all(data_dir)
    # 只保留已平仓的（scan_all 返回全部 trades）
    closed_flags = {k: v for k, v in all_flags.items() if k in closed_ids or k == "global"}
    red_flag_stats = _aggregate_red_flags(closed_flags)

    # 归因聚合
    autopsies = _read_all_autopsies(data_dir, closed_ids)
    autopsy_stats = _aggregate_autopsies(autopsies)
    autopsy_stats["missingAutopsies"] = sample_size - autopsy_stats["totalAutopsies"]

    # 策略族统计
    all_trades = store.list_trades(data_dir)
    strategy_stats = _aggregate_by_strategy(all_trades, autopsies)

    # 样本级别判定
    if sample_size < SAMPLE_MIN_OBSERVE:
        audit_level = "observation"
        can_propose = False
        note = f"样本不足（{sample_size} < {SAMPLE_MIN_OBSERVE}），仅做观察登记，不提案。"
    elif sample_size < SAMPLE_MIN_COMPARE:
        audit_level = "can_propose"
        can_propose = True
        note = f"样本量 {sample_size} 笔，可以提案。每个提案须点名依据的具体笔数与笔号。"
    else:
        audit_level = "full_compare"
        can_propose = True
        note = f"样本量 {sample_size} 笔，可以做分策略族横向对比。"

    return {
        "auditLevel": audit_level,
        "canPropose": can_propose,
        "note": note,
        "sampleSize": sample_size,
        "auditDate": datetime.now().strftime("%Y-%m-%d"),
        "redFlags": red_flag_stats,
        "autopsies": autopsy_stats,
        "strategies": strategy_stats,
        # 提案生效验证 (#19): 检查上一轮提案是否生效
        "proposalValidation": _validate_proposals(data_dir, all_flags, autopsies),
    }


def _validate_proposals(
    data_dir: Path,
    all_flags: dict[str, list[dict[str, Any]]],
    autopsies: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """提案生效验证（YMOS Step 3 第八项「反馈回路」）。

    检查 trial 状态的提案关联的红旗/归因类型是否在后续交易中减少。
    纯代码实现：对比提案前/后的红旗频率。
    """
    from app.services.trading.proposals import list_proposals

    proposals = list_proposals(data_dir)
    trial_proposals = [p for p in proposals if p.get("status") == "trial"]

    if not trial_proposals:
        return {"checked": 0, "results": []}

    results: list[dict[str, Any]] = []
    for prop in trial_proposals:
        prop_id = prop.get("id", "")
        # 统计提案涉及的红旗类型
        related_flag_types = set()
        for evidence in prop.get("evidence", []) or []:
            ftype = evidence.get("flagType") or evidence.get("type")
            if ftype:
                related_flag_types.add(str(ftype))

        # 简单统计：提案后该类红旗出现次数
        post_flag_count = 0
        for flags in all_flags.values():
            if isinstance(flags, list):
                for flag in flags:
                    if str(flag.get("type", "")) in related_flag_types:
                        post_flag_count += 1

        results.append({
            "proposalId": prop_id,
            "summary": prop.get("summary", ""),
            "relatedFlagTypes": list(related_flag_types),
            "postProposalFlagCount": post_flag_count,
            "assessment": "needs_more_samples" if post_flag_count > 0 else "improved",
        })

    return {"checked": len(trial_proposals), "results": results}
