"""AI 助手工具层 — 核心工具目录透传 + 面向各页面能力的本地查询工具。

设计原则(为项目服务、只读、有界):
- 每个工具对应一个页面的核心查询能力(行情/自选/看板/指数/板块轮动/市场
  环境/异动/持仓提醒/信号库/财务/个股分析/因子值/跑策略), 全部只读。
- 返回体有界: rows 全部带 limit 钳制, 大表(enriched/财务)先 filter 再取
  少量行; 数值统一四舍五入 4 位, 日期转 ISO 字符串, 保证 JSON 可序列化。
- 未知的工具名回退到核心 services.tool_catalog(因子/策略/数据能力/回测)。

本模块还提供:
- QUICK_SUGGESTS: 空会话快捷指令。
- summarize_tool_result: 工具结果的足迹卡一行摘要(不回传原始大 JSON)。
"""
from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.market_time import cn_today
from app.services import tool_catalog

# ----------------------------------------------------------------
# 快捷指令: label 展示、prompt 为点击后直接发送的完整问题。
# ----------------------------------------------------------------
QUICK_SUGGESTS: list[dict[str, str]] = [
    {
        "id": "market-overview",
        "label": "今天市场怎么样？",  # noqa: RUF001 — 面向用户中文文案含全角问号
        "prompt": "帮我看看今天的市场总览: 涨跌家数、成交额、涨停与连板梯队、情绪雷达, 以及领涨领跌板块。",
    },
    {
        "id": "watchlist-check",
        "label": "我的自选表现如何",
        "prompt": "查一下我的自选股列表, 按今日涨跌幅排序, 标出表现最好和最差的, 并简要点评。",
    },
    {
        "id": "list-strategies",
        "label": "我有哪些策略？",  # noqa: RUF001
        "prompt": "我目前有哪些策略？分别简述每个策略是做什么的、适用什么资产。",  # noqa: RUF001
    },
    {
        "id": "backtest-pick",
        "label": "回测我的策略",
        "prompt": "先用工具看看我有哪些策略, 然后挑一个回测最近半年, 给出收益、回撤、夏普和胜率的解读。",
    },
    {
        "id": "stock-analyze",
        "label": "分析一只个股",
        "prompt": "帮我分析 600519.SH: 结合分时与日K看最新走势, 给出关键价位(支撑/压力)、趋势状态和量价特征。",
    },
    {
        "id": "regime-check",
        "label": "当前市场环境",
        "prompt": "最近一个月市场环境(regime)状态如何演变？今天是强势还是弱势, 情绪周期处于什么阶段？",  # noqa: RUF001
    },
]


# ----------------------------------------------------------------
# 执行上下文 — 由路由从 app.state 注入, 测试可替换为桩对象。
# ----------------------------------------------------------------
@dataclass
class ToolContext:
    repo: Any = None
    quote_service: Any = None
    depth_service: Any = None
    engine: Any = None
    data_dir: Path | None = None

    @classmethod
    def build(
        cls,
        *,
        repo: Any = None,
        quote_service: Any = None,
        depth_service: Any = None,
        engine: Any = None,
        data_dir: str | Path | None = None,
    ) -> ToolContext:
        return cls(
            repo=repo,
            quote_service=quote_service,
            depth_service=depth_service,
            engine=engine,
            data_dir=Path(data_dir) if data_dir else None,
        )


_SYMBOL_RE = re.compile(r"^[0-9A-Za-z._-]{3,16}$")


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _clean_value(value: Any) -> Any:
    """JSON 序列化清洗: 日期转 ISO, 浮点四舍五入并剔除 nan/inf。"""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, 4)
    return value


def _clean_obj(value: Any) -> Any:
    """递归清洗 dict/list 结构(run_strategy 行等动态载荷)。"""
    if isinstance(value, dict):
        return {str(k): _clean_obj(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean_obj(v) for v in value]
    return _clean_value(value)


def _rows(
    df: pl.DataFrame | None,
    limit: int,
    columns: list[str] | None = None,
) -> list[dict[str, Any]]:
    """DataFrame → 有界的 JSON 行列表; 空/缺列安全。"""
    if df is None or df.is_empty():
        return []
    if columns:
        keep = [c for c in columns if c in df.columns]
        if keep:
            df = df.select(keep)
    if df.width > 60:
        df = df.select(df.columns[:60])
    rows: list[dict[str, Any]] = []
    for row in df.head(limit).to_dicts():
        rows.append({key: _clean_value(value) for key, value in row.items()})
    return rows


_QUOTE_COLUMNS = (
    "symbol", "close", "open", "high", "low", "volume", "amount",
    "prev_close", "change_pct", "change_amount", "amplitude", "turnover_rate",
)


def _asset_quote_frame(ctx: ToolContext, symbols: list[str]) -> pl.DataFrame:
    """symbols 的行情快照: 股票取 get_quotes_compat, ETF / 指数取各自的 enriched 缓存。

    get_quotes_compat 只含股票 enriched 缓存; ETF 与指数是独立缓存, 与自选页
    /api/watchlist/enriched 一样须按资产类型分流, 否则 ETF / 指数永远查不到价格。
    只对股票缓存里没有的代码判定资产类型, 全是股票时不加载 ETF / 指数缓存。
    """
    frames: list[pl.DataFrame] = []
    df = ctx.quote_service.get_quotes_compat()
    if df is not None and not df.is_empty() and "symbol" in df.columns:
        frames.append(df.filter(pl.col("symbol").is_in(symbols)))
    found = set(frames[0]["symbol"].to_list()) if frames else set()
    missing = [s for s in symbols if s not in found]
    if missing and ctx.repo is not None:
        by_asset: dict[str, list[str]] = {}
        for symbol in missing:
            asset_type = ctx.repo.resolve_asset_type(symbol)
            if asset_type != "stock":
                by_asset.setdefault(asset_type, []).append(symbol)
        for asset_type, members in by_asset.items():
            asset_df, _ = ctx.repo.get_enriched_latest_asset(asset_type)
            if asset_df is None or asset_df.is_empty() or "symbol" not in asset_df.columns:
                continue
            keep = [c for c in _QUOTE_COLUMNS if c in asset_df.columns]
            frames.append(asset_df.filter(pl.col("symbol").is_in(members)).select(keep))
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="diagonal_relaxed")


def _quotes_map(ctx: ToolContext, symbols: list[str]) -> dict[str, dict[str, Any]]:
    """symbol → {close, change_pct} 轻量行情映射(自选/持仓等合并展示用)。"""
    if ctx.quote_service is None or not symbols:
        return {}
    try:
        df = _asset_quote_frame(ctx, symbols)
    except Exception:  # 行情服务未就绪时静默降级为无价格列
        return {}
    if df is None or df.is_empty() or "symbol" not in df.columns:
        return {}
    keep = [c for c in ("symbol", "close", "change_pct") if c in df.columns]
    out: dict[str, dict[str, Any]] = {}
    for row in df.filter(pl.col("symbol").is_in(symbols)).select(keep).to_dicts():
        out[row["symbol"]] = {k: _clean_value(v) for k, v in row.items() if k != "symbol"}
    return out


def _require_repo(ctx: ToolContext) -> Any:
    if ctx.repo is None:
        raise ValueError("行情数据仓库未就绪, 请确认后端已完成启动。")
    return ctx.repo


def _validate_symbol(value: Any) -> str:
    symbol = str(value or "").strip()
    if not _SYMBOL_RE.fullmatch(symbol):
        raise ValueError(f"无效的证券代码: {symbol!r} (示例: 600519.SH / 300750.SZ)")
    return symbol


# ----------------------------------------------------------------
# 本地查询工具实现 (全部同步, 由 execute_assistant_tool 挪出事件循环)。
# ----------------------------------------------------------------
def _get_stock_quote(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if ctx.quote_service is None:
        raise ValueError("实时行情服务未启用, 无法查询个股快照。")
    raw = args.get("symbols") or []
    if not isinstance(raw, list) or not raw:
        raise ValueError("symbols 不能为空, 请传入证券代码数组 (如 ['600519.SH'])。")
    symbols = list(dict.fromkeys(_validate_symbol(s) for s in raw[:50]))
    sub = _asset_quote_frame(ctx, symbols)
    names = ctx.repo.get_name_map(symbols) if ctx.repo is not None else {}
    rows = _rows(sub, 50)
    for row in rows:
        row.setdefault("name", names.get(row.get("symbol"), ""))
    if not rows:
        return {"count": 0, "rows": [], "note": "未匹配到行情, 请确认代码格式 (如 600519.SH)。"}
    result: dict[str, Any] = {"count": len(rows), "rows": rows}
    # 单只查询时附当日分时小图; 批量查询 N 只附 N 张图会淹没回答, 不附。
    if len(rows) == 1:
        row = rows[0]
        prev_close = row.get("prev_close")
        if prev_close is None and row.get("close") is not None and row.get("change_pct") is not None:
            # 行情快照无昨收列时按 close/(1+pct) 反推, 供分时图画昨收基准线
            prev_close = round(float(row["close"]) / (1.0 + float(row["change_pct"])), 3)
        chart = _intraday_chart_payload(ctx, symbols[0], str(row.get("name") or ""), prev_close)
        if chart is not None:
            result["charts"] = [chart]
    return result


def _finite_float(value: Any) -> float | None:
    """转 float; None / 非数字 / nan / inf 返回 None, 避免写入非法 JSON。"""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _num(v: Any) -> float | None:
    number = _finite_float(v)
    return None if number is None else round(number, 3)


def _intraday_chart_payload(
    ctx: ToolContext, symbol: str, name: str, prev_close: float | None = None,
) -> dict[str, Any] | None:
    """当日分时小图 payload — 本地分钟K收盘序列, 非模型生成(结果可核对)。

    点数封顶 240(一个交易日); 本地无分钟数据时返回 None, 不阻断行情回答。
    """
    if ctx.repo is None:
        return None
    try:
        asset_type = ctx.repo.resolve_asset_type(symbol)
        # 取行情快照所在交易日 (昨收基准线也来自快照行): 周末/节假日/开盘前快照停在
        # 最近交易日, 按服务器本地 date.today() 查分钟分区会永远为空; 无快照日期时按北京日期。
        _, trade_date = ctx.repo.get_enriched_latest_asset(asset_type, refresh=False)
        df = ctx.repo.get_minute(symbol, trade_date or cn_today(), asset_type)
    except Exception:  # noqa: BLE001  分钟分区缺失/损坏时降级为无图
        return None
    if df is None or df.is_empty() or "close" not in df.columns or "datetime" not in df.columns:
        return None
    points: list[list[Any]] = []
    for row in df.sort("datetime").to_dicts():
        dt = row.get("datetime")
        close = _finite_float(row.get("close"))
        if dt is None or close is None:
            continue
        t = dt.strftime("%H:%M") if hasattr(dt, "strftime") else str(dt)[-8:-3]
        volume = _finite_float(row.get("volume"))
        points.append([t, round(close, 3), 0.0 if volume is None else round(volume, 2)])
    if len(points) < 2:
        return None
    payload: dict[str, Any] = {
        "kind": "intraday",
        "symbol": symbol,
        "name": name,
        "points": points[-240:],
    }
    if prev_close is not None:
        payload["prev_close"] = prev_close
    return payload


def _daily_kline_chart_payload(symbol: str, name: str, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """日K 小图 payload — [date, open, high, low, close, volume], 前端渲染蜡烛图。

    点数封顶 120, 避免长区间把 NDJSON 流式事件撑大; 不足 2 根不附图。
    """
    points = [
        [
            str(row["date"]), _num(row.get("open")), _num(row.get("high")),
            _num(row.get("low")), _num(row.get("close")), _num(row.get("volume")),
        ]
        for row in rows
        if row.get("date") is not None and row.get("close") is not None and row.get("open") is not None
    ]
    if len(points) < 2:
        return None
    return {"kind": "daily_kline", "symbol": symbol, "name": name, "points": points[-120:]}


def _get_stock_daily(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    repo = _require_repo(ctx)
    symbol = _validate_symbol(args.get("symbol"))
    days = _clamp(args.get("days"), 10, 500, 60)
    end = cn_today()
    start = end - timedelta(days=int(days * 1.9) + 20)
    df = repo.get_daily_asset(repo.resolve_asset_type(symbol), symbol, start, end)
    if df is None or df.is_empty():
        return {"symbol": symbol, "rows": [], "note": "本地没有该标的的日线数据。"}
    keep = [
        c for c in (
            "date", "open", "high", "low", "close", "volume", "amount",
            "change_pct", "turnover_rate", "amplitude", "ma5", "ma10", "ma20", "ma60",
        ) if c in df.columns
    ]
    rows = _rows(df.select(keep).tail(days), days)
    names = repo.get_name_map([symbol])
    name = names.get(symbol, "")
    result: dict[str, Any] = {
        "symbol": symbol,
        "name": name,
        "count": len(rows),
        "rows": rows,
    }
    kline = _daily_kline_chart_payload(symbol, name, rows)
    if kline is not None:
        result["charts"] = [kline]
    return result


def _get_stock_analysis(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from app.indicators.levels import compute_levels, summarize_levels

    repo = _require_repo(ctx)
    symbol = _validate_symbol(args.get("symbol"))
    end = cn_today()
    df = repo.get_daily_asset(repo.resolve_asset_type(symbol), symbol, end - timedelta(days=500), end)
    if df is None or df.is_empty():
        return {"symbol": symbol, "note": "本地没有该标的的日线数据, 无法分析。"}
    recent = df.tail(120)
    close = float(recent["close"][-1]) if recent.height else None
    levels = compute_levels(recent)
    keep = [
        c for c in (
            "date", "close", "change_pct", "amount", "turnover_rate", "amplitude",
            "ma5", "ma10", "ma20", "ma60", "high_60d", "low_60d",
        ) if c in recent.columns
    ]
    latest_rows = _rows(recent.select(keep).tail(1), 1)
    names = repo.get_name_map([symbol])
    return {
        "symbol": symbol,
        "name": names.get(symbol, ""),
        "close": _clean_value(close),
        "summary": summarize_levels(levels, close),
        "latest": latest_rows[0] if latest_rows else {},
        "levels": _clean_obj(levels),
    }


def _get_financials(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from app.services.financial_sync import FINANCIAL_TABLES, get_financial_df

    symbol = _validate_symbol(args.get("symbol"))
    table = str(args.get("table") or "metrics")
    if table not in FINANCIAL_TABLES:
        raise ValueError(f"table 必须是 {list(FINANCIAL_TABLES)} 之一。")
    periods = _clamp(args.get("periods"), 1, 8, 4)
    if ctx.data_dir is None:
        raise ValueError("数据目录未就绪。")
    df = get_financial_df(ctx.data_dir, table)
    if df is None or df.is_empty():
        return {"symbol": symbol, "table": table, "rows": [], "note": "本地暂无财务数据, 可在数据页同步。"}
    sub = df.filter(pl.col("symbol") == symbol)
    if sub.is_empty():
        return {"symbol": symbol, "table": table, "rows": [], "note": "本地财务数据中没有该标的。"}
    # 本地财务表的报告期列是 period_end; 物理行序取决于同步路径, 须显式排序后再取最近 N 期
    sort_col = next((c for c in ("period_end", "report_date", "end_date", "ann_date", "date") if c in sub.columns), None)
    if sort_col:
        sub = sub.sort(sort_col)
    sub = sub.tail(periods)
    if sub.height:
        nulls = dict(zip(sub.columns, sub.null_count().row(0), strict=True))
        keep = [c for c in sub.columns if nulls.get(c, 0) < sub.height]
        if keep:
            sub = sub.select(keep)
    rows = _rows(sub, periods)
    return {"symbol": symbol, "table": table, "count": len(rows), "rows": rows}


def _get_watchlist(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from app.services import watchlist as watchlist_service

    items = watchlist_service.list_symbols()
    if not items:
        return {"count": 0, "rows": [], "note": "自选列表为空。"}
    symbols = [str(i.get("symbol") or "") for i in items if i.get("symbol")]
    quotes = _quotes_map(ctx, symbols)
    names = ctx.repo.get_name_map(symbols) if ctx.repo is not None else {}
    rows = []
    for item in items[:300]:
        symbol = str(item.get("symbol") or "")
        quote = quotes.get(symbol, {})
        rows.append({
            "symbol": symbol,
            "name": names.get(symbol, ""),
            "note": item.get("note") or "",
            "close": quote.get("close"),
            "change_pct": quote.get("change_pct"),
        })
    return {"count": len(rows), "rows": rows}


def _get_market_overview(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from app.services.market_overview_builder import build_market_overview

    repo = _require_repo(ctx)
    return build_market_overview(repo, quote_service=ctx.quote_service, depth_service=ctx.depth_service)


def _get_indices(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if ctx.quote_service is None:
        raise ValueError("实时行情服务未启用, 无法查询指数。")
    df = ctx.quote_service.get_index_quotes()
    rows = _rows(df, 60)
    if not rows:
        return {"count": 0, "rows": [], "note": "指数实时缓存为空。"}
    return {"count": len(rows), "rows": rows}


def _get_sector_rotation(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from app.services.sector_rotation import build_sector_rotation

    repo = _require_repo(ctx)
    kind = str(args.get("kind") or "concept")
    if kind not in {"concept", "industry"}:
        raise ValueError("kind 必须是 concept 或 industry。")
    top = _clamp(args.get("top"), 10, 50, 20)
    data = build_sector_rotation(repo, kind=kind, top=top)
    status = data.get("status")
    if status in {"no_data", "empty"}:
        return {"status": status, "reason": data.get("reason", "")}
    sectors = data.get("sectors")
    cross = data.get("cross_events")
    return {
        "kind": kind,
        "status": status,
        "sectors": sectors[:top] if isinstance(sectors, list) else sectors,
        "cross_events": cross[:20] if isinstance(cross, list) else cross,
        "note": "已省略时间序列矩阵, 只保留板块榜与切换事件。",
    }


def _get_regime(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from app.services.regime_builder import STATE_LABELS, load_regime_history

    if ctx.data_dir is None:
        raise ValueError("数据目录未就绪。")
    days = _clamp(args.get("days"), 1, 120, 30)
    df = load_regime_history(ctx.data_dir)
    if df is None or df.is_empty():
        return {"count": 0, "rows": [], "note": "本地暂无市场环境(regime)数据。"}
    keep = [
        c for c in (
            "date", "state", "score", "phase", "max_consecutive",
            "first_board", "ge2_count", "promo_rate", "seal_rate",
        ) if c in df.columns
    ]
    sub = df.select(keep).sort("date", descending=True).head(days)
    rows = _rows(sub, days)
    for row in rows:
        state = row.get("state")
        if state:
            row["state_label"] = STATE_LABELS.get(str(state), str(state))
    return {"count": len(rows), "rows": rows}


def _get_abnormal(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from app.services.abnormal_moves import build_intraday, build_overview

    repo = _require_repo(ctx)
    kind = str(args.get("kind") or "intraday")
    if kind not in {"intraday", "overview"}:
        raise ValueError("kind 必须是 intraday 或 overview。")
    limit = _clamp(args.get("limit"), 10, 200, 50)
    if kind == "intraday":
        data = build_intraday(repo, limit=limit)
        rows = data.get("rows") or []
        return {"kind": kind, "cache_date": data.get("cache_date"), "counts": data.get("counts"), "rows": rows[:limit]}
    data = build_overview(repo, ctx.quote_service, limit=limit)
    rows = data.get("rows") or []
    return {"kind": kind, "rows": rows[:limit]}


def _get_lots(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from app.strategy.lots import load_all

    if ctx.data_dir is None:
        raise ValueError("数据目录未就绪。")
    lots = load_all(ctx.data_dir)
    if not lots:
        return {"count": 0, "rows": [], "note": "暂无持仓提醒记录。"}
    symbols = [str(lot.get("symbol") or "") for lot in lots if lot.get("symbol")]
    quotes = _quotes_map(ctx, symbols)
    names = ctx.repo.get_name_map(symbols) if ctx.repo is not None else {}
    rows = []
    for lot in lots[:100]:
        symbol = str(lot.get("symbol") or "")
        quote = quotes.get(symbol, {})
        rows.append({
            "symbol": symbol,
            "name": names.get(symbol, ""),
            "qty": lot.get("qty"),
            "cost_price": lot.get("cost_price"),
            "buy_date": _clean_value(lot.get("buy_date")),
            "target_pct": lot.get("target_pct"),
            "stop_pct": lot.get("stop_pct"),
            "close": quote.get("close"),
            "change_pct": quote.get("change_pct"),
        })
    return {"count": len(rows), "rows": rows}


def _list_signals(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from app.strategy.custom_signals import load_all

    if ctx.data_dir is None:
        raise ValueError("数据目录未就绪。")
    rows = [_clean_obj(r) for r in load_all(ctx.data_dir)[:100]]
    return {"count": len(rows), "rows": rows}


def _run_strategy(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from app.services.screener import ScreenerService
    from app.strategy.config import load_override

    repo = _require_repo(ctx)
    if ctx.engine is None:
        raise ValueError("策略引擎未就绪。")
    strategy_id = str(args.get("strategy_id") or "").strip()
    if not strategy_id:
        raise ValueError("strategy_id 不能为空, 可先用 list_strategies 查看。")
    svc = ScreenerService(repo)
    as_of = svc.latest_date()
    if as_of is None:
        raise ValueError("本地暂无行情数据, 无法执行策略。")
    overrides = load_override(ctx.data_dir, strategy_id) or {} if ctx.data_dir else {}
    context = svc.build_strategy_context(
        ctx.engine, as_of, [strategy_id], overrides_map={strategy_id: overrides},
    )
    result = ctx.engine.run(strategy_id, context, overrides=overrides or None)
    rows = [_clean_obj(r) for r in (getattr(result, "rows", None) or [])][:30]
    return {
        "strategy_id": strategy_id,
        "as_of": _clean_value(getattr(result, "as_of", as_of)),
        "total": int(getattr(result, "total", 0) or 0),
        "elapsed_ms": round(float(getattr(result, "elapsed_ms", 0.0) or 0.0)),
        "rows": rows,
    }


def _get_factor_values(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    repo = _require_repo(ctx)
    factor = str(args.get("factor") or "").strip()
    if not factor:
        raise ValueError("factor 不能为空, 可先用 list_factors 查看可用因子 id。")
    top = _clamp(args.get("top"), 5, 50, 30)
    descending = str(args.get("direction") or "desc") != "asc"
    df, as_of = repo.get_enriched_latest()
    if df is None or df.is_empty():
        raise ValueError("本地暂无行情数据, 无法读取因子值。")
    if factor not in df.columns:
        raise ValueError(f"因子 {factor} 不在物化列中, 请先用 list_factors 确认因子 id。")
    keep = [c for c in ("symbol", factor, "change_pct") if c in df.columns]
    sub = (
        df.select(keep)
        .drop_nulls(subset=[factor])
        .sort(factor, descending=descending)
        .head(top)
    )
    names = repo.get_name_map(sub["symbol"].to_list()) if sub.height else {}
    rows = _rows(sub, top)
    for row in rows:
        row.setdefault("name", names.get(row.get("symbol"), ""))
    return {"factor": factor, "as_of": _clean_value(as_of), "count": len(rows), "rows": rows}


_LOCAL_TOOLS: dict[str, Callable[[dict[str, Any], ToolContext], Any]] = {
    "get_stock_quote": _get_stock_quote,
    "get_stock_daily": _get_stock_daily,
    "get_stock_analysis": _get_stock_analysis,
    "get_financials": _get_financials,
    "get_watchlist": _get_watchlist,
    "get_market_overview": _get_market_overview,
    "get_indices": _get_indices,
    "get_sector_rotation": _get_sector_rotation,
    "get_regime": _get_regime,
    "get_abnormal": _get_abnormal,
    "get_lots": _get_lots,
    "list_signals": _list_signals,
    "run_strategy": _run_strategy,
    "get_factor_values": _get_factor_values,
}


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def _local_tool_schemas() -> list[dict[str, Any]]:
    return [
        _schema(
            "get_stock_quote",
            "查询一只或多只股票(≤50)的实时行情快照: 最新价、涨跌幅、成交额、换手率、振幅等。",
            {
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "证券代码数组, 如 ['600519.SH', '300750.SZ']",
                },
            },
            ["symbols"],
        ),
        _schema(
            "get_stock_daily",
            "查询个股日线行情(近 N 个交易日, ≤500): OHLCV、涨跌幅、换手率与常用均线。",
            {
                "symbol": {"type": "string", "description": "证券代码, 如 600519.SH"},
                "days": {"type": "integer", "description": "返回最近 N 个交易日 (10-500, 默认 60)"},
            },
            ["symbol"],
        ),
        _schema(
            "get_stock_analysis",
            "个股分析: 基于 120 日日线计算 11 类关键价位(支撑/压力/枢轴/布林等)与文字摘要, 附最新行情快照。",
            {"symbol": {"type": "string", "description": "证券代码"}},
            ["symbol"],
        ),
        _schema(
            "get_financials",
            "查询个股财务报表数据(本地已同步的最近几个报告期)。table: metrics(核心指标)/income(利润)/balance_sheet(资产负债)/cash_flow(现金流)/shares(股本)。",
            {
                "symbol": {"type": "string", "description": "证券代码"},
                "table": {"type": "string", "enum": ["metrics", "income", "balance_sheet", "cash_flow", "shares"], "description": "报表类型, 默认 metrics"},
                "periods": {"type": "integer", "description": "最近 N 个报告期 (1-8, 默认 4)"},
            },
            ["symbol"],
        ),
        _schema(
            "get_watchlist",
            "查询用户的自选股列表(含备注), 并合并最新价与今日涨跌幅, 用于回答「我的自选表现如何」。",
            {},
            [],
        ),
        _schema(
            "get_market_overview",
            "市场总览(看板首页口径): 指数、涨跌家数、成交额、涨停/连板梯队、涨跌幅分布、均线趋势占比、情绪雷达与领涨领跌/活跃榜。无参数。",
            {},
            [],
        ),
        _schema(
            "get_indices",
            "查询指数实时行情表(上证/深证/创业板/科创50 等), 返回点位与涨跌幅。",
            {},
            [],
        ),
        _schema(
            "get_sector_rotation",
            "盘中板块轮动(概念/行业分析页口径): 活跃板块榜、板块切换事件与资金流向排名。",
            {
                "kind": {"type": "string", "enum": ["concept", "industry"], "description": "板块维度, 默认 concept"},
                "top": {"type": "integer", "description": "榜单数量 (10-50, 默认 20)"},
            },
            [],
        ),
        _schema(
            "get_regime",
            "市场环境(regime)时序: 最近 N 日的强弱状态、情绪分、连板高度、晋级率/封板率与情绪周期阶段。",
            {"days": {"type": "integer", "description": "最近 N 个交易日 (1-120, 默认 30)"}},
            [],
        ),
        _schema(
            "get_abnormal",
            "异动监控: intraday=盘中异动(涨停/炸板/跌停/新高新低/放量), overview=交易所异动规则偏离总览。",
            {
                "kind": {"type": "string", "enum": ["intraday", "overview"], "description": "默认 intraday"},
                "limit": {"type": "integer", "description": "返回行数上限 (10-200, 默认 50)"},
            },
            [],
        ),
        _schema(
            "get_lots",
            "查询用户的持仓提醒(lots): 持仓数量、成本、目标/止损幅度, 合并最新价与今日涨跌幅。",
            {},
            [],
        ),
        _schema(
            "list_signals",
            "检索用户自定义信号的目录(信号库页): 名称、表达式、分组与启用状态。",
            {},
            [],
        ),
        _schema(
            "run_strategy",
            "以最新交易日报酬面执行一个选股策略并返回命中标的列表(≤30 行)。strategy_id 可先用 list_strategies 查询。",
            {"strategy_id": {"type": "string", "description": "策略 id"}},
            ["strategy_id"],
        ),
        _schema(
            "get_factor_values",
            "查询某因子在最新交易日的全市场取值排名(Top N), 附股票名称与今日涨跌幅。factor 用因子 id, 可先用 list_factors 查询。",
            {
                "factor": {"type": "string", "description": "因子 id"},
                "top": {"type": "integer", "description": "返回前 N 名 (5-50, 默认 30)"},
                "direction": {"type": "string", "enum": ["desc", "asc"], "description": "排序方向, 默认 desc(从大到小)"},
            },
            ["factor"],
        ),
    ]


def build_tool_schemas() -> list[dict[str, Any]]:
    """核心工具目录(因子/策略/能力/回测) + 本模块的页面查询工具。"""
    return [*tool_catalog.build_tool_schemas(), *_local_tool_schemas()]


async def execute_assistant_tool(name: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """统一执行入口: 本地工具(to_thread 防阻塞) → 核心工具目录回退。

    契约与核心 tool_catalog.execute_tool 一致: {"ok": bool, "result"|"error"}。
    """
    handler = _LOCAL_TOOLS.get(name)
    if handler is not None:
        try:
            result = await asyncio.to_thread(handler, dict(args or {}), ctx)
            return {"ok": True, "result": result}
        except Exception as exc:  # 工具失败按契约回填给模型, 不打断对话流
            return {"ok": False, "error": str(exc) or exc.__class__.__name__}
    return await tool_catalog.execute_tool(
        name,
        args,
        engine=ctx.engine,
        data_dir=str(ctx.data_dir) if ctx.data_dir else None,
    )


def summarize_tool_result(name: str, payload: dict[str, Any]) -> str:
    """把工具返回压缩成一行人类可读摘要, 用于前端足迹卡。"""
    if not payload.get("ok"):
        error = str(payload.get("error") or "执行失败")
        return f"失败: {error[:80]}"

    result = payload.get("result")
    if not isinstance(result, dict):
        return "完成"

    def _count(key: str = "rows") -> int:
        rows = result.get(key)
        return len(rows) if isinstance(rows, list) else int(result.get("count") or 0)

    if name == tool_catalog.LIST_FACTORS:
        factors = result.get("factors")
        return f"返回 {len(factors) if isinstance(factors, list) else 0} 个因子"
    if name == tool_catalog.LIST_STRATEGIES:
        strategies = result.get("strategies")
        return f"返回 {len(strategies) if isinstance(strategies, list) else 0} 个策略"
    if name == tool_catalog.LIST_DATA_CAPABILITIES:
        caps = result.get("capabilities")
        return f"返回 {len(caps) if isinstance(caps, list) else 0} 项能力"
    if name == tool_catalog.RUN_BACKTEST:
        stats = result.get("stats")
        if not isinstance(stats, dict) or not stats:
            return "回测完成(无指标)"
        return " · ".join(str(p) for p in (
            f"总收益 {stats.get('total_return', '-')}",
            f"年化 {stats.get('annual_return', '-')}",
            f"回撤 {stats.get('max_drawdown', '-')}",
            f"夏普 {stats.get('sharpe', '-')}",
        ))
    if name == "get_stock_quote":
        return f"查询 {_count()} 只个股实时行情"
    if name == "get_stock_daily":
        return f"返回 {_count()} 个交易日日线"
    if name == "get_stock_analysis":
        return "完成个股关键价位分析"
    if name == "get_financials":
        return f"返回 {result.get('table', '财务')} 近 {_count()} 期"
    if name == "get_watchlist":
        return f"返回自选 {_count()} 只"
    if name == "get_market_overview":
        breadth = result.get("breadth") or {}
        limit = (result.get("limit") or {}).get("limit_up", "-")
        return f"总览: 涨 {breadth.get('up', '-')} / 跌 {breadth.get('down', '-')} · 涨停 {limit}"
    if name == "get_indices":
        return f"返回 {_count()} 条指数行情"
    if name == "get_sector_rotation":
        return "完成板块轮动查询"
    if name == "get_regime":
        rows = result.get("rows") or []
        latest = rows[0].get("state_label") if rows else "-"
        return f"返回 {_count()} 日 regime, 最新 {latest}"
    if name == "get_abnormal":
        return f"返回异动 {_count()} 行"
    if name == "get_lots":
        return f"返回持仓提醒 {_count()} 条"
    if name == "list_signals":
        return f"返回 {_count()} 个信号定义"
    if name == "run_strategy":
        return f"命中 {result.get('total', 0)} 只 · 耗时 {result.get('elapsed_ms', '-')}ms"
    if name == "get_factor_values":
        return f"返回因子 {result.get('factor', '-')} Top{_count()}"

    return f"返回 {_count()} 行" if _count() else "完成"
