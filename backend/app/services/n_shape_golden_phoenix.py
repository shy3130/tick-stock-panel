"""N 字金凤凰研究因子 — n_shape_golden_phoenix_v1（独立、只读、默认关闭）。

设计定稿见 docs/ISSUE-8/final-design.md。边界：

- 读取边界：只接受 generation-pinned sealed reader（构造注入）；
  禁止 get_enriched_range 合并 overlay 或现有 signal_limit_up 作为替代。
- 能力门禁（优先于一切命中）：pinned reader 或 PIT 涨跌停制度/ST provider
  缺失时，整份评估返回 ``unavailable`` + reasons，不降级、不猜口径。
- 计算边界：事件状态机按 symbol/date 在内存运行，只用 raw_* 字段（统一
  原始价格尺度）；窗口按固定市场交易日集合校验。
- 输出边界：结构化证据、删失原因、coverage、provenance、forward 描述
  统计；reachability = daily_price_only。证据字段禁止交易语义
  （buy/sell/target/stop/action/entry/exit/position/order/long/short/hold/trade）。
- 产品边界：不接入 short_pool、不进入 Agent 工具、不改交易事实流。
"""
from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from typing import Any, Protocol

import polars as pl

logger = logging.getLogger(__name__)

FACTOR_ID = "n_shape_golden_phoenix_v1"
FACTOR_VERSION = 1
FACTOR_NAME = "N字金凤凰（研究）"
FACTOR_DESCRIPTION = (
    "低位首板后 2-10 市场日双缩量回调不破首板 raw_low，随后放量突破或二板的"
    "日线事件研究因子；仅输出证据与删失原因，无任何交易语义"
)
REACHABILITY = "daily_price_only"

# 事件窗口参数（逐字锁定）
PRIOR_CLEAN_DAYS = 60          # 首板前连续无涨停的市场日数
LOW_POSITION_MAX = 0.35       # 首板前 60 日价格分位上限
POST_WINDOW_MIN = 2            # 双缩量/触发最早允许的首板后市场日
POST_WINDOW_MAX = 10           # 双缩量/触发最晚允许的首板后市场日
VOLUME_SHRINK_RATIO = 0.70     # 调整期均量 / 首板量上限
VOLUME_PRE20_RATIO = 0.90      # 调整期均量 / 首板前20日均量上限
VOLUME_BREAKOUT_RATIO = 1.50   # 放量突破 / 调整期均量下限
MA_WINDOW = 5                  # 均线窗口（raw_close 尺度）
LIMIT_PRICE_TOL = 0.005        # 涨停价判定容差（与 indicators/pipeline 同口径）
FORWARD_HORIZONS = (1, 5, 10, 20)

# warmup：为覆盖首板前 60 个市场日，向 start 之前多取的日历日缓冲。
_CALENDAR_WARMUP_DAYS = 150

# 证据/事件字段禁用的交易语义词（子串匹配，小写）。
_BANNED_TRADING_TOKENS = (
    "buy", "sell", "target", "stop", "action", "entry", "exit",
    "position", "order", "long", "short", "hold", "trade",
)

#: 事件变体（同一首板可各自独立成立）
VARIANT_VOLUME_BREAKOUT = "volume_breakout"
VARIANT_SECOND_LIMIT_UP = "second_limit_up"
VARIANTS = (VARIANT_VOLUME_BREAKOUT, VARIANT_SECOND_LIMIT_UP)

# 涨跌停价整数算术（镜像 indicators/pipeline._limit_price，纯 Python 版）
_LIMIT_NUM_DEN: dict[float, tuple[int, int]] = {
    0.05: (105, 100), 0.10: (110, 100), 0.20: (120, 100), 0.30: (130, 100),
}


class GenerationPinnedDailyReader(Protocol):
    """generation-pinned sealed reader 契约（当前仓库尚无实现 → fail-closed）。"""

    def generation(self) -> str: ...

    def market_days(self, start: date, end: date) -> list[date]: ...

    def daily_bars(self, symbol: str, start: date, end: date) -> pl.DataFrame: ...


class PitLimitRegimeProvider(Protocol):
    """PIT 涨跌停制度/ST provider 契约（当前仓库尚无实现 → fail-closed）。"""

    def provider_id(self) -> str: ...

    def limit_up_pct(self, symbol: str, on_date: date) -> float | None: ...


#: repository 上用于发现能力的 duck-type 属性名（均不存在 → unavailable）。
PINNED_READER_ATTR = "generation_pinned_daily_reader"
PIT_PROVIDER_ATTR = "pit_limit_regime_provider"
REQUIRED_RAW_COLUMNS = ("raw_close", "raw_high", "raw_low", "volume", "close")


def resolve_pinned_reader(repo: Any) -> GenerationPinnedDailyReader | None:
    """从 repository 解析完整 generation-pinned reader；缺能力即 None。"""
    reader = getattr(repo, PINNED_READER_ATTR, None)
    required = ("generation", "market_days", "daily_bars")
    return reader if all(callable(getattr(reader, name, None)) for name in required) else None


def resolve_pit_provider(repo: Any) -> PitLimitRegimeProvider | None:
    """从 repository 解析完整 PIT 制度/ST provider；缺能力即 None。"""
    provider = getattr(repo, PIT_PROVIDER_ATTR, None)
    required = ("provider_id", "limit_up_pct")
    return provider if all(callable(getattr(provider, name, None)) for name in required) else None


# ── 交易语义禁令 ──────────────────────────────────────────────────────────


def assert_no_trading_tokens(name: str) -> None:
    """字段/键名含交易语义词时 fail-closed（内部契约守卫）。"""
    lowered = name.lower()
    for token in _BANNED_TRADING_TOKENS:
        if token in lowered:
            raise ValueError(f"trading semantics token {token!r} forbidden in field {name!r}")


def _validate_keys_no_trading_tokens(payload: Any) -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            assert_no_trading_tokens(str(key))
            _validate_keys_no_trading_tokens(value)
    elif isinstance(payload, list):
        for item in payload:
            _validate_keys_no_trading_tokens(item)


# ── 涨跌停判定（raw 价格 + PIT 制度） ─────────────────────────────────────


def limit_up_price(prev_raw_close: float, limit_pct: float) -> float:
    """交易所涨停价 = round(prev × (1+pct), 2)，整数「分」算术避免浮点误差。"""
    frac = _LIMIT_NUM_DEN.get(round(limit_pct, 2))
    if frac is None:
        raise ValueError(f"unsupported pit limit pct: {limit_pct!r}")
    num, den = frac
    cents = int(round(prev_raw_close * 100))
    return ((cents * num + den // 2) // den) / 100


def _is_limit_up(
    pit_provider: PitLimitRegimeProvider,
    symbol: str,
    bar: dict[str, Any],
    prev_raw_close: float | None,
) -> bool | None:
    """True/False=判定；None=PIT 制度未知或无昨收（fail-closed 由调用方删失）。"""
    if prev_raw_close is None or prev_raw_close <= 0:
        return None
    raw_close = bar["raw_close"]
    if raw_close is None or raw_close <= 0:
        return None
    pct = pit_provider.limit_up_pct(symbol, bar["date"])
    if pct is None or pct <= 0:
        return None
    return abs(raw_close - limit_up_price(prev_raw_close, pct)) < LIMIT_PRICE_TOL


# ── 能力门禁 envelope ─────────────────────────────────────────────────────


def _factor_meta() -> dict[str, Any]:
    return {
        "factor_id": FACTOR_ID,
        "version": FACTOR_VERSION,
        "name": FACTOR_NAME,
        "description": FACTOR_DESCRIPTION,
        "reachability": REACHABILITY,
    }


def unavailable_envelope(
    *,
    start: date,
    end: date,
    reasons: list[str],
) -> dict[str, Any]:
    """能力缺失的结构化 unavailable 载荷（研究状态，非 HTTP 错误）。"""
    return {
        "factor": _factor_meta(),
        "status": "unavailable",
        "unavailable_reasons": list(reasons),
        "request": {"start": start, "end": end},
        "provenance": {},
        "coverage": None,
        "events": [],
        "censored": [],
        "note": (
            "generation-pinned sealed reader 与 PIT 涨跌停制度/ST 能力是本因子的"
            "前置数据能力；缺失时显式返回 unavailable，不以合并 overlay 或"
            "signal_limit_up 替代"
        ),
    }


# ── 事件状态机 ────────────────────────────────────────────────────────────


def _bars_to_dicts(frame: pl.DataFrame, symbol: str) -> tuple[list[dict[str, Any]], dict | None]:
    """转内存行并做 raw 完整性检查；不完整 → (空列表, 删失记录)。"""
    if frame is None or frame.is_empty():
        return [], {"symbol": symbol, "code": "no_data", "detail": {}}
    missing_cols = [c for c in ("date", *REQUIRED_RAW_COLUMNS) if c not in frame.columns]
    if missing_cols:
        return [], {
            "symbol": symbol,
            "code": "raw_field_missing",
            "detail": {"fields": missing_cols},
        }
    rows = frame.sort("date").to_dicts()
    for row in rows:
        for field in REQUIRED_RAW_COLUMNS:
            value = row.get(field)
            if value is None or (isinstance(value, float) and (value != value or not math.isfinite(value))):
                return [], {
                    "symbol": symbol,
                    "code": "raw_field_missing",
                    "detail": {"fields": [field], "date": str(row.get("date"))},
                }
            if field.startswith("raw_") or field == "volume":
                if float(value) <= 0:
                    return [], {
                        "symbol": symbol,
                        "code": "raw_field_invalid",
                        "detail": {"fields": [field], "date": str(row.get("date"))},
                    }
    return rows, None


def _evidence(field: str, actual: Any, op: str, target: Any) -> dict[str, Any]:
    assert_no_trading_tokens(field)
    return {"field": field, "actual": actual, "op": op, "target": target}


def _forward_stats(
    bars_by_date: dict[date, dict[str, Any]],
    calendar: list[date],
    trigger_day: date,
) -> dict[str, Any]:
    """按固定市场日历计算 forward；缺任一预期 bar 时保持 null。"""
    base = bars_by_date[trigger_day]["raw_close"]
    stats: dict[str, Any] = {"base_raw_close": base}
    index = {day: i for i, day in enumerate(calendar)}
    trigger_index = index.get(trigger_day)
    for horizon in FORWARD_HORIZONS:
        key = f"forward_{horizon}d_raw_return"
        assert_no_trading_tokens(key)
        stats[key] = None
        if trigger_index is None or trigger_index + horizon >= len(calendar):
            continue
        later = bars_by_date.get(calendar[trigger_index + horizon])
        if later is not None and later.get("raw_close") is not None:
            stats[key] = later["raw_close"] / base - 1
    return stats


def _scan_symbol(
    *,
    symbol: str,
    rows: list[dict[str, Any]],
    calendar: list[date],
    event_window: tuple[date, date],
    pit_provider: PitLimitRegimeProvider,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """单 symbol 事件扫描；返回 (events, censored)。"""
    cal_index = {d: i for i, d in enumerate(calendar)}
    bars_by_date = {row["date"]: row for row in rows}
    off_calendar = [str(d) for d in bars_by_date if d not in cal_index]
    if off_calendar:
        return [], [{
            "symbol": symbol, "code": "date_not_in_market_calendar",
            "detail": {"dates": off_calendar[:5]},
        }]

    ordered = sorted(bars_by_date)
    # 逐日涨跌停标记（None = 无法判定）
    limit_flags: dict[date, bool | None] = {}
    prev_close: float | None = None
    for d in ordered:
        limit_flags[d] = _is_limit_up(pit_provider, symbol, bars_by_date[d], prev_close)
        prev_close = bars_by_date[d]["raw_close"]

    def _censor(code: str, **detail: Any) -> dict[str, Any]:
        return {"symbol": symbol, "code": code, "detail": detail}

    events: list[dict[str, Any]] = []
    censored: list[dict[str, Any]] = []

    for t in ordered:
        if not (event_window[0] <= t <= event_window[1]):
            continue
        if limit_flags[t] is None:
            censored.append(_censor("limit_regime_unknown", date=str(t)))
            continue
        if limit_flags[t] is not True:
            continue
        # 候选首板：前 60 个市场日（日历口径）无涨停
        t_pos = cal_index[t]
        if t_pos < PRIOR_CLEAN_DAYS:
            censored.append(_censor("insufficient_history", date=str(t)))
            continue
        prior_days = calendar[t_pos - PRIOR_CLEAN_DAYS:t_pos]
        missing_prior = [str(d) for d in prior_days if d not in bars_by_date]
        if missing_prior:
            censored.append(_censor("history_incomplete", dates=missing_prior[:5]))
            continue
        unknown_prior = [str(d) for d in prior_days if limit_flags.get(d) is None]
        if unknown_prior:
            censored.append(_censor("limit_regime_unknown", dates=unknown_prior[:5]))
            continue
        if any(limit_flags.get(d) is True for d in prior_days):
            continue  # 前期有涨停：不是首板 → 正常无事件
        # 低位：首板前收盘相对前 60 日高低区间的价格位置。
        prior_bars = [bars_by_date[d] for d in prior_days if d in bars_by_date]
        if len(prior_bars) < PRIOR_CLEAN_DAYS:
            censored.append(_censor("insufficient_history", date=str(t)))
            continue
        high60 = max(b["raw_high"] for b in prior_bars)
        low60 = min(b["raw_low"] for b in prior_bars)
        anchor = prior_bars[-1]["raw_close"]
        span = high60 - low60
        price_position = (anchor - low60) / span if span > 0 else None
        if price_position is None or price_position > LOW_POSITION_MAX:
            continue

        first_bar = bars_by_date[t]
        ref_low = first_bar["raw_low"]
        ref_high = first_bar["raw_high"]
        ref_vol = first_bar["volume"]
        pre20_days = calendar[t_pos - 20:t_pos]
        pre20_bars = [bars_by_date[d] for d in pre20_days if d in bars_by_date]
        pre20_avg = sum(b["volume"] for b in pre20_bars) / len(pre20_bars) if len(pre20_bars) == 20 else None
        if pre20_avg is None or ref_vol <= 0:
            censored.append(_censor("volume_history_incomplete", event_date=str(t)))
            continue

        window_days = calendar[t_pos + POST_WINDOW_MIN:t_pos + POST_WINDOW_MAX + 1]
        suspended = [str(d) for d in calendar[t_pos + 1:t_pos + POST_WINDOW_MAX + 1]
                     if d not in bars_by_date]
        if suspended:
            censored.append(_censor("suspended_in_window", dates=suspended[:5]))
            continue

        structure_ok = True
        shrink_done_day: date | None = None
        confirmed: dict[str, dict[str, Any]] = {}
        for d in window_days:
            bar = bars_by_date[d]
            d_pos = cal_index[d]
            if bars_by_date[calendar[d_pos - 1]]["raw_low"] < ref_low:
                structure_ok = False
                break
            if shrink_done_day is None:
                adjust_bars = [bars_by_date[x] for x in calendar[t_pos + 1:d_pos + 1]]
                if adjust_bars:
                    avg_volume = sum(x["volume"] for x in adjust_bars) / len(adjust_bars)
                    if avg_volume / ref_vol <= VOLUME_SHRINK_RATIO and avg_volume / pre20_avg <= VOLUME_PRE20_RATIO:
                        shrink_done_day = d
            if shrink_done_day is None:
                continue
            adjust_bars = [bars_by_date[x] for x in calendar[t_pos + 1:d_pos] if x in bars_by_date]
            adjust_avg = sum(x["volume"] for x in adjust_bars) / len(adjust_bars) if adjust_bars else None
            if VARIANT_VOLUME_BREAKOUT not in confirmed and adjust_avg is not None:
                ma5_days = calendar[max(0, d_pos - 4):d_pos + 1]
                ma10_days = calendar[max(0, d_pos - 9):d_pos + 1]
                ma5_bars = [bars_by_date[x] for x in ma5_days if x in bars_by_date]
                ma10_bars = [bars_by_date[x] for x in ma10_days if x in bars_by_date]
                ma5 = sum(x["raw_close"] for x in ma5_bars) / len(ma5_bars) if len(ma5_bars) == 5 else None
                ma10 = sum(x["raw_close"] for x in ma10_bars) / len(ma10_bars) if len(ma10_bars) == 10 else None
                if (
                    bar["raw_close"] > ref_high
                    and bar["volume"] >= adjust_avg * VOLUME_BREAKOUT_RATIO
                    and (
                        (ma5 is not None and bar["raw_close"] >= ma5)
                        or (ma10 is not None and bar["raw_close"] >= ma10)
                    )
                ):
                    confirmed[VARIANT_VOLUME_BREAKOUT] = _build_event(
                        symbol=symbol, variant=VARIANT_VOLUME_BREAKOUT, event_date=t,
                        confirm_date=d, calendar=calendar, price_position=price_position,
                        high60=high60, anchor_close=anchor, ref_low=ref_low, ref_high=ref_high,
                        ref_vol=ref_vol, bar=bar, ma5=ma5, adjust_avg=adjust_avg,
                        pre20_avg=pre20_avg, bars_by_date=bars_by_date,
                    )
            if VARIANT_SECOND_LIMIT_UP not in confirmed and limit_flags.get(d) is True:
                confirmed[VARIANT_SECOND_LIMIT_UP] = _build_event(
                    symbol=symbol, variant=VARIANT_SECOND_LIMIT_UP, event_date=t,
                    confirm_date=d, calendar=calendar, price_position=price_position,
                    high60=high60, anchor_close=anchor, ref_low=ref_low, ref_high=ref_high,
                    ref_vol=ref_vol, bar=bar, ma5=None, adjust_avg=adjust_avg,
                    pre20_avg=pre20_avg, bars_by_date=bars_by_date,
                )
            if len(confirmed) == len(VARIANTS):
                break

        events.extend(confirmed.values())
        if structure_ok is False or (structure_ok and len(confirmed) < len(VARIANTS)):
            # 结构破坏时到达这里说明未确认变体已被打断；正常走完窗口但变体
            # 未全部成立不属于删失（评估完成、无事件）。
            if not structure_ok:
                censored.append(_censor(
                    "structural_break_raw_low", event_date=str(t),
                    variants_pending=[v for v in VARIANTS if v not in confirmed],
                ))
    return events, censored


def _build_event(
    *,
    symbol: str,
    variant: str,
    event_date: date,
    confirm_date: date,
    calendar: list[date],
    price_position: float,
    high60: float,
    anchor_close: float,
    ref_low: float,
    ref_high: float,
    ref_vol: float,
    bar: dict[str, Any],
    ma5: float | None,
    adjust_avg: float | None,
    pre20_avg: float,
    bars_by_date: dict[date, dict[str, Any]],
) -> dict[str, Any]:
    evidence = [
        _evidence("price_position_60d", round(price_position, 4), "<=", LOW_POSITION_MAX),
        _evidence("prior_clean_market_days", PRIOR_CLEAN_DAYS, ">=", PRIOR_CLEAN_DAYS),
        _evidence("first_board_date", str(event_date), "==", "limit_up"),
        _evidence("anchor_raw_close", anchor_close, "within", f"raw_high60={high60}"),
        _evidence("structure_ref_raw_low", ref_low, "<=", "window_raw_low"),
        _evidence("adjust_volume_vs_first_board", round((adjust_avg or 0) / ref_vol, 4), "<=", VOLUME_SHRINK_RATIO),
        _evidence("adjust_volume_vs_pre20", round((adjust_avg or 0) / pre20_avg, 4), "<=", VOLUME_PRE20_RATIO),
        _evidence("confirm_date", str(confirm_date), "within", f"[event+{POST_WINDOW_MIN}, event+{POST_WINDOW_MAX}]"),
    ]
    if variant == VARIANT_VOLUME_BREAKOUT:
        evidence.extend([
            _evidence("confirm_volume_vs_adjustment", round(bar["volume"] / (adjust_avg or 1), 4), ">=", VOLUME_BREAKOUT_RATIO),
            _evidence("confirm_raw_close", bar["raw_close"], ">", ref_high),
            _evidence("ma5_raw_close", round(ma5, 4), "<=", bar["raw_close"]),
        ])
    else:
        evidence.append(_evidence("second_board_raw_close", bar["raw_close"], "==", "limit_up"))
    event = {
        "symbol": symbol,
        "variant": variant,
        "event_date": event_date,
        "confirm_date": confirm_date,
        "evidence": evidence,
        "forward": _forward_stats(bars_by_date, calendar, confirm_date),
    }
    _validate_keys_no_trading_tokens(event)
    return event


# ── 评估入口 ──────────────────────────────────────────────────────────────


def evaluate_n_shape(
    *,
    start: date,
    end: date,
    symbols: list[str] | None,
    pinned_reader: GenerationPinnedDailyReader | None,
    pit_provider: PitLimitRegimeProvider | None,
) -> dict[str, Any]:
    """评估 n_shape_golden_phoenix_v1；能力门禁优先于一切命中。"""
    if start > end:
        raise ValueError("start must be <= end")

    reasons: list[str] = []
    if pinned_reader is None:
        reasons.append("generation_pinned_reader_missing")
    if pit_provider is None:
        reasons.append("pit_regime_st_missing")
    if reasons:
        return unavailable_envelope(start=start, end=end, reasons=reasons)

    lookup_start = start - timedelta(days=_CALENDAR_WARMUP_DAYS)
    calendar = sorted(pinned_reader.market_days(lookup_start, end))
    if len(calendar) < PRIOR_CLEAN_DAYS + 1:
        return unavailable_envelope(
            start=start, end=end, reasons=["market_calendar_insufficient"])

    if symbols is None:
        universe = getattr(pinned_reader, "universe", None)
        if not callable(universe):
            return unavailable_envelope(start=start, end=end, reasons=["reader_universe_missing"])
        symbols = sorted({str(symbol) for symbol in universe(start, end) if str(symbol)})
    else:
        symbols = sorted({str(symbol) for symbol in symbols if str(symbol)})

    events: list[dict[str, Any]] = []
    censored: list[dict[str, Any]] = []
    evaluated = 0
    for symbol in symbols:
        frame = pinned_reader.daily_bars(symbol, lookup_start, end)
        rows, censor = _bars_to_dicts(frame, symbol)
        if censor is not None:
            censored.append(censor)
            continue
        evaluated += 1
        sym_events, sym_censored = _scan_symbol(
            symbol=symbol, rows=rows, calendar=calendar,
            event_window=(start, end), pit_provider=pit_provider,
        )
        events.extend(sym_events)
        censored.extend(sym_censored)

    events.sort(key=lambda e: (e["event_date"], e["symbol"], e["variant"]))
    censored.sort(key=lambda c: (c["symbol"], c["code"]))
    by_reason: dict[str, int] = {}
    for c in censored:
        by_reason[c["code"]] = by_reason.get(c["code"], 0) + 1

    payload = {
        "factor": _factor_meta(),
        "status": "ok",
        "unavailable_reasons": [],
        "request": {"start": start, "end": end},
        "provenance": {
            "pinned_reader": {"generation": pinned_reader.generation()},
            "pit_provider": {"provider_id": pit_provider.provider_id()},
            "price_scale": "raw",
        },
        "coverage": {
            "symbols_total": len(symbols),
            "evaluated": evaluated,
            "censored": len(censored),
            "events": len(events),
            "by_reason": by_reason,
        },
        "events": events,
        "censored": censored,
        "market_days_used": len(calendar),
    }
    _validate_keys_no_trading_tokens(payload)
    return payload


__all__ = [
    "FACTOR_ID",
    "FACTOR_VERSION",
    "FACTOR_NAME",
    "REACHABILITY",
    "VARIANT_VOLUME_BREAKOUT",
    "VARIANT_SECOND_LIMIT_UP",
    "VARIANTS",
    "PINNED_READER_ATTR",
    "PIT_PROVIDER_ATTR",
    "GenerationPinnedDailyReader",
    "PitLimitRegimeProvider",
    "resolve_pinned_reader",
    "resolve_pit_provider",
    "assert_no_trading_tokens",
    "limit_up_price",
    "evaluate_n_shape",
    "unavailable_envelope",
]
