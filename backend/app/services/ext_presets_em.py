"""Eastmoney ext_data presets beyond the built-in THS seed files."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

import polars as pl

from app.data_providers.fquant.symbols import code_to_symbol
from app.services.ext_data import ExtConfig, ExtField, PullConfig, rows_to_parquet

_DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_REPORT_LIST = "https://reportapi.eastmoney.com/report/list"
_NEWS_SEARCH = "https://search-api-web.eastmoney.com/search/jsonp"


def presets() -> list[ExtConfig]:
    return [
        _lockup_preset(),
        _holder_preset(),
        _margin_preset(),
        _block_preset(),
        _research_preset(),
        _news_preset(),
    ]


def fetcher(config_id: str):
    return {
        "ext_lockup_em": _seed_lockup,
        "ext_holder_em": _seed_holder,
        "ext_margin_em": _seed_margin,
        "ext_block_em": _seed_block,
        "ext_research_em": _seed_research,
        "ext_news_em": _seed_news,
    }.get(config_id)


def _base_preset(config_id: str, label: str, fields: list[ExtField], description: str) -> ExtConfig:
    return ExtConfig(
        id=config_id,
        label=label,
        mode="timeseries",
        fields=fields,
        description=description,
        symbol_map={"type": "mapped", "col": "stock_symbol"},
        code_map={"type": "mapped", "col": "code"},
        pull=PullConfig(url=_DATACENTER, method="GET", schedule_minutes=1440, enabled=False),
    )


def _common_fields(extra: list[ExtField]) -> list[ExtField]:
    return [
        ExtField("uid", "string", "唯一键"),
        ExtField("stock_symbol", "string", "标的代码"),
        ExtField("code", "string", "代码"),
        ExtField("name", "string", "名称"),
        *extra,
    ]


def _lockup_preset() -> ExtConfig:
    return _base_preset(
        "ext_lockup_em",
        "限售解禁",
        _common_fields([
            ExtField("free_date", "string", "解禁日期"),
            ExtField("share_type", "string", "解禁类型"),
            ExtField("free_shares", "float", "解禁股数"),
            ExtField("current_free_shares", "float", "本次解禁股数"),
            ExtField("able_free_shares", "float", "实际可流通股数"),
            ExtField("lift_market_cap", "float", "解禁市值"),
            ExtField("free_ratio", "float", "解禁占流通比%"),
            ExtField("total_ratio", "float", "解禁占总股本比%"),
        ]),
        "东方财富限售解禁日历，默认拉取未来 90 天。",
    )


def _holder_preset() -> ExtConfig:
    return _base_preset(
        "ext_holder_em",
        "股东户数",
        _common_fields([
            ExtField("end_date", "string", "报告期"),
            ExtField("holder_count", "float", "股东户数"),
            ExtField("holder_count_change", "float", "户数变化"),
            ExtField("holder_count_change_pct", "float", "户数变化率%"),
            ExtField("avg_hold_amount", "float", "户均持股市值"),
            ExtField("avg_hold_shares", "float", "户均持股数"),
            ExtField("total_market_cap", "float", "总市值"),
        ]),
        "东方财富股东户数最新披露快照。",
    )


def _margin_preset() -> ExtConfig:
    return _base_preset(
        "ext_margin_em",
        "融资融券",
        _common_fields([
            ExtField("trade_date", "string", "交易日期"),
            ExtField("financing_balance", "float", "融资余额"),
            ExtField("financing_buy", "float", "融资买入额"),
            ExtField("financing_repay", "float", "融资偿还额"),
            ExtField("short_balance", "float", "融券余额"),
            ExtField("short_volume", "float", "融券余量"),
            ExtField("margin_total_balance", "float", "融资融券余额"),
        ]),
        "东方财富融资融券个股明细，默认拉取最新交易日页。",
    )


def _block_preset() -> ExtConfig:
    return _base_preset(
        "ext_block_em",
        "大宗交易",
        _common_fields([
            ExtField("trade_date", "string", "交易日期"),
            ExtField("close_price", "float", "收盘价"),
            ExtField("deal_price", "float", "成交价"),
            ExtField("premium_ratio", "float", "溢价率%"),
            ExtField("deal_volume", "float", "成交量"),
            ExtField("deal_amount", "float", "成交额"),
            ExtField("buyer_seat", "string", "买方营业部"),
            ExtField("seller_seat", "string", "卖方营业部"),
        ]),
        "东方财富大宗交易明细，默认拉取最近 30 天。",
    )


def _research_preset() -> ExtConfig:
    return _base_preset(
        "ext_research_em",
        "研报/EPS",
        _common_fields([
            ExtField("publish_date", "string", "发布日期"),
            ExtField("title", "string", "标题"),
            ExtField("brokerage", "string", "机构"),
            ExtField("analyst", "string", "分析师"),
            ExtField("rating", "string", "评级"),
            ExtField("eps_this_year", "float", "本年EPS预测"),
            ExtField("eps_next_year", "float", "次年EPS预测"),
            ExtField("pe_this_year", "float", "本年PE预测"),
            ExtField("pe_next_year", "float", "次年PE预测"),
        ]),
        "东方财富个股研报和 EPS/PE 预测，自选股范围逐股拉取。",
    )


def _news_preset() -> ExtConfig:
    return _base_preset(
        "ext_news_em",
        "个股新闻",
        _common_fields([
            ExtField("published", "string", "发布时间"),
            ExtField("title", "string", "标题"),
            ExtField("url", "string", "链接"),
            ExtField("source", "string", "来源"),
            ExtField("snippet", "string", "摘要"),
        ]),
        "东方财富个股新闻，自选股范围逐股拉取。",
    )


async def _seed_lockup(config: ExtConfig, data_dir: Path) -> int:
    today = date.today()
    rows = _fetch_datacenter(
        {
            "reportName": "RPT_LIFT_STOCK",
            "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,FREE_DATE,FREE_SHARES_TYPE,FREE_SHARES,CURRENT_FREE_SHARES,ABLE_FREE_SHARES,LIFT_MARKET_CAP,FREE_RATIO,TOTAL_RATIO",
            "filter": f"(FREE_DATE>='{today.isoformat()}')(FREE_DATE<='{(today + timedelta(days=90)).isoformat()}')",
            "sortColumns": "FREE_DATE",
            "sortTypes": "1",
        },
        _flatten_lockup,
    )
    return _write_by_date(rows, config, data_dir, "free_date")


async def _seed_holder(config: ExtConfig, data_dir: Path) -> int:
    rows = _fetch_datacenter(
        {
            "reportName": "RPT_HOLDERNUMLATEST",
            "columns": "SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,END_DATE,HOLDER_NUM,HOLDER_NUM_CHANGE,HOLDER_NUM_RATIO,AVG_HOLD_AMT,AVG_HOLD_NUM,TOTAL_MARKET_CAP",
            "sortColumns": "END_DATE",
            "sortTypes": "-1",
        },
        _flatten_holder,
    )
    return _write_by_date(rows, config, data_dir, "end_date")


async def _seed_margin(config: ExtConfig, data_dir: Path) -> int:
    rows = _fetch_datacenter(
        {
            "reportName": "RPTA_WEB_RZRQ_GGMX",
            "columns": "ALL",
            "source": "WEB",
            "sortColumns": "DATE",
            "sortTypes": "-1",
        },
        _flatten_margin,
        max_pages=3,
    )
    return _write_by_date(rows, config, data_dir, "trade_date")


async def _seed_block(config: ExtConfig, data_dir: Path) -> int:
    end = date.today()
    start = end - timedelta(days=29)
    rows = _fetch_datacenter(
        {
            "reportName": "RPT_DATA_BLOCKTRADE",
            "columns": "TRADE_DATE,SECURITY_CODE,SECURITY_NAME_ABBR,CLOSE_PRICE,DEAL_PRICE,PREMIUM_RATIO,DEAL_VOLUME,DEAL_AMT,BUYER_NAME,SELLER_NAME",
            "filter": f"(TRADE_DATE>='{start.isoformat()}')(TRADE_DATE<='{end.isoformat()}')",
            "sortColumns": "TRADE_DATE",
            "sortTypes": "-1",
        },
        _flatten_block,
    )
    return _write_by_date(rows, config, data_dir, "trade_date")


async def _seed_research(config: ExtConfig, data_dir: Path) -> int:
    rows: list[dict] = []
    for symbol in _watchlist_symbols(data_dir):
        rows.extend(_fetch_research_for_symbol(symbol))
    if not rows:
        raise ValueError("东方财富研报返回 0 行")
    return _write_by_date(rows, config, data_dir, "publish_date")


async def _seed_news(config: ExtConfig, data_dir: Path) -> int:
    rows: list[dict] = []
    for symbol in _watchlist_symbols(data_dir):
        rows.extend(_fetch_news_for_symbol(symbol))
    if not rows:
        raise ValueError("东方财富新闻返回 0 行")
    return _write_by_date(rows, config, data_dir, "published")


def _fetch_datacenter(params: dict, flatten: Callable[[list[dict]], list[dict]], max_pages: int = 20) -> list[dict]:
    from app.services import eastmoney_client

    raw = eastmoney_client.get_datacenter_paged(
        _DATACENTER,
        {**params, "source": params.get("source", "WEB"), "client": params.get("client", "WEB")},
        max_pages=max_pages,
    )
    rows = flatten(raw)
    if not rows:
        raise ValueError(f"东方财富 {params.get('reportName')} 返回 0 行")
    return rows


def _watchlist_symbols(data_dir: Path, limit: int = 50) -> list[str]:
    path = data_dir / "user_data" / "watchlist.parquet"
    if not path.exists():
        raise ValueError("自选股为空，无法拉取逐股 Eastmoney 预设")
    df = pl.read_parquet(path)
    if df.is_empty() or "symbol" not in df.columns:
        raise ValueError("自选股为空，无法拉取逐股 Eastmoney 预设")
    return [str(s) for s in df["symbol"].to_list() if str(s).endswith((".SH", ".SZ", ".BJ"))][:limit]


def _fetch_research_for_symbol(symbol: str, limit: int = 10) -> list[dict]:
    from app.services import eastmoney_client

    code = symbol.split(".", 1)[0]
    payload = eastmoney_client.get_json(
        _REPORT_LIST,
        params={"code": code, "qType": "0", "pageSize": str(limit), "pageNo": "1"},
    )
    raw = payload.get("data") if isinstance(payload, dict) else []
    return _flatten_research(raw if isinstance(raw, list) else [], symbol)


def _fetch_news_for_symbol(symbol: str, limit: int = 10) -> list[dict]:
    from app.services import eastmoney_client

    code = symbol.split(".", 1)[0]
    param = json.dumps(
        {
            "uid": "",
            "keyword": code,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "default",
                    "pageIndex": 1,
                    "pageSize": limit,
                }
            },
        },
        ensure_ascii=False,
    )
    payload = eastmoney_client.get_json(_NEWS_SEARCH, params={"cb": "", "param": param, "_": "0"})
    result = payload.get("result") if isinstance(payload, dict) else None
    raw = result.get("cmsArticleWebOld") if isinstance(result, dict) else []
    return _flatten_news(raw if isinstance(raw, list) else [], symbol)


def _write_by_date(rows: list[dict], config: ExtConfig, data_dir: Path, date_key: str) -> int:
    total = 0
    by_date: dict[str, list[dict]] = {}
    for row in rows:
        day = str(row.get(date_key) or date.today().isoformat())[:10]
        by_date.setdefault(day, []).append(row)
    for day, chunk in by_date.items():
        total += rows_to_parquet(chunk, config, data_dir, snapshot_date=date.fromisoformat(day))
    return total


def _flatten_lockup(raw_rows: list[dict]) -> list[dict]:
    return [_base_row(r, r.get("FREE_DATE"), "lockup") | {
        "free_date": _date(r.get("FREE_DATE")),
        "share_type": r.get("FREE_SHARES_TYPE") or "",
        "free_shares": _num(r.get("FREE_SHARES")),
        "current_free_shares": _num(r.get("CURRENT_FREE_SHARES")),
        "able_free_shares": _num(r.get("ABLE_FREE_SHARES")),
        "lift_market_cap": _num(r.get("LIFT_MARKET_CAP")),
        "free_ratio": _num(r.get("FREE_RATIO")),
        "total_ratio": _num(r.get("TOTAL_RATIO")),
    } for r in raw_rows if r.get("SECURITY_CODE") and r.get("FREE_DATE")]


def _flatten_holder(raw_rows: list[dict]) -> list[dict]:
    return [_base_row(r, r.get("END_DATE"), "holder") | {
        "end_date": _date(r.get("END_DATE")),
        "holder_count": _num(r.get("HOLDER_NUM")),
        "holder_count_change": _num(r.get("HOLDER_NUM_CHANGE")),
        "holder_count_change_pct": _num(r.get("HOLDER_NUM_RATIO")),
        "avg_hold_amount": _num(r.get("AVG_HOLD_AMT")),
        "avg_hold_shares": _num(r.get("AVG_HOLD_NUM")),
        "total_market_cap": _num(r.get("TOTAL_MARKET_CAP")),
    } for r in raw_rows if r.get("SECURITY_CODE") and r.get("END_DATE")]


def _flatten_margin(raw_rows: list[dict]) -> list[dict]:
    rows = []
    for r in raw_rows:
        code = r.get("SCODE") or r.get("SECURITY_CODE")
        trade_date = r.get("DATE")
        if not code or not trade_date:
            continue
        rows.append(_base_row(r | {"SECURITY_CODE": code}, trade_date, "margin") | {
            "trade_date": _date(trade_date),
            "financing_balance": _num(r.get("RZYE")),
            "financing_buy": _num(r.get("RZMRE")),
            "financing_repay": _num(r.get("RZCHE")),
            "short_balance": _num(r.get("RQYE")),
            "short_volume": _num(r.get("RQYL")),
            "margin_total_balance": _num(r.get("RZRQYE")),
        })
    return rows


def _flatten_block(raw_rows: list[dict]) -> list[dict]:
    return [_base_row(r, f"{r.get('TRADE_DATE')}:{r.get('DEAL_PRICE')}:{r.get('DEAL_AMT')}", "block") | {
        "trade_date": _date(r.get("TRADE_DATE")),
        "close_price": _num(r.get("CLOSE_PRICE")),
        "deal_price": _num(r.get("DEAL_PRICE")),
        "premium_ratio": _num(r.get("PREMIUM_RATIO")),
        "deal_volume": _num(r.get("DEAL_VOLUME")),
        "deal_amount": _num(r.get("DEAL_AMT")),
        "buyer_seat": r.get("BUYER_NAME") or "",
        "seller_seat": r.get("SELLER_NAME") or "",
    } for r in raw_rows if r.get("SECURITY_CODE") and r.get("TRADE_DATE")]


def _flatten_research(raw_rows: list[dict], symbol: str) -> list[dict]:
    code = symbol.split(".", 1)[0]
    out = []
    for r in raw_rows:
        publish_date = _date(r.get("publishDate"))
        title = _text(r.get("title"))
        if not publish_date and not title:
            continue
        out.append({
            "uid": f"research:{code}:{r.get('infoCode') or title}:{publish_date}",
            "stock_symbol": symbol,
            "code": code,
            "name": _text(r.get("stockName")),
            "publish_date": publish_date,
            "title": title,
            "brokerage": _text(r.get("orgSName") or r.get("orgName")),
            "analyst": _text(r.get("researcher")),
            "rating": _text(r.get("emRatingName") or r.get("sRatingName")),
            "eps_this_year": _num(r.get("predictThisYearEps")),
            "eps_next_year": _num(r.get("predictNextYearEps")),
            "pe_this_year": _num(r.get("predictThisYearPe")),
            "pe_next_year": _num(r.get("predictNextYearPe")),
        })
    return out


def _flatten_news(raw_rows: list[dict], symbol: str) -> list[dict]:
    code = symbol.split(".", 1)[0]
    out = []
    for r in raw_rows:
        title = _text(r.get("title"))
        published = _date(r.get("date"))
        if not title and not published:
            continue
        out.append({
            "uid": f"news:{code}:{r.get('art_code') or r.get('url') or title}",
            "stock_symbol": symbol,
            "code": code,
            "name": "",
            "published": published,
            "title": title,
            "url": r.get("url") or "",
            "source": _text(r.get("mediaName")),
            "snippet": _text(r.get("content"))[:280],
        })
    return out


def _base_row(r: dict, suffix, kind: str) -> dict:
    code = str(r.get("SECURITY_CODE") or "").strip()
    return {
        "uid": f"{kind}:{code}:{suffix}",
        "stock_symbol": code_to_symbol(code, 1),
        "code": code,
        "name": r.get("SECURITY_NAME_ABBR") or r.get("SNAME") or "",
    }


def _date(value) -> str:
    return str(value or "")[:10]


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value) -> str:
    return " ".join(str(value or "").split())
