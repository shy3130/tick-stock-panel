"""K 线 / 同步 API。"""
from __future__ import annotations

import gzip
import json
import logging
import math
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from functools import lru_cache
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.indicators.pipeline import compute_enriched, compute_enriched_single
from app.market_time import cn_now, cn_today, in_continuous_session
from app.price_limits import is_no_limit_day, is_risk_warning_name, parse_listing_date, price_limit_pct
from app.db_safe import is_valid_ext_ident
from app.services import kline_sync, trading_day
from app.services import minute_adjust

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/kline", tags=["kline"])


def _json_safe(obj):
    """把 nan/inf 换成 None, 保证 JSON 合法。

    gzip 路径原先 allow_nan=True, 会写出前端 JSON.parse 不能吃的 NaN/Infinity;
    未压缩路径走 Starlette allow_nan=False, 遇到非有限浮点整段 500。
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _gzip_payload(request: Request, payload: dict, *, pref_key: str) -> dict | Response:
    """大 JSON 响应的传输压缩: 偏好开启 + 客户端接受 gzip + 响应超阈值才压。

    分时/日K各自使用独立偏好键 (沿用已有 *_batch_compress 存储键保证兼容)。
    level 6 实测 13MB ≈ 290ms CPU 压掉 87%; level 9 要 2.5s 不可用。
    datetime → isoformat, 与 FastAPI jsonable_encoder 输出一致
    (前端 since 增量按字符串字典序比较, 格式必须与非压缩路径相同)。
    """
    from app.services import preferences as _prefs
    _getters = {
        "minute_batch_compress": _prefs.get_minute_batch_compress,
        "daily_batch_compress": _prefs.get_daily_batch_compress,
    }
    getter = _getters.get(pref_key)
    compress_on = False
    if getter is not None:
        try:
            compress_on = bool(getter())
        except Exception:  # 偏好读取异常按不压缩返回原样
            compress_on = False
    payload = _json_safe(payload)
    headers = getattr(request, "headers", None) or {}
    if compress_on and "gzip" in (headers.get("accept-encoding") or ""):
        raw = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
            default=lambda o: o.isoformat() if hasattr(o, "isoformat") else str(o),
        ).encode()
        if len(raw) > 1024:
            return Response(
                content=gzip.compress(raw, 6),
                media_type="application/json",
                headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"},
            )
    return payload


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


@lru_cache(maxsize=8192)
def _name_pinyin_keys(name: str) -> tuple[str, ...]:
    """返回中文名称所有可能的拼音首字母串 (多音字展开为笛卡尔积)。

    '平安银行' -> ('PAYH',); '重庆百货' -> ('CQBH', 'CQMH', 'ZQBH', 'ZQMH')。
    非汉字字符原样保留: '万科A' -> ('WKA',)。
    股票名总量有限且不变, lru_cache 命中后单次查询 ≈ dict 查找, 全市场遍历 < 1ms。
    """
    from pypinyin import pinyin, Style
    if not name:
        return ()
    keys = [""]
    for group in pinyin(name, style=Style.FIRST_LETTER, heteronym=True):
        keys = [k + g.upper() for k in keys for g in group]
    return tuple(keys)


def _init_pinyin_dict() -> None:
    """加载 A 股高频多音字地名/词组词典, 使常见误读也能命中。

    pypinyin 默认词典对部分地名取常见读音 (如「重」→ chóng), 补充后「重庆」
    同时接受 zhòng/qìng (zq) 与 chóng/qīng (cq) 两种首字母, 与同花顺行为一致。
    幂等: 多次调用安全。
    """
    try:
        from pypinyin import load_phrases_dict
        # value 用二维 list: 每个字给一个或多个读音
        load_phrases_dict({
            "重庆": [["zhòng", "chóng"], ["qīng"]],
            "长安": [["cháng", "zhǎng"], ["ān"]],
            "长春": [["cháng", "zhǎng"], ["chūn"]],
            "长沙": [["cháng", "zhǎng"], ["shā"]],
            "长城": [["cháng", "zhǎng"], ["chéng"]],
            "长江": [["cháng", "zhǎng"], ["jiāng"]],
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("pypinyin phrases dict load failed (polyphone coverage may degrade): %s", exc)


_init_pinyin_dict()


def _match_pinyin(name: str, keyword: str) -> bool:
    """keyword 是否匹配 name 任一拼音首字母串的前缀 (支持多音字)。"""
    return any(k.startswith(keyword) for k in _name_pinyin_keys(name))


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
    is_pinyin_query = keyword.isalpha() and keyword.isascii()

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

    # 分层匹配: ① code/symbol 前缀 → ② 拼音首字母前缀(纯字母输入) → ③ 包含匹配
    prefix_hits = df.filter(prefix_mask).head(limit)
    if prefix_hits.height >= limit:
        matched = prefix_hits
    else:
        collected = [prefix_hits] if prefix_hits.height else []
        seen = set(prefix_hits["symbol"].to_list()) if prefix_hits.height else set()
        remaining = limit - prefix_hits.height

        # ② 拼音首字母前缀: 仅纯字母输入触发 (如 payh → 平安银行); 中文/代码输入零开销跳过
        if is_pinyin_query and remaining > 0:
            pinyin_rows = []
            for row in df.filter(~pl.col("symbol").is_in(seen)).iter_rows(named=True):
                if _match_pinyin(row["name"], keyword):
                    pinyin_rows.append(row)
                    if len(pinyin_rows) >= remaining:
                        break
            if pinyin_rows:
                collected.append(pl.DataFrame(pinyin_rows))
                seen.update(r["symbol"] for r in pinyin_rows)
                remaining -= len(pinyin_rows)

        # ③ 包含匹配补充
        if remaining > 0:
            contain_hits = df.filter(contains_mask & ~pl.col("symbol").is_in(seen)).head(remaining)
            if contain_hits.height:
                collected.append(contain_hits)

        matched = (
            pl.concat(collected, how="vertical") if len(collected) > 1
            else (collected[0] if collected else df.head(0))
        )
    rows = matched.select(["symbol", "name", "code", "asset_type"]).to_dicts()
    return {"results": rows}


@router.post("/instruments/names")
def instruments_names(request: Request, symbols: list[str]):
    """批量查标的名称 (股票 + ETF + 指数)。传入 symbol 列表, 返回 {symbol: name}。"""
    if not symbols:
        return {"names": {}}
    repo = request.app.state.repo
    return {"names": repo.get_name_map(symbols)}


def _get_stock_info(repo, symbol: str) -> dict:
    """从 instruments 内存缓存查标的名称 + 股本。

    该接口在个股弹窗打开时每秒被调用 (SSE invalidate 触发重拉), 走
    repo.get_instruments() 的 Polars 内存缓存按 symbol 过滤, 不再每请求
    DuckDB 扫 instruments parquet。列缺失时返回空 dict, 与旧 SQL 报错路径一致。
    """
    import polars as pl
    try:
        df = repo.get_instruments()
        needed = ("symbol", "name", "total_shares", "float_shares")
        if df.is_empty() or not all(c in df.columns for c in needed):
            return {}
        hit = df.filter(pl.col("symbol") == symbol).head(1)
        if hit.is_empty():
            return {}
        return {
            "name": hit["name"][0],
            "total_shares": hit["total_shares"][0],
            "float_shares": hit["float_shares"][0],
        }
    except Exception:  # noqa: BLE001
        return {}


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

    info: dict = {
        "rate": price_limit_pct(
            symbol,
            trade_date,
            is_risk_warning=(
                asset_type == "stock" and is_risk_warning_name(instrument_name)
            ),
        ),
        "limit_up": None,
        "limit_down": None,
        "no_limit": False,
        "source": "rule",
    }

    # instrument 行一次取出: 今日权威涨跌停价 + listing_date 窗口判定共用
    row: dict | None = None
    try:
        import polars as pl

        instruments = repo.get_instruments_asset(asset_type)
        available = [
            column
            for column in ("symbol", "limit_up", "limit_down", "listing_date")
            if column in instruments.columns
        ]
        if "symbol" in available and len(available) > 1:
            hit = instruments.filter(pl.col("symbol") == symbol).select(available).head(1)
            if not hit.is_empty():
                row = hit.to_dicts()[0]
    except Exception:
        row = None

    # 注册制新股上市初期无涨跌幅: listing_date 命中窗口时 no_limit=True,
    # 压过 rate 与维表值 (前端不再画涨跌停带, y 轴按实际数据自适应)
    if row is not None:
        listing = parse_listing_date(row.get("listing_date"))
        if listing is not None and is_no_limit_day(symbol, listing, trade_date):
            info["no_limit"] = True
            return info

    if trade_date != cn_today():
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


def _get_previous_closes(
    repo,
    symbol: str,
    trade_dates: list[date],
    asset_type: str,
) -> dict[date, float | None]:
    """Return the previous trading day's adjusted close for each session."""
    if not trade_dates:
        return {}
    start = min(trade_dates) - timedelta(days=45)
    end = max(trade_dates)
    try:
        daily = repo.get_daily_asset(
            asset_type,
            symbol,
            start,
            end,
            columns=["date", "close"],
        ).sort("date")
    except Exception:
        daily = None
    if daily is None or daily.is_empty():
        return {trade_date: None for trade_date in trade_dates}

    closes: list[tuple[date, float]] = []
    for daily_date, close in daily.select(["date", "close"]).iter_rows():
        if close is None:
            continue
        numeric = float(close)
        if math.isfinite(numeric) and numeric > 0:
            closes.append((daily_date, numeric))

    result: dict[date, float | None] = {}
    for trade_date in trade_dates:
        result[trade_date] = next(
            (close for daily_date, close in reversed(closes) if daily_date < trade_date),
            None,
        )
    return result


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
    # 未传 end_date 时用北京今天: 实时注入只在内存缓存命中时补当日 K,
    # 缓存冷时 parquet 当日行能否进结果取决于这个窗口右端。
    end = date.fromisoformat(end_date) if end_date else cn_today()
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
            return _gzip_payload(
                request,
                {"symbol": symbol, "name": stock_name, "stock_info": stock_info, "rows": []},
                pref_key="daily_batch_compress",
            )
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
        return _gzip_payload(
            request,
            _attach_ext(resp, repo, symbol, ext_columns),
            pref_key="daily_batch_compress",
        )

    rows = df.to_dicts()

    # 追加/覆盖今日实时蜡烛
    rows = _maybe_inject_live_candle(request, symbol, rows, asset_type)

    resp = {"symbol": symbol, "name": stock_name, "stock_info": stock_info, "rows": rows, "source": "enriched"}
    return _gzip_payload(
        request,
        _attach_ext(resp, repo, symbol, ext_columns),
        pref_key="daily_batch_compress",
    )


def _attach_ext(resp: dict, repo, symbol: str, ext_columns: Optional[str]) -> dict:
    """按 ext_columns 规格为单只股票 LEFT JOIN 扩展数据，平铺到 stock_info['ext']。

    key 形如 "{config_id}__{field_name}"，与自选列表 enriched 接口保持一致。
    委托 screener._load_ext_value_maps 取值: 复用其 (路径,mtime) 签名缓存,
    个股弹窗每秒重拉时不再重复读 ext parquet; 任何 ext 表/字段缺失都静默跳过。
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
        if config_id and field_name and is_valid_ext_ident(config_id):
            specs.append((config_id, field_name))
    if not specs:
        return resp

    try:
        from app.api.screener import _load_ext_value_maps
        value_maps = _load_ext_value_maps(repo, ext_columns)
    except Exception:  # noqa: BLE001
        value_maps = {}

    ext_values: dict = {}
    for config_id, field_name in specs:
        ext_col_name = f"{config_id}__{field_name}"
        vmap = value_maps.get(ext_col_name) or {}
        ext_values[ext_col_name] = vmap.get(symbol)

    stock_info = dict(resp.get("stock_info") or {})
    stock_info["ext"] = ext_values
    resp["stock_info"] = stock_info
    return resp


def _latest_live_candle(
    request: Request,
    symbol: str,
    asset_type: str = "stock",
    *,
    refresh_asset: bool = True,
) -> dict | None:
    """从内存缓存读取单只标的的当日实时 enriched 行。"""

    if asset_type == "stock":
        qs = getattr(request.app.state, "quote_service", None)
        if not qs:
            return None
        df_today, enriched_date = qs.get_enriched_today()
    elif asset_type in {"etf", "index"}:
        df_today, enriched_date = request.app.state.repo.get_enriched_latest_asset(
            asset_type, refresh=refresh_asset,
        )
    else:
        return None
    if df_today.is_empty():
        return None

    # 非交易日(周末/假日)缓存日期 != 北京今天, 跳过注入避免产生重复蜡烛。
    # 必须用 cn_today(): 美洲时区主机整个 A 股交易时段本地日期落后北京一天,
    # 旧代码盘中直接丢K。UTC 主机盘中(UTC 1:30-7:00)本地日期与北京相同, 并不丢K;
    # UTC 的旧症状是北京 00:00-08:00 把昨日残留快照误当实时K注入。
    if not enriched_date or enriched_date != cn_today():
        return None

    # 查找该 symbol 的实时 enriched 行
    import polars as pl
    try:
        q = df_today.filter(pl.col("symbol") == symbol).to_dicts()
        if not q:
            return None
        q = q[0]
    except Exception:
        return None

    close_price = q.get("close")
    if not close_price or close_price <= 0:
        return None

    # 沿用完整日K接口原有的实时行投影, 避免增量接口形成第二套字段契约。
    # API 在非交易时段可能返回 open/high/low=0, 用 close 填充避免异常蜡烛。
    raw_open = q.get("open")
    raw_high = q.get("high")
    raw_low = q.get("low")
    live_row = {
        "date": str(enriched_date),
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
    for key in ("ma5", "ma10", "ma20", "ma30", "ma60",
                "macd_dif", "macd_dea", "macd_hist",
                "kdj_k", "kdj_d", "kdj_j",
                "boll_upper", "boll_lower",
                "rsi_6", "rsi_14", "rsi_24",
                "atr_14", "vol_ratio_5d"):
        if key in q and q[key] is not None:
            live_row[key] = q[key]
    return live_row


def _maybe_inject_live_candle(request: Request, symbol: str, rows: list[dict], asset_type: str = "stock") -> list[dict]:
    """如果有当日实时 enriched 数据, 用实时数据生成今日蜡烛并追加/覆盖。"""
    live_row = _latest_live_candle(request, symbol, asset_type)
    if live_row is None:
        return rows

    # 如果已有今天的 enriched 行, 覆盖; 否则追加
    found = False
    for r in rows:
        if str(r.get("date")) == live_row["date"]:
            r.update(live_row)
            found = True
            break

    if not found:
        rows.append(live_row)

    return rows


@router.get("/daily/latest")
def get_daily_latest(
    request: Request,
    symbol: str = Query(..., description="标的代码,如 000001.SZ"),
):
    """返回内存中的当日单行 K 线, 供详情页实时增量更新。"""
    repo = request.app.state.repo
    asset_type = repo.resolve_asset_type(symbol)
    row = _latest_live_candle(request, symbol, asset_type, refresh_asset=False)
    return {
        "symbol": symbol,
        "row": row,
        "source": "live" if row is not None else "none",
    }


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
    from datetime import timedelta

    # 窗口右端必须是北京今天: QuoteService 当日 flush 的分区日期是北京交易日。
    # 美西主机整个 A 股交易时段、UTC 主机北京 00:00-08:00, date.today() 比北京早一天,
    # 迷你蜡烛会把当日实时 K 排除在窗口外。
    end = cn_today()
    start = end - timedelta(days=days * 2)  # 多取一些确保交易日够

    cols = ["symbol", "date", "open", "high", "low", "close", "volume"]

    # 按资产类型分组: stock 走批量缓存; etf/index 逐只查独立存储 (数量少, 成本可忽略)
    stock_symbols: list[str] = []
    etf_symbols: list[str] = []
    index_symbols: list[str] = []
    for s in symbols:
        t = repo.resolve_asset_type(s)
        if t == "etf":
            etf_symbols.append(s)
        elif t == "index":
            index_symbols.append(s)
        else:
            stock_symbols.append(s)

    frames: list[pl.DataFrame] = []
    if stock_symbols:
        df_stock = repo.get_daily_batch(stock_symbols, start, end, columns=cols)
        if not df_stock.is_empty():
            frames.append(df_stock)
    for sym in etf_symbols:
        sub = repo.get_etf_daily(sym, start, end, columns=cols)
        if not sub.is_empty():
            frames.append(sub)
    for sym in index_symbols:
        sub = repo.get_index_daily(sym, start, end, columns=cols)
        if not sub.is_empty():
            frames.append(sub)

    if not frames:
        return {"data": {}}
    df = pl.concat(frames, how="diagonal_relaxed")

    # 按 symbol 分组, 每只取最近 N 条。
    # partition_by 一次切分, 避免 N 只自选时对同一批数据做 N 次全帧过滤。
    result: dict[str, list[dict]] = {}
    for part in df.partition_by("symbol", maintain_order=True):
        sub = part.sort("date").tail(days)
        if not sub.is_empty():
            result[sub["symbol"][0]] = sub.to_dicts()

    # 日K批量同为大响应端点 (千只自选 MB 级), 与分时各自独立压缩开关
    return _gzip_payload(request, {"data": result}, pref_key="daily_batch_compress")


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
    # 增量响应: since (ISO datetime) 之前的K不回传, 客户端本地缓存合并。
    # since 应传客户端已持有的最后一根时间 — 形成中的动态K >= since, 每轮覆盖。
    since_str = body.get("since")
    since_dt: datetime | None = None
    if since_str:
        try:
            since_dt = datetime.fromisoformat(str(since_str))
            # 防御: 带 Z/偏移的 aware 输入 (如 toISOString) → 转北京墙钟再去 tz,
            # 否则与行里的 naive 北京时间比较会 TypeError 且差 8 小时
            if since_dt.tzinfo is not None:
                since_dt = since_dt.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
        except ValueError:
            since_dt = None
    # 自选分时本地优先标志: 全量分钟服务健康时, 股票缺口不再批量补拉
    # (本地分区由服务按间隔持续写入, 下一轮自然补全; 停牌/临停票补拉也是空, 无损)。
    # ETF 不在全量分钟 universe 内, 恒走补拉。服务不健康时回落现状补拉兜底。
    prefer_local = bool(body.get("prefer_local", False))
    if not symbols:
        return {"data": {}}

    repo = request.app.state.repo
    capset = request.app.state.capabilities

    # 权限守卫: 分钟K批量是 Pro+ 能力
    if not capset.has(Cap.KLINE_MINUTE_BATCH):
        raise HTTPException(status_code=403, detail="需要 Pro+ 权限 (kline.minute.batch)")

    trade_date = date.fromisoformat(trade_date_str) if trade_date_str else cn_today()

    # 非交易日(周末/节假日)才回退到最近有数据的交易日; 否则盘中会显示昨天而非今天。
    # 判据: 周末必回退; 工作日收盘后(>=15:30)仍无今日日K → 节假日, 回退。
    # (下方补拉已改为取到即落盘, 但只有真实交易时段才会写入当日分区,
    #  节假日当日分区恒为空, 不影响该回退判据。)
    if not trade_date_str:
        today = cn_today()
        # 周六/周日必非交易日; 工作日休市 (国庆等) 以交易日探针的「确定休市」为准,
        # 与 /api/index/minute 同口径 — 未知 (None) 维持下方收盘后判据
        need_fallback = today.weekday() >= 5 or trading_day.is_trading_day() is False
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
    elif h < 11 or (h == 11 and m <= 30):
        expected = (h - 9) * 60 + m - 30
    elif h < 13:
        expected = 120
    elif h < 15:
        expected = 120 + (h - 13) * 60 + m
    else:
        expected = 240

    # 本地状态分类 (补拉已改为取到即落盘, 完整性判定随之收紧):
    # - fresh:  根数 >= 期望-2 (时间边界容差), 直接用本地。原 0.9 比例阈值会让
    #           持久化数据在 90% 处冻结尾巴, 必须按根数差判。
    # - holes:  缺K → 全天重拉回填, 否则"最后一根+1min"的增量窗口永远不会
    #           回看洞。含两种: 中间的洞 (相邻间距非 1 分钟 / 非午休 91 分钟)
    #           与前部的洞 (首根显著晚于开盘 — 盘中重启/停机跨开盘的残留,
    #           连续的尾部K会被增量锚定锁死, 同样必须全天重拉)。
    # - stale:  仅尾部落后 → 增量拉, 请求量从"每轮全天"降为"每轮一根"量级。
    _LUNCH_GAP_MIN = 91  # 11:30 → 13:01
    # 前部洞基准: 开盘后 6 分钟 (容许无集合竞价K的数据源)。晚开/停牌复牌的票
    # 也会命中 → 全天拉幂等, 至多多一次批量请求, 与中间洞同一代价模型。
    day_open_floor = datetime(trade_date.year, trade_date.month, trade_date.day, 9, 36, 0)

    def _has_holes(sub: pl.DataFrame) -> bool:
        if not sub.is_empty() and sub["datetime"][0] > day_open_floor:
            return True
        gaps = sub["datetime"].diff().dt.total_minutes().drop_nulls()
        return gaps.filter((gaps != 1) & (gaps != _LUNCH_GAP_MIN)).len() > 0

    result: dict[str, list[dict]] = {}
    full_pull: list[str] = []          # 无数据或中间有洞 → 全天拉
    stale_last: dict[str, datetime] = {}  # 尾部落后 → symbol → 最后一根时间
    local_parts: dict[str, pl.DataFrame] = {}
    if not df_local.is_empty():
        for part in df_local.partition_by("symbol", maintain_order=True):
            local_parts[part["symbol"][0]] = part.sort("datetime")
    fresh_floor = max(0, expected - 2)
    for sym in symbols:
        sub = local_parts.get(sym, pl.DataFrame())
        if expected == 0 or sub.height >= fresh_floor:
            if not sub.is_empty():
                result[sym] = sub.to_dicts()
            continue
        if sub.is_empty() or _has_holes(sub):
            full_pull.append(sym)
        else:
            stale_last[sym] = sub["datetime"][-1]

    # prefer_local 生效判定: 仅当全量分钟服务健康 (freshness 契约, 见 minute_refresh.is_healthy)
    full_minute_healthy = False
    if prefer_local:
        svc = getattr(request.app.state, "minute_refresh", None)
        full_minute_healthy = bool(svc is not None and svc.is_healthy())
    if full_minute_healthy:
        # 纯尾部落后 (stale_last): 服务的增量轮下一轮就会补上, 股票不补拉省请求。
        # 空洞 (full_pull: 空分区 / 中间洞 / 前部洞): 服务增量锚定本地最新时间,
        # 永远不会回看洞 → 不压制, 由端点全天拉取并落盘修复。
        # ETF 不在服务 universe 内, 两类均维持补拉。
        for sym in stale_last:
            if sym not in etf_set:
                sub = local_parts.get(sym)
                if sub is not None and not sub.is_empty():
                    result[sym] = sub.to_dicts()
        stale_last = {s: t for s, t in stale_last.items() if s in etf_set}

    # Step 2: 补拉并落盘 (取到即写, upsert 语义; 下一轮命中本地, 请求量骤降)。
    # 落盘失败只降级 (log 后继续返回本轮数据), 不影响响应 —— 持久化是优化而非正确性前提。
    # 契约: 本端点只接受 stock/ETF (指数分钟K走 /api/index/minute 独立路径),
    # 按 asset_type 拆分调用 (自定义源 / TickFlow 路由均依赖 asset_type 正确传递)。
    day_start = datetime(trade_date.year, trade_date.month, trade_date.day, 9, 25, 0)
    session_end = datetime(trade_date.year, trade_date.month, trade_date.day, 15, 5, 0)
    lim = capset.limits(Cap.KLINE_MINUTE_BATCH)
    minute_dirs = {
        "stock": repo.store.data_dir / "kline_minute",
        "etf": repo.store.data_dir / "kline_etf_minute",
    }
    live_map: dict[str, pl.DataFrame] = {}

    def _pull(asset: str, sym_list: list[str], start: datetime) -> None:
        if not sym_list:
            return
        raw_basis = minute_adjust.minute_basis_is_raw(repo.store.data_dir)
        df_live = kline_sync.sync_minute_batch(
            sym_list,
            start_time=start,
            end_time=session_end,
            batch_size=lim.batch if lim else None,
            rpm=lim.rpm if lim else None,
            asset_type=asset,
            raw_basis=raw_basis,
        )
        if df_live.is_empty():
            return
        try:
            # 读-改-写必须持仓库写锁 (与全量分钟服务/盘后同步同一纪律, Windows 临时文件占用)。
            # 仅在拿到真实目录时落盘: data_dir 异常 (非 Path) 时跳过, 只返回本轮数据。
            # 落盘必须是原始口径 (raw_basis=True 时 sync 已按 adjust='none' 取回);
            # 对外响应再统一复权投影。
            minute_dir = minute_dirs[asset]
            if isinstance(minute_dir, Path):
                with repo._write_lock:
                    kline_sync._write_minute_partition(df_live, minute_dir)
        except Exception as e:  # noqa: BLE001
            logger.warning("minute-batch 补拉落盘失败 (降级为仅返回): %s", e)
        if raw_basis:
            df_live = minute_adjust.apply_minute_adjustment(
                df_live, repo.store.data_dir, asset,
            )
        for part in df_live.partition_by("symbol", maintain_order=True):
            live_map[part["symbol"][0]] = part.sort("datetime")

    _pull("stock", [s for s in full_pull if s not in etf_set], day_start)
    _pull("etf", [s for s in full_pull if s in etf_set], day_start)
    if stale_last:
        # 增量公共起点 = 最旧的最后一根本身: 最后一根是形成中的动态K (分钟内
        # 收盘/量/额持续变化), 必须重拉并以定版值覆盖; 重叠由 upsert/合并去重吸收
        inc_start = min(stale_last.values())
        if inc_start < session_end:
            _pull("stock", [s for s in stale_last if s not in etf_set], inc_start)
            _pull("etf", [s for s in stale_last if s in etf_set], inc_start)

    # 合并: 有增量/回填的 symbol = 本地 + 拉取 upsert; 仅拉到的 (missing) 直接进结果
    for sym, sub in local_parts.items():
        live = live_map.get(sym)
        if live is not None:
            merged = (
                pl.concat([sub, live])
                .unique(subset=["symbol", "datetime"], keep="last")
                .sort("datetime")
            )
            result[sym] = merged.to_dicts()
    for sym, live in live_map.items():
        if sym not in result:
            result[sym] = live.to_dicts()

    # since 增量过滤: 只回 >= since 的K (含动态最后一根), 无新增的 symbol 不回
    if since_dt is not None:
        result = {
            sym: [r for r in rows if r["datetime"] >= since_dt]
            for sym, rows in result.items()
        }
        result = {sym: rows for sym, rows in result.items() if rows}

    return _gzip_payload(
        request,
        {
            "data": result,
            "full_minute_local": full_minute_healthy,
            "incremental": since_dt is not None,
        },
        pref_key="minute_batch_compress",
    )


@router.get("/minute-range")
def get_minute_range(
    request: Request,
    symbol: str = Query(..., description="标的代码"),
    days: int = Query(10, ge=1, le=20, description="最近交易日数量"),
):
    """读取单只标的最近 N 个已落库交易日的分钟 K。"""
    import polars as pl

    repo = request.app.state.repo
    asset_type = repo.resolve_asset_type(symbol)
    stock_info = (
        _get_stock_info(repo, symbol)
        if asset_type == "stock"
        else _get_asset_info(repo, symbol, asset_type)
    )
    base_response = {
        "symbol": symbol,
        "name": stock_info.get("name"),
        "asset_type": asset_type,
        "requested_days": days,
    }

    # 指数分钟 K 不落本地仓库, 最新分时仍由 /api/index/minute 实时读取。
    if asset_type == "index":
        return _gzip_payload(
            request,
            {**base_response, "sessions": [], "source": "none"},
            pref_key="minute_batch_compress",
        )

    end = cn_today()
    start = end - timedelta(days=days * 3 + 20)
    minute = repo.get_minute_range([symbol], start, end, asset_type=asset_type)
    if minute.is_empty() or "datetime" not in minute.columns:
        return _gzip_payload(
            request,
            {**base_response, "sessions": [], "source": "none"},
            pref_key="minute_batch_compress",
        )

    minute = minute.with_columns(
        pl.col("datetime").dt.date().alias("_trade_date"),
    )
    trade_dates = sorted(minute["_trade_date"].unique().to_list())[-days:]
    previous_closes = _get_previous_closes(repo, symbol, trade_dates, asset_type)
    row_columns = [
        column
        for column in (
            "datetime", "open", "high", "low", "close", "volume", "amount"
        )
        if column in minute.columns
    ]
    sessions = []
    for trade_date in trade_dates:
        rows = (
            minute.filter(pl.col("_trade_date") == trade_date)
            .sort("datetime")
            .select(row_columns)
            .to_dicts()
        )
        if rows:
            sessions.append({
                "date": trade_date.isoformat(),
                "prev_close": previous_closes.get(trade_date),
                "rows": rows,
            })

    return _gzip_payload(
        request,
        {
            **base_response,
            "sessions": sessions,
            "source": "local" if sessions else "none",
        },
        pref_key="minute_batch_compress",
    )


@router.get("/minute")
def get_minute(
    request: Request,
    symbol: str = Query(..., description="标的代码"),
    trade_date: date | None = Query(None, alias="date", description="交易日期, 默认最新"),
    live: bool = Query(False, description="当日盘中跳过本地优先, 直接实时拉取(个股详情分时轮询用)"),
):
    """读取某只股票某天的分钟 K 线。

    - 本地有完整数据(240条) → 直接返回
    - 本地无数据或不完整 → 从有效分钟数据源实时拉取返回(不写入)
    - 自定义源失败时, 仅具备 TickFlow 单股分钟能力才回退 TickFlow
    - live=true 且当日连续竞价时段 → 跳过本地优先直接实时拉取:
      盘中分钟增量落盘的本地分区按 ≥60s 轮次更新, 90% 完整度启发式会让
      详情分时图停在上一增量轮, 与行情列表的节奏脱节
    """
    repo = request.app.state.repo
    capset = request.app.state.capabilities
    raw_basis = minute_adjust.minute_basis_is_raw(repo.store.data_dir)
    asset_type = repo.resolve_asset_type(symbol)
    stock_info = _get_stock_info(repo, symbol) if asset_type == "stock" else _get_asset_info(repo, symbol, asset_type)
    stock_name = stock_info.get("name")

    if trade_date is None:
        # 默认看今天, 而不是本地落盘的最近日 (盘中后者是昨天)。
        # 非交易日(周末/节假日)才回退到本地最近有数据的交易日。
        today = cn_today()
        # 同 /minute-batch: 周末必回退, 工作日休市以交易日探针「确定休市」为准
        need_fallback = today.weekday() >= 5 or trading_day.is_trading_day() is False
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
        # 本地无任何分钟K, 尝试从当前有效分钟源拉取当天
        trade_date = cn_today()
        df = kline_sync.fetch_minute_single(
            symbol, trade_date, asset_type=asset_type, capset=capset,
            raw_basis=raw_basis,
        )
        if raw_basis:
            df = minute_adjust.apply_minute_adjustment(df, repo.store.data_dir, asset_type)
        price_limit = _get_price_limit_info(
            repo, symbol, trade_date, asset_type, stock_name,
        )
        prev_close = _get_previous_closes(
            repo, symbol, [trade_date], asset_type,
        ).get(trade_date)
        return _gzip_payload(
            request,
            {
                "symbol": symbol, "name": stock_name, "stock_info": stock_info,
                "date": str(trade_date), "rows": df.to_dicts(),
                "source": "live" if not df.is_empty() else "none",
                "asset_type": asset_type,
                "price_limit": price_limit,
                "prev_close": prev_close,
            },
            pref_key="minute_batch_compress",
        )

    prev_close = _get_previous_closes(
        repo, symbol, [trade_date], asset_type,
    ).get(trade_date)
    price_limit = _get_price_limit_info(
        repo, symbol, trade_date, asset_type, stock_name,
    )

    if live and trade_date == cn_today() and in_continuous_session():
        # 详情分时轮询: 当日盘中实时拉取最新一根K, 不落盘; 拉空(源侧延迟/
        # 时段边界)则落回下方本地优先路径。
        live_df = kline_sync.fetch_minute_single(
            symbol, trade_date, asset_type=asset_type, capset=capset,
            raw_basis=raw_basis,
        )
        if not live_df.is_empty():
            if raw_basis:
                live_df = minute_adjust.apply_minute_adjustment(
                    live_df, repo.store.data_dir, asset_type,
                )
            return _gzip_payload(
                request,
                {
                    "symbol": symbol, "name": stock_name, "stock_info": stock_info,
                    "date": str(trade_date), "rows": live_df.to_dicts(),
                    "source": "live", "asset_type": asset_type,
                    "price_limit": price_limit, "prev_close": prev_close,
                },
                pref_key="minute_batch_compress",
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
        elif h < 11 or (h == 11 and m <= 30):
            expected = (h - 9) * 60 + m - 30  # 9:30 起
        elif h < 13:
            expected = 120  # 午休
        elif h < 15:
            expected = 120 + (h - 13) * 60 + m
        else:
            expected = 240

    is_complete = not df.is_empty() and len(df) >= expected * 0.9  # 允许 10% 容差

    if is_complete:
        return _gzip_payload(
            request,
            {
                "symbol": symbol, "name": stock_name, "stock_info": stock_info,
                "date": str(trade_date), "rows": df.to_dicts(), "source": "local",
                "asset_type": asset_type,
                "price_limit": price_limit,
                "prev_close": prev_close,
            },
            pref_key="minute_batch_compress",
        )

    # 本地不完整或无数据 → 从当前有效分钟源实时拉取
    live_df = kline_sync.fetch_minute_single(
        symbol, trade_date, asset_type=asset_type, capset=capset,
        raw_basis=raw_basis,
    )
    if raw_basis and not live_df.is_empty():
        live_df = minute_adjust.apply_minute_adjustment(
            live_df, repo.store.data_dir, asset_type,
        )
    return _gzip_payload(
        request,
        {
            "symbol": symbol, "name": stock_name, "stock_info": stock_info,
            "date": str(trade_date), "rows": live_df.to_dicts(),
            "source": "live" if not live_df.is_empty() else "none",
            "asset_type": asset_type,
            "price_limit": price_limit,
            "prev_close": prev_close,
        },
        pref_key="minute_batch_compress",
    )


@router.post("/sync")
def sync_symbol(
    request: Request,
    symbol: str = Query(...),
    days: int = Query(250, ge=10, le=2000),
):
    """手动触发单股同步(Free 用户在 K 线页用)。"""
    repo = request.app.state.repo
    capset = request.app.state.capabilities
    zero: list[str] = []
    n = kline_sync.sync_and_persist_daily_batch([symbol], repo, capset, count=days, zero_row_out=zero)
    resp = {"symbol": symbol, "rows_written": n}
    if zero:
        resp["zero_row_symbols"] = zero
    return resp


@router.post("/sync_batch")
def sync_batch(
    request: Request,
    symbols: list[str],
    days: int = Query(250, ge=10, le=2000),
):
    repo = request.app.state.repo
    capset = request.app.state.capabilities
    zero: list[str] = []
    n = kline_sync.sync_and_persist_daily_batch(symbols, repo, capset, count=days, zero_row_out=zero)
    resp = {"symbols": symbols, "rows_written": n}
    if zero:
        # fail-loud (#302): 裸符号被跳过/上游 200 空数据的标的显式列出,
        # 不再"回填显示成功、实际全库 0 行"
        resp["zero_row_symbols"] = zero
    return resp


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

    from app.services.pipeline_jobs import JobCancelledError, job_store, release_run_slot, run_with_capacity, try_acquire_run_slot
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
    job_id, is_new = job_store.create(long_running=True)
    if not is_new:
        return {"status": "reused", "job_id": job_id}

    async def task() -> None:
        if not try_acquire_run_slot(job_id):
            job_store.fail(job_id, "已有数据任务在运行(或上一次任务卡死未结束),请稍后再试")
            return
        loop = asyncio.get_event_loop()

        def progress(stage: str, pct: int, msg: str) -> None:
            job_store.progress(job_id, stage, pct, msg)

        try:
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
            # 剔除指数 symbol: 指数分钟K无本地存储, 落库会污染 kline_minute
            index_set = repo.get_index_symbol_set()
            universe = [s for s in universe if s not in index_set]
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

            written = await loop.run_in_executor(_long_task_executor, run_with_capacity, job_id, _run)

            # 刷新视图
            from app.jobs.daily_pipeline import _refresh_single_view
            _refresh_single_view(repo, "kline_minute")

            progress("done", 100, f"分钟 K 同步完成,{written} 行")
            job_store.succeed(job_id, {"minute_rows": written, "universe_size": len(universe)})
            invalidate_storage_cache()
        except JobCancelledError:
            # 已由 terminate() 标记失败, 拉取线程在分块回调处自行退出
            invalidate_storage_cache()
        except Exception as e:  # noqa: BLE001
            job_store.fail(job_id, str(e))
            invalidate_storage_cache()
        finally:
            release_run_slot(job_id)

    asyncio.create_task(task())
    return {"status": "started", "job_id": job_id}


@router.post("/sync_minute_single")
async def sync_minute_single(request: Request, body: dict):
    """手动拉取单只股票的分钟K并落库 (口径随基准标记: 未迁移=前复权, 已迁移=原始)。

    body: { "symbol": "000001.SZ" }
    用于个股分时图"获取数据"按钮: 本地无数据时单独拉取并持久化。
    """
    import asyncio

    from app.services.preferences import get_minute_sync_days

    symbol = body.get("symbol", "").strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol 不能为空")

    requested_days = body.get("days")
    if requested_days is not None:
        if isinstance(requested_days, bool) or not isinstance(requested_days, int):
            raise HTTPException(status_code=400, detail="days 必须是整数")
        if requested_days < 1 or requested_days > 30:
            raise HTTPException(status_code=400, detail="days 必须在 1 到 30 之间")

    repo = request.app.state.repo
    capset = request.app.state.capabilities

    # 指数分钟K无本地存储, 落库会污染股票分钟表 kline_minute;
    # 指数分钟数据走 /api/index/minute 实时读取, 此端点显式拒绝。
    if repo.resolve_asset_type(symbol) == "index":
        raise HTTPException(status_code=400, detail="指数分钟K不支持落库同步 (指数分钟数据走 /api/index/minute 实时读取)")

    if not _minute_allowed(capset):
        raise HTTPException(status_code=403, detail="需要 Pro+ 权限")

    days = requested_days if requested_days is not None else get_minute_sync_days()
    loop = asyncio.get_event_loop()

    def _run():
        return kline_sync.sync_and_persist_minute([symbol], repo, capset, days=days, force_full_days=True)

    written = await loop.run_in_executor(_long_task_executor, _run)

    # 刷新视图
    from app.jobs.daily_pipeline import _refresh_single_view
    _refresh_single_view(repo, "kline_minute")

    return {"status": "ok", "symbol": symbol, "rows": written}


@router.post("/minute-migrate")
async def minute_migrate(request: Request):
    """存量分钟K迁移为原始口径并启用读取时复权 (幂等, 见 services/minute_adjust)。

    - 用日K原始收盘价做锚点, 把历史"拉取时前复权"的分区换算回原始价;
    已是原始价的行 (全量分钟落盘) 锚点 k≈1 自动跳过;
    - 全部分区成功后创建 .raw_basis 标记: 此后读取自动复权投影、拉取改取原始价;
    - 失败不标记, 可重复调用续跑 (已换算分区二次运行为 no-op)。
    返回 { partitions, converted_symbols, skipped_no_daily, failed, marked }。
    """
    import asyncio

    repo = request.app.state.repo
    loop = asyncio.get_event_loop()
    stats = await loop.run_in_executor(
        _long_task_executor,
        lambda: minute_adjust.migrate_minute_to_raw(
            repo.store.data_dir, write_lock=repo._write_lock,
        ),
    )
    if stats.get("marked"):
        from app.jobs.daily_pipeline import _refresh_single_view
        _refresh_single_view(repo, "kline_minute")
        _refresh_single_view(repo, "kline_etf_minute")
    return {"status": "ok", **stats}


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
            # execute_one (cursor+close): 直连 db.execute 的未消费结果集会在 Windows 上
            # 钉住分区句柄, 导致下方 rmtree 静默删不掉被钉文件
            result = repo.execute_one("SELECT COUNT(*) AS cnt FROM kline_minute")
            removed = result[0] if result else 0
        except Exception:
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
        from app.services.pipeline_jobs import JobCancelledError, job_store, release_run_slot, run_with_capacity, try_acquire_run_slot
        from app.api.data import invalidate_storage_cache

        job_id, is_new = job_store.create()
        if not is_new:
            return {"status": "reused", "job_id": job_id}

        async def task() -> None:
            if not try_acquire_run_slot(job_id):
                job_store.fail(job_id, "已有数据任务在运行(或上一次任务卡死未结束),请稍后再试")
                return
            loop = asyncio.get_event_loop()

            def progress(stage: str, pct: int, msg: str,
                         stage_pct: int | None = None, skip_log: bool = False) -> None:
                job_store.progress(job_id, stage, pct, msg,
                                   stage_pct=stage_pct, skip_log=skip_log)

            try:
                result = await loop.run_in_executor(
                    _long_task_executor, run_with_capacity, job_id,
                    lambda: run_extend_history(repo, capset, value, unit, on_progress=progress),
                )
                if "error" in result:
                    job_store.fail(job_id, result["error"])
                else:
                    job_store.succeed(job_id, result)
                invalidate_storage_cache()
            except JobCancelledError:
                # 已由 terminate() 标记失败, 拉取线程在分块回调处自行退出
                invalidate_storage_cache()
            except Exception as e:
                logger.exception("extend_history failed: job_id=%s", job_id)
                job_store.fail(job_id, str(e))
                invalidate_storage_cache()
            finally:
                release_run_slot(job_id)

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
        from app.services.pipeline_jobs import JobCancelledError, job_store, release_run_slot, run_with_capacity, try_acquire_run_slot
        from app.api.data import invalidate_storage_cache

        job_id, is_new = job_store.create()
        if not is_new:
            return {"status": "reused", "job_id": job_id}

        async def task() -> None:
            if not try_acquire_run_slot(job_id):
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
                result = await loop.run_in_executor(_long_task_executor, run_with_capacity, job_id, _run)
                if "error" in result:
                    job_store.fail(job_id, result["error"])
                else:
                    job_store.succeed(job_id, result)
                invalidate_storage_cache()
            except JobCancelledError:
                # 已由 terminate() 标记失败, 拉取线程在分块回调处自行退出
                invalidate_storage_cache()
            except Exception as e:
                logger.exception("repair_daily failed: job_id=%s", job_id)
                job_store.fail(job_id, str(e))
                invalidate_storage_cache()
            finally:
                release_run_slot(job_id)

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

        from app.services.pipeline_jobs import JobCancelledError, job_store, release_run_slot, run_with_capacity, try_acquire_run_slot
        from app.api.data import invalidate_storage_cache

        job_id, is_new = job_store.create()
        if not is_new:
            return {"status": "reused", "job_id": job_id}

        async def task() -> None:
            if not try_acquire_run_slot(job_id):
                job_store.fail(job_id, "已有数据任务在运行(或上一次任务卡死未结束),请稍后再试")
                return
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
                    _long_task_executor, run_with_capacity, job_id,
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
            except JobCancelledError:
                # 已由 terminate() 标记失败, 拉取线程在分块回调处自行退出
                invalidate_storage_cache()
            except Exception as e:
                logger.exception("rebuild_enriched failed: job_id=%s", job_id)
                job_store.fail(job_id, str(e))
                invalidate_storage_cache()
            finally:
                release_run_slot(job_id)

        asyncio.create_task(task())
        return {"status": "started", "job_id": job_id}
    except Exception as e:
        import traceback as _tb
        logger.error("rebuild_enriched error: %s\n%s", e, _tb.format_exc())
        raise HTTPException(status_code=500, detail=str(e)) from e


# 长时间任务专用线程池（隔离于 FastAPI 默认线程池，防止阻塞请求处理）
import concurrent.futures as _cf
_long_task_executor = _cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="long-task")
