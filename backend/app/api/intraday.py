"""行情状态 / SSE 推送 API。

盘中选股相关端点已迁移至策略页面，此处仅保留全局行情基础设施。
SSE 推送三种事件 (使用标准 SSE event 字段):
  - quotes_updated: 行情数据刷新，前端 invalidate 对应 query
  - strategy_alert: 策略监控/告警触发，前端弹通知
  - depth_updated: 五档盘口修正完成，前端刷新连板梯队/看板封单数据
"""
from __future__ import annotations

import asyncio
import json
import re
import time

import polars as pl
from fastapi import APIRouter, Query, Request
from sse_starlette.sse import EventSourceResponse

from app.json_safe import finite_float_or_none

# 受控外部 fallback (P1 realtime) — 仅只读展示; 绝不写入 repository/enriched。
from app.services.external_fallback import get_adapter
from app.services.external_fallback.adapter import _cn_today_iso as _fb_cn_today_iso

router = APIRouter(prefix="/api/intraday", tags=["quotes"])


# 规范化 symbol 形状: <digits>.<SH|SZ|BJ|HK|INDEX>
# 调用方输入不得形成用户可控 URL/host; 此处仅做形状校验。
_SYMBOL_RE = re.compile(r"^\d{4,6}\.(SH|SZ|BJ|HK|INDEX)$")


def _normalize_symbols(raw: str | None, *, limit: int = 60) -> list[str]:
    """逗号分隔 symbol 字符串 → 规范化去重列表 (上限 limit)。

    非法形状直接丢弃; 输入不参与任何 URL/host 构造。
    """
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for token in str(raw).replace(";", ",").replace("\n", ",").split(","):
        s = token.strip().upper()
        if s and _SYMBOL_RE.match(s) and s not in seen:
            seen.add(s)
            out.append(s)
            if len(out) >= limit:
                break
    return out


def _maybe_external_fallback(
    symbols: list[str], local_rows: list[dict]
) -> tuple[list[dict], dict]:
    """本地优先 → 受控外部 realtime fallback resolver。

    返回 (final_rows, meta)。meta 仅在实际命中外部源时含降级标记,
    形状: {degraded: True, sources: {realtime: "tencent_quote"},
          fallback_reason: "local_snapshot_missing"|"local_snapshot_stale"}。
    本地当日数据或任何关闭/缺 scope/非交易日场景 → meta 为空 dict。
    绝不把结果交给 QuoteService / repository 写入。
    """
    if not symbols:
        return local_rows, {}
    result = get_adapter().resolve_realtime(symbols, local_rows)
    if not result.used_fallback:
        return local_rows, {}
    # 外部行覆盖展示; 保留 source="tencent_quote" + degraded 语义
    meta = {
        "degraded": True,
        "sources": {"realtime": "tencent_quote"},
        "fallback_reason": result.reason.value if result.reason else None,
    }
    return result.rows, meta


def _get_quote_service(request: Request):
    """获取全局 QuoteService。"""
    return getattr(request.app.state, "quote_service", None)

# 核心指数代码: canonical (.INDEX) + 旧式后缀兼容 (仅分类用, 数据路径统一 .INDEX)。
# 注意: 000001.SH = 上证指数 (指数), 但 000001.SZ = 平安银行 (股票) — 靠后缀区分。
# 与 QuoteService._is_index_record 口径一致: .INDEX 后缀或已知指数代码即指数。
_CORE_INDEX_SYMBOLS = frozenset({
    "000001.INDEX", "399001.INDEX", "399006.INDEX", "000680.INDEX",
    # 旧式后缀仅作分类兼容; 数据查询前统一 canonical_index_symbol 规范化
    "000001.SH", "399001.SZ", "399006.SZ", "000680.SH",
})


def _is_index_symbol(
    symbol: str, *, cached_index_symbols: set[str] | None = None
) -> bool:
    """判断 symbol 是否走指数缓存路径。

    分类口径与 QuoteService._is_index_record 一致: .INDEX 后缀、核心指数代码
    (含旧式 .SH/.SZ 后缀), 或当前存在于指数实时缓存中 (覆盖用户自定义
    realtime_index_symbols) → 指数; 其余 .SH/.SZ/.BJ/.HK → 股票。
    注意 000001.SH (上证指数) 与 000001.SZ (平安银行) 靠后缀区分。
    """
    if symbol.endswith(".INDEX") or symbol in _CORE_INDEX_SYMBOLS:
        return True
    return symbol in (cached_index_symbols or ())


def _read_stock_quotes_from_cache(
    qs, stock_symbols: list[str]
) -> tuple[list[dict], list[str]]:
    """从股票实时缓存 (QuoteService.get_quotes_compat / enriched) 读取并按 symbol 过滤。

    只读本地缓存, 绝不请求 provider; 返回 (命中行, 缓存缺失的 symbol)。
    缺失的 symbol 由调用方交给受控外部 fallback 补齐。
    """
    if not stock_symbols:
        return [], []
    try:
        df = qs.get_quotes_compat()
    except Exception:  # noqa: BLE001
        return [], list(stock_symbols)
    if df is None or df.is_empty() or "symbol" not in df.columns:
        return [], list(stock_symbols)
    hit_df = df.filter(pl.col("symbol").is_in(stock_symbols))
    hit_rows = hit_df.to_dicts() if not hit_df.is_empty() else []
    hits = {r.get("symbol") for r in hit_rows}
    missing = [s for s in stock_symbols if s not in hits]
    return hit_rows, missing


def _stamp_local_stock_rows(rows: list[dict], *, has_recent: bool) -> None:
    """给未触发外部 fallback 的本地股票行打 provenance (原地)。

    据行内 date/timestamp 是否为当前交易日标 realtime / local_disk; has_recent
    (来自 qs.status) 在日期缺失时兜底。绝不把昨日数据叫 realtime。
    """
    today = _fb_cn_today_iso()
    for r in rows:
        ts = r.get("date") or r.get("timestamp")
        if ts and str(ts)[:10] == today:
            r["source"] = "realtime"
        elif has_recent and ts is None:
            r["source"] = "realtime"
        else:
            r["source"] = "local_disk"


def _partition_stock_rows_by_freshness(
    stock_symbols: list[str], stock_rows: list[dict]
) -> tuple[list[dict], list[str], list[dict]]:
    """按 symbol 分新鲜度: 保留当日 fresh 行, 只把 missing/stale 交给 resolver。

    adapter._local_snapshot_is_fresh 是「任一行当日即整批 fresh」, 在混合请求
    (A fresh + B missing/stale) 下会零网络且丢 B。这里按 symbol 精确分区:
      - 行 date/timestamp == 当前交易日 → fresh, 原样保留 (零网络)
      - 行存在但日期 < 今日 → stale, 行连同 symbol 交给 resolver
      - 无行 → missing, symbol 交给 resolver
    返回 (fresh_rows, gap_symbols, gap_rows)。gap_rows 为 stale 行 (供 resolver
    分类 reason), fresh 行不进 resolver。
    """
    today = _fb_cn_today_iso()
    fresh_rows: list[dict] = []
    stale_rows_by_sym: dict[str, dict] = {}
    gap_symbols: list[str] = []
    for sym in stock_symbols:
        row = next((r for r in stock_rows if r.get("symbol") == sym), None)
        if row is None:
            gap_symbols.append(sym)
            continue
        ts = row.get("date") or row.get("timestamp")
        if ts and str(ts)[:10] == today:
            fresh_rows.append(row)
        else:
            gap_symbols.append(sym)
            stale_rows_by_sym[sym] = row
    # gap_rows 按 gap_symbols 顺序排列 stale 行 (missing symbol 无行)
    gap_rows = [stale_rows_by_sym[s] for s in gap_symbols if s in stale_rows_by_sym]
    return fresh_rows, gap_symbols, gap_rows


def _fallback_index_quotes_from_daily(request: Request, symbols: list[str] | None = None) -> list[dict]:
    """实时指数缓存为空时，从本地指数日 K 取最近收盘价作为兜底。"""
    from app.data_providers.fquant.symbols import canonical_index_symbol
    repo = getattr(request.app.state, "repo", None)
    if not repo:
        return []

    params: list[str] = []
    symbol_filter = ""
    if symbols:
        symbols = [canonical_index_symbol(s) for s in symbols]
        placeholders = ", ".join("?" for _ in symbols)
        symbol_filter = f"WHERE symbol IN ({placeholders})"
        params.extend(symbols)

    try:
        rows = repo.execute_all(
            f"""
            WITH ranked AS (
                SELECT symbol, date, close,
                       row_number() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
                FROM kline_index_daily
                {symbol_filter}
            ), latest AS (
                SELECT symbol,
                       max(CASE WHEN rn = 1 THEN date END) AS date,
                       max(CASE WHEN rn = 1 THEN close END) AS last_price,
                       max(CASE WHEN rn = 2 THEN close END) AS prev_close
                FROM ranked
                WHERE rn <= 2
                GROUP BY symbol
            )
            SELECT latest.symbol, latest.date, latest.last_price, latest.prev_close
            FROM latest
            ORDER BY latest.symbol
            """,
            params,
        )
    except Exception:  # noqa: BLE001
        return []

    out: list[dict] = []
    for symbol, dt, raw_last_price, raw_prev_close in rows:
        last_price = finite_float_or_none(raw_last_price)
        prev_close = finite_float_or_none(raw_prev_close)
        change_amount = None
        change_pct = None
        if last_price is not None and prev_close not in (None, 0):
            change_amount = last_price - prev_close
            change_pct = change_amount / prev_close * 100
        out.append({
            "symbol": symbol,
            "name": None,
            "date": str(dt) if dt else None,
            "last_price": last_price,
            "close": last_price,
            "prev_close": prev_close,
            "change_amount": change_amount,
            "change_pct": change_pct,
            "source": "index_daily",
        })
    return out




def _fallback_index_quotes_from_provider(symbols: list[str] | None = None) -> list[dict]:
    """QuoteService 尚无指数缓存时，走当前 realtime provider 拉取最新指数行情。"""
    from app.data_providers.fquant.symbols import canonical_index_symbol
    if symbols:
        symbol_list = [canonical_index_symbol(s) for s in symbols]
    else:
        symbol_list = ["000001.INDEX", "399001.INDEX", "399006.INDEX", "000680.INDEX"]
    try:
        from app.data_providers.registry import get_active_provider_name, get_provider

        provider = get_provider(get_active_provider_name("realtime"))
        if not getattr(provider.capabilities, "realtime", False):
            return []
        df = provider.get_realtime(symbols=symbol_list)
    except Exception:  # noqa: BLE001
        return []

    if df is None or df.is_empty():
        return []

    out: list[dict] = []
    for row in df.to_dicts():
        ext = row.get("ext") or {}
        last_price = finite_float_or_none(
            row.get("last_price") if row.get("last_price") is not None else row.get("close")
        )
        prev_close = finite_float_or_none(
            row.get("prev_close") if row.get("prev_close") is not None else ext.get("prev_close")
        )
        change_amount = finite_float_or_none(
            row.get("change_amount") if row.get("change_amount") is not None else ext.get("change_amount")
        )
        change_pct = finite_float_or_none(
            row.get("change_pct") if row.get("change_pct") is not None else ext.get("change_pct")
        )
        if change_amount is None and last_price is not None and prev_close not in (None, 0):
            change_amount = last_price - prev_close
        if change_pct is None and change_amount is not None and prev_close not in (None, 0):
            change_pct = change_amount / prev_close * 100

        out.append({
            "symbol": row.get("symbol"),
            "name": row.get("name"),
            "date": str(row.get("date")) if row.get("date") is not None else None,
            "last_price": last_price,
            "close": last_price,
            "prev_close": prev_close,
            "change_amount": change_amount,
            "change_pct": change_pct,
            "source": row.get("source") or "provider_realtime",
            "timestamp": row.get("timestamp"),
        })
    return [row for row in out if row.get("symbol")]


@router.get("/status")
def status(request: Request):
    """行情状态 (来自全局 QuoteService)。"""
    qs = _get_quote_service(request)
    if qs:
        return qs.status()
    return {"enabled": False, "running": False, "symbol_count": 0, "index_symbol_count": 0,
            "quote_age_ms": None, "is_trading_hours": False, "last_fetch_ms": None}


@router.get("/indices")
def index_quotes(
    request: Request,
    symbols: str | None = Query(None, description="逗号分隔的指数 symbol 列表"),
):
    """返回指数行情：优先实时缓存，缓存为空时走 provider realtime，最后日线兜底。

    受控外部 fallback (P1): 当本地所有路径均无当日数据时, 才尝试腾讯公共源。
    fallback 响应追加 degraded/sources/fallback_reason (仅实际命中时); 旧字段保留。
    """
    from app.data_providers.fquant.symbols import canonical_index_symbol
    raw_list = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else None
    symbol_list = [canonical_index_symbol(s) for s in raw_list] if raw_list else None
    qs = _get_quote_service(request)
    source = "realtime"
    if qs:
        df = qs.get_index_quotes(symbol_list)
        rows = df.to_dicts() if not df.is_empty() else []
    else:
        rows = []
    if not rows:
        rows = _fallback_index_quotes_from_provider(symbol_list)
        source = "provider_realtime"
    if not rows:
        rows = _fallback_index_quotes_from_daily(request, symbol_list)
        source = "index_daily"

    # 本地优先 → 受控外部 fallback (仅展示, 绝不写 repository/enriched)
    raw_norm = _normalize_symbols(symbols, limit=60) if symbols else []
    norm_symbols = [canonical_index_symbol(s) for s in raw_norm]
    final_rows, meta = _maybe_external_fallback(norm_symbols, rows)
    if meta:
        source = "fallback_external"
    return {"rows": final_rows, "count": len(final_rows), "source": source, **meta}


@router.get("/snapshot")
def snapshot_quotes(
    request: Request,
    symbols: str | None = Query(None, description="逗号分隔的 symbol 列表 (最多 60 个)"),
):
    """只读 realtime 快照端点 (同一响应形状)。

    股票 symbol 优先从 QuoteService 股票实时缓存 (get_quotes_compat / enriched) 读取
    并按 symbol 过滤; 按 symbol 分新鲜度:
      - 当日命中行 → fresh, 零网络, 原样保留 (source=realtime)
      - 缺失/陈旧 symbol + 对应 stale 行 → 交给受控外部 fallback resolver:
        开启则调腾讯替换并标 degraded (source=fallback_external);
        关闭则保留 stale 行 (source=local_disk, 绝不叫 realtime)。
    混合请求 (A fresh + B missing/stale) 不会因 A fresh 而丢 B。本地行据其 date
    区分当日(realtime) vs 非当日(local_disk)。指数 symbol 按既有分类走指数缓存路径
    (实时缓存 → provider realtime → 日线兜底)。输入 symbol 经形状规范化, 不得形成
    用户可控 URL/host。绝不把结果交给 QuoteService / repository 写入。
    """
    norm_symbols = _normalize_symbols(symbols, limit=60)
    # 保持既有无参契约：返回 QuoteService 的默认指数快照，而不是空集合。
    if not norm_symbols:
        return index_quotes(request, symbols=None)
    qs = _get_quote_service(request)

    # ---- 分类: 指数 vs 股票 (禁止用 get_index_quotes 处理股票) ----
    # 分类口径与 QuoteService._is_index_record 一致: .INDEX 后缀 / 核心指数代码 /
    # 当前存在于指数实时缓存 (覆盖用户自定义 realtime_index_symbols) → 指数。
    cached_index_symbols: set[str] = set()
    if qs:
        try:
            idx_df = qs.get_index_quotes()
            if idx_df is not None and not idx_df.is_empty() and "symbol" in idx_df.columns:
                cached_index_symbols = set(idx_df["symbol"].to_list())
        except Exception:  # noqa: BLE001
            pass
    index_symbols = [
        s for s in norm_symbols
        if _is_index_symbol(s, cached_index_symbols=cached_index_symbols)
    ]
    # 指数数据路径统一 canonical .INDEX (cache/provider/daily 查询均用 .INDEX)
    if index_symbols:
        from app.data_providers.fquant.symbols import canonical_index_symbol
        index_symbols = [canonical_index_symbol(s) for s in index_symbols]
    stock_symbols = [
        s for s in norm_symbols
        if not _is_index_symbol(s, cached_index_symbols=cached_index_symbols)
    ]

    # ---- 股票: 本地实时缓存读取 (绝不请求 provider) + 按 symbol 分新鲜度 ----
    if qs and stock_symbols:
        stock_rows, _missing = _read_stock_quotes_from_cache(qs, stock_symbols)
    else:
        stock_rows = []
    fresh_stock_rows, gap_symbols, gap_rows = _partition_stock_rows_by_freshness(
        stock_symbols, stock_rows
    )

    # ---- 指数: 保留既有缓存路径 (实时 → provider → 日线), 禁止回归 ----
    index_rows: list[dict] = []
    index_source = "realtime"
    if index_symbols:
        if qs:
            df = qs.get_index_quotes(index_symbols)
            index_rows = df.to_dicts() if not df.is_empty() else []
        if not index_rows:
            index_rows = _fallback_index_quotes_from_provider(index_symbols)
            index_source = "provider_realtime"
        if not index_rows:
            index_rows = _fallback_index_quotes_from_daily(request, index_symbols)
            index_source = "index_daily"

    # ---- 受控外部 fallback resolver (本地优先; 仅补 missing/stale symbols) ----
    # 只把 gap_symbols + gap_rows 交给 resolver: fresh 行不进 resolver, 故混合请求
    # (A fresh + B missing/stale) 不会因 A fresh 而丢 B。resolver 命中外部 → 替换 gap
    # 行并标 degraded; 关闭/未命中 → 返回原 gap_rows (stale 行保留)。
    gap_resolved_rows, stock_meta = _maybe_external_fallback(gap_symbols, gap_rows)
    stock_used_external = bool(stock_meta)
    index_final_rows, index_meta = _maybe_external_fallback(index_symbols, index_rows)

    meta: dict = {}
    if index_meta:
        meta.update(index_meta)
    if stock_meta:
        meta.update(stock_meta)

    # ---- 股票 provenance ----
    # fresh 行: 当日 → realtime。gap 行: 触发外部则为外部行 (source 已是 tencent_quote);
    # 未触发外部则按行内日期标 realtime/local_disk (绝不把昨日数据叫 realtime)。
    # qs.status().has_recent_data 在 gap 行日期缺失时兜底。
    has_recent = True
    if qs:
        try:
            has_recent = bool(qs.status().get("has_recent_data", True))
        except Exception:  # noqa: BLE001
            has_recent = True
    _stamp_local_stock_rows(fresh_stock_rows, has_recent=True)
    if not stock_used_external:
        _stamp_local_stock_rows(gap_resolved_rows, has_recent=has_recent)

    stock_final_rows = fresh_stock_rows + gap_resolved_rows

    # ---- 汇总 source ----
    if meta:
        source = "fallback_external"
    elif stock_used_external:
        source = "fallback_external"
    else:
        # 未触发外部: 股票本地 + 指数本地/provider/daily 混合 → 取较低保真度
        stock_has_local_disk = any(
            r.get("source") == "local_disk" for r in stock_final_rows
        )
        if index_symbols and index_source in ("provider_realtime", "index_daily"):
            source = index_source
        elif stock_has_local_disk:
            source = "local_disk"
        else:
            source = "realtime"

    final_rows = stock_final_rows + index_final_rows
    return {"rows": final_rows, "count": len(final_rows), "source": source, **meta}


@router.get("/stream")
async def quote_stream(request: Request):
    """SSE 端点: 行情更新 + 告警推送 + 五档修正。

    使用 sse-starlette EventSourceResponse:
    - 标准 SSE event 字段，前端按 event name 监听
    - 内置断线检测，客户端断开立即终止 generator
    - 内置 ping 心跳，保持连接活跃
    """
    qs = _get_quote_service(request)

    async def event_generator():
        while True:
            # 同时等待三类信号: 行情更新 / 告警 / 五档修正
            tasks: dict[str, asyncio.Future] = {
                "quote": asyncio.ensure_future(
                    asyncio.to_thread(qs.wait_for_update, timeout=5.0) if qs else asyncio.sleep(5)
                ),
                "alert": asyncio.ensure_future(
                    asyncio.to_thread(qs.wait_for_alert, timeout=5.0) if qs else asyncio.sleep(5)
                ),
                "depth": asyncio.ensure_future(
                    asyncio.to_thread(qs.wait_for_depth_update, timeout=5.0) if qs else asyncio.sleep(5)
                ),
                "review": asyncio.ensure_future(
                    asyncio.to_thread(qs.wait_for_review, timeout=5.0) if qs else asyncio.sleep(5)
                ),
            }

            done, pending = await asyncio.wait(
                list(tasks.values()),
                timeout=30.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()

            # 先推送告警 (如果有)
            if qs:
                alerts = qs.pop_alerts()
                if alerts:
                    for chunk_start in range(0, len(alerts), 20):
                        chunk = alerts[chunk_start:chunk_start + 20]
                        yield {
                            "event": "strategy_alert",
                            "data": json.dumps({
                                "ts": int(time.time() * 1000),
                                "alerts": chunk,
                            }, ensure_ascii=False),
                        }

                # 推送复盘进度 (定时复盘流式生成时) — 前端 reviewStore 直接消费
                # 事件已是 recap_market_stream 产出的 JSON 字符串, 逐条转发
                for evt_json in qs.pop_review_events():
                    yield {
                        "event": "review_progress",
                        "data": evt_json,
                    }

            # 推送行情更新 (行情信号触发)
            if tasks["quote"] in done:
                try:
                    update_result = tasks["quote"].result()
                except Exception:  # noqa: BLE001
                    update_result = False
                if update_result:
                    yield {
                        "event": "quotes_updated",
                        "data": json.dumps({
                            "ts": int(time.time() * 1000),
                            "symbol_count": qs._symbol_count if qs else 0,
                        }),
                    }

            # 推送五档修正完成 (depth 信号触发) — 前端刷新连板梯队封单数据
            if tasks["depth"] in done:
                try:
                    depth_result = tasks["depth"].result()
                except Exception:  # noqa: BLE001
                    depth_result = False
                if depth_result:
                    yield {
                        "event": "depth_updated",
                        "data": json.dumps({
                            "ts": int(time.time() * 1000),
                        }),
                    }

    return EventSourceResponse(event_generator())


@router.post("/refresh")
def refresh_quotes(request: Request):
    """手动刷新一次行情数据。"""
    qs = _get_quote_service(request)
    if qs:
        return qs.refresh()
    return {"error": "QuoteService not available"}
