"""Eastmoney ext_data presets beyond the built-in THS seed files."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from app.data_providers.fquant.symbols import code_to_symbol
from app.services.ext_data import ExtConfig, ExtField, PullConfig, rows_to_parquet

_DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def presets() -> list[ExtConfig]:
    return [_lockup_preset(), _holder_preset(), _margin_preset(), _block_preset()]


def fetcher(config_id: str):
    return {
        "ext_lockup_em": _seed_lockup,
        "ext_holder_em": _seed_holder,
        "ext_margin_em": _seed_margin,
        "ext_block_em": _seed_block,
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
