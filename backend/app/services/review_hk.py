"""港股复盘数据装配 —— 从 fstore DuckDB 的全市场横截面聚合。

## 为什么港股是独立一套,而不是复用 review_series

港股**没有涨跌停制度**(见 `app/markets.py`: HK 的 `has_price_limit=False`),
所以 A 股复盘的核心读数在港股下**语义不存在**:涨停/跌停/炸板/封板率/连板天梯,
一个都用不了。题材轮动同样落空 —— fstore `base_infos` 里港股的 `tags` 全是空数组,
没有概念映射。

硬把港股塞进 A 股那套分区,结果是一屏恒为 0 的指标,用户会以为数据坏了,
而不是"这个制度不存在"。所以港股走自己的、更薄的分区:市场宽度 + 涨跌榜。

## 数据源与列的硬约束(已实测)

源:fstore `daily_markets`(asset_type=3) 左连 `base_infos` 取名称/板块,
经 `FQuantProvider.get_hk_market_panel()` 取出。覆盖完整(近期每日约 2925 只)。

港股行**只有** price/change_percent/volume/amount 四个行情列有值;
hslv(换手)/zgj(最高)/zdj(最低)/jrkpj(开盘)/zrspj(昨收)/资金流等列**全是 NULL**。
因此港股做不了:换手榜、冲高回落(需高开)、振幅榜。别再去试了。

## 单位

provider 返回的 `change_pct` **已经是百分数**(12.53 = 12.53%),与 A 股 enriched
那条链的小数口径(0.1253)不同。本模块对外统一输出百分数,与 review_series 一致。

公共入口:
    hk_breadth_series(as_of, days)
    hk_movers(as_of, limit)
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

import polars as pl

from app.services.review_series import _cached, _clamp_days, _finite, _ratio

logger = logging.getLogger(__name__)

# 强势/弱势阈值(百分数)。港股无涨跌停,用 ±5% 作为"显著异动"的替代读数。
STRONG_PCT = 5.0


def _hk_panel(as_of: date | None, trading_days: int) -> tuple[pl.DataFrame, list[date], date | None]:
    """取最近 trading_days 个交易日的港股横截面面板。"""
    try:
        from app.data_providers.registry import get_active_provider_name, get_provider

        provider = get_provider(get_active_provider_name("daily"))
        getter = getattr(provider, "get_hk_market_panel", None)
        if getter is None:
            return pl.DataFrame(), [], None
    except Exception:  # noqa: BLE001
        return pl.DataFrame(), [], None

    end = as_of or date.today()
    # 交易日 → 日历天,留足缓冲覆盖港股长假
    start = end - timedelta(days=min(int(trading_days * 1.7) + 15, 220))
    try:
        df = getter(start, end)
    except Exception as e:  # noqa: BLE001
        logger.warning("港股面板取数失败: %s", e)
        return pl.DataFrame(), [], None

    if df is None or df.is_empty() or "date" not in df.columns:
        return pl.DataFrame(), [], None

    if as_of is not None:
        df = df.filter(pl.col("date") <= as_of)
    if df.is_empty():
        return pl.DataFrame(), [], as_of

    # 停牌过滤:与 A 股同一把尺子(无成交且无涨跌幅)
    df = df.filter(
        (pl.col("volume").fill_null(0) > 0) | (pl.col("change_pct").fill_null(0) != 0)
    )
    if df.is_empty():
        return pl.DataFrame(), [], as_of

    dates = sorted(df.get_column("date").unique().to_list())
    keep = dates[-(trading_days + 1):] if len(dates) > trading_days else dates
    df = df.filter(pl.col("date").is_in(keep))
    return df, keep, (as_of or keep[-1])


def _pct_bands(values: list[float]) -> list[dict]:
    """涨跌幅分桶(百分数)。港股无涨跌停,分桶边界比 A 股放宽到 ±7%。"""
    bands = [
        ("<-7%", None, -7.0),
        ("-7~-5%", -7.0, -5.0),
        ("-5~-2%", -5.0, -2.0),
        ("-2~0%", -2.0, 0.0),
        ("0~2%", 0.0, 2.0),
        ("2~5%", 2.0, 5.0),
        ("5~7%", 5.0, 7.0),
        (">7%", 7.0, None),
    ]
    total = len(values) or 1
    out = []
    for label, low, high in bands:
        if low is None:
            count = sum(1 for v in values if v < high)
        elif high is None:
            count = sum(1 for v in values if v >= low)
        else:
            count = sum(1 for v in values if low <= v < high)
        out.append({"label": label, "count": count, "pct": count / total * 100})
    return out


def hk_breadth_series(as_of: date | None = None, days: int = 30) -> dict:
    """港股市场宽度 —— 近 N 个交易日的涨跌家数 / 成交额 / 平均涨幅时序。"""
    days = _clamp_days(days)
    return _cached(("hk_breadth", as_of, days), lambda: _hk_breadth_series(as_of, days))


def _hk_breadth_series(as_of: date | None, days: int) -> dict:
    df, dates, as_of = _hk_panel(as_of, days)
    if df.is_empty():
        return {"as_of": str(as_of) if as_of else None, "days": days, "series": []}

    agg = (
        df.group_by("date")
        .agg([
            pl.len().alias("total"),
            pl.col("amount").fill_null(0).sum().alias("total_amount"),
            (pl.col("change_pct") > 0).sum().alias("up_count"),
            (pl.col("change_pct") < 0).sum().alias("down_count"),
            (pl.col("change_pct") == 0).sum().alias("flat_count"),
            (pl.col("change_pct") >= STRONG_PCT).sum().alias("strong_up"),
            (pl.col("change_pct") <= -STRONG_PCT).sum().alias("strong_down"),
            pl.col("change_pct").mean().alias("avg_change"),
            pl.col("change_pct").median().alias("median_change"),
        ])
        .sort("date")
    )

    series: list[dict] = []
    prev: dict | None = None
    for row in agg.to_dicts():
        total = int(row.get("total") or 0)
        total_amount = _finite(row.get("total_amount"))
        item = {
            "trade_date": str(row["date"]),
            "total": total,
            "total_amount": total_amount,
            "amount_change_rate": _ratio(
                (total_amount or 0) - (_finite(prev.get("total_amount")) or 0) if prev else None,
                _finite(prev.get("total_amount")) if prev else None,
            ),
            "up_count": int(row.get("up_count") or 0),
            "down_count": int(row.get("down_count") or 0),
            "flat_count": int(row.get("flat_count") or 0),
            "up_pct": _ratio(int(row.get("up_count") or 0), total),
            "strong_up": int(row.get("strong_up") or 0),
            "strong_down": int(row.get("strong_down") or 0),
            # provider 已是百分数,直接透传
            "avg_change": _finite(row.get("avg_change")),
            "median_change": _finite(row.get("median_change")),
        }
        series.append(item)
        prev = item

    if len(series) > days:
        series = series[-days:]
    return {
        "as_of": str(as_of),
        "days": days,
        "strong_pct": STRONG_PCT,
        "series": series,
    }


def _brief_rows(frame: pl.DataFrame, limit: int) -> list[dict]:
    out = []
    for row in frame.head(limit).to_dicts():
        out.append({
            "symbol": row.get("symbol"),
            "name": row.get("name"),
            "board": row.get("board") or "",
            "close": _finite(row.get("close")),
            "change_pct": _finite(row.get("change_pct")),
            "amount": _finite(row.get("amount")),
        })
    return out


def hk_movers(as_of: date | None = None, limit: int = 20) -> dict:
    """港股涨跌榜 —— 单日涨幅榜 / 跌幅榜 / 成交额榜 + 板块分布 + 涨跌分布。"""
    limit = max(5, min(int(limit or 20), 100))
    return _cached(("hk_movers", as_of, limit), lambda: _hk_movers(as_of, limit))


def _hk_movers(as_of: date | None, limit: int) -> dict:
    df, dates, as_of = _hk_panel(as_of, 2)
    empty: dict[str, Any] = {
        "as_of": str(as_of) if as_of else None, "trade_date": None,
        "top_gainers": [], "top_losers": [], "top_amount": [],
        "boards": [], "distribution": [],
    }
    if df.is_empty() or not dates:
        return empty

    today = dates[-1]
    cur = df.filter(pl.col("date") == today)
    if cur.is_empty():
        return empty

    has_pct = cur.filter(pl.col("change_pct").is_not_null())

    # 板块分布(港股只有 主板 / 创业板 两类)
    boards = []
    if "board" in cur.columns:
        board_agg = (
            cur.filter(pl.col("board").is_not_null() & (pl.col("board") != ""))
            .group_by("board")
            .agg([
                pl.len().alias("count"),
                (pl.col("change_pct") > 0).sum().alias("up"),
                (pl.col("change_pct") < 0).sum().alias("down"),
                pl.col("amount").fill_null(0).sum().alias("amount"),
                pl.col("change_pct").mean().alias("avg_change"),
            ])
            .sort("amount", descending=True)
        )
        for row in board_agg.to_dicts():
            count = int(row.get("count") or 0)
            boards.append({
                "board": row.get("board"),
                "count": count,
                "up": int(row.get("up") or 0),
                "down": int(row.get("down") or 0),
                "up_pct": _ratio(int(row.get("up") or 0), count),
                "amount": _finite(row.get("amount")),
                "avg_change": _finite(row.get("avg_change")),
            })

    pct_values = [v for v in has_pct.get_column("change_pct").to_list() if v is not None]

    return {
        "as_of": str(as_of),
        "trade_date": str(today),
        "top_gainers": _brief_rows(has_pct.sort("change_pct", descending=True), limit),
        "top_losers": _brief_rows(has_pct.sort("change_pct"), limit),
        "top_amount": _brief_rows(cur.sort("amount", descending=True, nulls_last=True), limit),
        "boards": boards,
        "distribution": _pct_bands(pct_values),
    }
