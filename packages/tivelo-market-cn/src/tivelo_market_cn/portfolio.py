"""One A-share research clock, with execution terms selected by security."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from json import dumps
from typing import Literal, Self

from pydantic import model_validator
from tivelo_contracts.base import Contract, fingerprint
from tivelo_contracts.data import Instrument
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import DefinitionReference
from tivelo_contracts.market import (
    FeeQuote,
    OrderQuantityTerms,
    OrderType,
    PriceBounds,
    ReferencePrice,
    TradingDay,
    TradingTerms,
)

from .calendar import CalendarVersion, CNCalendar
from .classification import CNEquityClass
from .profile_presets import CashEquityProfileRequest, compile_cash_equity_profile
from .quantity_presets import load_quantity_preset
from .rules import CNTradingRules

_COMMON_CALENDAR_ID = "cn.equity.portfolio.regular-auction"


class CNPortfolioProfile(Contract):
    profile_format: Literal["tivelo-cn-portfolio-v1", "tivelo-cn-bars-v1"] = (
        "tivelo-cn-portfolio-v1"
    )
    request: CashEquityProfileRequest
    venue_rules: tuple[DefinitionReference, ...] = ()

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.request.preset_version is None or self.request.instrument_classes is None:
            raise ValueError("portfolio profile must pin its preset version and security classes")
        if not self.uses_bars and not self.venue_rules:
            raise ValueError("a preset portfolio must pin its compiled venue rules")
        return self

    @property
    def uses_bars(self) -> bool:
        return self.profile_format == "tivelo-cn-bars-v1"

    def bind_daily_snapshot(self, reader, *, universe, start, end) -> CNPortfolioRules:
        from .bar_calendar import read_bar_calendar

        if not self.uses_bars:
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "This profile holds a declared calendar")
        if not self.request.start <= start <= end <= self.request.end:
            raise TiveloError(
                ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Dates are outside the market profile"
            )
        calendar = read_bar_calendar(reader, universe=universe, start=start, end=end)
        members = _compile_members(self.request, bar_calendar=calendar)
        bound = self.model_copy(update={"venue_rules": tuple(rule.reference for rule in members)})
        return CNPortfolioRules(bound, members)


def _compile_members(
    request: CashEquityProfileRequest, *, bar_calendar: CalendarVersion | None = None
) -> tuple[CNTradingRules, ...]:
    classes = request.instrument_classes
    if classes is None:
        raise TiveloError(
            ErrorCode.CONTRACT_INVALID, "Portfolio archives must pin security classes"
        )
    members = []
    for venue in sorted({item.venue_id for item in classes}):
        calendar = None
        if bar_calendar is not None:
            calendar = CalendarVersion.create(
                bar_calendar.contents.model_copy(
                    update={
                        "calendar_id": f"cn.equity.{venue.lower()}.regular-auction",
                        "venue_id": venue,
                    }
                )
            )
        profile = compile_cash_equity_profile(
            request.model_copy(
                update={
                    "venue_id": venue,
                    "instrument_classes": tuple(item for item in classes if item.venue_id == venue),
                }
            ),
            calendar=calendar,
        )
        members.append(
            CNTradingRules(
                profile.configuration,
                CNCalendar(profile.calendar, from_bars=bar_calendar is not None),
            )
        )
    return tuple(members)


def compile_portfolio_profile(
    request: CashEquityProfileRequest, *, use_bars: bool
) -> CNPortfolioProfile:
    version = request.selected_preset_version
    classes = request.instrument_classes
    if classes is None:
        quantities = load_quantity_preset(version=version)
        boards = {(item.venue_id, item.board) for item in quantities.contents.schedules}
        classes = tuple(
            CNEquityClass(
                venue_id=venue, board=board, risk_status="normal", listing_phase="regular"
            )
            for venue, board in sorted(boards)
            if request.venue_id is None or venue == request.venue_id
        )
    if not classes:
        raise TiveloError(
            ErrorCode.UNSUPPORTED_ASSET, "The selected preset has no matching classes"
        )
    request = request.model_copy(
        update={
            "preset_version": version,
            "instrument_classes": tuple(sorted(classes, key=lambda item: item.spec_revision)),
        }
    )
    if use_bars:
        return CNPortfolioProfile(profile_format="tivelo-cn-bars-v1", request=request)
    return CNPortfolioProfile(
        request=request,
        venue_rules=tuple(member.reference for member in _compile_members(request)),
    )


def load_portfolio(profile: CNPortfolioProfile) -> CNPortfolioRules:
    members = _compile_members(profile.request)
    if tuple(member.reference for member in members) != profile.venue_rules:
        raise TiveloError(
            ErrorCode.DATA_CONFLICT, "Compiled venue rules differ from the portfolio pins"
        )
    return CNPortfolioRules(profile, members)


def _clock_key(day: TradingDay) -> tuple[date, datetime, datetime, str]:
    return day.trading_date, day.open_at, day.close_at, day.bar_convention_id


class CNPortfolioRules:
    market_id = "cn.equity"
    buy_cost_monotone = True

    def __init__(self, profile: CNPortfolioProfile, members: tuple[CNTradingRules, ...]):
        self._members = {member.calendar.version.contents.venue_id: member for member in members}
        self.version = profile.request.selected_preset_version
        self.observed_bar_clock = profile.uses_bars
        self.reference = DefinitionReference(
            definition_id="cn.equity.portfolio.cash-research",
            version=self.version,
            sha256=fingerprint(profile),
            parameters_json=dumps({"calendar_source": "bars"}) if self.observed_bar_clock else "{}",
        )

    def trading_days(self, start: date, end: date, *, as_of: datetime) -> tuple[TradingDay, ...]:
        primary, *others = (
            member.trading_days(start, end, as_of=as_of) for member in self._members.values()
        )
        clock = tuple(map(_clock_key, primary))
        if any(tuple(map(_clock_key, days)) != clock for days in others):
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Venue calendars differ at the daily clock")
        return tuple(
            rows[0].model_copy(
                update={
                    "calendar_id": _COMMON_CALENDAR_ID,
                    "available_at": max(day.available_at for day in rows),
                }
            )
            for rows in zip(primary, *others, strict=True)
        )

    def next_open(self, after_date: date, *, as_of: datetime) -> datetime:
        openings = {member.next_open(after_date, as_of=as_of) for member in self._members.values()}
        if len(openings) != 1:
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Venue calendars differ at the next opening")
        return openings.pop()

    def matches_calendar(self, instrument: Instrument, day: TradingDay) -> bool:
        member = self._members.get(instrument.venue_id)
        return (
            day.calendar_id == _COMMON_CALENDAR_ID
            and member is not None
            and instrument.calendar_id == member.calendar.version.contents.calendar_id
        )

    def supports(self, instrument: Instrument) -> bool:
        member = self._members.get(instrument.venue_id)
        return member is not None and member.supports(instrument)

    def _for(self, instrument: Instrument) -> CNTradingRules:
        member = self._members.get(instrument.venue_id)
        if member is None:
            raise TiveloError(
                ErrorCode.UNSUPPORTED_ASSET, "Instrument venue is outside the A-share profile"
            )
        return member

    def trading_terms(self, instrument: Instrument, *, at: datetime) -> TradingTerms:
        return self._for(instrument).trading_terms(instrument, at=at)

    def is_open(self, instrument: Instrument, *, at: datetime) -> bool:
        return self._for(instrument).is_open(instrument, at=at)

    def buy_quantity(
        self,
        instrument: Instrument,
        quantity: Decimal,
        *,
        at: datetime,
        order_type: OrderType | None = None,
    ) -> Decimal:
        return self._for(instrument).buy_quantity(
            instrument, quantity, at=at, order_type=order_type
        )

    def sell_quantity(
        self,
        instrument: Instrument,
        quantity: Decimal,
        *,
        available: Decimal,
        at: datetime,
        order_type: OrderType | None = None,
    ) -> Decimal:
        return self._for(instrument).sell_quantity(
            instrument, quantity, available=available, at=at, order_type=order_type
        )

    def quantity_terms(
        self, instrument: Instrument, order_type: OrderType, *, at: datetime
    ) -> OrderQuantityTerms:
        return self._for(instrument).quantity_terms(instrument, order_type, at=at)

    def order_quantity(
        self,
        instrument: Instrument,
        side: str,
        quantity: Decimal,
        *,
        order_type: OrderType,
        available: Decimal | None = None,
        at: datetime,
    ) -> Decimal:
        return self._for(instrument).order_quantity(
            instrument, side, quantity, order_type=order_type, available=available, at=at
        )

    def sellable_at(self, instrument: Instrument, filled_at: datetime) -> datetime:
        return self._for(instrument).sellable_at(instrument, filled_at)

    def price_bounds(
        self, instrument: Instrument, reference: ReferencePrice | None, *, at: datetime
    ) -> PriceBounds:
        return self._for(instrument).price_bounds(instrument, reference, at=at)

    def fee_breakdown(
        self,
        instrument: Instrument,
        side: str,
        quantity: Decimal,
        raw_price: Decimal,
        *,
        at: datetime,
    ) -> FeeQuote:
        return self._for(instrument).fee_breakdown(instrument, side, quantity, raw_price, at=at)

    def fees(
        self,
        instrument: Instrument,
        side: str,
        quantity: Decimal,
        raw_price: Decimal,
        *,
        at: datetime,
    ) -> Decimal:
        return self.fee_breakdown(instrument, side, quantity, raw_price, at=at).total
