"""竞价排行的后续收益评估。只读日 K, 不把收益写回特征。"""

from __future__ import annotations

from datetime import date

import polars as pl

from app.auction.contracts import parse_trade_date
from app.auction.service import AuctionHubService
from app.config import settings
from app.parquet import scan_enriched_parquet


class AuctionResearchService:
    def __init__(self, hub: AuctionHubService) -> None:
        self.hub = hub

    def run(
        self,
        *,
        trade_date: str | date,
        as_of_ms: int | None = None,
        style: str = "momentum",
        limit: int = 20,
        horizons: list[int] | None = None,
    ) -> dict:
        day = parse_trade_date(trade_date)
        ranked = self.hub.rankings(
            trade_date=day, as_of_ms=as_of_ms, style=style, limit=limit
        )
        symbols = [row["symbol"] for row in ranked["rows"]]
        windows = horizons or [1, 5, 10, 20]
        closes = _close_map(symbols)
        metrics: dict[str, float | None] = {}
        for horizon in windows:
            rets: list[float] = []
            hits = 0
            for symbol in symbols:
                series = closes.get(symbol) or []
                ret = _forward_return(series, day, horizon)
                if ret is None:
                    continue
                rets.append(ret)
                if ret > 0:
                    hits += 1
            metrics[f"coverage_{horizon}d"] = (len(rets) / len(symbols)) if symbols else 0.0
            metrics[f"average_return_{horizon}d"] = (
                sum(rets) / len(rets) if rets else None
            )
            metrics[f"hit_rate_{horizon}d"] = (hits / len(rets)) if rets else None
        return {
            "trade_date": day.isoformat(),
            "as_of_ms": ranked["as_of_ms"],
            "style": style,
            "symbols": symbols,
            "metrics": metrics,
        }


def _close_map(symbols: list[str]) -> dict[str, list[tuple[date, float]]]:
    if not symbols:
        return {}
    # 用 enriched 前复权 close (CONTRIBUTING §3.2): 跨除权日的后续收益不被除权跳变污染。
    # 缺失 enriched 时才降级到不复权 kline_daily close, 并在结果中无法区分 (罕见)。
    data_dir = settings.data_dir / "kline_daily_enriched"
    if not data_dir.exists():
        return {}
    try:
        frame = (
            scan_enriched_parquet(str(data_dir / "**/*.parquet"))
            .filter(pl.col("symbol").is_in(symbols))
            .select(["symbol", "date", "close"])
            .collect()
        )
    except Exception:
        return {}
    out: dict[str, list[tuple[date, float]]] = {}
    for row in frame.to_dicts():
        if row.get("close") is None:
            continue
        out.setdefault(row["symbol"], []).append((row["date"], float(row["close"])))
    for series in out.values():
        series.sort(key=lambda item: item[0])
    return out


def _forward_return(
    series: list[tuple[date, float]],
    trade_date: date,
    horizon: int,
) -> float | None:
    dates = [item[0] for item in series]
    try:
        index = dates.index(trade_date)
    except ValueError:
        return None
    target = index + horizon
    if target >= len(series):
        return None
    start = series[index][1]
    end = series[target][1]
    if not start:
        return None
    return end / start - 1.0
