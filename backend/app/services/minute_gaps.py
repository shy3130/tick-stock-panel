"""分钟数据缺口体检: 扫描本地 kline_minute 分区, 找出不完整的 (symbol, 交易日)。

背景 (2026-10 用户反馈): 自选/策略列表的分时列「今天能看、昨天不能看、前天又能看」。
根因是本地分钟分区存在残缺 (全量分钟服务中断导致某段分钟未落盘), 而
/api/kline/minute 把「不完整」等同于「无数据」直接丢弃 → 白屏。
本模块只负责**发现**缺口, 不做补齐 (补齐受上游历史窗口限制, 见 api/kline.py)。

判定口径:
- 完整交易日应为 240 根 (09:30~11:30 + 13:00~15:00, 含 09:30 与 15:00)。
- 实盘落盘常见 241 根 (含 09:30 与 15:00 两个边界) 或 238~241 浮动的少量差异,
  因此与 /api/kline/minute 的 is_complete 同口径: >= expected * 0.9 视为完整。
- 当日 (北京今天) 按已开盘分钟数折算 expected, 避免把盘中正常进度误报为缺口。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)

# 完整交易日标准根数 (与 api/kline.py get_minute 的 expected 基准一致)
FULL_DAY_BARS = 240
# 完整性容差: 与 get_minute 的 0.9 同口径, 避免两处结论互相矛盾
COMPLETE_RATIO = 0.9
# 单次扫描最多返回的缺口行数 (避免全市场缺口清单撑爆响应)
MAX_GAP_ROWS = 2000


@dataclass(frozen=True)
class MinuteGap:
    """一条缺口: 某 symbol 在某交易日只有 actual_bars 根 (期望 expected_bars)。"""

    symbol: str
    trade_date: str
    actual_bars: int
    expected_bars: int
    first_time: str | None
    last_time: str | None

    @property
    def missing_bars(self) -> int:
        return max(0, self.expected_bars - self.actual_bars)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "date": self.trade_date,
            "actual_bars": self.actual_bars,
            "expected_bars": self.expected_bars,
            "missing_bars": self.missing_bars,
            "first_time": self.first_time,
            "last_time": self.last_time,
        }


def _expected_bars_for(trade_date: _date, today: _date, now_hm: tuple[int, int]) -> int:
    """某交易日应有的分钟根数。历史日恒为 240; 当日按已交易分钟折算。"""
    if trade_date != today:
        return FULL_DAY_BARS
    h, m = now_hm
    if h < 9 or (h == 9 and m < 30):
        return 0      # 尚未开盘: 不参与判定
    if h < 11 or (h == 11 and m <= 30):
        return (h - 9) * 60 + m - 30
    if h < 13:
        return 120    # 午休
    if h < 15:
        return 120 + (h - 13) * 60 + m
    return FULL_DAY_BARS


def scan_minute_gaps(
    minute_dir: Path,
    *,
    today: _date,
    now_hm: tuple[int, int],
    symbols: list[str] | None = None,
    dates: list[str] | None = None,
    limit: int = MAX_GAP_ROWS,
) -> dict:
    """扫描分钟分区, 返回缺口清单与按日/按股票的汇总。

    symbols / dates 为可选过滤 (前端点某只股票或某一天时收窄); 都不传 = 全量扫描。
    """
    if not minute_dir.exists():
        return {
            "status": "ok", "scanned_dates": 0, "scanned_symbols": 0,
            "complete_dates": [], "gaps": [], "summary_by_date": [], "truncated": False,
        }

    symbol_set = set(symbols) if symbols else None
    date_set = set(dates) if dates else None

    gaps: list[MinuteGap] = []
    scanned_dates = 0
    scanned_symbols: set[str] = set()
    complete_dates: list[str] = []
    truncated = False

    for part in sorted(minute_dir.glob("date=*/part.parquet")):
        date_str = part.parent.name.removeprefix("date=")
        if date_set and date_str not in date_set:
            continue
        try:
            trade_date = _date.fromisoformat(date_str)
        except ValueError:
            continue
        expected = _expected_bars_for(trade_date, today, now_hm)
        if expected <= 0:
            continue   # 未开盘的当日分区不判定
        try:
            df = pl.read_parquet(part, columns=["symbol", "datetime"])
        except Exception as e:  # noqa: BLE001
            logger.warning("分钟分区读取失败 %s: %s", part, e)
            continue
        scanned_dates += 1
        if df.is_empty():
            complete_dates.append(date_str)
            continue
        if symbol_set is not None:
            df = df.filter(pl.col("symbol").is_in(list(symbol_set)))
            if df.is_empty():
                continue
        scanned_symbols.update(df["symbol"].unique().to_list())

        agg = df.group_by("symbol").agg(
            pl.len().alias("n"),
            pl.col("datetime").min().alias("first"),
            pl.col("datetime").max().alias("last"),
        )
        threshold = expected * COMPLETE_RATIO
        bad = agg.filter(pl.col("n") < threshold)
        if bad.is_empty():
            complete_dates.append(date_str)
            continue
        for row in bad.sort("n").iter_rows(named=True):
            gaps.append(MinuteGap(
                symbol=row["symbol"],
                trade_date=date_str,
                actual_bars=int(row["n"]),
                expected_bars=expected,
                first_time=row["first"].strftime("%H:%M") if row["first"] else None,
                last_time=row["last"].strftime("%H:%M") if row["last"] else None,
            ))
            if len(gaps) >= limit:
                truncated = True
                break
        if truncated:
            break

    # 按日期汇总: 哪天有几只股票残、合计缺多少根
    by_date: dict[str, dict] = {}
    for gap in gaps:
        cell = by_date.setdefault(gap.trade_date, {
            "date": gap.trade_date, "gap_symbols": 0, "missing_bars": 0, "expected_bars": gap.expected_bars,
        })
        cell["gap_symbols"] += 1
        cell["missing_bars"] += gap.missing_bars

    return {
        "status": "ok",
        "scanned_dates": scanned_dates,
        "scanned_symbols": len(scanned_symbols),
        "complete_dates": sorted(complete_dates),
        "gaps": [g.to_dict() for g in gaps],
        "summary_by_date": sorted(by_date.values(), key=lambda c: c["date"]),
        "truncated": truncated,
        "limit": limit,
    }
