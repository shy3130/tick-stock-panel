"""K 线 / 同步 API。"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from app.indicators.pipeline import compute_enriched, compute_enriched_single
from app.services import kline_sync

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/kline", tags=["kline"])


def _asset_type_for_symbol(symbol: str) -> str:
    from app.data_providers.fquant.symbols import is_etf_symbol

    upper = symbol.upper()
    if upper.endswith(".HK"):
        return "hk"
    if upper.endswith(".INDEX"):
        return "index"
    if is_etf_symbol(upper):
        return "etf"
    return "stock"


@router.get("/instruments/search")
def search_instruments(
    request: Request,
    q: str = Query("", min_length=0, max_length=50, description="搜索关键词"),
    limit: int = Query(20, ge=1, le=50),
):
    """模糊搜索标的 (本地 instruments 优先, Eastmoney suggest 只补足缺口)。"""
    from app.services.symbol_search import search_symbols

    return {"results": search_symbols(request.app.state.repo, q, limit)}


@router.post("/instruments/names")
def instruments_names(request: Request, symbols: list[str]):
    """批量查股票名称。传入 symbol 列表, 返回 {symbol: name}。"""
    if not symbols:
        return {"names": {}}
    repo = request.app.state.repo
    df = repo.get_instruments()
    if df.is_empty():
        return {"names": {}}
    import polars as pl
    matched = df.filter(pl.col("symbol").is_in(symbols)).select(["symbol", "name"])
    names = {row["symbol"]: row["name"] for row in matched.iter_rows(named=True)}
    return {"names": names}


def _get_stock_info(repo, symbol: str) -> dict:
    """从 instruments 视图查标的名称 + 股本。"""
    try:
        row = repo.execute_one(
            "SELECT name, total_shares, float_shares FROM instruments WHERE symbol = ? LIMIT 1",
            [symbol],
        )
    except Exception:  # noqa: BLE001
        return {}
    if not row:
        return {}
    return {
        "name": row[0],
        "total_shares": row[1],
        "float_shares": row[2],
    }


@router.get("/daily")
def get_daily(
    request: Request,
    symbol: str = Query(..., description="标的代码,如 000001.SZ"),
    days: int = Query(120, ge=10, le=2000),
    start_date: Optional[str] = Query(None, description="起始日期 YYYY-MM-DD, 优先于 days"),
    end_date: Optional[str] = Query(None, description="截止日期 YYYY-MM-DD, 默认今天"),
    ext_columns: Optional[str] = Query(None, description="逗号分隔的 ext 列: config_id.field_name"),
):
    """读取本地 enriched 表中某只股票的日 K。

    - 若 QuoteService 有实时行情, 追加/覆盖今日实时蜡烛
    - Free 用户: 若 enriched 表里没有该股票, 实时拉取 + 本地算 enriched 返回
    - ext_columns: 可选，动态 LEFT JOIN 扩展数据表，结果平铺到 stock_info.ext 下
      (key 为 "{config_id}__{field_name}")，供日K信息条等场景展示自定义字段
    """
    import polars as pl

    repo = request.app.state.repo
    end = date.fromisoformat(end_date) if end_date else date.today()
    if start_date:
        start = date.fromisoformat(start_date)
    else:
        start = end - timedelta(days=days)
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end, datetime.min.time())

    stock_info = _get_stock_info(repo, symbol)
    stock_name = stock_info.get("name")

    from app.services.data_mode import is_local_daily_mode
    if is_local_daily_mode():
        from app.data_providers.registry import get_active_provider_name, get_provider

        provider = get_provider(get_active_provider_name("daily"))
        asset_type = _asset_type_for_symbol(symbol)
        raw = provider.get_daily([symbol], start_dt, end_dt, asset_type)
        if raw.is_empty():
            return {"symbol": symbol, "name": stock_name, "stock_info": stock_info, "rows": []}
        factors = pl.DataFrame()
        if asset_type == "stock":
            try:
                factors = provider.get_adj_factors([symbol], start_dt, end_dt, asset_type)
            except Exception as e:  # noqa: BLE001
                logger.debug("本地模式单股除权因子拉取失败 %s: %s", symbol, e)
        enriched = compute_enriched(raw, factors=factors)
        rows = _maybe_inject_live_candle(request, symbol, enriched.tail(days).to_dicts())
        resp = {
            "symbol": symbol,
            "name": stock_name,
            "stock_info": stock_info,
            "rows": rows,
            "source": "local_disk",
        }
        return _attach_ext(resp, repo, symbol, ext_columns)

    # 从 enriched 表读取 (已含前复权 OHLCV + 技术指标 + 信号)
    df = repo.get_daily(symbol, start, end)

    if df.is_empty():
        try:
            raw = kline_sync.sync_daily_batch([symbol], count=days + 30)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"数据源拉取失败: {e}") from e
        if raw.is_empty():
            return {"symbol": symbol, "name": stock_name, "stock_info": stock_info, "rows": []}
        # 拉除权因子做前复权；无能力时空 df → compute_enriched 退回未复权
        factors = pl.DataFrame()
        capset = getattr(request.app.state, "capabilities", None)
        try:
            from app.capabilities import Cap
            if capset and capset.has(Cap.ADJ_FACTOR):
                factors = kline_sync.fetch_adj_factor_single(symbol)
        except Exception as e:  # noqa: BLE001
            logger.debug("单股除权因子拉取失败 %s: %s", symbol, e)
        enriched = compute_enriched(raw, factors=factors)
        rows = enriched.tail(days).to_dicts()
        # 即使 live 模式也尝试追加实时蜡烛
        rows = _maybe_inject_live_candle(request, symbol, rows)
        resp = {"symbol": symbol, "name": stock_name, "stock_info": stock_info, "rows": rows, "source": "live"}
        return _attach_ext(resp, repo, symbol, ext_columns)

    rows = df.to_dicts()

    # 追加/覆盖今日实时蜡烛
    rows = _maybe_inject_live_candle(request, symbol, rows)

    resp = {"symbol": symbol, "name": stock_name, "stock_info": stock_info, "rows": rows, "source": "enriched"}
    return _attach_ext(resp, repo, symbol, ext_columns)


def _attach_ext(resp: dict, repo, symbol: str, ext_columns: Optional[str]) -> dict:
    """按 ext_columns 规格为单只股票 LEFT JOIN 扩展数据，平铺到 stock_info['ext']。

    key 形如 "{config_id}__{field_name}"，与自选列表 enriched 接口保持一致。
    JOIN 逻辑参考 watchlist.watchlist_enriched；任何 ext 表/字段缺失都静默跳过。
    """
    if not ext_columns or not ext_columns.strip():
        return resp

    specs: list[tuple[str, str]] = []
    for part in ext_columns.split(","):
        part = part.strip()
        if "." not in part:
            continue
        config_id, field_name = part.split(".", 1)
        config_id, field_name = config_id.strip(), field_name.strip()
        if config_id and field_name:
            specs.append((config_id, field_name))
    if not specs:
        return resp

    import polars as pl
    data_dir = repo.store.data_dir
    try:
        from app.services.ext_data import ExtConfigStore
        from app.api.ext_data import _read_ext_dataframe
        ext_store = ExtConfigStore(data_dir)
        configs = {c.id: c for c in ext_store.load_all()}
    except Exception:  # noqa: BLE001
        configs = {}

    ext_values: dict = {}
    for config_id, field_name in specs:
        ext_col_name = f"{config_id}__{field_name}"
        value = None
        try:
            cfg = configs.get(config_id)
            if cfg:
                ext_df, _ = _read_ext_dataframe(cfg, data_dir)
            else:
                ext_df = pl.from_arrow(
                    repo.store.db.query(
                        f'SELECT symbol, "{field_name}" FROM ext_{config_id}'
                    ).arrow()
                )
            if not ext_df.is_empty() and "symbol" in ext_df.columns and field_name in ext_df.columns:
                # 时序表取最新分区，避免一个 symbol 多行
                row = (
                    ext_df
                    .select(["symbol", field_name])
                    .unique(subset=["symbol"], keep="last")
                    .filter(pl.col("symbol") == symbol)
                )
                if not row.is_empty():
                    value = row[field_name][0]
        except Exception as e:  # noqa: BLE001
            logger.debug("kline ext join failed for %s.%s: %s", config_id, field_name, e)
        ext_values[ext_col_name] = value

    stock_info = dict(resp.get("stock_info") or {})
    stock_info["ext"] = ext_values
    resp["stock_info"] = stock_info
    return resp


def _maybe_inject_live_candle(request: Request, symbol: str, rows: list[dict]) -> list[dict]:
    """如果 QuoteService 有实时 enriched 数据, 用实时数据生成今日蜡烛并追加/覆盖。"""
    qs = getattr(request.app.state, "quote_service", None)
    if not qs:
        return rows

    df_today, enriched_date = qs.get_enriched_today()
    if df_today.is_empty():
        return rows

    # 非交易日（周末/假日）缓存的行情日期 != 今天，跳过注入避免产生重复蜡烛
    if not enriched_date or enriched_date != date.today():
        return rows

    # 查找该 symbol 的实时 enriched 行
    import polars as pl
    try:
        q = df_today.filter(pl.col("symbol") == symbol).to_dicts()
        if not q:
            return rows
        q = q[0]
    except Exception:  # noqa: BLE001
        return rows

    close_price = q.get("close")
    if not close_price or close_price <= 0:
        return rows

    today_str = str(enriched_date)

    # enriched 行已包含 OHLCV + 全套指标, 直接用它
    # 修复: API 在非交易时段可能返回 open/high/low=0, 用 close 填充避免异常蜡烛
    raw_open = q.get("open")
    raw_high = q.get("high")
    raw_low = q.get("low")
    live_row: dict = {
        "date": today_str,
        "symbol": symbol,
        "open": raw_open if raw_open and raw_open > 0 else close_price,
        "high": raw_high if raw_high and raw_high > 0 else close_price,
        "low": raw_low if raw_low and raw_low > 0 else close_price,
        "close": close_price,
        "volume": q.get("volume"),
        "amount": q.get("amount"),
        "change_pct": q.get("change_pct"),
        "is_live": True,
    }
    # 补上 enriched 的技术指标字段
    for key in ("ma5", "ma10", "ma20", "ma30", "ma60",
                "macd_dif", "macd_dea", "macd_hist",
                "kdj_k", "kdj_d", "kdj_j",
                "boll_upper", "boll_lower",
                "rsi_6", "rsi_14", "rsi_24",
                "atr_14", "vol_ratio_5d"):
        if key in q and q[key] is not None:
            live_row[key] = q[key]

    # 如果已有今天的 enriched 行, 覆盖; 否则追加
    found = False
    for i, r in enumerate(rows):
        if str(r.get("date")) == today_str:
            r.update(live_row)
            found = True
            break

    if not found:
        rows.append(live_row)

    return rows


class DailyBatchRequest:
    """批量日K请求。"""
    symbols: list[str]
    days: int = 12


@router.post("/daily-batch")
def get_daily_batch(request: Request, body: dict):
    """批量获取多只股票最近 N 天日K (OHLCV)。

    用于自选列表迷你蜡烛图等场景，只返回基础列，不返回全部 enriched 指标。
    """
    symbols = body.get("symbols", [])
    days = body.get("days", 12)
    if not symbols:
        return {"data": {}}
    days = max(5, min(60, days))

    repo = request.app.state.repo
    import polars as pl
    from datetime import date, timedelta

    end = date.today()
    start = end - timedelta(days=days * 2)  # 多取一些确保交易日够
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end, datetime.min.time())

    cols = ["symbol", "date", "open", "high", "low", "close", "volume"]
    from app.services.data_mode import is_local_daily_mode
    if is_local_daily_mode():
        from app.data_providers.registry import get_active_provider_name, get_provider

        provider = get_provider(get_active_provider_name("daily"))
        frames = []
        for asset_type in ("stock", "etf", "index", "hk"):
            group = [sym for sym in symbols if _asset_type_for_symbol(sym) == asset_type]
            if not group:
                continue
            part = provider.get_daily(group, start_dt, end_dt, asset_type)
            if not part.is_empty():
                frames.append(part)
        raw = pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()
        if raw.is_empty():
            return {"data": {}}
        df = raw.select([c for c in cols if c in raw.columns])
    else:
        df = repo.get_daily_batch(symbols, start, end, columns=cols)
        if df.is_empty():
            return {"data": {}}

    if df.is_empty():
        return {"data": {}}

    # 按 symbol 分组, 每只取最近 N 条
    result: dict[str, list[dict]] = {}
    for sym in symbols:
        sub = df.filter(pl.col("symbol") == sym).sort("date").tail(days)
        if not sub.is_empty():
            result[sym] = sub.to_dicts()

    return {"data": result}


@router.get("/minute")
def get_minute(
    request: Request,
    symbol: str = Query(..., description="标的代码"),
    trade_date: date | None = Query(None, alias="date", description="交易日期, 默认最新"),
):
    """读取某只股票某天的分钟 K 线。

    - 本地有完整数据(240条) → 直接返回
    - 本地无数据或不完整 → 从当前 provider 拉取返回（不写入）
    """
    repo = request.app.state.repo
    stock_info = _get_stock_info(repo, symbol)
    stock_name = stock_info.get("name")

    if trade_date is None:
        trade_date = repo.latest_minute_date(symbol)
    if trade_date is None:
        # 本地无任何分钟K，尝试从当前 provider 拉取当天
        trade_date = date.today()
        df = kline_sync.fetch_minute_single(symbol, trade_date)
        return {
            "symbol": symbol, "name": stock_name, "stock_info": stock_info,
            "date": str(trade_date), "rows": df.to_dicts(), "source": "live",
        }

    df = repo.get_minute(symbol, trade_date)

    # 完整交易日应有 240 条分钟K；如果是今天(盘中)，期望条数按已交易分钟估算
    expected = 240
    today = date.today()
    if trade_date == today:
        from datetime import datetime as _dt
        now = _dt.now()
        h, m = now.hour, now.minute
        if h < 9 or (h == 9 and m < 30):
            expected = 0  # 还没开盘
        elif h < 12 or (h == 12 and m == 0):
            expected = (h - 9) * 60 + m - 30  # 9:30 起
        elif h < 13:
            expected = 120  # 午休
        elif h < 15:
            expected = 120 + (h - 13) * 60 + m
        else:
            expected = 240

    is_complete = not df.is_empty() and len(df) >= expected * 0.9  # 允许 10% 容差

    if is_complete:
        return {
            "symbol": symbol, "name": stock_name, "stock_info": stock_info,
            "date": str(trade_date), "rows": df.to_dicts(), "source": "local",
        }

    # 本地不完整或无数据 → 从当前 provider 拉取
    live_df = kline_sync.fetch_minute_single(symbol, trade_date)
    return {
        "symbol": symbol, "name": stock_name, "stock_info": stock_info,
        "date": str(trade_date), "rows": live_df.to_dicts(),
        "source": "live" if not live_df.is_empty() else "none",
    }


@router.post("/sync")
def sync_symbol(
    request: Request,
    symbol: str = Query(...),
    days: int = Query(250, ge=10, le=2000),
):
    """手动触发单股同步(Free 用户在 K 线页用)。"""
    repo = request.app.state.repo
    capset = request.app.state.capabilities
    n = kline_sync.sync_and_persist_daily_batch([symbol], repo, capset, count=days)
    return {"symbol": symbol, "rows_written": n}


@router.post("/sync_batch")
def sync_batch(
    request: Request,
    symbols: list[str],
    days: int = Query(250, ge=10, le=2000),
):
    repo = request.app.state.repo
    capset = request.app.state.capabilities
    n = kline_sync.sync_and_persist_daily_batch(symbols, repo, capset, count=days)
    return {"symbols": symbols, "rows_written": n}


@router.post("/refresh_views")
def refresh_views(request: Request):
    """刷新所有 DuckDB 视图(解决视图状态不一致问题)。"""
    from app.jobs.daily_pipeline import _refresh_views
    repo = request.app.state.repo
    _refresh_views(repo)
    return {"status": "ok"}


@router.post("/sync_minute")
async def sync_minute(request: Request):
    """手动触发分钟 K 同步(全市场)。返回 pipeline job_id 可轮询进度。"""
    import asyncio

    from app.services.pipeline_jobs import job_store
    from app.api.data import invalidate_storage_cache
    from app.services.preferences import get_minute_sync_days
    from app.capabilities import Cap

    repo = request.app.state.repo
    capset = request.app.state.capabilities

    if not capset.has(Cap.KLINE_MINUTE_BATCH):
        raise HTTPException(status_code=403, detail="需要 Pro+ 权限")

    job_id = job_store.create()
    existing = job_store.get(job_id)
    if existing and existing["status"] == "running":
        return {"status": "reused", "job_id": job_id}

    async def task() -> None:
        job_store.start(job_id)
        loop = asyncio.get_event_loop()

        def progress(stage: str, pct: int, msg: str) -> None:
            job_store.progress(job_id, stage, pct, msg)

        try:
            progress("sync_minute", 5, "解析标的池…")
            universe: list[str] = []
            try:
                provider = kline_sync._get_data_provider()
                if provider.capabilities.instruments:
                    import polars as pl
                    inst = provider.get_instruments("stock")
                    if not inst.is_empty() and "symbol" in inst.columns:
                        universe = sorted(inst["symbol"].cast(pl.Utf8).to_list())
            except Exception:  # noqa: BLE001
                universe = []
            # 补充 instruments 全量标的，覆盖北交所、新股等
            inst_path = repo.store.data_dir / "instruments" / "instruments.parquet"
            if inst_path.exists():
                try:
                    import polars as pl
                    inst = pl.read_parquet(inst_path, columns=["symbol"])
                    universe = sorted(set(universe) | set(inst["symbol"].to_list()))
                except Exception:  # noqa: BLE001
                    pass
            progress("sync_minute", 10, f"标的池 {len(universe)} 只")

            days = get_minute_sync_days()

            def _run():
                return kline_sync.sync_and_persist_minute(universe, repo, capset, days=days)

            written = await loop.run_in_executor(_long_task_executor, _run)

            # 刷新视图
            from app.jobs.daily_pipeline import _refresh_single_view
            _refresh_single_view(repo, "kline_minute")

            progress("done", 100, f"分钟 K 同步完成,{written} 行")
            job_store.succeed(job_id, {"minute_rows": written, "universe_size": len(universe)})
            invalidate_storage_cache()
        except Exception as e:  # noqa: BLE001
            job_store.fail(job_id, str(e))
            invalidate_storage_cache()

    asyncio.create_task(task())
    return {"status": "started", "job_id": job_id}


@router.post("/extend_history")
async def extend_history(request: Request):
    """向前扩展历史日K数据 — 独立于盘后管道。

    body: { "value": int, "unit": "day"|"month"|"year" }
    返回 job_id,可轮询 /api/pipeline/jobs 查看进度。
    """
    import asyncio
    import traceback as _tb
    try:
        body = await request.json()
        value = body.get("value")
        unit = body.get("unit", "month")
        if not value or value <= 0:
            raise HTTPException(status_code=400, detail="value 必须为正整数")
        if unit not in ("day", "month", "year"):
            raise HTTPException(status_code=400, detail="unit 只支持 day/month/year")

        repo = request.app.state.repo
        capset = request.app.state.capabilities

        from app.capabilities import Cap
        if not capset.has(Cap.KLINE_DAILY_BATCH):
            raise HTTPException(status_code=403, detail="当前数据源不支持批量日K")

        from app.services.extend_history import run_extend_history
        from app.services.pipeline_jobs import job_store
        from app.api.data import invalidate_storage_cache

        job_id = job_store.create()
        existing = job_store.get(job_id)
        if existing and existing["status"] == "running":
            return {"status": "reused", "job_id": job_id}

        async def task() -> None:
            job_store.start(job_id)
            loop = asyncio.get_event_loop()

            def progress(stage: str, pct: int, msg: str,
                         stage_pct: int | None = None, skip_log: bool = False) -> None:
                job_store.progress(job_id, stage, pct, msg,
                                   stage_pct=stage_pct, skip_log=skip_log)

            try:
                result = await loop.run_in_executor(
                    _long_task_executor,
                    lambda: run_extend_history(repo, capset, value, unit, on_progress=progress),
                )
                if "error" in result:
                    job_store.fail(job_id, result["error"])
                else:
                    job_store.succeed(job_id, result)
                invalidate_storage_cache()
            except Exception as e:
                logger.exception("extend_history failed: job_id=%s", job_id)
                job_store.fail(job_id, str(e))
                invalidate_storage_cache()

        asyncio.create_task(task())
        return {"status": "started", "job_id": job_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("extend_history error: %s\n%s", e, _tb.format_exc())
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/rebuild_enriched")
async def rebuild_enriched(request: Request):
    """全量重算 enriched 表 — 不获取任何数据,仅基于已有 kline_daily + adj_factor 重算复权+指标。

    返回 job_id,可轮询 /api/pipeline/jobs 查看进度。
    """
    import asyncio
    try:
        repo = request.app.state.repo

        from app.services.pipeline_jobs import job_store
        from app.api.data import invalidate_storage_cache

        job_id = job_store.create()
        existing = job_store.get(job_id)
        if existing and existing["status"] == "running":
            return {"status": "reused", "job_id": job_id}

        async def task() -> None:
            job_store.start(job_id)
            loop = asyncio.get_event_loop()

            def progress(stage: str, pct: int, msg: str,
                         stage_pct: int | None = None, skip_log: bool = False) -> None:
                job_store.progress(job_id, stage, pct, msg,
                                   stage_pct=stage_pct, skip_log=skip_log)

            try:
                progress("rebuild_enriched", 10, "全量计算 enriched…")
                from app.indicators.pipeline import run_pipeline

                def _batch_progress(cur: int, tot: int) -> None:
                    pct = 10 + int(85 * cur / tot)
                    progress("rebuild_enriched", pct,
                             f"计算指标 批次 {cur}/{tot}",
                             stage_pct=int(100 * cur / tot), skip_log=True)

                written = await loop.run_in_executor(
                    _long_task_executor,
                    lambda: run_pipeline(on_batch_done=_batch_progress),
                )

                enriched_dir = repo.store.data_dir / "kline_daily_enriched"
                enriched_days = len(list(enriched_dir.glob("date=*"))) if enriched_dir.exists() else 0

                # 刷新视图
                d = repo.store.data_dir.as_posix()
                for view_name, glob in [
                    ("kline_enriched", f"{d}/kline_daily_enriched/**/*.parquet"),
                ]:
                    try:
                        repo.db.execute(
                            f"CREATE OR REPLACE VIEW {view_name} AS "
                            f"SELECT * FROM read_parquet('{glob}', union_by_name=true)"
                        )
                    except Exception:
                        pass

                progress("rebuild_enriched", 100, f"完成,覆盖 {enriched_days} 天")
                job_store.succeed(job_id, {
                    "enriched_days": enriched_days,
                    "enriched_rows": written,
                })
                invalidate_storage_cache()
            except Exception as e:
                logger.exception("rebuild_enriched failed: job_id=%s", job_id)
                job_store.fail(job_id, str(e))
                invalidate_storage_cache()

        asyncio.create_task(task())
        return {"status": "started", "job_id": job_id}
    except Exception as e:
        import traceback as _tb
        logger.error("rebuild_enriched error: %s\n%s", e, _tb.format_exc())
        raise HTTPException(status_code=500, detail=str(e)) from e


# 长时间任务专用线程池（隔离于 FastAPI 默认线程池，防止阻塞请求处理）
import concurrent.futures as _cf
_long_task_executor = _cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="long-task")


@router.post("/extend_minute_history")
async def extend_minute_history(request: Request):
    """向前扩展分钟K历史数据 — 仅拉数据,不做任何后续处理。

    body: { "value": int, "unit": "day"|"month" }
    - day 单位:1~15 天(所有有分钟K能力的数据源可用)
    - month 单位:1~6 月(每月按 30 天计,即最多 180 天)—— 需数据源声明支持
    返回 job_id,可轮询 /api/pipeline/jobs 查看进度。
    """
    import asyncio
    import traceback as _tb
    try:
        body = await request.json()
        value = body.get("value")
        unit = body.get("unit", "day")
        if not value or value <= 0:
            raise HTTPException(status_code=400, detail="value 必须为正整数")
        if unit not in ("day", "month"):
            raise HTTPException(status_code=400, detail="unit 只支持 day/month")

        repo = request.app.state.repo
        capset = request.app.state.capabilities

        from app.capabilities import Cap
        if not capset.has(Cap.KLINE_MINUTE_BATCH):
            raise HTTPException(status_code=403, detail="当前数据源不支持批量分钟K")

        if unit == "month":
            if not capset.has(Cap.KLINE_MINUTE_MONTH):
                raise HTTPException(
                    status_code=403,
                    detail="当前数据源不支持按月扩展分钟K历史",
                )

        # 计算天数上限:day 最多 15 天;month 最多 6 月(180 天)
        from datetime import timedelta
        if unit == "month":
            total_days = min(value * 30, 180)
        else:
            total_days = min(value, 15)

        if total_days <= 0:
            raise HTTPException(status_code=400, detail="扩展范围无效")

        from app.services.pipeline_jobs import job_store
        from app.api.data import invalidate_storage_cache

        job_id = job_store.create()
        existing = job_store.get(job_id)
        if existing and existing["status"] == "running":
            return {"status": "reused", "job_id": job_id}

        async def task() -> None:
            job_store.start(job_id)
            loop = asyncio.get_event_loop()

            def progress(stage: str, pct: int, msg: str,
                         stage_pct: int | None = None, skip_log: bool = False) -> None:
                job_store.progress(job_id, stage, pct, msg,
                                   stage_pct=stage_pct, skip_log=skip_log)

            try:
                # 获取当前最早日期
                earliest = repo.earliest_minute_date()
                if not earliest:
                    # 本地无分钟K数据 → 以今天为基准往前获取
                    from datetime import date as _date
                    latest = _date.today()
                else:
                    latest = earliest

                new_start = latest - timedelta(days=total_days)
                if new_start >= latest:
                    job_store.fail(job_id, "扩展范围无效")
                    invalidate_storage_cache()
                    return

                start_str = new_start.strftime("%Y-%m-%d")
                end_str = latest.strftime("%Y-%m-%d")

                progress("extend_minute", 5, "解析标的池…")
                universe = _resolve_minute_universe(capset, repo)
                progress("extend_minute", 8, f"标的池: {len(universe)} 只")

                from app.capabilities import Cap

                lim = capset.limits(Cap.KLINE_MINUTE_BATCH)
                batch_size = lim.batch if lim and lim.batch else 100
                rpm = lim.rpm if lim else 30

                def _run():
                    """全部在 executor 线程里完成,避免阻塞事件循环。"""
                    from app.services.kline_sync import sync_minute_batch
                    from datetime import datetime as _dt

                    def _chunk(cur: int, tot: int) -> None:
                        progress("extend_minute", 8 + int(85 * cur / tot),
                                 f"分钟K 批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)

                    df = sync_minute_batch(
                        universe,
                        start_time=_dt.combine(new_start, _dt.min.time()),
                        end_time=_dt.combine(latest, _dt.min.time()),
                        batch_size=batch_size, rpm=rpm,
                        on_chunk_done=_chunk,
                    )

                    written = 0
                    day_count = 0
                    if not df.is_empty():
                        import polars as pl
                        df = df.with_columns(pl.col("datetime").dt.date().alias("_trade_date"))
                        for day_df in df.partition_by("_trade_date"):
                            trade_date = day_df["_trade_date"][0]
                            out = repo.store.data_dir / "kline_minute" / f"date={trade_date}" / "part.parquet"
                            out.parent.mkdir(parents=True, exist_ok=True)
                            if out.exists():
                                existing_df = pl.read_parquet(out)
                                if "datetime" in existing_df.columns:
                                    existing_df = existing_df.filter(pl.col("datetime").is_not_null())
                                day_df = pl.concat([existing_df, day_df.drop("_trade_date")]).unique(
                                    subset=["symbol", "datetime"], keep="last",
                                )
                            else:
                                day_df = day_df.drop("_trade_date")
                            day_df = day_df.sort("symbol", "datetime")
                            day_df.write_parquet(out)
                            written += day_df.height
                            day_count += 1

                        # 刷新视图
                        d = repo.store.data_dir.as_posix()
                        try:
                            repo.db.execute(
                                f"CREATE OR REPLACE VIEW kline_minute AS "
                                f"SELECT * FROM read_parquet('{d}/kline_minute/**/*.parquet', union_by_name=true)"
                            )
                        except Exception:
                            pass
                    return written, day_count

                progress("extend_minute", 10, f"获取分钟K [{start_str} ~ {end_str}]…")
                written, day_count = await loop.run_in_executor(_long_task_executor, _run)

                progress("extend_minute", 95, f"分钟K 完成,{day_count} 天")
                job_store.succeed(job_id, {
                    "minute_days": day_count,
                    "universe_size": len(universe),
                    "earliest_before": (earliest or latest).isoformat(),
                    "earliest_after": new_start.isoformat(),
                })
                invalidate_storage_cache()
            except Exception as e:
                logger.exception("extend_minute_history failed: job_id=%s", job_id)
                job_store.fail(job_id, str(e))
                invalidate_storage_cache()

        asyncio.create_task(task())
        return {"status": "started", "job_id": job_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("extend_minute_history error: %s\n%s", e, _tb.format_exc())
        raise HTTPException(status_code=500, detail=str(e)) from e


def _resolve_minute_universe(capset, repo) -> list[str]:
    """分钟K标的池解析。"""
    from app.capabilities import Cap
    if capset.has(Cap.KLINE_MINUTE_BATCH):
        try:
            provider = kline_sync._get_data_provider()
            if provider.capabilities.instruments:
                import polars as pl
                inst = provider.get_instruments("stock")
                if not inst.is_empty() and "symbol" in inst.columns:
                    return sorted(inst["symbol"].cast(pl.Utf8).to_list())
        except Exception:
            pass
    return []
