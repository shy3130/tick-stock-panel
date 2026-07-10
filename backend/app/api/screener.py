"""Screener API。"""
from __future__ import annotations

import glob as _glob
import logging
import math
import os
import re
import time
from dataclasses import asdict
from datetime import date, datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.services.screener import PRESET_STRATEGIES, ScreenerService, strategy_supports_asset
from app.services import strategy_cache
from app.strategy import config as strategy_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/screener", tags=["screener"])


class CustomRequest(BaseModel):
    conditions: list[str]
    order_by: Optional[str] = None
    limit: int = 30
    pool: Optional[list[str]] = None
    as_of: Optional[date] = None
    ext_columns: Optional[str] = None
    asset_type: str = "stock"


class PresetRequest(BaseModel):
    strategy_id: str
    pool: Optional[list[str]] = None
    as_of: Optional[date] = None
    ext_columns: Optional[str] = None
    asset_type: str = "stock"


def _safe(result_dict: dict) -> dict:
    """sanitize for JSON(NaN / Inf → None)."""
    rows = result_dict.get("rows", [])
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, float) and not math.isfinite(v):
                r[k] = None
    return result_dict


_EXT_IDENT_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _safe_ext_value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# ── 扩展列 value_map 缓存 ────────────────────────────────────────────
# 每次请求 _load_ext_value_maps 都会重新从磁盘读 ext parquet 并重建 {symbol: value}。
# 用底层 parquet 文件的 (路径, mtime) 签名做 memoize: 文件未变则复用上次的 map,
# parquet 被重写 (mtime 变化) 时自动失效重算。仅缓存基于 config 的快照/时序路径,
# 无 config 的 DuckDB view 回退路径不缓存 (少见)。
_ext_value_map_cache: dict[tuple[str, str], tuple[Any, dict[str, Any]]] = {}


def _ext_parquet_signature(cfg, data_dir) -> Optional[tuple]:
    """该扩展配置底层 parquet 文件的 (路径, mtime) 签名; 出错返回 None (禁用缓存)。"""
    try:
        from app.api.ext_data import _parquet_glob
        pattern = _parquet_glob(cfg, data_dir)
        files = sorted(_glob.glob(pattern, recursive=True))
        if not files:
            return None
        return tuple((f, os.path.getmtime(f)) for f in files)
    except Exception:  # noqa: BLE001
        return None


def _load_ext_value_maps(repo, ext_columns: Optional[str]) -> dict[str, dict[str, Any]]:
    """按请求加载扩展列，返回 {输出列名: {symbol: value}}。

    策略结果缓存是共享文件，不能被不同 ext_columns 组合污染；因此扩展列只在
    返回前通过该投影映射追加到结果副本中。

    基于 config 的路径按 parquet 文件 mtime 签名 memoize, 文件未变时跳过磁盘重读。
    """
    ext_specs = _parse_ext_columns(ext_columns) if ext_columns else []
    if not ext_specs:
        return {}

    import polars as pl
    from app.api.ext_data import _read_ext_dataframe
    from app.services.ext_data import ExtConfigStore

    db = repo.store.db
    data_dir = repo.store.data_dir
    ext_store = ExtConfigStore(data_dir)
    configs = {c.id: c for c in ext_store.load_all()}
    value_maps: dict[str, dict[str, Any]] = {}

    for config_id, field_name in ext_specs:
        out_col = f"{config_id}__{field_name}"
        cfg = configs.get(config_id)
        cache_key = (config_id, field_name)
        sig = _ext_parquet_signature(cfg, data_dir) if cfg else None
        try:
            if cfg:
                # 命中缓存 (文件签名一致) → 复用, 免去磁盘重读
                cached = _ext_value_map_cache.get(cache_key)
                if cached is not None and sig is not None and cached[0] == sig:
                    value_maps[out_col] = cached[1]
                    continue
                # 时序扩展表只取最新分区，避免历史分区把同一 symbol JOIN 放大。
                ext_df, _ = _read_ext_dataframe(cfg, data_dir)
            else:
                view_name = f"ext_{config_id}"
                ext_df = pl.from_arrow(db.query(
                    f"SELECT symbol, {_quote_ident(field_name)} FROM {view_name}"
                ).arrow())

            if ext_df.is_empty() or "symbol" not in ext_df.columns or field_name not in ext_df.columns:
                continue

            ext_df = ext_df.select(["symbol", field_name]).unique(subset=["symbol"], keep="last")
            vmap = {
                str(row["symbol"]): _safe_ext_value(row.get(field_name))
                for row in ext_df.to_dicts()
                if row.get("symbol")
            }
            value_maps[out_col] = vmap
            if cfg and sig is not None:
                _ext_value_map_cache[cache_key] = (sig, vmap)
        except Exception as e:  # noqa: BLE001
            logger.debug("screener ext column join skipped for %s.%s: %s", config_id, field_name, e)

    return value_maps


def _row_with_ext(row: dict, ext_values: dict[str, dict[str, Any]], symbol: Optional[str] = None) -> dict:
    next_row = dict(row)
    sym = symbol or next_row.get("symbol")
    for out_col, value_map in ext_values.items():
        next_row[out_col] = value_map.get(str(sym)) if sym else None
    return next_row


def _rows_with_ext(rows: list[dict], ext_values: dict[str, dict[str, Any]]) -> list[dict]:
    if not ext_values:
        return rows
    return [_row_with_ext(r, ext_values) for r in rows]


def _result_with_ext(result_dict: dict, ext_values: dict[str, dict[str, Any]]) -> dict:
    if not ext_values:
        return result_dict
    return {**result_dict, "rows": _rows_with_ext(result_dict.get("rows", []), ext_values)}


def _results_with_ext(results: dict[str, dict], ext_values: dict[str, dict[str, Any]]) -> dict[str, dict]:
    if not ext_values:
        return results
    return {sid: _result_with_ext(r, ext_values) for sid, r in results.items()}


def _cache_payload_with_ext(cached: dict, ext_values: dict[str, dict[str, Any]]) -> dict:
    if not ext_values:
        return cached

    payload = dict(cached)
    payload["results"] = _results_with_ext(cached.get("results", {}), ext_values)

    ever_rows = cached.get("today_ever_rows")
    if isinstance(ever_rows, dict):
        enriched_ever: dict[str, dict[str, dict]] = {}
        for sid, sym_map in ever_rows.items():
            if not isinstance(sym_map, dict):
                continue
            enriched_ever[sid] = {
                sym: _row_with_ext(row, ext_values, symbol=sym)
                for sym, row in sym_map.items()
                if isinstance(row, dict)
            }
        payload["today_ever_rows"] = enriched_ever

    return payload


def _update_cache_strategy(data_dir, as_of: str, strategy_id: str, safe_data: dict) -> None:
    """单跑后更新缓存中该策略的结果，保持缓存与最新计算一致。"""
    from app.services import strategy_cache
    cached = strategy_cache.read_cache(data_dir)
    if cached and cached.get("as_of") == as_of:
        results = cached.get("results", {})
        results[strategy_id] = {
            "total": safe_data.get("total", 0),
            "as_of": as_of,
            "rows": safe_data.get("rows", []),
        }
        strategy_cache.write_cache(data_dir, as_of, results)


@router.get("/strategies")
def strategies(request: Request, asset_type: str = Query("stock")):
    """策略清单（内置 + 自定义 + AI）。按 asset_type 过滤：ETF 仅返回技术类内置策略。"""
    data_dir = request.app.state.repo.store.data_dir
    presets = []
    seen_ids: set[str] = set()

    # 内置策略
    for k, v in PRESET_STRATEGIES.items():
        if not strategy_supports_asset(v, asset_type):
            continue
        overrides = strategy_config.load_override(data_dir, k)
        name = (overrides.get("name") or v["name"]) if overrides else v["name"]
        desc = (overrides.get("description") or v["description"]) if overrides else v["description"]
        presets.append({"id": k, "name": name, "description": desc, "source": "builtin"})
        seen_ids.add(k)

    # 自定义/AI 策略（不在 PRESET_STRATEGIES 中的）; 未标注资产类型, 保守仅 stock 返回
    engine = getattr(request.app.state, "strategy_engine", None)
    if engine and asset_type == "stock":
        for meta in engine.list_strategies():
            sid = meta["id"]
            if sid not in seen_ids:
                overrides = strategy_config.load_override(data_dir, sid)
                name = (overrides.get("name") or meta["name"]) if overrides else meta["name"]
                desc = (overrides.get("description") or meta.get("description", "")) if overrides else meta.get("description", "")
                presets.append({"id": sid, "name": name, "description": desc, "source": meta.get("source", "custom")})
                seen_ids.add(sid)
        # 暴露加载失败的策略,让前端可见(避免"策略静默消失"误判为正常)
        load_errors = engine.load_errors() if engine else []
    else:
        load_errors = []

    return {"presets": presets, "load_errors": load_errors}


@router.post("/run")
def run_custom(req: CustomRequest, request: Request):
    repo = request.app.state.repo
    svc = ScreenerService(repo, asset_type=req.asset_type)
    as_of = req.as_of or svc.latest_date()
    if not as_of:
        raise HTTPException(status_code=400,
                            detail="无可用数据日期 — enriched 表为空,请先运行盘后管道")
    result = svc.run(
        as_of=as_of,
        conditions=req.conditions,
        order_by=req.order_by,
        limit=req.limit,
        pool=req.pool,
    )
    safe_data = _safe(asdict(result))
    ext_values = _load_ext_value_maps(repo, req.ext_columns)
    return _result_with_ext(safe_data, ext_values)


@router.post("/run_preset")
def run_preset(req: PresetRequest, request: Request):
    repo = request.app.state.repo
    svc = ScreenerService(repo, asset_type=req.asset_type)
    as_of = req.as_of or svc.latest_date()
    if not as_of:
        raise HTTPException(status_code=400, detail="无可用数据日期")

    # 加载用户保存的策略配置
    data_dir = request.app.state.repo.store.data_dir
    ext_values = _load_ext_value_maps(repo, req.ext_columns)
    overrides = strategy_config.load_override(data_dir, req.strategy_id)
    bf = overrides.get("basic_filter") if overrides else None
    dl = overrides.get("display_limit") if overrides else None
    if dl is None and overrides and "display_limit" in overrides:
        dl = 0

    # 内置策略
    if req.strategy_id in PRESET_STRATEGIES:
        try:
            result = svc.run_preset(req.strategy_id, as_of=as_of, pool=req.pool, basic_filter=bf, display_limit=dl)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        safe_data = _safe(asdict(result))
        _update_cache_strategy(data_dir, str(as_of), req.strategy_id, safe_data)
        return _result_with_ext(safe_data, ext_values)

    # 自定义/AI 策略 — 通过 StrategyEngine 执行
    engine = getattr(request.app.state, "strategy_engine", None)
    if not engine:
        raise HTTPException(status_code=404, detail=f"策略引擎未初始化或策略 {req.strategy_id} 不存在")

    try:
        result = engine.run(req.strategy_id, as_of, pool=req.pool, overrides=overrides or None)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    data = asdict(result)

    if dl is not None and dl > 0:
        data["rows"] = data["rows"][:dl]
        data["total"] = min(data["total"], dl)

    # 单跑后更新缓存中该策略的结果（保持缓存最新）
    safe_data = _safe(data)
    _update_cache_strategy(data_dir, str(as_of), req.strategy_id, safe_data)

    return _result_with_ext(safe_data, ext_values)


@router.get("/cached")
def get_cached(
    request: Request,
    ext_columns: Optional[str] = Query(None, description="逗号分隔: config_id.field_name"),
):
    """读取策略结果缓存, 并叠加监控引擎本轮实时算出的结果。

    - 盘后缓存 (strategy_cache.json): 非监控策略 / 页面秒加载用, run_all 写入。
    - 监控引擎内存结果 (latest_strategy_results): 实时行情每轮对「加入监控的策略」算出,
      不落盘 (避免与 read_cache 的 mtime 校验冲突), 在此直接叠加覆盖盘后结果。
      被监控的策略拿到新鲜数据, 非监控策略仍用盘后缓存。
    """
    data_dir = request.app.state.repo.store.data_dir
    cached = strategy_cache.read_cache(data_dir)
    if cached is None:
        cached = {"as_of": None, "results": {}, "updated_at": None}

    # 叠加监控引擎内存里的实时结果 (若有), 用新鲜数据覆盖同策略的盘后结果
    monitor_engine = getattr(request.app.state, "monitor_engine", None)
    if monitor_engine is not None:
        realtime_results = monitor_engine.latest_strategy_results()
        if realtime_results:
            results = dict(cached.get("results") or {})
            results.update(realtime_results)
            cached = dict(cached)
            cached["results"] = results
            # 有实时数据时, 以最新时间戳为准
            import time as _time
            cached["updated_at"] = int(_time.time() * 1000)

    # 无任何数据 (盘后缓存空 + 无实时结果) → 返回空标记, 前端据此提示
    if not cached.get("results") and cached.get("as_of") is None:
        return {"as_of": None, "results": {}, "updated_at": None}

    ext_values = _load_ext_value_maps(request.app.state.repo, ext_columns)
    return _cache_payload_with_ext(cached, ext_values)


@router.get("/market-snapshot")
def market_snapshot(request: Request):
    """最新全市场轻量行情快照，供板块/概念聚合分析使用。"""
    import polars as pl

    repo = request.app.state.repo
    svc = ScreenerService(repo)
    as_of = svc.latest_date()
    if not as_of:
        return {"as_of": None, "rows": []}

    df = svc._load_enriched_for_date(as_of)
    if df.is_empty():
        return {"as_of": str(as_of), "rows": []}

    if "close" in df.columns and "total_shares" in df.columns and "market_cap" not in df.columns:
        df = df.with_columns((pl.col("close") * pl.col("total_shares")).alias("market_cap"))
    if "close" in df.columns and "float_shares" in df.columns and "float_market_cap" not in df.columns:
        df = df.with_columns((pl.col("close") * pl.col("float_shares")).alias("float_market_cap"))

    cols = [
        "symbol", "name", "close", "change_pct", "amount", "volume",
        "turnover_rate", "vol_ratio_5d", "total_shares", "float_shares",
        "market_cap", "float_market_cap", "consecutive_limit_ups",
    ]
    df = df.select([c for c in cols if c in df.columns])
    rows = df.to_dicts()
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, float) and not math.isfinite(v):
                r[k] = None

    return {"as_of": str(as_of), "rows": rows}


@router.post("/run_all")
def run_all(request: Request, body: Optional[dict] = None):
    """批量运行指定策略,只返回每个策略的命中数。

    优化: 从 enriched 读取一次目标日期数据, 所有策略共享。
    body.strategy_ids: 只跑指定的策略 ID 列表, 为空则跑全部。
    """
    from datetime import date as date_type

    t_total = time.perf_counter()

    body = body or {}
    repo = request.app.state.repo
    svc = ScreenerService(repo)

    # 解析日期
    raw_date = body.get("as_of")
    if raw_date:
        as_of = date_type.fromisoformat(str(raw_date)) if isinstance(raw_date, str) else raw_date
    else:
        as_of = svc.latest_date()
    if not as_of:
        return {"as_of": None, "results": {}}

    # 一次读取目标日期的全部数据
    t0 = time.perf_counter()
    precomputed = svc._load_enriched_for_date(as_of)
    logger.info("run_all: _load_enriched_for_date took %.1fms", (time.perf_counter() - t0) * 1000)

    results: dict[str, dict] = {}
    data_dir = request.app.state.repo.store.data_dir

    # 收集需要运行的策略 ID (如果指定了 strategy_ids 则只跑这些)
    requested_ids = body.get("strategy_ids")
    all_ids = list(PRESET_STRATEGIES.keys())
    engine = getattr(request.app.state, "strategy_engine", None)
    if engine:
        for meta in engine.list_strategies():
            sid = meta["id"]
            if sid not in PRESET_STRATEGIES:
                all_ids.append(sid)

    if requested_ids and isinstance(requested_ids, list):
        id_set = set(requested_ids)
        all_ids = [sid for sid in all_ids if sid in id_set]

    if not all_ids:
        return {"as_of": str(as_of), "results": {}}

    # 批量预加载所有 override 配置
    t0 = time.perf_counter()
    all_overrides = strategy_config.list_overrides(data_dir)
    logger.info("run_all: list_overrides took %.1fms (%d overrides)", (time.perf_counter() - t0) * 1000, len(all_overrides))

    # 历史策略: 只在需要时加载 (只加载 all_ids 中包含的 filter_history 策略)
    t0 = time.perf_counter()
    shared_history = None
    id_set = set(all_ids)
    if engine:
        history_strats = [
            (sid, s) for sid, s in engine._strategies.items()
            if s.filter_history_fn and sid in id_set
        ]
        if history_strats:
            max_lb = min(max(s.lookback_days for _, s in history_strats), 30)
            shared_history = svc._load_enriched_history(as_of, max(1, max_lb))
    else:
        history_strats = []
    logger.info("run_all: _load_enriched_history took %.1fms (history_strats=%d)", (time.perf_counter() - t0) * 1000, len(history_strats))

    for sid in all_ids:
        try:
            overrides = all_overrides.get(sid, {})
            bf = overrides.get("basic_filter") if overrides else None
            dl = overrides.get("display_limit") if overrides else None
            if dl is None and overrides and "display_limit" in overrides:
                dl = 0

            if sid in PRESET_STRATEGIES:
                r = svc.run_preset(sid, as_of=as_of, precomputed=precomputed, basic_filter=bf, display_limit=dl)
            else:
                r = engine.run(
                    sid, as_of, overrides=overrides or None,
                    precomputed=precomputed, precomputed_history=shared_history,
                )
                if dl is not None and dl > 0:
                    r.rows = r.rows[:dl]
                    r.total = min(r.total, dl)

            safe_rows = _safe(asdict(r)).get("rows", [])
            results[sid] = {"total": r.total, "as_of": str(as_of), "rows": safe_rows}
        except (ValueError, Exception):
            continue

    elapsed = (time.perf_counter() - t_total) * 1000
    logger.info("run_all: total took %.1fms (%d strategies)", elapsed, len(all_ids))

    # 写入策略缓存 (供页面秒加载)
    if results:
        try:
            strategy_cache.write_cache(data_dir, str(as_of), results)
        except Exception:  # noqa: BLE001
            pass

    ext_values = _load_ext_value_maps(repo, body.get("ext_columns"))
    return {"as_of": str(as_of), "results": _results_with_ext(results, ext_values)}


@router.get("/limit-ladder")
def limit_ladder(
    request: Request,
    as_of: Optional[date] = None,
    direction: str = Query("up", description="up=涨停梯队 | down=跌停梯队"),
    ext_columns: Optional[str] = Query(None, description="逗号分隔: config_id.field_name"),
):
    """连板/连跌梯队 — 按连板数分组, 含三状态。
    返回: tiers = [{ boards, count, stocks: [{symbol,name,change_pct,status,...}] }]

    direction=up (默认):
      status: limit_up=涨停 | broken=炸板(摸板未封) | failed=断板(晋级失败)
    direction=down:
      status: limit_down=跌停 | recovery=翘板(跌停后回升,含收阳条件) | failed=止跌(昨日跌停今日未跌停也未翘板)

    ext_columns: 动态 JOIN 扩展数据, 如 "concept.concept,industry.industry"
    """
    import polars as pl

    is_down = direction == "down"

    # 按 direction 参数化字段映射
    if is_down:
        sig_col = "signal_limit_down"
        consec_col = "consecutive_limit_downs"
        broken_col = "signal_limit_down_recovery"
        status_main, status_broken, status_failed = "limit_down", "recovery", "failed"
    else:
        sig_col = "signal_limit_up"
        consec_col = "consecutive_limit_ups"
        broken_col = "signal_broken_limit_up"
        status_main, status_broken, status_failed = "limit_up", "broken", "failed"

    repo = request.app.state.repo
    svc = ScreenerService(repo)
    as_of = as_of or svc.latest_date()
    if not as_of:
        raise HTTPException(status_code=400, detail="无可用数据日期")

    df = svc._load_enriched_for_date(as_of)
    if df.is_empty():
        return {"as_of": str(as_of), "tiers": [], "counts": {"up": 0, "down": 0}}

    # 双方向涨跌停计数(不论当前 direction, 前端始终同时显示)
    count_up_raw = int(df.filter(pl.col("signal_limit_up").fill_null(False)).height) if "signal_limit_up" in df.columns else 0
    count_down_raw = int(df.filter(pl.col("signal_limit_down").fill_null(False)).height) if "signal_limit_down" in df.columns else 0

    # 双方向 sealed 修正: 减去各自的假涨停(假涨停已归炸板, 不计入涨停数)
    depth_svc_global = getattr(request.app.state, "depth_service", None)
    fake_up = 0
    fake_down = 0
    sealed_up_ready = False
    sealed_down_ready = False
    if depth_svc_global:
        up_map = depth_svc_global.get_sealed_map(as_of, is_down=False)
        down_map = depth_svc_global.get_sealed_map(as_of, is_down=True)
        sealed_up_ready = bool(up_map) and depth_svc_global.is_sealed_ready(as_of)
        sealed_down_ready = bool(down_map) and depth_svc_global.is_sealed_ready(as_of)
        if up_map:
            fake_up = sum(1 for v in up_map.values() if v.get("sealed") is False)
        if down_map:
            fake_down = sum(1 for v in down_map.values() if v.get("sealed") is False)
    count_up = count_up_raw - fake_up if sealed_up_ready else count_up_raw
    count_down = count_down_raw - fake_down if sealed_down_ready else count_down_raw

    # 双方向 sealed 明细(供前端弹窗同时显示涨跌停)
    def _count_sealed(m: dict, ready: bool):
        if not m or not ready:
            return {"real": 0, "fake": 0, "pending": 0}
        real = sum(1 for v in m.values() if v.get("sealed") is True)
        fake = sum(1 for v in m.values() if v.get("sealed") is False)
        pending = sum(1 for v in m.values() if v.get("sealed") is None)
        return {"real": real, "fake": fake, "pending": pending}
    sealed_counts_up = _count_sealed(up_map, sealed_up_ready)
    sealed_counts_down = _count_sealed(down_map, sealed_down_ready)

    # 加载前一日的 prev consecutive_limit_ups/downs
    # 窄读: 仅取前一交易日的 [symbol, consec_col] 两列 (存储列, 直接谓词下推读 parquet),
    # 替代旧的 range(1,10) 循环逐日 _load_enriched_for_date 全量指标重算 (最坏 9× 全市场重算)。
    prev_consec: pl.DataFrame = svc.load_prior_consecutive(as_of, consec_col)

    if not prev_consec.is_empty():
        df = df.join(prev_consec, on="symbol", how="left")
    else:
        df = df.with_columns(pl.lit(0).cast(pl.UInt32).alias("prev_consec"))

    # 表达式
    is_limit = pl.col(sig_col).fill_null(False) if sig_col in df.columns else pl.lit(False)
    is_broken = pl.col(broken_col).fill_null(False) if broken_col in df.columns else pl.lit(False)
    consec = pl.col(consec_col).fill_null(0) if consec_col in df.columns else pl.lit(0)
    prev_c = pl.col("prev_consec").fill_null(0)

    # 计算 status + boards (结构涨跌停对称, 仅字段与字面量不同)
    is_failed = ~is_limit & ~is_broken & (prev_c > 0)
    df = df.with_columns([
        pl.when(is_limit).then(pl.lit(status_main))
        .when(is_broken).then(pl.lit(status_broken))
        .when(is_failed).then(pl.lit(status_failed))
        .otherwise(None).alias("status"),
        pl.when(is_limit).then(consec)
        .when(is_broken | is_failed).then(prev_c + 1)
        .otherwise(0).cast(pl.UInt32).alias("boards"),
    ])

    df = df.filter(pl.col("status").is_not_null() & (pl.col("boards") > 0))

    # ── 五档 sealed 叠加(独立旁路, 不改 signal_limit_up) ──
    # 假涨停(收盘价=涨停价但卖一有量)从 limit 降级为 broken(归炸板视图)
    # 真涨停保留 + 附封单量; sealed=null(待确认/降级)保持原状
    depth_svc = getattr(request.app.state, "depth_service", None)
    sealed_ready = False
    sealed_age: float | None = None
    if depth_svc:
        sealed_map = depth_svc.get_sealed_map(as_of, is_down=is_down)
        sealed_ready = bool(sealed_map) and depth_svc.is_sealed_ready(as_of)
        sealed_age = depth_svc.get_sealed_age(as_of) if sealed_ready else None

        if sealed_map:
            # 构建 sealed 列(symbol → sealed bool, vol)
            sym_sealed = {s: v.get("sealed") for s, v in sealed_map.items()}
            sym_vol = {s: v.get("vol") for s, v in sealed_map.items()}

            # JOIN sealed: 对每只 status=main 的票, 看 sealed 值
            sealed_rows = pl.DataFrame({
                "symbol": list(sym_sealed.keys()),
                "_sealed": list(sym_sealed.values()),
                "_sealed_vol": list(sym_vol.values()),
            }) if sym_sealed else pl.DataFrame()

            if not sealed_rows.is_empty():
                df = df.join(sealed_rows, on="symbol", how="left")
                # 假涨停(main 状态但 sealed=False)→ 降级为 broken
                df = df.with_columns(
                    pl.when(
                        (pl.col("status") == status_main)
                        & pl.col("_sealed").is_not_null()
                        & (pl.col("_sealed") == False)  # noqa: E712
                    ).then(pl.lit(status_broken))
                    .otherwise(pl.col("status")).alias("status"),
                    # sealed_status: real/fake/pending/null
                    pl.when(
                        (pl.col("status") == status_main)
                        & (pl.col("_sealed") == True)  # noqa: E712
                    ).then(pl.lit("real"))
                    .when(
                        (pl.col("_sealed") == False)  # noqa: E712
                    ).then(pl.lit("fake"))
                    .when(
                        (pl.col("status") == status_main)
                        & pl.col("_sealed").is_null()
                    ).then(pl.lit("pending"))
                    .otherwise(None).alias("sealed_status"),
                    pl.col("_sealed_vol").alias("sealed_vol"),
                ).drop(["_sealed", "_sealed_vol"])
            else:
                df = df.with_columns(
                    pl.lit(None).alias("sealed_status"),
                    pl.lit(None).alias("sealed_vol"),
                )
        else:
            df = df.with_columns(
                pl.lit(None).alias("sealed_status"),
                pl.lit(None).alias("sealed_vol"),
            )
    else:
        df = df.with_columns(
            pl.lit(None).alias("sealed_status"),
            pl.lit(None).alias("sealed_vol"),
        )

    # 动态 JOIN 扩展数据
    ext_specs = _parse_ext_columns(ext_columns) if ext_columns else []
    ext_col_names: list[str] = []
    if ext_specs:
        db = repo.store.db
        data_dir = repo.store.data_dir
        from app.services.ext_data import ExtConfigStore

        ext_store = ExtConfigStore(data_dir)
        configs = {c.id: c for c in ext_store.load_all()}

        for config_id, field_name in ext_specs:
            view_name = f"ext_{config_id}"
            ext_col_name = f"{config_id}__{field_name}"
            try:
                ext_df = pl.from_arrow(db.query(
                    f"SELECT symbol, \"{field_name}\" FROM {view_name}"
                ).arrow())
                if not ext_df.is_empty() and "symbol" in ext_df.columns:
                    ext_df = ext_df.rename({field_name: ext_col_name})
                    df = df.join(ext_df.select(["symbol", ext_col_name]), on="symbol", how="left")
                    ext_col_names.append(ext_col_name)
            except Exception:
                cfg = configs.get(config_id)
                if cfg:
                    try:
                        from app.api.ext_data import _parquet_glob
                        glob = _parquet_glob(cfg, data_dir)
                        ext_df = pl.read_parquet(glob)
                        if not ext_df.is_empty() and "symbol" in ext_df.columns and field_name in ext_df.columns:
                            ext_df = ext_df.select(["symbol", field_name]).rename({field_name: ext_col_name})
                            df = df.join(ext_df, on="symbol", how="left")
                            ext_col_names.append(ext_col_name)
                    except Exception:
                        pass

    # 选择输出列
    cols = ["symbol", "name", "close", "change_pct", "boards", "status", consec_col, "sealed_status", "sealed_vol"] + ext_col_names
    df = df.select([c for c in cols if c in df.columns])
    # 排序: boards 降序, status 按主状态→炸/翘→断/止
    status_order = pl.when(pl.col("status") == status_main).then(0)
    status_order = status_order.when(pl.col("status") == status_broken).then(1)
    status_order = status_order.otherwise(2).alias("_status_order")
    df = df.with_columns(status_order).sort(["boards", "_status_order"], descending=[True, False]).drop("_status_order")

    rows = df.to_dicts()
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, float) and not math.isfinite(v):
                r[k] = None

    # 按 boards 分组
    tiers: dict[int, list] = {}
    for r in rows:
        n = int(r.get("boards") or 0)
        tiers.setdefault(n, []).append(r)

    tier_list = [
        {"boards": n, "count": len(stocks), "stocks": stocks}
        for n, stocks in sorted(tiers.items(), key=lambda x: -x[0])
    ]

    return {
        "as_of": str(as_of),
        "tiers": tier_list,
        "counts": {"up": count_up, "down": count_down},
        "counts_raw": {"up": count_up_raw, "down": count_down_raw},
        "sealed_ready": sealed_ready,
        "sealed_age": round(sealed_age, 0) if sealed_age is not None else None,
        "sealed_counts": {
            "real": sum(1 for t in tier_list for s in t.get("stocks", []) if s.get("sealed_status") == "real"),
            "fake": sum(1 for t in tier_list for s in t.get("stocks", []) if s.get("sealed_status") == "fake"),
            "pending": sum(1 for t in tier_list for s in t.get("stocks", []) if s.get("sealed_status") == "pending"),
        },
        "sealed_counts_up": sealed_counts_up,
        "sealed_counts_down": sealed_counts_down,
    }


def _parse_ext_columns(ext_columns: str) -> list[tuple[str, str]]:
    """解析 'config_id1.field1,config_id2.field2' 为 [(config_id, field_name), ...]。"""
    result = []
    for part in ext_columns.split(","):
        part = part.strip()
        if "." not in part:
            continue
        config_id, field_name = part.split(".", 1)
        config_id = config_id.strip()
        field_name = field_name.strip()
        if not config_id or not field_name:
            continue
        if not _EXT_IDENT_RE.match(config_id) or "\x00" in field_name:
            continue
        result.append((config_id, field_name))
    return result
