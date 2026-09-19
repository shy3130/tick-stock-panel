"""自选股 API。"""
from __future__ import annotations

import logging
import math
import time
from datetime import UTC, date, datetime
from typing import Callable

import anyio
import polars as pl
from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel

from app.db_safe import is_valid_ext_ident, quote_ident
from app.market_time import CN_TZ
from app.price_limits import (
    polars_is_risk_warning_name,
    polars_limit_price,
    polars_price_limit_pct,
)
from app.services import watchlist
from app.services.watchlist_csv import import_watchlist_codes, import_watchlist_csv
from app.services.watchlist_ocr import import_watchlist_image
from app.services.watchlist_ocr.provider import get_ocr_provider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

_MAX_IMPORT_IMAGE_BYTES = 12 * 1024 * 1024  # 12MB
_IMPORT_IMAGE_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/bmp",
    "image/gif",
}
# OCR 独立并发上限：避免多张大图同时解码 + 多 Tesseract 子进程
_OCR_LIMITER = anyio.CapacityLimiter(2)
# CSV/TXT 导入：文本远小于截图，上限 5MB 足够
_MAX_IMPORT_CSV_BYTES = 5 * 1024 * 1024
_IMPORT_CSV_TYPES = {
    "text/csv",
    "text/plain",
    "application/csv",
}
# 上传分块读取粒度 (与 ext_data 上传一致)
_UPLOAD_CHUNK_BYTES = 1024 * 1024


async def _read_upload_capped(file: UploadFile, max_bytes: int, too_large: str) -> bytes:
    """分块读取上传内容, 累计超过 max_bytes 立即拒绝(400), 返回完整字节。

    与 ext_data._write_upload_capped 同类保护: 一次性 `await file.read()` 会先把整个
    文件读入内存再比较长度, 上限在那之后才生效, 一个远超上限的上传照样把进程内存
    顶满; 分块读取在越过上限的那一块就停止, 内存占用不超过上限 + 一块。
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(400, too_large)
        chunks.append(chunk)
    return b"".join(chunks)


class AddRequest(BaseModel):
    symbol: str
    note: str = ""
    group_id: str | None = None


class BatchAddRequest(BaseModel):
    symbols: list[str]
    note: str = ""
    group_id: str | None = None
    group_ids: list[str] | None = None


class GroupNameRequest(BaseModel):
    name: str
    color: str | None = None


class GroupReorderRequest(BaseModel):
    ordered_ids: list[str]


class GroupAssignRequest(BaseModel):
    group_id: str | None = None


class ImportCodesRequest(BaseModel):
    text: str


def _with_names(rows: list[dict], request: Request) -> list[dict]:
    if not rows:
        return rows
    try:
        # 股票 + ETF 名称统一由 repo.get_name_map 解析, 自选列表可混合持有
        name_by_symbol = request.app.state.repo.get_name_map([r.get("symbol") for r in rows])
        if not name_by_symbol:
            return rows
        return [{**row, "name": name_by_symbol.get(row.get("symbol"))} for row in rows]
    except Exception as e:  # noqa: BLE001
        logger.debug("attach watchlist names failed: %s", e)
        return rows


@router.get("")
def list_all(request: Request):
    return {"symbols": _with_names(watchlist.list_symbols(), request)}


@router.post("")
def add_one(req: AddRequest, request: Request):
    try:
        rows = watchlist.add(req.symbol, req.note, req.group_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"symbols": _with_names(rows, request)}


@router.post("/batch")
def add_batch(req: BatchAddRequest, request: Request):
    try:
        rows, added = watchlist.add_batch(
            req.symbols,
            req.note,
            group_id=req.group_id,
            group_ids=req.group_ids,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"symbols": _with_names(rows, request), "added": added}


@router.get("/groups")
def list_groups():
    return {"groups": watchlist.list_groups()}


@router.post("/groups")
def create_group(req: GroupNameRequest):
    try:
        groups, group = watchlist.create_group(req.name, req.color)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"groups": groups, "group": group}


@router.put("/groups/reorder")
def reorder_groups(req: GroupReorderRequest):
    """重排分组前后顺序 (json 数组顺序即定义顺序, 侧边栏/标签栏/分组视图共用)。"""
    try:
        groups = watchlist.reorder_groups(req.ordered_ids)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"groups": groups}


@router.put("/groups/{group_id}")
def rename_group(group_id: str, req: GroupNameRequest):
    try:
        groups = watchlist.rename_group(group_id, req.name, req.color)
    except KeyError as e:
        raise HTTPException(404, "自选分组不存在") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"groups": groups}


@router.delete("/groups/{group_id}")
def delete_group(group_id: str, request: Request):
    try:
        groups, rows = watchlist.delete_group(group_id)
    except KeyError as e:
        raise HTTPException(404, "自选分组不存在") from e
    return {"groups": groups, "symbols": _with_names(rows, request)}


@router.post("/groups/{group_id}/clear")
def clear_group(group_id: str, request: Request):
    """清空分组成员:把该分组内所有股票转为未分组,保留分组定义。"""
    try:
        rows = watchlist.clear_group(group_id)
    except KeyError as e:
        raise HTTPException(404, "自选分组不存在") from e
    return {"symbols": _with_names(rows, request)}


@router.get("/ocr-status")
def ocr_status():
    """当前 OCR 引擎是否可用（前端可据此提示安装依赖）。"""
    provider = get_ocr_provider()
    return {"provider": provider.name, "available": provider.available()}


@router.post("/import-image")
async def import_from_image(request: Request, file: UploadFile = File(...)):
    """从自选截图识别股票代码，返回候选列表（不自动写入自选）。"""
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    filename = (file.filename or "").lower()
    # 严格白名单：不接受任意 image/*（如 image/svg+xml）
    ok_type = content_type in _IMPORT_IMAGE_TYPES
    ok_ext = filename.endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"))
    if not ok_type and not ok_ext:
        raise HTTPException(400, "仅支持 JPG / PNG / WebP / BMP / GIF 图片")

    data = await _read_upload_capped(file, _MAX_IMPORT_IMAGE_BYTES, "图片过大（上限 12MB）")
    if not data:
        raise HTTPException(400, "空文件")

    existing = {r["symbol"] for r in watchlist.list_symbols()}
    data_dir = request.app.state.repo.store.data_dir
    try:
        # OCR 为同步 CPU/子进程；独立 limiter 限制并发，避免卡住事件循环（行情 SSE 等）
        result = await anyio.to_thread.run_sync(
            lambda: import_watchlist_image(data, data_dir, existing_symbols=existing),
            limiter=_OCR_LIMITER,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.exception("watchlist import-image failed")
        raise HTTPException(500, f"识别失败: {e}") from e

    # 响应不回传整段 raw_text（可能很长）；调试时可开 query，这里默认省略
    result.pop("raw_text", None)
    return result


def _run_candidate_import(parse: Callable[[], dict], empty_msg: str) -> dict:
    """执行候选解析：ValueError→400、其他→500、空候选→400、剥离 raw_text。"""
    try:
        result = parse()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.exception("watchlist import failed")
        raise HTTPException(500, f"解析失败: {e}") from e
    if not result["candidates"]:
        raise HTTPException(400, empty_msg)
    result.pop("raw_text", None)
    return result


@router.post("/import-csv")
async def import_from_csv(request: Request, file: UploadFile = File(...)):
    """从 CSV / TXT 导入自选候选列表（不自动写入自选）。

    兼容同花顺/东财/通达信导出（逗号或 Tab 分隔、UTF-8 或 GBK 编码）。目标分组
    在候选确认时由前端传入 batch 接口，本端点只做解析与主数据校验。
    """
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    filename = (file.filename or "").lower()
    ok_type = content_type in _IMPORT_CSV_TYPES
    ok_ext = filename.endswith((".csv", ".txt"))
    if not ok_type and not ok_ext:
        raise HTTPException(400, "仅支持 CSV / TXT 文件")

    data = await _read_upload_capped(file, _MAX_IMPORT_CSV_BYTES, "文件过大（上限 5MB）")
    if not data:
        raise HTTPException(400, "空文件")

    data_dir = request.app.state.repo.store.data_dir
    # 解码与自选/instruments parquet 读取为同步 CPU/IO，挪线程池避免卡事件循环
    return await anyio.to_thread.run_sync(
        lambda: _run_candidate_import(
            lambda: import_watchlist_csv(
                data,
                data_dir,
                existing_symbols={r["symbol"] for r in watchlist.list_symbols()},
            ),
            "文件中未识别到股票代码或名称",
        )
    )


@router.post("/import-codes")
def import_from_codes(req: ImportCodesRequest, request: Request):
    """从粘贴的证券代码导入自选候选列表（不自动写入自选）。"""
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "请输入要导入的股票代码")

    existing = {r["symbol"] for r in watchlist.list_symbols()}
    data_dir = request.app.state.repo.store.data_dir
    return _run_candidate_import(
        lambda: import_watchlist_codes(text, data_dir, existing_symbols=existing),
        "未识别到股票代码",
    )


@router.post("/{symbol}/top")
def move_one_to_top(symbol: str, request: Request):
    rows = watchlist.move_to_top(symbol)
    return {"symbols": _with_names(rows, request)}


@router.put("/{symbol}/group")
def assign_group(symbol: str, req: GroupAssignRequest, request: Request):
    """互斥设定分组(仅保留此组; None=移出全部分组)。多组操作用 members 端点。"""
    try:
        rows = watchlist.set_group(symbol, req.group_id)
    except KeyError as e:
        raise HTTPException(404, "自选标的不存在") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"symbols": _with_names(rows, request)}


@router.post("/groups/{group_id}/members/{symbol}")
def add_member(group_id: str, symbol: str, request: Request):
    """把标的加入分组(多组成员关系: 不影响其他分组)。"""
    try:
        rows = watchlist.add_to_group(symbol, group_id)
    except KeyError as e:
        raise HTTPException(404, "自选标的不存在") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"symbols": _with_names(rows, request)}


@router.delete("/groups/{group_id}/members/{symbol}")
def remove_member(group_id: str, symbol: str, request: Request):
    """把标的移出分组(仅摘本组标签; 标的仍在自选, 可能落入未分组)。"""
    try:
        rows = watchlist.remove_from_group(symbol, group_id)
    except KeyError as e:
        raise HTTPException(404, "自选标的不存在") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"symbols": _with_names(rows, request)}


@router.delete("/{symbol}")
def remove_one(symbol: str, request: Request):
    rows = watchlist.remove(symbol)
    return {"symbols": _with_names(rows, request)}


@router.delete("")
def clear_all():
    """清空自选列表。"""
    count = watchlist.clear()
    return {"removed": count}


# 自选页需要的列
_WATCHLIST_COLS = [
    "symbol", "close", "open", "high", "low", "prev_close", "volume",
    "change_pct", "change_amount", "amount",
    "turnover_rate",
    "amplitude", "annual_vol_20d",
    "vol_ratio_5d",
    "ma5", "ma10", "ma20", "ma60",
    "vol_ma5", "vol_ma10",
    "high_60d", "low_60d",
    "rsi_6", "rsi_14", "rsi_24",
    "macd_dif", "macd_dea", "macd_hist",
    "kdj_k", "kdj_d", "kdj_j",
    "boll_upper", "boll_lower",
    "atr_14",
    "momentum_5d", "momentum_10d", "momentum_20d", "momentum_30d", "momentum_60d",
    "deviate_3d", "deviate_10d", "deviate_30d",
    "consecutive_limit_ups", "consecutive_limit_downs",
    "signal_limit_up", "signal_limit_down", "signal_volume_surge",
    "signal_ma_golden_5_20", "signal_macd_golden", "signal_n_day_high",
    "signal_boll_breakout_upper", "signal_ma20_breakout",
    "signal_ma_dead_5_20", "signal_macd_dead", "signal_n_day_low",
    "signal_boll_breakdown_lower", "signal_ma20_breakdown",
]


def _added_dates_bj(entries: list[dict]) -> dict[str, date]:
    """自选条目的 added_at (naive UTC ISO 串) → 北京日期; 空值/非法值跳过该条。

    added_at 由 watchlist.add_batch 以 datetime.utcnow() 写入 (无 Z 后缀), 属 UTC 语义;
    必须显式按 UTC 解析再转北京 —— 否则北京时间 00:00-08:00 加入的记录会被算到前一天。
    换算惯例同 backtest/minute_replay.py, 服务器本地时区不参与 (CONTRIBUTING §3.3)。
    """
    out: dict[str, date] = {}
    for entry in entries:
        raw = entry.get("added_at")
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw))
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        out[entry["symbol"]] = dt.astimezone(CN_TZ).date()
    return out


def _added_base_closes(
    repo,
    added_dates: dict[str, date],
    stock_symbols: set[str],
) -> pl.DataFrame:
    """取各标的「加入日或之前最近一个交易日」的前复权收盘价, 作为区间收益基准。

    返回 symbol/base_close 两列表; 无可用值返回空表, 由调用方按 null 降级。

    - **只覆盖股票**: 内存历史缓存 (get_enriched_range) 仅含股票, ETF/指数只有最新日
      缓存, 逐只回补会在行情 tick 热路径上形成 N+1 parquet 扫描, 故显式降级为「—」。
    - **基准日取 <= 加入日**: 周末/节假日/停牌自然回落到前一交易日。与 K 线竖线的
      「不 snap」有意不同 —— 价格口径可以回落, 日期事实不行。
    - **超出缓存窗口 (约 300 个自然日) 的标的直接跳过**, 不触发 scan 兜底。
    """
    stock_targets = {s: d for s, d in added_dates.items() if s in stock_symbols}
    if not stock_targets:
        return pl.DataFrame()  # 没有可算的股票标的 → 不碰 repo
    span = repo.get_enriched_history_span()
    if span is None:
        return pl.DataFrame()
    span_start, span_end = span
    in_range = {s: d for s, d in stock_targets.items() if d >= span_start}
    if len(in_range) < len(stock_targets):
        logger.debug(
            "自选加入后涨跌幅: %d/%d 只早于可查询历史 (%s 之前), 降级为 null",
            len(stock_targets) - len(in_range), len(stock_targets), span_start,
        )
    if not in_range:
        return pl.DataFrame()
    # 窗口必须钳制在缓存区间内: get_enriched_range 是全有或全无的 (cache_min > start
    # 或 cache_max < end 即整体返回 None), 一只越界的标的会让全部标的失去基准价。
    # start > end 只出现在「加入日晚于缓存末日」(本地数据未同步) 时, 退化为取缓存末日。
    end = min(max(in_range.values()), span_end)
    start = min(min(in_range.values()), end)
    hist = repo.get_enriched_range(
        start, end, symbols=list(in_range), columns=["symbol", "date", "close"]
    )
    if hist is None or hist.is_empty():
        return pl.DataFrame()
    added = pl.DataFrame(
        {"symbol": list(in_range), "added_date": list(in_range.values())},
        schema={"symbol": pl.Utf8, "added_date": pl.Date},
    )
    return (
        hist.join(added, on="symbol", how="inner")
        .filter(pl.col("date") <= pl.col("added_date"))
        .group_by("symbol")
        .agg(pl.col("close").sort_by(pl.col("date")).last().alias("base_close"))
    )


@router.get("/enriched")
def watchlist_enriched(
    request: Request,
    ext_columns: str | None = Query(None, description="逗号分隔的 ext 列: config_id.field_name"),
):
    """自选股 enriched 数据 — 直接从 enriched 最新日读取, 无重计算。

    仅两列为按行向量化现算 (不落盘): 涨跌停价, 以及「加入后涨跌幅」(pct_since_added,
    以加入日收盘价为基准)。本端点在行情 tick 热路径上被反复调用, 故不做历史 scan 兜底。

    ext_columns 参数示例: "industry_rating.score,fund_flow.net_inflow"
    会动态 LEFT JOIN 对应的 ext_{config_id} DuckDB view。
    """
    t0 = time.perf_counter()

    repo = request.app.state.repo
    entries = watchlist.list_symbols()
    symbols = [r["symbol"] for r in entries]
    if not symbols:
        return {"rows": [], "as_of": None, "elapsed_ms": 0}

    # 按资产拆分自选 symbol; ETF enriched 是独立缓存, 仅自选真的含 ETF 才去加载
    # (避免无 ETF 用户在缓存冷启动时触发 ETF 全量懒加载)
    etf_set = repo.get_etf_symbol_set()
    index_set = repo.get_index_symbol_set()
    etf_symbols = [s for s in symbols if s in etf_set]
    index_symbols = [s for s in symbols if s not in etf_set and s in index_set]
    stock_symbols = [s for s in symbols if s not in etf_set and s not in index_set]

    df_e, cache_date = repo.get_enriched_latest()

    # 以自选列表为主表 LEFT JOIN enriched, 保证自选的每一只都返回一行;
    # 不在 enriched 缓存里的标的 (新股/冷门股/新用户未同步) 指标为 null, 前端渲染为 "—".
    # 旧实现是 df_e.filter(is_in(stock_symbols)), 方向反了 (以 enriched 为主),
    # 会把不在缓存 universe 里的自选股静默丢弃.
    if stock_symbols:
        watchlist_df = pl.DataFrame({"symbol": stock_symbols})
        if df_e.is_empty():
            df = watchlist_df
        else:
            df = watchlist_df.join(df_e, on="symbol", how="left")
    else:
        df = pl.DataFrame()

    # ETF 行合并; 缺失列 (换手率/涨跌停信号等) 为 null
    etf_date = None
    if etf_symbols:
        df_etf_all, etf_date = repo.get_enriched_latest_asset("etf")
        etf_watchlist_df = pl.DataFrame({"symbol": etf_symbols})
        if not df_etf_all.is_empty():
            # ETF 同样以自选为主表 LEFT JOIN, 缺失标的指标为 null
            df_etf = etf_watchlist_df.join(df_etf_all, on="symbol", how="left")
        else:
            df_etf = etf_watchlist_df
        df = df_etf if df.is_empty() else pl.concat([df, df_etf], how="diagonal_relaxed")

    # 指数行合并 (镜像 ETF 分支); 缺失列 (换手率/涨跌停信号等) 为 null
    index_date = None
    if index_symbols:
        df_idx_all, index_date = repo.get_enriched_latest_asset("index")
        idx_watchlist_df = pl.DataFrame({"symbol": index_symbols})
        if not df_idx_all.is_empty():
            df_idx = idx_watchlist_df.join(df_idx_all, on="symbol", how="left")
        else:
            df_idx = idx_watchlist_df
        df = df_idx if df.is_empty() else pl.concat([df, df_idx], how="diagonal_relaxed")

    # as_of 取三类缓存中较旧者
    dates = [d for d in (cache_date if stock_symbols else None, etf_date, index_date) if d is not None]
    as_of = min(dates) if dates else None
    if df.is_empty():
        return {"rows": [], "as_of": str(as_of) if as_of else None, "elapsed_ms": 0}

    # JOIN float_shares (仅股票有) + 名称 (股票/ETF 统一走 get_name_map)
    df_i = repo.get_instruments()
    if not df_i.is_empty() and "float_shares" in df_i.columns:
        df = df.join(df_i.select(["symbol", "float_shares"]), on="symbol", how="left")
    name_map = repo.get_name_map(df["symbol"].to_list())
    df = df.with_columns(
        pl.col("symbol").replace_strict(name_map, default=None, return_dtype=pl.Utf8).alias("name")
    )

    # 标注资产类型: 前端据此渲染徽标/豁免板块筛选/分时列降级
    asset_map = {**{s: "etf" for s in etf_symbols}, **{s: "index" for s in index_symbols}}
    df = df.with_columns(
        pl.col("symbol").replace_strict(asset_map, default="stock", return_dtype=pl.Utf8).alias("asset_type")
    )

    # 选择内置需要的列
    keep = [c for c in _WATCHLIST_COLS + ["name", "float_shares", "asset_type"] if c in df.columns]
    df = df.select(keep)

    # 涨跌停价 (交易所整数分半进位口径, 仅股票): prev_close/名称已在行上, 对自选的
    # 几十~几百行向量化现算为亚毫秒级, 不写回 enriched 存储。ETF (跨境/债券 5% 等)
    # 与指数的涨跌幅规则不在 price_limits 覆盖内, 置 null 由前端渲染 "—"。
    as_of_date = as_of if isinstance(as_of, date) else None
    if as_of_date is None and as_of:
        try:
            as_of_date = date.fromisoformat(str(as_of)[:10])
        except ValueError:
            as_of_date = None
    if {"symbol", "prev_close", "name", "asset_type"}.issubset(df.columns) and as_of_date is not None:
        pct = polars_price_limit_pct(
            pl.col("symbol"),
            pl.lit(as_of_date),
            polars_is_risk_warning_name(pl.col("name")),
        )
        stock_with_prev = (pl.col("asset_type") == "stock") & pl.col("prev_close").is_not_null()
        df = df.with_columns(
            pl.when(stock_with_prev)
            .then(polars_limit_price(pl.col("prev_close"), pct, up=True))
            .otherwise(None)
            .alias("limit_up_price"),
            pl.when(stock_with_prev)
            .then(polars_limit_price(pl.col("prev_close"), pct, up=False))
            .otherwise(None)
            .alias("limit_down_price"),
        )

    # 「加入日期」与「加入以来」— 与涨跌停价同为读时现算, 不落盘。只在这一处挂一次即
    # 覆盖 stock/etf/index 三分支。pct_since_added 为小数口径 (与 momentum_* 一致)。
    try:
        added_at_df = pl.DataFrame(
            {
                "symbol": symbols,
                # 旧 schema 迁移补的空串归一为 null, 前端只需判断一种「缺失」
                "added_at": [(r.get("added_at") or None) for r in entries],
            },
            schema={"symbol": pl.Utf8, "added_at": pl.Utf8},
        )
        with_base = df.join(added_at_df, on="symbol", how="left")
        base_df = _added_base_closes(repo, _added_dates_bj(entries), set(stock_symbols))
        if base_df.is_empty():
            # 无基准价 (缓存冷 / 全为 ETF 或超窗): 整列降级为 null
            df = with_base.with_columns(pl.lit(None, dtype=pl.Float64).alias("pct_since_added"))
        else:
            # 整链算完再回绑 df, 异常时 df 保持原样
            df = (
                with_base.join(base_df, on="symbol", how="left")
                .with_columns(
                    pl.when(
                        pl.col("base_close").is_not_null()
                        & pl.col("base_close").is_finite()
                        & (pl.col("base_close") > 0)
                        & pl.col("close").is_not_null()
                    )
                    .then(pl.col("close") / pl.col("base_close") - 1.0)
                    .otherwise(None)
                    .alias("pct_since_added")
                )
                .drop("base_close")
            )
    except Exception as e:
        # 装饰列失败不得影响自选主表; 两列必须存在 (值可为 null), 否则前端排序/筛选静默失效
        logger.warning("自选加入日期/加入后涨跌幅计算跳过: %s", e, exc_info=True)
        df = df.with_columns(
            pl.lit(None, dtype=pl.Utf8).alias("added_at"),
            pl.lit(None, dtype=pl.Float64).alias("pct_since_added"),
        )

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
                    ext_df = pl.from_arrow(db.query(
                        f"SELECT symbol, {quote_ident(field_name)} FROM {view_name}"
                    ).arrow())
                if not ext_df.is_empty() and "symbol" in ext_df.columns:
                    ext_df = (
                        ext_df
                        .select(["symbol", field_name])
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
                        if not ext_df.is_empty() and "symbol" in ext_df.columns and field_name in ext_df.columns:
                            ext_df = (
                                ext_df
                                .select(["symbol", field_name])
                                .unique(subset=["symbol"], keep="last")
                                .rename({field_name: ext_col_name})
                            )
                            df = df.join(ext_df, on="symbol", how="left")
                    except Exception as e2:
                        logger.debug("ext join fallback failed for %s.%s: %s", config_id, field_name, e2)

    # sanitize NaN / Inf
    float_cols = [c for c in df.columns if df[c].dtype.is_float()]
    if float_cols:
        df = df.with_columns([
            pl.when(pl.col(c).is_nan() | pl.col(c).is_infinite())
              .then(None)
              .otherwise(pl.col(c))
              .alias(c)
            for c in float_cols
        ])

    # 按自选添加顺序（新加的在前）重排行
    order_map = {s: i for i, s in enumerate(symbols)}
    df = df.with_columns(pl.col("symbol").map_elements(lambda s: order_map.get(s, len(symbols)), return_dtype=pl.Int32).alias("_sort_order"))
    df = df.sort("_sort_order").drop("_sort_order")

    rows = df.to_dicts()
    elapsed = (time.perf_counter() - t0) * 1000
    return {"rows": rows, "as_of": str(as_of) if as_of else None, "elapsed_ms": elapsed}


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
        if config_id and field_name and is_valid_ext_ident(config_id):
            result.append((config_id, field_name))
    return result
