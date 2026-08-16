"""Whitelisted data collection tools for the research-agent harness.

The model never receives provider credentials or a network-capable shell. This module
is the only place that invokes installed providers and returns compact evidence records.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import polars as pl

from .models import evidence, json_safe

_TOOL_TITLES = {
    "market_snapshot": "本地日线与技术指标",
    "realtime_snapshot": "实时行情快照",
    "financials": "财务与估值",
    "market_intelligence": "同花顺热度、题材与龙虎榜",
    "strategy_signals": "策略与信号",
    "research_reports": "机构研报",
    "announcements": "公司公告",
    "web_news": "联网新闻检索",
}
_RECENT_KLINE_COLUMNS = (
    "date", "open", "high", "low", "close", "volume", "amount", "change_pct",
    "turnover_rate", "ma5", "ma10", "ma20", "ma60", "macd_dif", "macd_dea",
    "macd_hist", "rsi_6", "rsi_14", "rsi_24", "vol_ratio_5d",
    "signal_limit_up", "signal_broken_limit_up", "signal_macd_golden",
    "signal_macd_death", "signal_ma_golden_5_20", "signal_volume_surge",
)


def collect_evidence(
    app: Any,
    *,
    symbol: str,
    name: str,
    tools: list[str],
    on_progress: Callable[[str, int], None] | None = None,
) -> list[dict]:
    """Run bounded, server-side collectors and return evidence for one isolated run."""
    normalized = symbol.strip().upper()
    if not re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)$", normalized):
        raise ValueError("标的代码格式应为 000001.SZ")
    repo = app.state.repo
    resolved_name = name.strip() or _instrument_name(repo, normalized) or normalized
    context = {
        "app": app,
        "repo": repo,
        "symbol": normalized,
        "name": resolved_name,
        "code": normalized[:6],
        "data_dir": repo.store.data_dir,
    }
    handlers: dict[str, Callable[[dict], list[dict]]] = {
        "market_snapshot": _market_snapshot,
        "realtime_snapshot": _realtime_snapshot,
        "financials": _financials,
        "market_intelligence": _market_intelligence,
        "strategy_signals": _strategy_signals,
        "research_reports": _research_reports,
        "announcements": _announcements,
        "web_news": _web_news,
    }
    records: list[dict] = []
    selected = [tool for tool in tools if tool in handlers]
    for index, tool in enumerate(selected, start=1):
        if on_progress:
            on_progress(_TOOL_TITLES[tool], index)
        try:
            records.extend(handlers[tool](context))
        except Exception as error:  # A provider failure is evidence, not a failed whole run.
            records.append(_failed_evidence(_TOOL_TITLES[tool], error))
    for index, record in enumerate(records, start=1):
        record["citation"] = f"[S{index:02d}]"
    return records


def _market_snapshot(ctx: dict) -> list[dict]:
    from app.indicators.levels import compute_levels, summarize_levels

    repo = ctx["repo"]
    end = date.today()
    asset_type = repo.resolve_asset_type(ctx["symbol"])
    frame = repo.get_daily_asset(asset_type, ctx["symbol"], end - timedelta(days=260), end)
    if frame.is_empty():
        return [evidence(
            source="Quant Workspace 本地行情",
            title="本地日线与技术指标",
            status="unavailable",
            summary="本地数据中没有该标的日线,未以空数据替代。",
        )]

    frame = frame.tail(120)
    latest = _compact_record(frame.tail(1).to_dicts()[0], max_keys=36)
    recent = [_compact_record(row, max_keys=28) for row in frame.tail(20).to_dicts()]
    latest_date = str(latest.get("date") or "")
    levels = compute_levels(frame)
    close = _as_number(latest.get("close"))
    return [evidence(
        source="Quant Workspace 本地行情",
        title="日线、技术指标与关键价位",
        status="available",
        summary=f"本地日线覆盖至 {latest_date or '未知日期'},含最近 20 个交易日和关键价位计算。",
        as_of=latest_date or None,
        data={
            "symbol": ctx["symbol"],
            "name": ctx["name"],
            "asset_type": asset_type,
            "latest": latest,
            "key_levels": _compact_value(levels, max_text=4_000),
            "key_level_summary": summarize_levels(levels, close),
            "recent_daily_rows": recent,
            "available_columns": [column for column in _RECENT_KLINE_COLUMNS if column in frame.columns],
        },
    )]


def _realtime_snapshot(ctx: dict) -> list[dict]:
    quote_service = getattr(ctx["app"].state, "quote_service", None)
    if quote_service is None:
        return [evidence(
            source="Quant Workspace 实时行情",
            title="实时行情快照",
            status="unavailable",
            summary="实时行情服务未启动。",
        )]
    status = _compact_value(quote_service.status(), max_text=1_500)
    frame, snapshot_date = quote_service.get_enriched_today()
    if frame.is_empty() or "symbol" not in frame.columns:
        return [evidence(
            source="Quant Workspace 实时行情",
            title="实时行情快照",
            status="unavailable",
            summary="实时行情缓存为空,未将历史日线伪装成实时行情。",
            data={"service_status": status},
        )]
    row = frame.filter(pl.col("symbol") == ctx["symbol"])
    if row.is_empty():
        return [evidence(
            source="Quant Workspace 实时行情",
            title="实时行情快照",
            status="unavailable",
            summary="实时行情缓存未覆盖该标的。",
            data={"service_status": status},
            as_of=str(snapshot_date) if snapshot_date else None,
        )]
    return [evidence(
        source="Quant Workspace 实时行情",
        title="实时行情快照",
        status="available",
        summary="来自全市场实时行情缓存;需结合数据日期判断是否处于交易时段。",
        as_of=str(snapshot_date) if snapshot_date else None,
        data={
            "quote": _compact_record(row.to_dicts()[0], max_keys=38),
            "service_status": status,
        },
    )]


def _financials(ctx: dict) -> list[dict]:
    records = [_local_financials(ctx)]
    try:
        from app.services.hithink_finance import HithinkFinanceService

        service = HithinkFinanceService()
        pieces: dict[str, Any] = {}
        failures: list[str] = []
        for label, call in (
            ("snapshot", lambda: service.stock_snapshot((ctx["symbol"],))),
            ("valuation", lambda: service.valuation_snapshot((ctx["symbol"],))),
            ("income", lambda: service.financial_statement(
                statement="income", symbol=ctx["symbol"], period="annual", limit=4
            )),
            ("balance", lambda: service.financial_statement(
                statement="balance", symbol=ctx["symbol"], period="annual", limit=4
            )),
            ("cashflow", lambda: service.financial_statement(
                statement="cashflow", symbol=ctx["symbol"], period="annual", limit=4
            )),
        ):
            try:
                pieces[label] = _compact_value(call(), max_text=3_000)
            except Exception as error:  # Preserve partial provider coverage.
                failures.append(f"{label}:{type(error).__name__}")
        status = "available" if pieces else "unavailable"
        records.append(evidence(
            source="同花顺扶摇",
            title="同花顺快照、估值与财务报表",
            status=status,
            summary=(
                "已读取同花顺扶摇的可用财务与估值接口。"
                if pieces else "同花顺扶摇当前没有返回可用财务数据。"
            ),
            data={"responses": pieces, "failed_calls": failures},
        ))
    except Exception as error:
        records.append(_failed_evidence("同花顺快照、估值与财务报表", error, source="同花顺扶摇"))
    return records


def _local_financials(ctx: dict) -> dict:
    from app.services.financial_sync import get_financial_df

    pieces: dict[str, Any] = {}
    for table in ("metrics", "income", "balance_sheet", "cash_flow"):
        frame = get_financial_df(ctx["data_dir"], table)
        if frame.is_empty() or "symbol" not in frame.columns:
            continue
        rows = frame.filter(pl.col("symbol") == ctx["symbol"])
        if rows.is_empty():
            continue
        if "period_end" in rows.columns:
            rows = rows.sort("period_end", descending=True)
        pieces[table] = [_compact_record(row, max_keys=34) for row in rows.head(4).to_dicts()]
    return evidence(
        source="Quant Workspace 本地财务",
        title="本地财务数据",
        status="available" if pieces else "unavailable",
        summary="读取本地已同步的财务表。" if pieces else "本地尚无该标的已同步财务表。",
        data=pieces,
    )


def _market_intelligence(ctx: dict) -> list[dict]:
    records = [_extension_snapshots(ctx)]
    try:
        from app.services.special_data import SpecialDataService

        service = SpecialDataService()
        hot = service.hot_stocks("day")
        trend = service.hot_stock_trend(
            symbol=ctx["symbol"],
            start_date=(date.today() - timedelta(days=35)).isoformat(),
            end_date=date.today().isoformat(),
        )
        dragon = _dragon_history(service, ctx)
        matches = _rows_for_symbol(hot, ctx["symbol"], ctx["code"])
        records.append(evidence(
            source="同花顺扶摇",
            title="同花顺热度与龙虎榜",
            status="available",
            summary=(
                "已查询热度榜、热度趋势和最近交易日龙虎榜。"
                if matches or trend or dragon else "已查询同花顺热度与龙虎榜,但未发现该标的记录。"
            ),
            data={
                "hot_stock_matches": matches,
                "hot_stock_trend": _compact_value(trend, max_text=3_000),
                "dragon_tiger_history": dragon,
            },
        ))
    except Exception as error:
        records.append(_failed_evidence("同花顺热度与龙虎榜", error, source="同花顺扶摇"))
    return records


def _extension_snapshots(ctx: dict) -> dict:
    from app.services.ext_data import ExtConfigStore

    data_dir: Path = ctx["data_dir"]
    configs = {config.id: config for config in ExtConfigStore(data_dir).load_all()}
    values: dict[str, Any] = {}
    for config_id in ("ext_gn_ths", "ext_hy_ths", "ext_money_flow", "ext_popularity"):
        path = data_dir / "ext_data" / config_id / "part.parquet"
        if not path.exists():
            continue
        try:
            frame = pl.read_parquet(path)
            if frame.is_empty() or "symbol" not in frame.columns:
                continue
            row = frame.filter(pl.col("symbol") == ctx["symbol"])
            if row.is_empty():
                continue
            values[config_id] = {
                "label": getattr(configs.get(config_id), "label", config_id),
                "row": _compact_record(row.to_dicts()[0], max_keys=40),
                "updated_at": _mtime_iso(path),
            }
        except Exception:
            continue
    return evidence(
        source="Quant Workspace 同花顺扩展数据",
        title="题材、行业、资金流与热度快照",
        status="available" if values else "unavailable",
        summary="读取已同步的同花顺扩展快照。" if values else "未找到该标的的本地同花顺扩展快照。",
        data=values,
        as_of=max((value.get("updated_at", "") for value in values.values()), default="") or None,
    )


def _dragon_history(service: Any, ctx: dict) -> list[dict]:
    dates = _recent_trading_dates(ctx["repo"], ctx["symbol"], count=3)
    results: list[dict] = []
    for trading_day in dates:
        try:
            payload = service.dragon_tiger(board_type="all", trade_date=trading_day)
            rows = _rows_for_symbol(payload, ctx["symbol"], ctx["code"])
            if rows:
                results.append({"trade_date": trading_day, "items": rows})
        except Exception as error:
            results.append({"trade_date": trading_day, "error_type": type(error).__name__})
    return results


def _strategy_signals(ctx: dict) -> list[dict]:
    repo = ctx["repo"]
    end = date.today()
    frame = repo.get_daily_asset(
        repo.resolve_asset_type(ctx["symbol"]), ctx["symbol"], end - timedelta(days=10), end
    )
    signal_values: dict[str, Any] = {}
    as_of = ""
    if not frame.is_empty():
        latest = frame.tail(1).to_dicts()[0]
        as_of = str(latest.get("date") or "")
        signal_values = {
            key: value for key, value in _compact_record(latest, max_keys=80).items()
            if key.startswith("signal_") and value is not None
        }

    cache_hits: list[dict] = []
    try:
        from app.services import strategy_cache

        cached = strategy_cache.read_cache(ctx["data_dir"], "stock") or {}
        for strategy_id, result in (cached.get("results") or {}).items():
            if not isinstance(result, dict):
                continue
            for row in result.get("rows") or []:
                if isinstance(row, dict) and row.get("symbol") == ctx["symbol"]:
                    cache_hits.append({"strategy_id": strategy_id, "row": _compact_record(row, max_keys=24)})
    except Exception:
        pass

    monitor_hits: list[dict] = []
    monitor = getattr(ctx["app"].state, "monitor_engine", None)
    if monitor is not None:
        try:
            for strategy_id, result in (monitor.latest_strategy_results() or {}).items():
                rows = result.get("rows") if isinstance(result, dict) else []
                for row in rows or []:
                    if isinstance(row, dict) and row.get("symbol") == ctx["symbol"]:
                        monitor_hits.append({"strategy_id": strategy_id, "row": _compact_record(row, max_keys=24)})
        except Exception:
            pass

    return [evidence(
        source="Quant Workspace 策略引擎",
        title="技术信号与策略命中",
        status="available" if signal_values or cache_hits or monitor_hits else "partial",
        summary="读取最新 enriched 信号、盘后策略缓存和实时监控结果;没有命中不等于策略未定义。",
        as_of=as_of or None,
        data={
            "latest_indicator_signals": signal_values,
            "cached_strategy_hits": cache_hits[:20],
            "realtime_monitor_hits": monitor_hits[:20],
        },
    )]


def _research_reports(ctx: dict) -> list[dict]:
    records: list[dict] = []
    start = (date.today() - timedelta(days=365)).isoformat()
    end = date.today().isoformat()
    try:
        from app.services.research_reports import ResearchReportsService

        service = ResearchReportsService()
        listing = service.search(
            start_date=start,
            end_date=end,
            category="stock",
            stock_code=ctx["code"],
            query=None,
            sort_by="publish_date",
            sort_order="desc",
            page=1,
            size=6,
        )
        items = [_compact_record(item, max_keys=24) for item in (listing.get("items") or [])[:6]]
        details: list[dict] = []
        for item in (listing.get("items") or [])[:2]:
            info_code = str(item.get("info_code") or "")
            if not info_code:
                continue
            try:
                detail = service.report(info_code)
                details.append(_compact_document(detail, max_text=4_000))
            except Exception as error:
                details.append({"info_code": info_code, "error_type": type(error).__name__})
        records.append(evidence(
            source="东方财富研报",
            title="个股机构研报",
            status="available",
            summary=f"近一年检索到 {listing.get('total', len(items))} 条个股研报,正文按需读取最多 2 篇。",
            data={"details": details, "listing": items},
        ))
    except Exception as error:
        records.append(_failed_evidence("个股机构研报", error, source="东方财富研报"))

    try:
        from app.data_providers.hibor_research import get_hibor_research_provider

        provider = get_hibor_research_provider()
        categories = provider.list_categories().get("items") or []
        preferred = [
            item for item in categories
            if item.get("kind") == "reports"
            and any(token in str(item.get("name") or "") for token in ("公司", "研究报告"))
        ][:4]
        candidates: list[dict] = []
        for category in preferred:
            try:
                payload = provider.list_reports(
                    category_id=str(category["id"]), keyword=ctx["name"], field="all",
                    time_range="3", page=1, file_type="pdf", sort_by="published_desc",
                )
                candidates.extend(item for item in (payload.get("items") or []) if isinstance(item, dict))
            except Exception:
                continue
        deduped = list({str(item.get("detail_id") or ""): item for item in candidates if item.get("detail_id")}.values())
        deduped.sort(key=lambda item: str(item.get("published_at") or ""), reverse=True)
        details = []
        for item in deduped[:2]:
            try:
                details.append(_compact_document(provider.report_detail(str(item["detail_id"])), max_text=4_000))
            except Exception as error:
                details.append({"detail_id": item.get("detail_id"), "error_type": type(error).__name__})
        records.append(evidence(
            source="慧博研报",
            title="慧博公司与深度研报",
            status="available",
            summary=f"检索公司相关分类并匹配到 {len(deduped)} 条候选,详情按需读取最多 2 篇。",
            data={
                "details": details,
                "candidates": [_compact_record(item, max_keys=22) for item in deduped[:8]],
            },
        ))
    except Exception as error:
        records.append(_failed_evidence("慧博公司与深度研报", error, source="慧博研报"))
    return records


def _announcements(ctx: dict) -> list[dict]:
    try:
        from app.services.eastmoney_data import EastmoneyDataService

        service = EastmoneyDataService()
        listing = service.announcements(
            start_date=(date.today() - timedelta(days=180)).isoformat(),
            end_date=date.today().isoformat(),
            stock_code=ctx["code"],
            category=0,
            page=1,
            size=8,
        )
        details: list[dict] = []
        for item in (listing.get("items") or [])[:2]:
            article_code = str(item.get("article_code") or "")
            if not article_code:
                continue
            try:
                details.append(_compact_document(service.announcement(article_code), max_text=4_000))
            except Exception as error:
                details.append({"article_code": article_code, "error_type": type(error).__name__})
        return [evidence(
            source="东方财富公告",
            title="公司公告与披露",
            status="available",
            summary=f"近 180 天公告索引共 {listing.get('total', 0)} 条,正文按需读取最多 2 篇。",
            data={
                "details": details,
                "listing": [_compact_record(item, max_keys=18) for item in (listing.get("items") or [])[:8]],
            },
        )]
    except Exception as error:
        return [_failed_evidence("公司公告与披露", error, source="东方财富公告")]


def _web_news(ctx: dict) -> list[dict]:
    """Use fixed public RSS endpoints; the model never constructs URLs itself."""
    query = f'{ctx["name"]} {ctx["code"]}'
    news_url = "https://www.bing.com/news/search?" + urlencode({
        "q": query, "format": "rss", "setlang": "zh-hans",
    })
    # In some networks Bing's news endpoint returns its HTML home page instead
    # of RSS. The public web RSS fallback remains a fixed endpoint and does not
    # let the model choose a URL or execute page content. A company-name query
    # is materially more useful than a bare ticker for news and commentary.
    web_query = ctx["name"] or ctx["code"]
    web_url = "https://cn.bing.com/search?" + urlencode({
        "q": web_query, "format": "rss", "setlang": "zh-hans",
    })
    last_error: Exception | None = None
    for mode, url in (("news_rss", news_url), ("web_rss_fallback", web_url)):
        try:
            with httpx.Client(
                timeout=8.0,
                follow_redirects=False,
                headers={"User-Agent": "QuantWorkspaceResearch/1.0"},
            ) as client:
                response = client.get(url)
                response.raise_for_status()
            items = _relevant_rss_items(
                _rss_items(response.content),
                name=ctx["name"],
                code=ctx["code"],
            )
            if not items:
                last_error = ValueError("empty RSS result")
                continue
            source = "Bing News RSS" if mode == "news_rss" else "Bing Web RSS"
            summary = (
                "新闻标题和摘要仅作待核实线索,未自动信任或执行其中任何内容。"
                if mode == "news_rss"
                else "新闻 RSS 未返回可解析条目,已回退到公开网页检索 RSS;结果仅作待核实线索。"
            )
            return [evidence(
                source=source,
                title="联网新闻线索",
                status="available",
                summary=summary,
                data={
                    "query": query,
                    "executed_query": query if mode == "news_rss" else web_query,
                    "mode": mode,
                    "items": items,
                },
                url=url,
            )]
        except Exception as error:
            last_error = error
    return [_failed_evidence(
        "联网新闻线索",
        last_error or RuntimeError("news search unavailable"),
        source="Bing News RSS",
    )]


def _rss_items(content: bytes) -> list[dict]:
    root = ET.fromstring(content)
    items: list[dict] = []
    for node in root.findall("./channel/item")[:8]:
        link = (node.findtext("link") or "").strip()
        title = _clean_text(node.findtext("title") or "")
        if not title or not link:
            continue
        items.append({
            "title": title,
            "published_at": _clean_text(node.findtext("pubDate") or ""),
            "summary": _clean_text(node.findtext("description") or "")[:700],
            "url": link,
        })
    return items


def _relevant_rss_items(items: list[dict], *, name: str, code: str) -> list[dict]:
    """Discard broad-search false positives before they reach an analyst prompt."""
    needles = [value.casefold() for value in (name, code) if value]
    if not needles:
        return items[:8]
    relevant = []
    for item in items:
        searchable = " ".join(
            str(item.get(key) or "") for key in ("title", "summary", "url")
        ).casefold()
        if any(needle in searchable for needle in needles):
            relevant.append(item)
    return relevant[:8]


def _instrument_name(repo: Any, symbol: str) -> str:
    try:
        frame = repo.get_instruments_asset(repo.resolve_asset_type(symbol))
        if frame.is_empty() or not {"symbol", "name"}.issubset(frame.columns):
            return ""
        row = frame.filter(pl.col("symbol") == symbol)
        return str(row["name"][0]) if not row.is_empty() else ""
    except Exception:
        return ""


def _recent_trading_dates(repo: Any, symbol: str, *, count: int) -> list[str]:
    try:
        end = date.today()
        frame = repo.get_daily_asset(repo.resolve_asset_type(symbol), symbol, end - timedelta(days=30), end)
        if frame.is_empty() or "date" not in frame.columns:
            return []
        return [str(value) for value in frame.tail(count)["date"].to_list()][::-1]
    except Exception:
        return []


def _rows_for_symbol(payload: Any, symbol: str, code: str) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    rows: list[dict] = []
    for key in ("items", "stock_items"):
        for item in payload.get(key) or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("symbol") or "").upper() == symbol or str(item.get("code") or "") == code:
                rows.append(_compact_record(item, max_keys=28))
    return rows[:12]


def _compact_document(value: Any, *, max_text: int) -> dict:
    if not isinstance(value, dict):
        return {"value": _compact_value(value, max_text=max_text)}
    result = _compact_record(value, max_keys=28)
    for key in ("text", "summary", "content"):
        if isinstance(result.get(key), str):
            result[key] = result[key][:max_text]
            if len(str(value.get(key) or "")) > max_text:
                result[f"{key}_truncated"] = True
    return result


def _compact_record(value: dict, *, max_keys: int) -> dict:
    compact: dict[str, Any] = {}
    for key, item in list(value.items())[:max_keys]:
        compact[str(key)] = _compact_value(item, max_text=1_200)
    return json_safe(compact)


def _compact_value(value: Any, *, max_text: int) -> Any:
    if isinstance(value, str):
        return value[:max_text]
    if isinstance(value, dict):
        return {str(key): _compact_value(item, max_text=max_text) for key, item in list(value.items())[:40]}
    if isinstance(value, (list, tuple)):
        return [_compact_value(item, max_text=max_text) for item in list(value)[:40]]
    return json_safe(value)


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", value)).strip()


def _failed_evidence(title: str, error: Exception, *, source: str | None = None) -> dict:
    return evidence(
        source=source or "Quant Workspace",
        title=title,
        status="unavailable",
        summary=f"该数据源本次未返回可用结果({type(error).__name__})。",
        error_type=type(error).__name__,
    )


def _as_number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _mtime_iso(path: Path) -> str:
    from datetime import datetime

    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return ""
