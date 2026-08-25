"""Signal Scorecard 每日管道钩子 — post-enriched 实例化 + pending 评估。

两个入口:
    generate_instances(repo, data_dir, tracked_signals, today)
        读当天 enriched 信号列为 True 的 (symbol) 行, 对每个生成不可变 SignalEvent
        并 append (确定性 id 去重)。仅在每日管道 post-enriched 后批量调用,
        不在盘中实时生成 (盘中数据会变, 会造成同日多次 fire)。

    evaluate_pending(repo, data_dir)
        遍历所有尚有未完成 horizon 的事件, 对到期 horizon (前向交易日已够 N 个)
        计算并 append outcome。前向不足的 horizon 保持 pending, 不 append unable。

安全边界: 不接 provider、不写交易事件、不生成荐股/买卖建议。
tracked_signals 白名单 (默认空) 防止全市场信号洪泛。
"""
from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any

import polars as pl

from app.services.signal_scorecard_eval import (
    compute_forward_window,
    direction_for_signal,
    evaluate_outcome,
)
from app.services.signal_scorecard_store import (
    ENGINE_VERSION,
    HORIZONS,
    append_event,
    append_outcome,
    list_events,
    list_outcomes,
    make_event_id,
    make_outcome_id,
)

logger = logging.getLogger(__name__)


def generate_instances(
    repo: Any,
    data_dir: Any,
    tracked_signals: list[dict[str, Any]],
    today: date,
) -> dict[str, Any]:
    """读当天 enriched 信号列, 对 True 的 (symbol) 生成 SignalEvent 并 append。

    tracked_signals: [{signal_key, signal_name, signal_kind, direction, enabled}]。
    today: 锚定日 (信号触发交易日)。
    返回 {generated, skipped_dups, reason}。
    """
    active = [t for t in tracked_signals if t.get("enabled", True) and t.get("signal_key")]
    if not active:
        return {"generated": 0, "skipped_dups": 0, "reason": "no_tracked_signals"}

    # 逐 signal_key 取 name/kind/direction (后者覆盖前者)。
    name_map: dict[str, str] = {}
    kind_map: dict[str, str] = {}
    dir_map: dict[str, str | None] = {}
    signal_keys: list[str] = []
    for t in active:
        sk = t["signal_key"]
        name_map[sk] = t.get("signal_name", sk)
        kind_map[sk] = t.get("signal_kind", "builtin")
        dir_map[sk] = t.get("direction")
        if sk not in signal_keys:
            signal_keys.append(sk)

    cols = ["symbol", "close", "name"] + [
        sk for sk in signal_keys if sk not in ("symbol", "close", "name")
    ]
    try:
        df = repo.get_enriched_range(today, today, columns=cols)
    except Exception:
        logger.warning("scorecard generate_instances: enriched query failed", exc_info=True)
        return {"generated": 0, "skipped_dups": 0, "reason": "enriched_query_failed"}
    if df is None or df.is_empty():
        return {"generated": 0, "skipped_dups": 0, "reason": "no_enriched_for_date"}

    date_str = today.isoformat() if hasattr(today, "isoformat") else str(today)
    now_ts = time.time()
    generated = 0
    skipped_dups = 0

    for sk in signal_keys:
        if sk not in df.columns:
            continue
        try:
            hits = df.filter(pl.col(sk).fill_null(False).cast(pl.Boolean))
        except Exception:
            logger.debug("scorecard: signal col %s filter failed", sk, exc_info=True)
            continue
        for rec in hits.iter_rows(named=True):
            symbol = rec.get("symbol")
            if not symbol:
                continue
            symbol = str(symbol)
            close = rec.get("close")
            name = rec.get("name") or symbol
            direction = direction_for_signal(kind_map.get(sk, "builtin"), dir_map.get(sk))
            event = {
                "id": make_event_id(sk, symbol, date_str),
                "signal_key": sk,
                "signal_name": name_map.get(sk, sk),
                "signal_kind": kind_map.get(sk, "builtin"),
                "source": "pipeline",
                "symbol": symbol,
                "name": str(name),
                "date": date_str,
                "anchor_price": float(close) if close is not None else None,
                "direction_expected": direction,
                "created_ts": now_ts,
                "context": {},
            }
            if append_event(data_dir, event):
                generated += 1
            else:
                skipped_dups += 1

    return {
        "generated": generated,
        "skipped_dups": skipped_dups,
        "reason": "ok" if generated or skipped_dups else "no_hits",
    }


def evaluate_pending(repo: Any, data_dir: Any) -> dict[str, Any]:
    """遍历所有尚有未完成 horizon 的事件, 对到期 horizon 计算 outcome 并 append。

    前向交易日不足的 horizon 保持 pending (不 append unable 冻结未来)。
    返回 {evaluated_events, appended_outcomes, skipped_pending}。
    """
    events = list_events(data_dir)
    if not events:
        return {"evaluated_events": 0, "appended_outcomes": 0, "skipped_pending": 0}

    outcomes = list_outcomes(data_dir)
    done: dict[str, set[int]] = {}
    for o in outcomes:
        eid = o.get("event_id")
        h = o.get("horizon")
        if eid is not None and h is not None:
            done.setdefault(eid, set()).add(h)

    now_ts = time.time()
    evaluated = 0
    appended = 0
    skipped_pending = 0

    for ev in events:
        eid = ev.get("id")
        if not eid:
            continue
        missing = [h for h in HORIZONS if h not in done.get(eid, set())]
        if not missing:
            continue
        evaluated += 1

        date_str = ev.get("date")
        try:
            anchor_date = date.fromisoformat(str(date_str))
        except (TypeError, ValueError):
            continue

        # 一次取最大 horizon 的前向窗口, 小 horizon 切片复用。
        max_h = max(missing)
        fwd = compute_forward_window(repo, ev.get("symbol", ""), anchor_date, max_h)
        if fwd is None:
            skipped_pending += 1
            continue

        for h in missing:
            if len(fwd) < h:
                # 前向交易日不足 → 保持 pending, 不 append unable
                continue
            result = evaluate_outcome(ev, fwd[:h], h)
            # unable(insufficient) 不应出现 (已按 len 过滤); 其他 unable 也不 append,
            # 避免数据缺口被冻结为终态。
            if result["eval_status"] != "completed":
                continue
            outcome = {
                "id": make_outcome_id(eid, h, ENGINE_VERSION),
                "event_id": eid,
                "horizon": h,
                "eval_window_days": h,
                "engine_version": ENGINE_VERSION,
                "evaluated_ts": now_ts,
                "anchor_date": str(date_str),
                **result,
            }
            if append_outcome(data_dir, outcome):
                appended += 1

    return {
        "evaluated_events": evaluated,
        "appended_outcomes": appended,
        "skipped_pending": skipped_pending,
    }


def backfill(
    repo: Any,
    data_dir: Any,
    signal_keys: list[str],
    tracked_signals: list[dict[str, Any]],
    date_from: date,
    date_to: date,
) -> dict[str, Any]:
    """扫描历史 enriched 分区, 逐日回填信号实例 + 立即评估 pending。

    仅允许 tracked_signals 白名单内的 signal_key (调用方已校验, 此处二次防御)。
    date_from/date_to 限定回填范围, 防止全市场全历史爆破。
    返回 {days_scanned, generated, skipped_dups, appended_outcomes}。
    """
    allowed = {t["signal_key"] for t in tracked_signals if t.get("signal_key")}
    safe_keys = [sk for sk in signal_keys if sk in allowed]
    if not safe_keys or date_from > date_to:
        return {
            "days_scanned": 0, "generated": 0, "skipped_dups": 0,
            "appended_outcomes": 0, "reason": "no_allowed_keys_or_bad_range",
        }

    tracked_subset = [t for t in tracked_signals if t.get("signal_key") in set(safe_keys)]
    days_scanned = 0
    total_generated = 0
    total_skipped = 0
    cur = date_from
    from datetime import timedelta as _td
    while cur <= date_to:
        res = generate_instances(repo, data_dir, tracked_subset, cur)
        if res.get("reason") != "no_enriched_for_date":
            days_scanned += 1
        total_generated += res.get("generated", 0)
        total_skipped += res.get("skipped_dups", 0)
        cur = cur + _td(days=1)

    eval_res = evaluate_pending(repo, data_dir)
    return {
        "days_scanned": days_scanned,
        "generated": total_generated,
        "skipped_dups": total_skipped,
        "appended_outcomes": eval_res.get("appended_outcomes", 0),
    }
