"""K 线 / 同步 API。"""
from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from app.indicators.pipeline import compute_enriched, compute_enriched_single
from app.market_time import cn_now, cn_today
from app.price_limits import is_risk_warning_name, price_limit_pct
from app.services import kline_sync

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/kline", tags=["kline"])


def _minute_allowed(capset) -> bool:
    """是否有分钟K权限 (TickFlow Pro+ 或 custom minute 源)。"""
    from app.tickflow.capabilities import Cap
    if capset.has(Cap.KLINE_MINUTE_BATCH):
        return True
    from app.services import preferences
    provider = preferences.get_minute_data_provider()
    _, fallback, error = kline_sync._resolve_minute_provider(provider)
    if error is not None:
        logger.warning("minute provider resolution failed while checking access: %s", error)
    return not fallback


@router.get("/instruments/search")
def search_instruments(
    request: Request,
    q: str = Query("", min_length=0, max_length=50, description="搜索关键词"),
    limit: int = Query(20, ge=1, le=50),
    asset_types: str = Query("stock", description="逗号分隔的资产类型: stock,etf"),
):
    """模糊搜索标的 (代码 / 名称)。从内存 instruments 缓存中查。

    默认只搜股票, 保持既有调用方行为不变; 自选等场景传 asset_types=stock,etf
    可一并搜出 ETF, 结果附带 asset_type 字段供前端区分。
    """
    if not q.strip():
        return {"results": []}

    repo = request.app.state.repo
    import polars as pl

    types = [t.strip() for t in asset_types.split(",") if t.strip()]
    parts: list[pl.DataFrame] = []
    for t in types:
        df_t = repo.get_instruments_asset(t)
        if df_t.is_empty() or "symbol" not in df_t.columns:
            continue
        # dtype 全部归一到 Utf8: 股票/ETF 两份缓存来源不同 (ETF 含 legacy 合并), 防 concat SchemaError
        parts.append(df_t.with_columns([
            pl.col("symbol").cast(pl.Utf8).alias("symbol"),
            (pl.col("name").cast(pl.Utf8) if "name" in df_t.columns else pl.lit("")).alias("name"),
            (pl.col("code").cast(pl.Utf8) if "code" in df_t.columns else pl.lit("")).alias("code"),
            pl.lit(t).alias("asset_type"),
        ]).select(["symbol", "name", "code", "asset_type"]))
    if not parts:
        return {"results": []}
    df = pl.concat(parts, how="vertical")

    keyword = q.strip().upper()

    # code/symbol 前缀优先，再 name 包含匹配
    prefix_mask = (
        pl.col("code").str.starts_with(keyword)
        | pl.col("symbol").str.to_uppercase().str.starts_with(keyword)
    )
    contains_mask = (
        pl.col("code").str.contains(keyword, literal=True)
        | pl.col("symbol").str.to_uppercase().str.contains(keyword, literal=True)
        | pl.col("name").str.contains(keyword, literal=True)
    )

    # 前缀匹配优先，剩余名额用包含匹配补充
    prefix_hits = df.filter(prefix_mask).head(limit)
    if prefix_hits.height >= limit:
        matched = prefix_hits
    else:
        remaining = limit - prefix_hits.height
        # 排除已匹配的 symbol
        prefix_symbols = set(prefix_hits["symbol"].to_list()) if not prefix_hits.is_empty() else set()
        contain_hits = df.filter(contains_mask & ~pl.col("symbol").is_in(prefix_symbols)).head(remaining)
        matched = pl.concat([prefix_hits, contain_hits]) if not prefix_hits.is_empty() else contain_hits
    rows = matched.select(["symbol", "name", "code", "asset_type"]).to_dicts()
    return {"results": rows}


@router.post("/instruments/names")
def instruments_names(request: Request, symbols: list[str]):
    """批量查标的名称 (股票 + ETF)。传入 symbol 列表, 返回 {symbol: name}。"""
    if not symbols:
        return {"names": {}}
    repo = request.app.state.repo
    return {"names": repo.get_name_map(symbols)}


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


def _get_asset_info(repo, symbol: str, asset_type: str) -> dict:
    """非股票标的 (ETF / 指数) 的名称信息 — 从对应 instruments 缓存查, 无股本概念。"""
    import polars as pl
    try:
        df = repo.get_instruments_asset(asset_type)
        if df.is_empty() or "symbol" not in df.columns or "name" not in df.columns:
            return {}
        hit = df.filter(pl.col("symbol") == symbol).head(1)
        if hit.is_empty():
            return {}
        return {"name": hit["name"][0]}
    except Exception:
        return {}


def _get_price_limit_info(
    repo,
    symbol: str,
    trade_date: date,
    asset_type: str,
    instrument_name: str | None,
) -> dict | None:
    """Return the date-aware limit rule and today's authoritative prices."""
    if asset_type == "index":
        return None

    info = {
        "rate": price_limit_pct(
            symbol,
            trade_date,
            is_risk_warning=(
                asset_type == "stock" and is_risk_warning_name(instrument_name)
            ),
        ),
        "limit_up": None,
        "limit_down": None,
        "source": "rule",
    }
    if trade_date != cn_today():
        return info

    try:
        import polars as pl

        instruments = repo.get_instruments_asset(asset_type)
        available = [
            column
            for column in ("symbol", "limit_up", "limit_down")
            if column in instruments.columns
        ]
        if "symbol" not in available or len(available) == 1:
            return info
        hit = instruments.filter(pl.col("symbol") == symbol).select(available).head(1)
        row = hit.to_dicts()[0] if not hit.is_empty() else None
    except Exception:
        return info
    if row is None:
        return info

    has_authoritative_price = False
    for field in ("limit_up", "limit_down"):
        value = row.get(field)
        if value is None:
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(numeric) and 0 < numeric < 10_000:
            info[field] = numeric
            has_authoritative_price = True
    if has_authoritative_price:
        info["source"] = "instrument"
    return info


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

    asset_type = repo.resolve_asset_type(symbol)
    stock_info = _get_stock_info(repo, symbol) if asset_type == "stock" else _get_asset_info(repo, symbol, asset_type)
    stock_name = stock_info.get("name")

    # 从 enriched 表读取 (已含前复权 OHLCV + 技术指标 + 信号); ETF/指数走独立存储
    df = repo.get_daily_asset(asset_type, symbol, start, end)

    if df.is_empty():
        try:
            raw = kline_sync.sync_daily_batch([symbol], count=days + 30)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"TickFlow fetch failed: {e}") from e
        if raw.is_empty():
            return {"symbol": symbol, "name": stock_name, "stock_info": stock_info, "rows": []}
        # 拉除权因子做前复权 (Starter+ 有权限), 否则空 df → compute_enriched 退回未复权
        factors = pl.DataFrame()
        capset = getattr(request.app.state, "capabilities", None)
        try:
            from app.tickflow.capabilities import Cap
            if capset and capset.has(Cap.ADJ_FACTOR):
                factors = kline_sync.fetch_adj_factor_single(symbol)
        except Exception as e:  # noqa: BLE001
            logger.debug("单股除权因子拉取失败 %s: %s", symbol, e)
        enriched = compute_enriched(raw, factors=factors)
        rows = enriched.tail(days).to_dicts()
        # 即使 live 模式也尝试追加实时蜡烛
        rows = _maybe_inject_live_candle(request, symbol, rows, asset_type)
        resp = {"symbol": symbol, "name": stock_name, "stock_info": stock_info, "rows": rows, "source": "live"}
        return _attach_ext(resp, repo, symbol, ext_columns)

    rows = df.to_dicts()

    # 追加/覆盖今日实时蜡烛
    rows = _maybe_inject_live_candle(request, symbol, rows, asset_type)

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


def _maybe_inject_live_candle(request: Request, symbol: str, rows: list[dict], asset_type: str = "stock") -> list[dict]:
    """如果有当日实时 enriched 数据, 用实时数据生成今日蜡烛并追加/覆盖。

    stock 走 QuoteService 的股票实时缓存; etf 走 ETF enriched 缓存 (开启实时 ETF
    拉取时为盘中数据, 否则为磁盘最新日, 由下方"非今日不注入"守卫自然跳过)。
    """
    if asset_type == "stock":
        qs = getattr(request.app.state, "quote_service", None)
        if not qs:
            return rows
        df_today, enriched_date = qs.get_enriched_today()
    elif asset_type == "etf":
        df_today, enriched_date = request.app.state.repo.get_enriched_latest_asset("etf")
    else:
        return rows
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

    cols = ["symbol", "date", "open", "high", "low", "close", "volume"]
    df = repo.get_daily_batch(symbols, start, end, columns=cols)

    if df.is_empty():
        return {"data": {}}

    # 按 symbol 分组, 每只取最近 N 条
    result: dict[str, list[dict]] = {}
    for sym in symbols:
        sub = df.filter(pl.col("symbol") == sym).sort("date").tail(days)
        if not sub.is_empty():
            result[sym] = sub.to_dicts()

    return {"data": result}


@router.post("/minute-batch")
def get_minute_batch(request: Request, body: dict):
    """批量获取多只股票某天的分钟K (分时图用)。

    - 本地优先: 先从 kline_minute parquet 读, 完整的直接用
    - 缺失补拉: 本地不完整的 symbol 用 sync_minute_batch 批量实时拉 (不落库)
    - 需 Pro+ 权限 (kline.minute.batch)
    """
    from datetime import datetime
    import polars as pl
    from app.tickflow.capabilities import Cap

    symbols: list[str] = body.get("symbols", [])
    trade_date_str: str | None = body.get("date")
    if not symbols:
        return {"data": {}}

    repo = request.app.state.repo
    capset = request.app.state.capabilities

    # 权限守卫: 分钟K批量是 Pro+ 能力
    if not capset.has(Cap.KLINE_MINUTE_BATCH):
        raise HTTPException(status_code=403, detail="需要 Pro+ 权限 (kline.minute.batch)")

    trade_date = date.fromisoformat(trade_date_str) if trade_date_str else cn_today()

    # 非交易日(周末/节假日)才回退到最近有数据的交易日; 否则盘中会显示昨天而非今天。
    # 注意: 不能用 latest_minute_date_global() 判断盘中是否为交易日 —— 批量实时补拉
    # 不落库 (见下方 sync_minute_batch 无 on_segment), 盘中它恒返回上次全量同步日,
    # 用它做判据会导致 trade_date 永久回退到昨天, 再因 expected=240 判定昨日"完整"
    # 而不再补拉今天, 形成永远显示昨日的死循环。
    # 判据改为: 周末必回退; 工作日收盘后(>=15:30)仍无今日日K → 节假日, 回退。
    if not trade_date_str:
        today = cn_today()
        need_fallback = today.weekday() >= 5  # 周六/周日必非交易日
        if not need_fallback:
            now_cn = cn_now()
            after_close = now_cn.hour > 15 or (now_cn.hour == 15 and now_cn.minute >= 30)
            if after_close:
                latest_daily = repo.latest_daily_date()
                if latest_daily is None or latest_daily < today:
                    need_fallback = True
        if need_fallback:
            recent_date = repo.latest_minute_date_global()
            if recent_date is None:
                recent_date = repo.latest_daily_date()
            if recent_date is not None:
                trade_date = recent_date

    # Step 1: 本地优先 — 一次 scan 读全部 symbol 当日分钟K (股票 / ETF 分钟数据分开存储)
    etf_set = repo.get_etf_symbol_set()
    stock_syms = [s for s in symbols if s not in etf_set]
    etf_syms = [s for s in symbols if s in etf_set]
    df_local = repo.get_minute_batch(stock_syms, trade_date)
    if etf_syms:
        df_etf = repo.get_minute_batch(etf_syms, trade_date, asset_type="etf")
        if df_local.is_empty():
            df_local = df_etf
        elif not df_etf.is_empty():
            df_local = pl.concat([df_local, df_etf], how="diagonal_relaxed")

    # 期望条数 (盘中按当前时刻估算, 盘后 240)
    now = cn_now()
    h, m = now.hour, now.minute
    if trade_date != cn_today():
        expected = 240
    elif h < 9 or (h == 9 and m < 30):
        expected = 0
    elif h < 12 or (h == 12 and m == 0):
        expected = (h - 9) * 60 + m - 30
    elif h < 13:
        expected = 120
    elif h < 15:
        expected = 120 + (h - 13) * 60 + m
    else:
        expected = 240

    # 按 symbol 分组, 判定哪些不完整需要补拉
    result: dict[str, list[dict]] = {}
    incomplete: list[str] = []
    for sym in symbols:
        if df_local.is_empty():
            sub = pl.DataFrame()
        else:
            sub = df_local.filter(pl.col("symbol") == sym).sort("datetime")
        if expected > 0 and (sub.is_empty() or len(sub) < expected * 0.9):
            incomplete.append(sym)
        elif not sub.is_empty():
            result[sym] = sub.to_dicts()

    # Step 2: 缺失的 symbol 批量实时拉取 (不落库)
    if incomplete:
        start_time = datetime(trade_date.year, trade_date.month, trade_date.day, 9, 25, 0)
        end_time = datetime(trade_date.year, trade_date.month, trade_date.day, 15, 5, 0)
        lim = capset.limits(Cap.KLINE_MINUTE_BATCH)
        # etf_set 已在上方获取, 直接复用 — 按 asset_type 拆分调用 sync_minute_batch
        # (自定义源 / TickFlow 路由均依赖 asset_type 正确传递)
        # 契约: 本端点只接受 stock/ETF (指数分钟K走 /api/index/minute 独立路径),
        # 故两分支已覆盖全部 incomplete。若未来放开指数支持, 需额外加 index 分支
        # 以避免被误路由为 stock。
        stock_incomplete = [s for s in incomplete if s not in etf_set]
        etf_incomplete = [s for s in incomplete if s in etf_set]
        live_parts: list[pl.DataFrame] = []
        if stock_incomplete:
            df_s = kline_sync.sync_minute_batch(
                stock_incomplete,
                start_time=start_time,
                end_time=end_time,
                batch_size=lim.batch if lim else None,
                rpm=lim.rpm if lim else None,
                asset_type="stock",
            )
            if not df_s.is_empty():
                live_parts.append(df_s)
        if etf_incomplete:
            df_e = kline_sync.sync_minute_batch(
                etf_incomplete,
                start_time=start_time,
                end_time=end_time,
                batch_size=lim.batch if lim else None,
                rpm=lim.rpm if lim else None,
                asset_type="etf",
            )
            if not df_e.is_empty():
                live_parts.append(df_e)
        if live_parts:
            live_df = pl.concat(live_parts, how="diagonal_relaxed")
            for sym in incomplete:
                sub = live_df.filter(pl.col("symbol") == sym).sort("datetime")
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
    - 本地无数据或不完整 → 从 TickFlow 实时拉取返回（不写入）
    """
    repo = request.app.state.repo
    asset_type = repo.resolve_asset_type(symbol)
    stock_info = _get_stock_info(repo, symbol) if asset_type == "stock" else _get_asset_info(repo, symbol, asset_type)
    stock_name = stock_info.get("name")

    if trade_date is None:
        # 默认看今天, 而不是本地落盘的最近日 (盘中后者是昨天)。
        # 非交易日(周末/节假日)才回退到本地最近有数据的交易日。
        today = cn_today()
        need_fallback = today.weekday() >= 5  # 周六/周日必非交易日
        if not need_fallback:
            now_cn = cn_now()
            after_close = now_cn.hour > 15 or (now_cn.hour == 15 and now_cn.minute >= 30)
            if after_close:
                latest_daily = repo.latest_daily_date()
                if latest_daily is None or latest_daily < today:
                    need_fallback = True
        if need_fallback:
            recent = repo.latest_minute_date(symbol, asset_type=asset_type)
            if recent is None:
                recent = repo.latest_daily_date()
            trade_date = recent if recent is not None else today
        else:
            trade_date = today
    if trade_date is None:
        # 本地无任何分钟K，尝试从 TickFlow 拉取当天
        trade_date = cn_today()
        df = kline_sync.fetch_minute_single(symbol, trade_date, asset_type=asset_type)
        price_limit = _get_price_limit_info(
            repo, symbol, trade_date, asset_type, stock_name,
        )
        return {
            "symbol": symbol, "name": stock_name, "stock_info": stock_info,
            "date": str(trade_date), "rows": df.to_dicts(), "source": "live",
            "price_limit": price_limit,
        }

    price_limit = _get_price_limit_info(
        repo, symbol, trade_date, asset_type, stock_name,
    )
    df = repo.get_minute(symbol, trade_date, asset_type=asset_type)

    # 完整交易日应有 240 条分钟K；如果是今天(盘中)，期望条数按已交易分钟估算
    expected = 240
    today = cn_today()
    if trade_date == today:
        now = cn_now()
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
            "price_limit": price_limit,
        }

    # 本地不完整或无数据 → 从 TickFlow 实时拉取
    live_df = kline_sync.fetch_minute_single(symbol, trade_date, asset_type=asset_type)
    return {
        "symbol": symbol, "name": stock_name, "stock_info": stock_info,
        "date": str(trade_date), "rows": live_df.to_dicts(),
        "source": "live" if not live_df.is_empty() else "none",
        "price_limit": price_limit,
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
    """手动触发分钟 K 同步(全市场)。返回 pipeline job_id 可轮询进度。

    body 可选: { "days": int } — 指定拉取天数 (不传则用偏好设置)。
    """
    import asyncio

    from app.services.pipeline_jobs import job_store, release_run_slot, try_acquire_run_slot, LONG_JOB_TIMEOUT_S
    from app.api.data import invalidate_storage_cache
    from app.services.preferences import get_minute_sync_days
    from app.tickflow.capabilities import Cap
    from app.tickflow.pools import get_pool

    repo = request.app.state.repo
    capset = request.app.state.capabilities

    if not _minute_allowed(capset):
        raise HTTPException(status_code=403, detail="需要 Pro+ 权限")

    # 可选 body: { "days": int, "extend": bool }
    # days: 拉取天数; extend: 向前扩展模式 (从最早数据往前补)
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass
    override_days = body.get("days")
    extend_flag = body.get("extend")

    # 分钟K全市场同步是长任务(数据量是日K的 ~240 倍),用更宽松的卡死阈值
    job_id, is_new = job_store.create(timeout_s=LONG_JOB_TIMEOUT_S)
    if not is_new:
        return {"status": "reused", "job_id": job_id}

    async def task() -> None:
        if not try_acquire_run_slot():
            job_store.fail(job_id, "已有数据任务在运行(或上一次任务卡死未结束),请稍后再试")
            return
        loop = asyncio.get_event_loop()

        def progress(stage: str, pct: int, msg: str) -> None:
            job_store.progress(job_id, stage, pct, msg)

        try:
            job_store.start(job_id)
            progress("sync_minute", 5, "解析标的池…")
            universe = sorted(set(get_pool("watchlist")) | set(get_pool("CN_Equity_A")))
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

            days = override_days if override_days else get_minute_sync_days()
            # extend=1 → 向前扩展; days>=365 也自动向前扩展
            extend_backward = bool(extend_flag) or days >= 365

            def _on_chunk(done: int, total: int, seg_label: str) -> None:
                # 进度映射: 10% (标的池解析完) → 95%, 留 5% 给写入+刷新
                pct = 10 + int((done / max(total, 1)) * 85)
                progress("sync_minute", pct, f"拉取分钟K… {done}/{total} 批 [{seg_label}]")

            def _run():
                return kline_sync.sync_and_persist_minute(
                    universe, repo, capset, days=days,
                    extend_backward=extend_backward,
                    on_chunk_done=_on_chunk,
                )

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
        finally:
            release_run_slot()

    asyncio.create_task(task())
    return {"status": "started", "job_id": job_id}


@router.post("/sync_minute_single")
async def sync_minute_single(request: Request, body: dict):
    """手动拉取单只股票的分钟K并落库 (前复权)。

    body: { "symbol": "000001.SZ" }
    用于个股分时图"获取数据"按钮: 本地无数据时单独拉取并持久化。
    """
    from app.services.preferences import get_minute_sync_days
    from app.tickflow.capabilities import Cap

    symbol = body.get("symbol", "").strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol 不能为空")

    repo = request.app.state.repo
    capset = request.app.state.capabilities

    if not _minute_allowed(capset):
        raise HTTPException(status_code=403, detail="需要 Pro+ 权限")

    days = get_minute_sync_days()
    loop = asyncio.get_event_loop()

    def _run():
        return kline_sync.sync_and_persist_minute([symbol], repo, capset, days=days)

    written = await loop.run_in_executor(_long_task_executor, _run)

    # 刷新视图
    from app.jobs.daily_pipeline import _refresh_single_view
    _refresh_single_view(repo, "kline_minute")

    return {"status": "ok", "symbol": symbol, "rows": written}


@router.post("/clear_minute")
async def clear_minute(request: Request):
    """清空全部分钟K数据 (仅 kline_minute, 不影响其他数据)。

    删除 data/kline_minute/ 下所有分区 parquet, 刷新视图。
    需二次确认: body { "confirm": true }。
    """
    import shutil

    body = await request.json() if request.method == "POST" else {}
    if not body.get("confirm"):
        raise HTTPException(status_code=400, detail="需传 confirm: true 以确认清空")

    repo = request.app.state.repo
    minute_dir = repo.store.data_dir / "kline_minute"

    # 统计待删除行数 (用于返回)
    removed = 0
    if minute_dir.exists():
        try:
            result = repo.db.execute("SELECT COUNT(*) AS cnt FROM kline_minute").fetchone()
            removed = result[0] if result else 0
        except Exception:  # noqa: BLE001
            pass
        # 仅删 kline_minute 目录, 绝不触碰其他目录
        shutil.rmtree(minute_dir, ignore_errors=True)

    # 刷新视图 (重建空视图)
    from app.jobs.daily_pipeline import _refresh_single_view
    _refresh_single_view(repo, "kline_minute")

    from app.api.data import invalidate_storage_cache
    invalidate_storage_cache()

    logger.info("minute K cleared: %d rows removed", removed)
    return {"status": "ok", "removed": removed}


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

        from app.tickflow.capabilities import Cap
        if not capset.has(Cap.KLINE_DAILY_BATCH):
            raise HTTPException(status_code=403, detail="需要 Pro+ 权限 (batch K-line)")

        from app.services.extend_history import run_extend_history
        from app.services.pipeline_jobs import job_store, release_run_slot, try_acquire_run_slot
        from app.api.data import invalidate_storage_cache

        job_id, is_new = job_store.create()
        if not is_new:
            return {"status": "reused", "job_id": job_id}

        async def task() -> None:
            if not try_acquire_run_slot():
                job_store.fail(job_id, "已有数据任务在运行(或上一次任务卡死未结束),请稍后再试")
                return
            loop = asyncio.get_event_loop()

            def progress(stage: str, pct: int, msg: str,
                         stage_pct: int | None = None, skip_log: bool = False) -> None:
                job_store.progress(job_id, stage, pct, msg,
                                   stage_pct=stage_pct, skip_log=skip_log)

            try:
                job_store.start(job_id)
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
            finally:
                release_run_slot()

        asyncio.create_task(task())
        return {"status": "started", "job_id": job_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("extend_history error: %s\n%s", e, _tb.format_exc())
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/repair_daily")
async def repair_daily(request: Request):
    """修正 / 补全日K数据 — 从指定起始日期重拉到今天。

    典型场景: 昨天没看盘 / 服务挂了,本地日K缺了若干天。
    用户选起始日期,复用盘后管道全流程重拉 [start_date ~ 今天]。

    body: { "start_date": "YYYY-MM-DD" }
    返回 job_id,可轮询 /api/pipeline/jobs 查看进度。
    """
    import asyncio
    import traceback as _tb
    from datetime import date as _date
    try:
        body = await request.json()
        raw = body.get("start_date")
        if not raw:
            raise HTTPException(status_code=400, detail="start_date 必填 (YYYY-MM-DD)")
        try:
            start_date = _date.fromisoformat(str(raw))
        except ValueError:
            raise HTTPException(status_code=400, detail="start_date 格式错误 (应为 YYYY-MM-DD)")

        if start_date > _date.today():
            raise HTTPException(status_code=400, detail="起始日期不能晚于今天")

        repo = request.app.state.repo
        capset = request.app.state.capabilities

        from app.tickflow.capabilities import Cap
        if not capset.has(Cap.KLINE_DAILY_BATCH):
            raise HTTPException(status_code=403, detail="需要 Pro+ 权限 (batch K-line)")

        from app.services.repair_daily import run_repair_daily
        from app.services.pipeline_jobs import job_store, release_run_slot, try_acquire_run_slot
        from app.api.data import invalidate_storage_cache

        job_id, is_new = job_store.create()
        if not is_new:
            return {"status": "reused", "job_id": job_id}

        async def task() -> None:
            if not try_acquire_run_slot():
                job_store.fail(job_id, "已有数据任务在运行(或上一次任务卡死未结束),请稍后再试")
                return
            loop = asyncio.get_event_loop()
            qs = getattr(request.app.state, "quote_service", None)

            def progress(stage: str, pct: int, msg: str,
                         stage_pct: int | None = None, skip_log: bool = False) -> None:
                job_store.progress(job_id, stage, pct, msg,
                                   stage_pct=stage_pct, skip_log=skip_log)

            def _run() -> dict:
                # 修正运行期间暂停实时行情, 防止覆写同一批 parquet 竞态
                if qs:
                    with qs.paused():
                        return run_repair_daily(repo, capset, start_date, on_progress=progress)
                return run_repair_daily(repo, capset, start_date, on_progress=progress)

            try:
                job_store.start(job_id)
                result = await loop.run_in_executor(_long_task_executor, _run)
                if "error" in result:
                    job_store.fail(job_id, result["error"])
                else:
                    job_store.succeed(job_id, result)
                invalidate_storage_cache()
            except Exception as e:
                logger.exception("repair_daily failed: job_id=%s", job_id)
                job_store.fail(job_id, str(e))
                invalidate_storage_cache()
            finally:
                release_run_slot()

        asyncio.create_task(task())
        return {"status": "started", "job_id": job_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("repair_daily error: %s\n%s", e, _tb.format_exc())
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/rebuild_enriched")
async def rebuild_enriched(request: Request):
    """全量重算 enriched 表 — 不获取任何数据,仅基于已有 kline_daily + adj_factor 重算复权+指标。

    返回 job_id,可轮询 /api/pipeline/jobs 查看进度。
    """
    import asyncio
    try:
        repo = request.app.state.repo

        from app.services.pipeline_jobs import job_store, release_run_slot, try_acquire_run_slot
        from app.api.data import invalidate_storage_cache

        job_id, is_new = job_store.create()
        if not is_new:
            return {"status": "reused", "job_id": job_id}

        async def task() -> None:
            if not try_acquire_run_slot():
                job_store.fail(job_id, "已有数据任务在运行(或上一次任务卡死未结束),请稍后再试")
                return
            loop = asyncio.get_event_loop()

            def progress(stage: str, pct: int, msg: str,
                         stage_pct: int | None = None, skip_log: bool = False) -> None:
                job_store.progress(job_id, stage, pct, msg,
                                   stage_pct=stage_pct, skip_log=skip_log)

            try:
                job_store.start(job_id)
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
            finally:
                release_run_slot()

        asyncio.create_task(task())
        return {"status": "started", "job_id": job_id}
    except Exception as e:
        import traceback as _tb
        logger.error("rebuild_enriched error: %s\n%s", e, _tb.format_exc())
        raise HTTPException(status_code=500, detail=str(e)) from e


# 长时间任务专用线程池（隔离于 FastAPI 默认线程池，防止阻塞请求处理）
import concurrent.futures as _cf
_long_task_executor = _cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="long-task")
