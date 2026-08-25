"""复盘数据序列装配 —— 从 DuckDB enriched 面板聚合多日复盘指标。

对齐 `../fquant` 复盘模块的分区语义(情绪周期 / 连板天梯 / 题材轮动 / 风险线索),
但数据源换成本项目的 enriched parquet + repo 内存缓存,不引入新的上游依赖:

    kline_daily_enriched(parquet) → repo 300 日内存缓存(含指标/信号)
        → ScreenerService._load_enriched_history() → 本模块 group_by(date) → API

口径约定(全模块统一,API 输出即此口径):
  - enriched 的 `change_pct` 是**小数**(0.05 = 5%),本模块对外一律输出**百分数**(5.0)。
  - 涨停口径对齐 `market_overview_builder`: `signal_limit_up` 或 `consecutive_limit_ups > 0`。
  - 停牌过滤: `volume == 0` 且 `change_pct == 0` 的行剔除(与总览页同一把尺子)。
  - 晋级率按**板层分布跨日派生**(今日 N+1 板数 / 昨日 N 板数),不做个股连板配对,
    因此它是"梯队整体晋级强度",而非"某只票是否晋级"。

公共入口:
    emotion_series(repo, as_of, days)
    ladder_series(repo, as_of, days)
    theme_rotation(repo, as_of, days, top)
    review_clues(repo, as_of, limit)
"""
from __future__ import annotations

import logging
import math
import time
from datetime import date, timedelta
from typing import Any, Callable

import polars as pl

from app.services.market_overview_builder import symbol_dimension_map
from app.services.screener import ScreenerService

logger = logging.getLogger(__name__)

# enriched 内存缓存覆盖约 300 日历天,warmup 校验会吃掉一部分,
# 留出余量后可安全回扫的交易日上限。
MAX_TRADING_DAYS = 90
HIGH_BOARD_FROM = 6  # >=6 板归入"高标"

# 题材轮动的噪声标签 —— 交易机制/互联互通/指数成分类"伪题材"。
#
# 它们描述的是**这只票能不能被买**,不是**资金在炒什么**:同花顺概念表里
# 「融资融券」覆盖 68% 的股票、「深股通」34%、「沪股通」30%,任何一天的涨停股
# 里它们都排前三,会把真正的主线(芯片/机器人/…)挤出 Top N,矩阵就没法看主线切换了。
#
# 注意不能改用"覆盖率阈值"来自动过滤:「国企改革」覆盖 26% 但是**真题材**,
# 会被一并误杀。只能按名单排除。
# 作用于本模块的题材归集与线索表概念标签(线索表只展示 3 个概念,不该被这类标签占位);
# 不影响概念分析页等处对原始 ext_data 的展示。
NON_THEME_TAGS = frozenset({
    "融资融券", "沪股通", "深股通", "转融券标的",
    "MSCI中国", "富时罗素", "标普道琼斯",
})


# ================================================================
# 结果缓存
#
# 单次分区聚合要在 300 日 enriched 缓存(~百万行)上做 filter + group_by,
# 实测 ~600ms;题材/线索还要重扫一遍 ext_data parquet 建维度映射。
# 复盘是盘后语义、同一交易日内结果不变, 故按 (分区, 参数) 做 TTL 缓存,
# 数据刷新时由 invalidate_review_cache() 清空(对齐 overview 的做法)。
# ================================================================

_CACHE_TTL = 300.0
_cache: dict[tuple, tuple[float, Any]] = {}


def invalidate_review_cache() -> None:
    """清空复盘分区结果缓存。清除/重拉数据后调用。"""
    _cache.clear()


def _cached(key: tuple, build: Callable[[], Any]) -> Any:
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None:
        ts, value = hit
        if now - ts < _CACHE_TTL:
            return value
        del _cache[key]
    value = build()
    _cache[key] = (now, value)
    # 参数组合有限(分区 × days × top),不会无界增长;仍设上限兜底
    if len(_cache) > 64:
        for stale in [k for k, (ts, _) in _cache.items() if now - ts >= _CACHE_TTL]:
            _cache.pop(stale, None)
    return value


def _dimension_map(repo, kind: str, level: int | None = None) -> dict[str, list[str]]:
    """带缓存的 symbol → 概念/行业 映射(底层每次调用都要重扫 ext parquet)。"""
    return _cached(("dim", kind, level), lambda: symbol_dimension_map(repo, kind, level))


# ================================================================
# 通用工具
# ================================================================

def _finite(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _pct(v: Any) -> float | None:
    """小数涨跌幅 → 百分数。"""
    f = _finite(v)
    return None if f is None else f * 100


def _ratio(numerator: float | int | None, denominator: float | int | None) -> float | None:
    """晋级率/封板率等比值 → 百分数;分母为 0 时返回 None(而非 0,以示"无样本")。"""
    n = _finite(numerator)
    d = _finite(denominator)
    if n is None or d is None or d == 0:
        return None
    return n / d * 100


def _clamp_days(days: int) -> int:
    return max(2, min(int(days or 20), MAX_TRADING_DAYS))


_REVIEW_HISTORY_COLUMNS = [
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "raw_close",
    "raw_high",
    "raw_low",
    "turnover_rate",
    "consecutive_limit_ups",
    "consecutive_limit_downs",
    "prev_close",
    "change_pct",
]


def _load_review_history(repo, start: date, end: date) -> pl.DataFrame:
    """只计算复盘需要的价变与涨跌停列，避免构造整套技术指标。"""
    df = repo.get_enriched_range(
        start,
        end,
        columns=_REVIEW_HISTORY_COLUMNS,
    )
    if df is None or df.is_empty():
        return pl.DataFrame()

    stored_names = (
        "consecutive_limit_ups",
        "consecutive_limit_downs",
    )
    for name in stored_names:
        stored = f"_stored_{name}"
        if name in df.columns:
            df = df.rename({name: stored})
        else:
            df = df.with_columns(pl.lit(0).cast(pl.UInt32).alias(stored))

    instruments = repo.get_instruments()
    if instruments is None or instruments.is_empty() or "symbol" not in instruments.columns:
        instruments = pl.DataFrame(schema={"symbol": pl.Utf8})

    from app.indicators.pipeline import compute_limit_signals

    df = compute_limit_signals(df.sort(["symbol", "date"]), instruments)
    # 名称/行业等展示字段来自 instruments JOIN: 快速路径列集不含 name,
    # 需显式补齐供 _brief / 题材龙头展示
    if "name" not in df.columns and "name" in instruments.columns:
        df = df.join(
            instruments.select(["symbol", "name"]).unique(subset=["symbol"], keep="first"),
            on="symbol",
            how="left",
        )
    df = df.drop([name for name in stored_names if name in df.columns]).rename(
        {f"_stored_{name}": name for name in stored_names}
    )
    return df.with_columns(
        (pl.col("consecutive_limit_ups").fill_null(0) > 0).alias(
            "signal_limit_up"
        ),
        (pl.col("consecutive_limit_downs").fill_null(0) > 0).alias(
            "signal_limit_down"
        ),
    )


def _load_window(repo, as_of: date | None, trading_days: int) -> tuple[pl.DataFrame, list[date], date | None]:
    """加载最近 `trading_days` 个交易日的 enriched 面板。

    返回 (df, dates, as_of)。df 已做停牌过滤并附带派生列;dates 升序。
    数据不可用时返回空 DataFrame。
    """
    svc = ScreenerService(repo)
    as_of = as_of or svc.latest_date()
    if not as_of:
        return pl.DataFrame(), [], None

    # 交易日 → 日历天(约 1 : 1.5),再留 10 天缓冲覆盖长假
    calendar_days = min(int(trading_days * 1.6) + 10, 200)
    start = as_of - timedelta(days=calendar_days)
    df = _load_review_history(repo, start, as_of)
    if df.is_empty() or "date" not in df.columns:
        return pl.DataFrame(), [], as_of

    df = df.filter(pl.col("date") <= as_of)
    if df.is_empty():
        return pl.DataFrame(), [], as_of

    # 停牌过滤(对齐 market_overview_builder)
    if "volume" in df.columns and "change_pct" in df.columns:
        df = df.filter(
            (pl.col("volume").fill_null(0) > 0) | (pl.col("change_pct").fill_null(0) != 0)
        )

    dates = sorted(df.get_column("date").unique().to_list())
    # 只保留窗口内最后 N 个交易日(多算一天以支撑"较前日"类派生指标)
    keep = dates[-(trading_days + 1):] if len(dates) > trading_days else dates
    df = df.filter(pl.col("date").is_in(keep))
    return df, keep, as_of


def _with_flags(df: pl.DataFrame) -> pl.DataFrame:
    """补齐聚合需要的布尔/数值派生列,缺列时以中性值兜底。"""
    def col_or(name: str, default: Any) -> pl.Expr:
        return pl.col(name).fill_null(default) if name in df.columns else pl.lit(default)

    consec_up = col_or("consecutive_limit_ups", 0)
    return df.with_columns([
        # 涨停 = 信号为真 或 连板数 > 0(与总览页同口径)
        (col_or("signal_limit_up", False) | (consec_up > 0)).alias("_is_limit_up"),
        col_or("signal_limit_down", False).alias("_is_limit_down"),
        col_or("signal_broken_limit_up", False).alias("_is_broken"),
        consec_up.alias("_boards"),
        col_or("change_pct", 0.0).alias("_chg"),
    ])


# ================================================================
# 逐日聚合 —— 情绪周期与连板天梯的共同底表
# ================================================================

def _daily_agg(df: pl.DataFrame) -> list[dict]:
    """一次 group_by(date) 产出逐日复盘指标(情绪周期 + 天梯共用)。"""
    if df.is_empty():
        return []

    df = _with_flags(df)
    amount = pl.col("amount").fill_null(0) if "amount" in df.columns else pl.lit(0.0)

    agg = (
        df.group_by("date")
        .agg([
            amount.sum().alias("total_amount"),
            (pl.col("_chg") > 0).sum().alias("up_count"),
            (pl.col("_chg") < 0).sum().alias("down_count"),
            (pl.col("_chg") == 0).sum().alias("flat_count"),
            (pl.col("_chg") <= -0.07).sum().alias("down_more_than_7_count"),
            pl.col("_is_limit_up").sum().alias("limit_up_count"),
            pl.col("_is_limit_down").sum().alias("limit_down_count"),
            pl.col("_is_broken").sum().alias("break_count"),
            pl.col("_boards").max().alias("max_board_count"),
            pl.col("_chg").mean().alias("avg_change"),
            *[
                (pl.col("_boards") == n).sum().alias(f"board_{n}")
                for n in range(1, HIGH_BOARD_FROM)
            ],
            (pl.col("_boards") >= HIGH_BOARD_FROM).sum().alias("high_board"),
        ])
        .sort("date")
    )

    rows = agg.to_dicts()
    out: list[dict] = []
    prev: dict | None = None
    for row in rows:
        boards = {f"board_{n}": int(row.get(f"board_{n}") or 0) for n in range(1, HIGH_BOARD_FROM)}
        high_board = int(row.get("high_board") or 0)
        limit_up = int(row.get("limit_up_count") or 0)
        break_count = int(row.get("break_count") or 0)
        total_amount = _finite(row.get("total_amount"))

        # 连板数(2 板及以上)—— 情绪周期里"接力强度"的直接读数
        connected = sum(v for k, v in boards.items() if k != "board_1") + high_board

        item = {
            "trade_date": str(row["date"]),
            "total_amount": total_amount,
            "amount_change_rate": _ratio(
                (total_amount or 0) - (_finite(prev.get("total_amount")) or 0) if prev else None,
                _finite(prev.get("total_amount")) if prev else None,
            ),
            "up_count": int(row.get("up_count") or 0),
            "down_count": int(row.get("down_count") or 0),
            "flat_count": int(row.get("flat_count") or 0),
            "down_more_than_7_count": int(row.get("down_more_than_7_count") or 0),
            "limit_up_count": limit_up,
            "limit_down_count": int(row.get("limit_down_count") or 0),
            "break_count": break_count,
            # 封板率 = 涨停 / (涨停 + 炸板)
            "seal_rate": _ratio(limit_up, limit_up + break_count),
            "max_board_count": int(row.get("max_board_count") or 0),
            "connected_board_count": connected,
            "avg_change": _pct(row.get("avg_change")),
            **boards,
            "high_board": high_board,
        }
        out.append(item)
        prev = item
    return out


def _with_promotion(series: list[dict]) -> list[dict]:
    """按板层分布跨日派生晋级率(今日 N+1 板 / 昨日 N 板)。"""
    out: list[dict] = []
    for i, row in enumerate(series):
        prev = series[i - 1] if i > 0 else None
        item = dict(row)
        if prev is None:
            item.update({
                "promotion_rate": None,
                "first_to_second_rate": None,
                "second_to_third_rate": None,
                "third_to_fourth_rate": None,
                "fourth_to_fifth_rate": None,
                "fifth_to_high_rate": None,
            })
        else:
            item.update({
                # 总晋级率 = 今日连板数(≥2) / 昨日涨停数
                "promotion_rate": _ratio(row["connected_board_count"], prev["limit_up_count"]),
                "first_to_second_rate": _ratio(row["board_2"], prev["board_1"]),
                "second_to_third_rate": _ratio(row["board_3"], prev["board_2"]),
                "third_to_fourth_rate": _ratio(row["board_4"], prev["board_3"]),
                "fourth_to_fifth_rate": _ratio(row["board_5"], prev["board_4"]),
                "fifth_to_high_rate": _ratio(row["high_board"], prev["board_5"]),
            })
        out.append(item)
    return out


# ================================================================
# 分区入口
# ================================================================

def emotion_series(repo, as_of: date | None = None, days: int = 30) -> dict:
    """情绪周期 —— 近 N 个交易日的市场情绪原始读数时序。

    对齐 fquant 的 EmotionDailyPoint: 只给原始计数(涨停/跌停/炸板/封板率/最高连板/
    成交额/涨跌家数),**不给情绪分**。情绪分需要 ext_data 概念排名(见 Dashboard 的
    radar),按日回扫代价过高且不是 fquant 复盘的口径。
    """
    days = _clamp_days(days)
    return _cached(("emotion", as_of, days), lambda: _emotion_series(repo, as_of, days))


def _emotion_series(repo, as_of: date | None, days: int) -> dict:
    df, dates, as_of = _load_window(repo, as_of, days)
    if df.is_empty():
        return {"as_of": str(as_of) if as_of else None, "days": days, "series": []}

    series = _daily_agg(df)
    # 窗口首日的 amount_change_rate 无前值可比,裁掉多取的那一天
    if len(series) > days:
        series = series[-days:]
    return {
        "as_of": str(as_of),
        "days": days,
        "trade_dates": [s["trade_date"] for s in series],
        "series": series,
    }


def ladder_series(repo, as_of: date | None = None, days: int = 20) -> dict:
    """连板天梯 —— 近 N 个交易日的板层分布 + 晋级率序列。"""
    days = _clamp_days(days)
    return _cached(("ladder", as_of, days), lambda: _ladder_series(repo, as_of, days))


def _ladder_series(repo, as_of: date | None, days: int) -> dict:
    df, dates, as_of = _load_window(repo, as_of, days)
    if df.is_empty():
        return {"as_of": str(as_of) if as_of else None, "days": days, "series": []}

    series = _with_promotion(_daily_agg(df))
    if len(series) > days:
        series = series[-days:]
    return {
        "as_of": str(as_of),
        "days": days,
        "high_board_from": HIGH_BOARD_FROM,
        "series": series,
    }


def theme_rotation(repo, as_of: date | None = None, days: int = 10, top: int = 8) -> dict:
    """题材轮动 —— 近 N 日 × Top 题材的涨停矩阵。

    每日只取涨停股,按 ext_data 概念映射聚合(涨停数 / 最高板 / 成交额 / 平均涨幅)。
    未配置概念 ext 数据时返回空矩阵 + available=False,由前端引导去数据页拉取预设。
    """
    days = _clamp_days(days)
    top = max(3, min(int(top or 8), 20))
    return _cached(("rotation", as_of, days, top), lambda: _theme_rotation(repo, as_of, days, top))


def _theme_rotation(repo, as_of: date | None, days: int, top: int) -> dict:
    df, dates, as_of = _load_window(repo, as_of, days)
    if df.is_empty():
        return {"as_of": str(as_of) if as_of else None, "days": days, "available": False,
                "reason": "no_data", "themes": [], "trade_dates": [], "cells": []}

    concept_map = _dimension_map(repo, "concept")
    if not concept_map:
        return {"as_of": str(as_of), "days": days, "available": False,
                "reason": "no_concept_ext",
                "themes": [], "trade_dates": [], "cells": []}

    df = _with_flags(df).filter(pl.col("_is_limit_up"))
    if df.is_empty():
        return {"as_of": str(as_of), "days": days, "available": True,
                "themes": [], "trade_dates": [], "cells": []}

    cols = [c for c in ["symbol", "date", "name", "amount", "_boards", "_chg"] if c in df.columns]
    rows = df.select(cols).to_dicts()

    # (date, theme) → 聚合
    buckets: dict[tuple[str, str], dict] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "")
        themes = concept_map.get(symbol) or concept_map.get(symbol.split(".", 1)[0]) or []
        themes = [t for t in themes if t not in NON_THEME_TAGS]
        if not themes:
            continue
        trade_date = str(row["date"])
        for theme in themes:
            item = buckets.setdefault((trade_date, theme), {
                "trade_date": trade_date, "name": theme,
                "limit_up_count": 0, "max_board_count": 0, "amount": 0.0, "_chg_sum": 0.0,
                "leaders": [],
            })
            item["limit_up_count"] += 1
            item["max_board_count"] = max(item["max_board_count"], int(_finite(row.get("_boards")) or 0))
            item["amount"] += _finite(row.get("amount")) or 0
            item["_chg_sum"] += _finite(row.get("_chg")) or 0
            item["leaders"].append({
                "symbol": symbol,
                "name": row.get("name"),
                "boards": int(_finite(row.get("_boards")) or 0),
            })

    trade_dates = sorted({d for d, _ in buckets})
    if len(trade_dates) > days:
        trade_dates = trade_dates[-days:]
    date_set = set(trade_dates)

    # 题材行:按"窗口内涨停总数"取 Top,保证矩阵行稳定(而非每列各取各的 Top)
    totals: dict[str, int] = {}
    for (trade_date, theme), item in buckets.items():
        if trade_date in date_set:
            totals[theme] = totals.get(theme, 0) + item["limit_up_count"]
    themes = [t for t, _ in sorted(totals.items(), key=lambda kv: -kv[1])[:top]]
    theme_set = set(themes)

    cells = []
    for (trade_date, theme), item in buckets.items():
        if trade_date not in date_set or theme not in theme_set:
            continue
        count = item["limit_up_count"]
        leaders = sorted(item["leaders"], key=lambda s: -s["boards"])[:3]
        cells.append({
            "trade_date": trade_date,
            "name": theme,
            "limit_up_count": count,
            "max_board_count": item["max_board_count"],
            "amount": item["amount"],
            "avg_change": item["_chg_sum"] / count * 100 if count else None,
            "leaders": leaders,
        })

    return {
        "as_of": str(as_of),
        "days": days,
        "available": True,
        "themes": themes,
        "trade_dates": trade_dates,
        "cells": sorted(cells, key=lambda c: (c["trade_date"], -c["limit_up_count"])),
    }


def _brief(row: dict, industry_map: dict[str, list[str]], concept_map: dict[str, list[str]],
           extra: dict | None = None) -> dict:
    symbol = str(row.get("symbol") or "")
    plain = symbol.split(".", 1)[0]
    industry = industry_map.get(symbol) or industry_map.get(plain) or []
    concepts = concept_map.get(symbol) or concept_map.get(plain) or []
    concepts = [c for c in concepts if c not in NON_THEME_TAGS]
    out = {
        "symbol": symbol,
        "name": row.get("name"),
        "close": _finite(row.get("close")),
        "change_pct": _pct(row.get("change_pct")),
        "amount": _finite(row.get("amount")),
        "turnover_rate": _finite(row.get("turnover_rate")),
        "boards": int(_finite(row.get("consecutive_limit_ups")) or 0),
        "industry": industry[0] if industry else "",
        "concepts": concepts[:3],
    }
    if extra:
        out.update(extra)
    return out


def review_clues(repo, as_of: date | None = None, limit: int = 20) -> dict:
    """风险与线索 —— 单日五张清单:炸板池 / 跌停池 / 冲高回落 / 成交额榜 / 反包股。

    冲高回落: 盘中最高较昨收 ≥ +5%,收盘涨幅 ≤ +2%,按回落幅度((收-高)/高)排序。
    反包股:   昨日跌幅 ≥ 3%,今日涨幅 ≥ 5%,且今收 > 昨开(实体吞没)。
    """
    limit = max(5, min(int(limit or 20), 100))
    return _cached(("clues", as_of, limit), lambda: _review_clues(repo, as_of, limit))


def _review_clues(repo, as_of: date | None, limit: int) -> dict:
    # 需要昨日数据做反包判定,取 2 个交易日窗口
    df, dates, as_of = _load_window(repo, as_of, 2)
    if df.is_empty() or not dates:
        return {"as_of": str(as_of) if as_of else None, "broken": [], "limit_down": [],
                "surge_and_fade": [], "top_amount": [], "rebound": []}

    today = dates[-1]
    prev = dates[-2] if len(dates) >= 2 else None
    cur = df.filter(pl.col("date") == today)
    if cur.is_empty():
        return {"as_of": str(as_of), "broken": [], "limit_down": [],
                "surge_and_fade": [], "top_amount": [], "rebound": []}

    industry_map = _dimension_map(repo, "industry", level=2)
    concept_map = _dimension_map(repo, "concept")

    def briefs(frame: pl.DataFrame, extra_keys: dict[str, str] | None = None) -> list[dict]:
        out = []
        for row in frame.head(limit).to_dicts():
            extra = {k: _finite(row.get(src)) for k, src in (extra_keys or {}).items()}
            out.append(_brief(row, industry_map, concept_map, extra))
        return out

    # ── 炸板池 / 跌停池 ──
    broken = (
        cur.filter(pl.col("signal_broken_limit_up").fill_null(False))
        .sort("amount", descending=True)
        if "signal_broken_limit_up" in cur.columns else cur.head(0)
    )
    limit_down = (
        cur.filter(pl.col("signal_limit_down").fill_null(False))
        .sort("amount", descending=True)
        if "signal_limit_down" in cur.columns else cur.head(0)
    )

    # ── 冲高回落 ──
    surge = cur.head(0)
    if {"high", "prev_close", "close", "change_pct"} <= set(cur.columns):
        surge = (
            cur.with_columns([
                ((pl.col("high") / pl.col("prev_close") - 1) * 100).alias("_high_pct"),
                ((pl.col("close") / pl.col("high") - 1) * 100).alias("_fade_pct"),
            ])
            .filter(
                (pl.col("_high_pct") >= 5)
                & (pl.col("change_pct").fill_null(0) * 100 <= 2)
                & pl.col("_fade_pct").is_finite()
            )
            .sort("_fade_pct")
        )

    # ── 成交额榜 ──
    top_amount = cur.sort("amount", descending=True) if "amount" in cur.columns else cur.head(0)

    # ── 反包股(需昨日) ──
    rebound = cur.head(0)
    if prev is not None and {"open", "close", "change_pct"} <= set(cur.columns):
        prev_df = df.filter(pl.col("date") == prev).select([
            pl.col("symbol"),
            pl.col("change_pct").alias("_prev_chg"),
            pl.col("open").alias("_prev_open"),
        ])
        rebound = (
            cur.join(prev_df, on="symbol", how="inner")
            .filter(
                (pl.col("_prev_chg").fill_null(0) * 100 <= -3)
                & (pl.col("change_pct").fill_null(0) * 100 >= 5)
                & (pl.col("close") > pl.col("_prev_open"))
            )
            .with_columns((pl.col("_prev_chg") * 100).alias("_prev_chg_pct"))
            .sort("change_pct", descending=True)
        )

    return {
        "as_of": str(as_of),
        "trade_date": str(today),
        "prev_date": str(prev) if prev else None,
        "broken": briefs(broken),
        "limit_down": briefs(limit_down),
        "surge_and_fade": briefs(surge, {"high_pct": "_high_pct", "fade_pct": "_fade_pct"}),
        "top_amount": briefs(top_amount),
        "rebound": briefs(rebound, {"prev_change_pct": "_prev_chg_pct"}),
    }
