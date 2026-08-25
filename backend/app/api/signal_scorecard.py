"""Signal Scorecard REST API — 回顾性信号命中率记分卡查询与评估。

前缀 /api/signal-scorecard, 镜像 alerts.py/signals.py 胶水模式。
所有数据来自本地 JSONL (signal_events.jsonl + signal_outcomes.jsonl) +
enriched parquet (前向评估)。不接 provider、不生成荐股/买卖建议。

端点:
    GET  /events                       事件列表 (时间倒序, 支持过滤)
    GET  /stats                        按 signal_key × horizon 聚合记分卡
    GET  /events/{event_id}/outcomes   单事件详情 + 各 horizon outcome
    POST /evaluate                     幂等批量评估所有 pending 事件到期 horizon
    POST /backfill                     白名单内历史回填 (首次启用时用)
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from app.services import preferences, signal_scorecard_store as store
from app.services.signal_scorecard_eval import NEUTRAL_BAND_PCT
from app.jobs import signal_scorecard_job as job

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/signal-scorecard", tags=["signal-scorecard"])


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


def _repo(request: Request):
    return request.app.state.repo


class TrackedSignalItem(BaseModel):
    """显式 opt-in 的信号跟踪白名单项。"""

    model_config = ConfigDict(extra="forbid")

    signal_key: str
    signal_name: str = ""
    signal_kind: str = "builtin"
    direction: Literal["up", "not_up"] = "up"
    enabled: bool = True


class TrackedSignalsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[TrackedSignalItem]


@router.get("/tracked-signals")
def get_tracked_signals():
    """读取信号记分卡白名单；默认空列表，不自动启用任何信号。"""
    return {"items": preferences.get_tracked_signals()}


@router.put("/tracked-signals")
def update_tracked_signals(req: TrackedSignalsRequest):
    """保存显式白名单；只影响后续事件实例化，不回写历史事实流。"""
    items = preferences.set_tracked_signals(
        [item.model_dump(mode="python") for item in req.items]
    )
    return {"items": items}

# ── 事件列表 ─────────────────────────────────────────────
@router.get("/events")
def list_events(
    request: Request,
    signal_key: str | None = Query(None),
    symbol: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    status: str | None = Query(None, description="pending | mature"),
    limit: int = Query(500, ge=1, le=5000),
):
    """查询信号事件 (时间倒序)。status 由 outcomes 派生。"""
    if status is not None and status not in ("pending", "mature"):
        raise HTTPException(400, "status must be 'pending' or 'mature'")
    data_dir = _data_dir(request)
    events = store.list_events(
        data_dir,
        signal_key=signal_key,
        symbol=symbol,
        date_from=date_from,
        date_to=date_to,
        status=status,
        limit=limit,
    )
    return {"events": events, "total": len(events)}


# ── 聚合记分卡 ───────────────────────────────────────────
@router.get("/stats")
def get_stats(
    request: Request,
    signal_key: str | None = Query(None),
    horizon: int | None = Query(None, description="1 | 3 | 5 | 10"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
):
    """按 signal_key × horizon 分组聚合: hit_rate / 样本量 / 平均涨跌幅。

    hit_rate 分母仅含 completed 样本; pending 不计入 (不外推)。
    """
    if horizon is not None and horizon not in store.HORIZONS:
        raise HTTPException(400, f"horizon must be one of {store.HORIZONS}")
    data_dir = _data_dir(request)
    events = store.list_events(
        data_dir, signal_key=signal_key, date_from=date_from, date_to=date_to
    )
    outcome_map = store.event_outcome_map(data_dir, [e["id"] for e in events if e.get("id")])

    horizons = [horizon] if horizon is not None else list(store.HORIZONS)
    groups: dict[tuple[str, int], dict] = {}
    for ev in events:
        sk = ev.get("signal_key")
        if not sk:
            continue
        ev_outcomes = outcome_map.get(ev["id"], [])
        for h in horizons:
            oc = next((o for o in ev_outcomes if o.get("horizon") == h), None)
            key = (sk, h)
            g = groups.setdefault(key, {
                "signal_key": sk, "horizon": h,
                "total": 0, "completed": 0,
                "hit": 0, "miss": 0, "neutral": 0,
                "returns": [],
            })
            g["total"] += 1
            if oc and oc.get("eval_status") == "completed":
                g["completed"] += 1
                out = oc.get("outcome")
                if out == "hit":
                    g["hit"] += 1
                elif out == "miss":
                    g["miss"] += 1
                elif out == "neutral":
                    g["neutral"] += 1
                r = oc.get("stock_return_pct")
                if isinstance(r, (int, float)):
                    g["returns"].append(float(r))

    stats = []
    for (sk, h), g in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        completed = g["completed"]
        rets = g["returns"]
        stats.append({
            "signal_key": sk,
            "horizon": h,
            "total": g["total"],
            "completed": completed,
            "pending": g["total"] - completed,
            "hit_count": g["hit"],
            "miss_count": g["miss"],
            "neutral_count": g["neutral"],
            "hit_rate_pct": round(100.0 * g["hit"] / completed, 2) if completed else None,
            "avg_return_pct": round(sum(rets) / len(rets), 4) if rets else None,
            "sample_size": completed,
        })
    return {
        "stats": stats,
        "neutral_band_pct": NEUTRAL_BAND_PCT,
        "horizons": list(store.HORIZONS),
    }


# ── 单事件详情 ───────────────────────────────────────────
@router.get("/events/{event_id}/outcomes")
def event_detail(event_id: str, request: Request):
    """返回单事件 + 各 horizon 的 outcome 行 (含 pending 占位)。"""
    data_dir = _data_dir(request)
    events = store.list_events(data_dir)
    ev = next((e for e in events if e.get("id") == event_id), None)
    if ev is None:
        raise HTTPException(404, "event not found")
    outcomes = store.list_outcomes(data_dir, event_id=event_id)
    by_horizon = {o.get("horizon"): o for o in outcomes}
    horizon_rows = []
    for h in store.HORIZONS:
        oc = by_horizon.get(h)
        horizon_rows.append({
            "horizon": h,
            "eval_status": oc.get("eval_status") if oc else "pending",
            "outcome": oc.get("outcome") if oc else None,
            "direction_correct": oc.get("direction_correct") if oc else None,
            "stock_return_pct": oc.get("stock_return_pct") if oc else None,
            "end_close": oc.get("end_close") if oc else None,
            "unable_reason": oc.get("unable_reason") if oc else None,
            "evaluated_ts": oc.get("evaluated_ts") if oc else None,
        })
    return {"event": ev, "outcomes": horizon_rows, "status": store.event_status(data_dir, event_id)}


# ── 幂等批量评估 ─────────────────────────────────────────
@router.post("/evaluate")
def evaluate(request: Request):
    """扫描所有 pending 事件, 对到期 horizon 计算并 append outcome (幂等)。

    前向交易日不足的 horizon 保持 pending。重复调用不重复写 outcome。
    """
    repo = _repo(request)
    data_dir = _data_dir(request)
    try:
        result = job.evaluate_pending(repo, data_dir)
    except Exception as e:  # noqa: BLE001
        logger.warning("scorecard evaluate failed: %s", e)
        raise HTTPException(500, f"evaluate failed: {e}")
    return {"ok": True, **result}


# ── 白名单回填 ───────────────────────────────────────────
@router.post("/backfill")
def backfill(
    request: Request,
    signal_keys: str = Query("", description="逗号分隔的 signal_key, 必须在 tracked_signals 白名单内"),
    date_from: str = Query(..., description="YYYY-MM-DD"),
    date_to: str = Query(..., description="YYYY-MM-DD"),
):
    """扫描历史 enriched 分区回填信号实例 + 评估。仅白名单 + 限范围。

    拒绝未跟踪或已停用 (enabled=False) 的 signal_key (防全市场爆破)。
    范围上限 400 天。
    """
    tracked = preferences.get_tracked_signals()
    allowed = {t["signal_key"] for t in tracked if t.get("enabled", True)}
    if not allowed:
        raise HTTPException(400, "no tracked_signals configured (scorecard is opt-in)")

    requested = [s.strip() for s in (signal_keys or "").split(",") if s.strip()]
    if not requested:
        requested = list(allowed)
    rejected = [sk for sk in requested if sk not in allowed]
    if rejected:
        raise HTTPException(
            400, f"signal_keys not in tracked_signals whitelist: {rejected}"
        )

    try:
        d_from = date.fromisoformat(date_from)
        d_to = date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(400, "date_from/date_to must be YYYY-MM-DD")
    if d_from > d_to:
        raise HTTPException(400, "date_from must be <= date_to")
    if (d_to - d_from).days > 400:
        raise HTTPException(400, "backfill range exceeds 400 days limit")

    repo = _repo(request)
    data_dir = _data_dir(request)
    try:
        result = job.backfill(repo, data_dir, requested, tracked, d_from, d_to)
    except Exception as e:  # noqa: BLE001
        logger.warning("scorecard backfill failed: %s", e)
        raise HTTPException(500, f"backfill failed: {e}")
    return {"ok": True, **result}
