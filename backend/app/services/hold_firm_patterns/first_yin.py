"""F1 ``first_yin_complement`` 纯检测器（Issue #38 final-design §5）。

只消费调用方注入的已排序 bars、PIT market facts 与交易日历；不做 I/O、
不计算收益、不维护跨调用状态。涨停判定使用 facts 里的 PIT published band
对 raw close 做 ``PRICE_ABS_TOL`` 容差比较，天然覆盖 10/20/30cm 与 ST；
``regime``/``is_st`` 只作为诊断值写入 evidence，不影响核心 mask。
qualified = 首阴守现场复算 MA5 且量态（<=0.70 缩量 / >=1.50 放量，中间态
不通过）且 landmark 日互补（缩量后 >=1.50x、放量后 <=0.70x）。
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from datetime import date

from app.services.hold_firm_patterns.models import (
    FACTOR_IDS,
    PRICE_ABS_TOL,
    Bar,
    CensorReason,
    DetectionEvidence,
    FactorId,
    Landmark,
    LandmarkKind,
    MarketFactsRow,
    MarketFactsSource,
    ParentDetection,
)

MIN_LIMIT_UP_STREAK = 3
FIRST_YIN_SEARCH_DAYS = 5
MA5_WINDOW = 5
YIN_SHRINK_MAX_RATIO = 0.70
YIN_EXPAND_MIN_RATIO = 1.50
COMPLEMENT_EXPAND_MIN_RATIO = 1.50
COMPLEMENT_SHRINK_MAX_RATIO = 0.70


def _close_at_band(raw_close: float, published_limit_up: float) -> bool:
    """raw 收盘价落在涨停价 ±0.005 容差带内才算可证明的涨停。"""
    return published_limit_up - PRICE_ABS_TOL <= raw_close <= published_limit_up + PRICE_ABS_TOL


def _one_price_at_upper(bar: Bar, published_limit_up: float) -> bool:
    """raw 四价全贴涨停价：entry unreachable 的诊断标志（不改变检测）。"""
    return all(
        abs(value - published_limit_up) <= PRICE_ABS_TOL
        for value in (
            bar.quote_open_raw,
            bar.quote_high_raw,
            bar.quote_low_raw,
            bar.quote_close_raw,
        )
    )


def _censored(symbol: str, anchor_date: date, reason: CensorReason) -> ParentDetection:
    return ParentDetection(
        factor_id=FACTOR_IDS[0],
        symbol=symbol,
        anchor_date=anchor_date,
        landmark=None,
        censor=reason,
    )


class FirstYinDetector:
    """连续 >=3 个可交易涨停日后 1..5 日首阴的量价互补检测。"""

    @property
    def factor_id(self) -> FactorId:
        return FACTOR_IDS[0]

    def detect(
        self,
        symbol: str,
        bars: Sequence[Bar],
        facts: MarketFactsSource,
        calendar: Sequence[date],
    ) -> tuple[ParentDetection, ...]:
        ordered = sorted(bars, key=lambda bar: bar.date)
        bar_by_date = {bar.date: bar for bar in ordered}
        days = tuple(sorted(calendar))
        streak: list[tuple[date, Bar, MarketFactsRow]] = []
        detections: list[ParentDetection] = []

        def flush() -> None:
            nonlocal streak
            if len(streak) >= MIN_LIMIT_UP_STREAK:
                detection = self._detect_for_streak(
                    symbol, streak, ordered, bar_by_date, facts, days
                )
                if detection is not None:
                    detections.append(detection)
            streak = []

        for day in days:
            bar = bar_by_date.get(day)
            row = facts.row(symbol, day) if bar is not None else None
            if (
                bar is not None
                and row is not None
                and _close_at_band(bar.quote_close_raw, row.published_limit_up)
            ):
                streak.append((day, bar, row))
                continue
            # 缺 bar（停牌日无 K 线）/缺 facts row/未封板：极大连续涨停 run 在此截断。
            flush()
        flush()
        return tuple(detections)

    def _detect_for_streak(
        self,
        symbol: str,
        streak: list[tuple[date, Bar, MarketFactsRow]],
        ordered: Sequence[Bar],
        bar_by_date: dict[date, Bar],
        facts: MarketFactsSource,
        days: tuple[date, ...],
    ) -> ParentDetection | None:
        last_up_day = streak[-1][0]
        window_start = bisect_left(days, last_up_day) + 1
        window = days[window_start : window_start + FIRST_YIN_SEARCH_DAYS]
        for day in window:
            bar = bar_by_date.get(day)
            row = facts.row(symbol, day) if bar is not None else None
            if bar is None or row is None:
                # 窗口内停牌/数据缺口会掩盖真正的首阴，不能猜，事件级删失。
                return _censored(symbol, last_up_day, CensorReason.SELECTION_WINDOW_INCOMPLETE)
            if bar.research_close_adj < bar.research_open_adj:
                return self._detect_for_first_yin(
                    symbol, streak, day, bar, ordered, bar_by_date, facts, days
                )
        if len(window) < FIRST_YIN_SEARCH_DAYS:
            # 数据在窗口扫完前截断：首阴是否存在不可判定。
            return _censored(symbol, last_up_day, CensorReason.SELECTION_WINDOW_INCOMPLETE)
        # 完整扫满 5 个市场日仍无阴线：父事件从未形成，不是删失。
        return None

    def _detect_for_first_yin(
        self,
        symbol: str,
        streak: list[tuple[date, Bar, MarketFactsRow]],
        yin_day: date,
        yin_bar: Bar,
        ordered: Sequence[Bar],
        bar_by_date: dict[date, Bar],
        facts: MarketFactsSource,
        days: tuple[date, ...],
    ) -> ParentDetection:
        _, last_up_bar, _ = streak[-1]
        warmup_end = bisect_right(ordered, yin_day, key=lambda bar: bar.date)
        ma5_bars = ordered[max(0, warmup_end - MA5_WINDOW) : warmup_end]
        if len(ma5_bars) < MA5_WINDOW:
            return _censored(symbol, yin_day, CensorReason.WARMUP_INCOMPLETE)
        ma5 = sum(bar.research_close_adj for bar in ma5_bars) / MA5_WINDOW

        if last_up_bar.volume <= 0 or yin_bar.volume <= 0:
            return _censored(symbol, yin_day, CensorReason.SELECTION_WINDOW_INCOMPLETE)
        yin_ratio = yin_bar.volume / last_up_bar.volume
        if yin_ratio <= YIN_SHRINK_MAX_RATIO:
            volume_state = "shrink"
        elif yin_ratio >= YIN_EXPAND_MIN_RATIO:
            volume_state = "expand"
        else:
            volume_state = "middle"

        landmark_index = bisect_right(days, yin_day)
        if landmark_index >= len(days):
            return _censored(symbol, yin_day, CensorReason.SELECTION_WINDOW_INCOMPLETE)
        landmark_day = days[landmark_index]
        landmark_bar = bar_by_date.get(landmark_day)
        landmark_row = facts.row(symbol, landmark_day) if landmark_bar is not None else None
        if landmark_bar is None or landmark_row is None or landmark_bar.volume <= 0:
            return _censored(symbol, yin_day, CensorReason.SELECTION_WINDOW_INCOMPLETE)
        complement_ratio = landmark_bar.volume / yin_bar.volume
        if volume_state == "shrink":
            complement_pass = complement_ratio >= COMPLEMENT_EXPAND_MIN_RATIO
        elif volume_state == "expand":
            complement_pass = complement_ratio <= COMPLEMENT_SHRINK_MAX_RATIO
        else:
            complement_pass = False

        ma5_held = yin_bar.research_close_adj >= ma5
        qualified = ma5_held and volume_state != "middle" and complement_pass
        values: dict[str, object] = {
            "thresholds": {
                "yin_shrink_max": YIN_SHRINK_MAX_RATIO,
                "yin_expand_min": YIN_EXPAND_MIN_RATIO,
                "complement_expand_min": COMPLEMENT_EXPAND_MIN_RATIO,
                "complement_shrink_max": COMPLEMENT_SHRINK_MAX_RATIO,
            },
            "ma5_window": MA5_WINDOW,
            "limit_up_streak_days": len(streak),
            "limit_up_streak_dates": [day.isoformat() for day, _, _ in streak],
            "limit_up_days": [
                {
                    "date": day.isoformat(),
                    "raw_close": bar.quote_close_raw,
                    "published_limit_up": row.published_limit_up,
                    "one_price_at_upper": _one_price_at_upper(bar, row.published_limit_up),
                    "is_st": row.is_st,
                    "regime": row.regime,
                }
                for day, bar, row in streak
            ],
            "last_limit_up_volume": last_up_bar.volume,
            "first_yin_date": yin_day.isoformat(),
            "first_yin_volume": yin_bar.volume,
            "yin_volume_ratio": yin_ratio,
            "volume_state": volume_state,
            "volume_state_pass": volume_state != "middle",
            "ma5_adj": ma5,
            "first_yin_close_adj": yin_bar.research_close_adj,
            "ma5_held": ma5_held,
            "complement_date": landmark_day.isoformat(),
            "complement_volume": landmark_bar.volume,
            "complement_volume_ratio": complement_ratio,
            "complement_pass": complement_pass,
        }
        landmark = Landmark(
            kind=LandmarkKind.FIRST_YIN_NEXT_CLOSE,
            anchor_date=yin_day,
            landmark_date=landmark_day,
        )
        return ParentDetection(
            factor_id=FACTOR_IDS[0],
            symbol=symbol,
            anchor_date=yin_day,
            landmark=landmark,
            evidence=DetectionEvidence(qualified=qualified, values=values),
        )
