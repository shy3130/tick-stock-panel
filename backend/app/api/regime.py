"""市场环境(regime) API — 时序查询 + 手动重算。

装配逻辑在 app.services.regime_builder(纯函数), API 层薄壳 + TTL 缓存。
"""
from __future__ import annotations

import threading
import time
from datetime import date
from typing import Any

from fastapi import APIRouter, Query, Request

from app.services import regime_builder

router = APIRouter(prefix="/api/regime", tags=["regime"])

_CACHE_TTL = 5.0
_cache: dict[str, Any] | None = None
_cache_ts: float = 0.0
_cache_lock = threading.Lock()


def invalidate_regime_cache() -> None:
    """清空 regime 查询缓存。批算/重算后调用。"""
    global _cache, _cache_ts
    with _cache_lock:
        _cache = None
        _cache_ts = 0.0


def _data_dir(request: Request) -> Any:
    return request.app.state.repo.store.data_dir


def _df_to_records(df) -> list[dict]:
    """polars DataFrame → JSON 安全的 list[dict](date 转 ISO 字符串)。"""
    if df is None or df.is_empty():
        return []
    records = []
    for r in df.to_dicts():
        if "date" in r and r["date"] is not None:
            r["date"] = str(r["date"])
        records.append(r)
    return records


@router.get("/history")
def regime_history(
    request: Request,
    start: date | None = Query(None),
    end: date | None = Query(None),
    limit: int = Query(120, ge=1, le=1000),
):
    """历史环境时序(含状态/指标)。默认最近 N 天。"""
    global _cache, _cache_ts
    cache_key = f"hist|{start}|{end}|{limit}"
    with _cache_lock:
        if (
            _cache is not None
            and _cache.get("key") == cache_key
            and (time.time() - _cache_ts) < _CACHE_TTL
        ):
            return _cache["data"]

    df = regime_builder.load_regime_history(_data_dir(request))
    if df.is_empty():
        result: dict = {"rows": [], "total": 0}
    else:
        if start:
            df = df.filter(pl_col_date(df, ">=", start))
        if end:
            df = df.filter(pl_col_date(df, "<=", end))
        # limit 仅在"最近 N 天"模式(未传 start/end)生效;
        # 日期范围模式(传了 start/end, 如"全部")应返回完整范围, 不截断。
        if start is None and end is None:
            df = df.sort("date", descending=True).head(limit)
        df = df.sort("date")
        rows = _df_to_records(df)
        result = {"rows": rows, "total": len(rows)}

    with _cache_lock:
        _cache = {"key": cache_key, "data": result}
        _cache_ts = time.time()
    return result


def pl_col_date(df, op: str, value: date):
    """polars 日期过滤辅助(避免重复 import)。"""
    import polars as pl

    col = pl.col("date")
    return col >= value if op == ">=" else col <= value


@router.get("/latest")
def regime_latest(request: Request):
    """最新一日环境(轻量)。"""
    df = regime_builder.load_regime_history(_data_dir(request))
    if df.is_empty():
        return {"row": None}
    latest = df.sort("date", descending=True).head(1)
    rows = _df_to_records(latest)
    return {"row": rows[0] if rows else None}


@router.get("/states")
def regime_states(
    request: Request,
    days: int = Query(60, ge=1, le=1000),
):
    """状态分布统计(各状态天数/占比)。"""
    df = regime_builder.load_regime_history(_data_dir(request))
    if df.is_empty():
        return {"distribution": [], "days": 0}
    df = df.sort("date", descending=True).head(days)
    total = df.height
    counts = df.group_by("state").len().sort("len", descending=True)
    distribution = [
        {
            "state": r["state"],
            "label": regime_builder.STATE_LABELS.get(r["state"], r["state"]),
            "count": r["len"],
            "pct": round(r["len"] / total * 100, 1) if total else 0,
        }
        for r in counts.to_dicts()
    ]
    return {"distribution": distribution, "days": total}


@router.get("/coverage")
def regime_coverage(request: Request):
    """regime 数据覆盖元信息(供数据画像)。"""
    return regime_builder.get_regime_coverage(_data_dir(request))


@router.post("/recompute")
def regime_recompute(request: Request, start: date | None = None, end: date | None = None):
    """手动触发重算(全量或指定区间)。管理员操作。

    - 不传 start: 强制全量重算(enriched 最早日 ~ 今天), 覆盖所有已有行。
      与 daily_pipeline 的增量补差(compute_regime_incremental)不同 —— 此接口面向
      人工「我要重新算一遍」的预期, 必须真正重算而非增量补缺口。
    - 传 start: 仅重算 [start, end] 区间。
    """
    repo = request.app.state.repo
    data_dir = _data_dir(request)
    end = end or date.today()
    if start is None:
        # 全量: 从 enriched 最早日强制重算到今天
        earliest = regime_builder.earliest_enriched_date(repo)
        if earliest is None:
            invalidate_regime_cache()
            return {"ok": True, "computed": 0}
        start = earliest
    new_rows = regime_builder.run_regime_batch(repo, start=start, end=end)
    if not new_rows.is_empty():
        regime_builder.upsert_regime_history(data_dir, new_rows)
    invalidate_regime_cache()
    return {"ok": True, "computed": new_rows.height if not new_rows.is_empty() else 0}
