"""自选股 API。"""

from __future__ import annotations

import logging
import math
import time
from datetime import date

import polars as pl
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.db_safe import is_valid_ext_ident, quote_ident
from app.services import watchlist

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


class AddRequest(BaseModel):
    symbol: str
    note: str = ""
    group_id: str | None = None


class BatchAddRequest(BaseModel):
    symbols: list[str]
    note: str = ""
    group_id: str | None = None


class GroupNameRequest(BaseModel):
    name: str
    color: str | None = None


class GroupReorderRequest(BaseModel):
    ordered_ids: list[str]


class GroupAssignRequest(BaseModel):
    group_id: str | None = None


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


def _with_names(rows: list[dict], request: Request) -> list[dict]:
    if not rows:
        return rows
    try:
        df_i = request.app.state.repo.get_instruments()
        if df_i.is_empty() or "symbol" not in df_i.columns or "name" not in df_i.columns:
            return rows
        name_by_symbol = dict(df_i.select(["symbol", "name"]).iter_rows())
        return [{**row, "name": name_by_symbol.get(row.get("symbol"))} for row in rows]
    except Exception as e:  # noqa: BLE001
        logger.debug("attach watchlist names failed: %s", e)
        return rows


@router.get("")
def list_all(request: Request):
    return {"symbols": _with_names(watchlist.list_symbols(_data_dir(request)), request)}


@router.post("")
def add_one(req: AddRequest, request: Request):
    try:
        rows = watchlist.add(req.symbol, req.note, req.group_id, data_dir=_data_dir(request))
    except watchlist.WatchlistGroupError as e:
        raise HTTPException(400, str(e)) from e
    return {"symbols": _with_names(rows, request)}


@router.post("/batch")
def add_batch(req: BatchAddRequest, request: Request):
    try:
        rows, added = watchlist.add_batch(
            req.symbols, req.note, req.group_id, data_dir=_data_dir(request)
        )
    except watchlist.WatchlistGroupError as e:
        raise HTTPException(400, str(e)) from e
    return {"symbols": _with_names(rows, request), "added": added}


@router.get("/groups")
def list_groups(request: Request):
    return {"groups": watchlist.list_groups(_data_dir(request))}


@router.post("/groups")
def create_group(req: GroupNameRequest, request: Request):
    try:
        groups, group = watchlist.create_group(req.name, req.color, data_dir=_data_dir(request))
    except watchlist.DuplicateGroupNameError as e:
        raise HTTPException(409, str(e)) from e
    except watchlist.WatchlistGroupError as e:
        raise HTTPException(400, str(e)) from e
    return {"groups": groups, "group": group}


@router.put("/groups/reorder")
def reorder_groups(req: GroupReorderRequest, request: Request):
    """重排分组前后顺序 (json 数组顺序即定义顺序, 标签栏共用)。"""
    try:
        groups = watchlist.reorder_groups(req.ordered_ids, data_dir=_data_dir(request))
    except watchlist.WatchlistGroupError as e:
        raise HTTPException(400, str(e)) from e
    return {"groups": groups}


@router.put("/groups/{group_id}")
def rename_group(group_id: str, req: GroupNameRequest, request: Request):
    try:
        groups = watchlist.rename_group(group_id, req.name, req.color, data_dir=_data_dir(request))
    except KeyError as e:
        raise HTTPException(404, "自选分组不存在") from e
    except watchlist.DuplicateGroupNameError as e:
        raise HTTPException(409, str(e)) from e
    except watchlist.WatchlistGroupError as e:
        raise HTTPException(400, str(e)) from e
    return {"groups": groups}


@router.delete("/groups/{group_id}")
def delete_group(group_id: str, request: Request):
    try:
        groups, rows = watchlist.delete_group(group_id, data_dir=_data_dir(request))
    except KeyError as e:
        raise HTTPException(404, "自选分组不存在") from e
    return {"groups": groups, "symbols": _with_names(rows, request)}


@router.post("/groups/{group_id}/clear")
def clear_group(group_id: str, request: Request):
    """清空分组成员: 把该分组内所有股票转为未分组, 保留分组定义。"""
    try:
        rows = watchlist.clear_group(group_id, data_dir=_data_dir(request))
    except KeyError as e:
        raise HTTPException(404, "自选分组不存在") from e
    return {"symbols": _with_names(rows, request)}


@router.post("/groups/{group_id}/members/{symbol}")
def add_member(group_id: str, symbol: str, request: Request):
    """把标的加入分组 (多组成员关系: 不影响其他分组; 幂等)。"""
    try:
        rows = watchlist.add_to_group(symbol, group_id, data_dir=_data_dir(request))
    except KeyError as e:
        raise HTTPException(404, "自选标的不存在") from e
    except watchlist.WatchlistGroupError as e:
        raise HTTPException(400, str(e)) from e
    return {"symbols": _with_names(rows, request)}


@router.delete("/groups/{group_id}/members/{symbol}")
def remove_member(group_id: str, symbol: str, request: Request):
    """把标的移出分组 (仅摘本组标签; 标的仍在自选; 幂等)。"""
    try:
        rows = watchlist.remove_from_group(symbol, group_id, data_dir=_data_dir(request))
    except KeyError as e:
        raise HTTPException(404, "自选标的不存在") from e
    except watchlist.WatchlistGroupError as e:
        raise HTTPException(400, str(e)) from e
    return {"symbols": _with_names(rows, request)}


@router.post("/{symbol}/top")
def move_one_to_top(symbol: str, request: Request):
    rows = watchlist.move_to_top(symbol, data_dir=_data_dir(request))
    return {"symbols": _with_names(rows, request)}


@router.put("/{symbol}/group")
def assign_group(symbol: str, req: GroupAssignRequest, request: Request):
    """互斥设定分组 (仅保留此组; None=移出全部分组)。多组操作用 members 端点。"""
    try:
        rows = watchlist.set_group(symbol, req.group_id, data_dir=_data_dir(request))
    except KeyError as e:
        raise HTTPException(404, "自选标的不存在") from e
    except watchlist.WatchlistGroupError as e:
        raise HTTPException(400, str(e)) from e
    return {"symbols": _with_names(rows, request)}


@router.delete("/{symbol}")
def remove_one(symbol: str, request: Request):
    rows = watchlist.remove(symbol, data_dir=_data_dir(request))
    return {"symbols": _with_names(rows, request)}


@router.delete("")
def clear_all(request: Request):
    """清空自选列表。"""
    count = watchlist.clear(_data_dir(request))
    return {"removed": count}


# 自选页需要的列
_WATCHLIST_COLS = [
    "symbol",
    "close",
    "change_pct",
    "change_amount",
    "amount",
    "turnover_rate",
    "amplitude",
    "annual_vol_20d",
    "vol_ratio_5d",
    "ma5",
    "ma10",
    "ma20",
    "ma60",
    "vol_ma5",
    "vol_ma10",
    "high_60d",
    "low_60d",
    "rsi_6",
    "rsi_14",
    "rsi_24",
    "macd_dif",
    "macd_dea",
    "macd_hist",
    "kdj_k",
    "kdj_d",
    "kdj_j",
    "boll_upper",
    "boll_lower",
    "atr_14",
    "momentum_5d",
    "momentum_10d",
    "momentum_20d",
    "momentum_30d",
    "momentum_60d",
    "consecutive_limit_ups",
    "consecutive_limit_downs",
    "signal_limit_up",
    "signal_limit_down",
    "signal_volume_surge",
    "signal_ma_golden_5_20",
    "signal_macd_golden",
    "signal_n_day_high",
    "signal_boll_breakout_upper",
    "signal_ma20_breakout",
    "signal_ma_dead_5_20",
    "signal_macd_dead",
    "signal_n_day_low",
    "signal_boll_breakdown_lower",
    "signal_ma20_breakdown",
]


@router.get("/enriched")
def watchlist_enriched(
    request: Request,
    ext_columns: str | None = Query(None, description="逗号分隔的 ext 列: config_id.field_name"),
):
    """自选股 enriched 数据 — 直接从 enriched 最新日读取, 无即时计算。

    ext_columns 参数示例: "industry_rating.score,fund_flow.net_inflow"
    会动态 LEFT JOIN 对应的 ext_{config_id} DuckDB view。
    """
    t0 = time.perf_counter()

    repo = request.app.state.repo
    symbols = [r["symbol"] for r in watchlist.list_symbols(_data_dir(request))]
    if not symbols:
        return {"rows": [], "as_of": None, "elapsed_ms": 0}

    df_e, cache_date = repo.get_enriched_latest()
    if df_e.is_empty():
        return {"rows": [], "as_of": None, "elapsed_ms": 0}

    # 按 symbol 过滤
    df = df_e.filter(pl.col("symbol").is_in(symbols))
    if df.is_empty():
        return {"rows": [], "as_of": str(cache_date) if cache_date else None, "elapsed_ms": 0}

    # JOIN instruments 取 name + float_shares
    df_i = repo.get_instruments()
    if not df_i.is_empty() and "name" in df_i.columns:
        inst_cols = [c for c in ["symbol", "name", "float_shares"] if c in df_i.columns]
        df = df.join(df_i.select(inst_cols), on="symbol", how="left")

    # 选择内置需要的列
    keep = [c for c in _WATCHLIST_COLS + ["name", "float_shares"] if c in df.columns]
    df = df.select(keep)

    # 动态 JOIN 扩展数据表
    ext_specs = _parse_ext_columns(ext_columns) if ext_columns else []
    if ext_specs:
        db = repo.store.db
        data_dir = repo.store.data_dir
        from app.services.ext_data import ExtConfigStore
        from app.api.ext_data import _read_ext_dataframe

        ext_store = ExtConfigStore(data_dir)
        configs = {c.id: c for c in ext_store.load_all()}

        for config_id, field_name in ext_specs:
            view_name = f"ext_{config_id}"
            ext_col_name = f"{config_id}__{field_name}"
            try:
                # 扩展时序数据必须只取最新分区；否则一个 symbol 会按历史分区数被 JOIN 放大。
                cfg = configs.get(config_id)
                if cfg:
                    ext_df, _ = _read_ext_dataframe(cfg, data_dir)
                else:
                    ext_df = pl.from_arrow(
                        db.query(
                            f"SELECT symbol, {quote_ident(field_name)} FROM {view_name}"
                        ).arrow()
                    )
                if not ext_df.is_empty() and "symbol" in ext_df.columns:
                    ext_df = (
                        ext_df.select(["symbol", field_name])
                        .unique(subset=["symbol"], keep="last")
                        .rename({field_name: ext_col_name})
                    )
                    df = df.join(ext_df.select(["symbol", ext_col_name]), on="symbol", how="left")
            except Exception:
                # view 不存在或字段不存在，尝试直接读 parquet
                cfg = configs.get(config_id)
                if cfg:
                    try:
                        ext_df, _ = _read_ext_dataframe(cfg, data_dir)
                        if (
                            not ext_df.is_empty()
                            and "symbol" in ext_df.columns
                            and field_name in ext_df.columns
                        ):
                            ext_df = (
                                ext_df.select(["symbol", field_name])
                                .unique(subset=["symbol"], keep="last")
                                .rename({field_name: ext_col_name})
                            )
                            df = df.join(ext_df, on="symbol", how="left")
                    except Exception as e2:
                        logger.debug(
                            "ext join fallback failed for %s.%s: %s", config_id, field_name, e2
                        )

    # sanitize NaN / Inf
    float_cols = [c for c in df.columns if df[c].dtype.is_float()]
    if float_cols:
        df = df.with_columns(
            [
                pl.when(pl.col(c).is_nan() | pl.col(c).is_infinite())
                .then(None)
                .otherwise(pl.col(c))
                .alias(c)
                for c in float_cols
            ]
        )

    # 按自选添加顺序（新加的在前）重排行
    order_map = {s: i for i, s in enumerate(symbols)}
    df = df.with_columns(
        pl.col("symbol")
        .map_elements(lambda s: order_map.get(s, len(symbols)), return_dtype=pl.Int32)
        .alias("_sort_order")
    )
    df = df.sort("_sort_order").drop("_sort_order")

    rows = df.to_dicts()
    elapsed = (time.perf_counter() - t0) * 1000
    return {"rows": rows, "as_of": str(cache_date) if cache_date else None, "elapsed_ms": elapsed}


def _parse_ext_columns(ext_columns: str) -> list[tuple[str, str]]:
    """解析 'config_id1.field1,config_id2.field2' 为 [(config_id, field_name), ...]"""
    result = []
    for part in ext_columns.split(","):
        part = part.strip()
        if "." not in part:
            continue
        config_id, field_name = part.split(".", 1)
        config_id = config_id.strip()
        field_name = field_name.strip()
        if config_id and field_name and is_valid_ext_ident(config_id) and "\x00" not in field_name:
            result.append((config_id, field_name))
    return result
