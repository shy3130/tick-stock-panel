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

from fastapi import APIRouter, Query, Request
from sse_starlette.sse import EventSourceResponse

# 受控外部 fallback (P1 realtime) — 仅只读展示; 绝不写入 repository/enriched。
from app.services.external_fallback import get_adapter

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


def _fallback_index_quotes_from_daily(request: Request, symbols: list[str] | None = None) -> list[dict]:
    """实时指数缓存为空时，从本地指数日 K 取最近收盘价作为兜底。"""
    repo = getattr(request.app.state, "repo", None)
    if not repo:
        return []

    params: list[str] = []
    symbol_filter = ""
    if symbols:
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
    for symbol, dt, last_price, prev_close in rows:
        change_amount = None
        change_pct = None
        if last_price is not None and prev_close not in (None, 0):
            change_amount = float(last_price) - float(prev_close)
            change_pct = change_amount / float(prev_close) * 100
        out.append({
            "symbol": symbol,
            "name": None,
            "date": str(dt) if dt else None,
            "last_price": float(last_price) if last_price is not None else None,
            "close": float(last_price) if last_price is not None else None,
            "prev_close": float(prev_close) if prev_close is not None else None,
            "change_amount": change_amount,
            "change_pct": change_pct,
            "source": "index_daily",
        })
    return out


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fallback_index_quotes_from_provider(symbols: list[str] | None = None) -> list[dict]:
    """QuoteService 尚无指数缓存时，走当前 realtime provider 拉取最新指数行情。"""
    symbol_list = symbols or ["000001.SH", "399001.SZ", "399006.SZ", "000680.SH"]
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
        last_price = _to_float(row.get("last_price") if row.get("last_price") is not None else row.get("close"))
        prev_close = _to_float(row.get("prev_close") if row.get("prev_close") is not None else ext.get("prev_close"))
        change_amount = _to_float(
            row.get("change_amount") if row.get("change_amount") is not None else ext.get("change_amount")
        )
        change_pct = _to_float(
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
    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else None
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
    norm_symbols = _normalize_symbols(symbols, limit=60) if symbols else []
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

    本地优先: 取 QuoteService 缓存或 provider realtime; 缺失/陈旧且受控 fallback
    启用时才调腾讯公共源。输入 symbol 经形状规范化, 不得形成用户可控 URL/host。
    绝不把结果交给 QuoteService / repository 写入。
    """
    norm_symbols = _normalize_symbols(symbols, limit=60)
    qs = _get_quote_service(request)
    local_rows: list[dict] = []
    source = "realtime"
    if qs:
        df = qs.get_index_quotes(norm_symbols) if norm_symbols else qs.get_index_quotes()
        local_rows = df.to_dicts() if not df.is_empty() else []
    if not local_rows:
        local_rows = _fallback_index_quotes_from_provider(norm_symbols or None)
        source = "provider_realtime"

    final_rows, meta = _maybe_external_fallback(norm_symbols, local_rows)
    if meta:
        source = "fallback_external"
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
