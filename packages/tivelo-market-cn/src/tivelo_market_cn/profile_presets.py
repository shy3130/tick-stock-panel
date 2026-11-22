"""Assemble bounded, reproducible domestic cash-equity market profiles."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator
from tivelo_contracts.base import CalendarDate, Contract, Key, canonical_bytes
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import MarketAdapterSpec
from tivelo_contracts.market import TradingTerms

from .adapter import CNMarketProfile
from .calendar import CN_TIMEZONE, CalendarVersion
from .classification import CNEquityClass
from .fee_presets import BrokerFeePolicy, compile_fee_rules, load_fee_preset
from .order_prices import ObservedBookAgeMs, continuous_limit_reference
from .presets import Venue, compile_calendar, load_calendar_preset
from .price_presets import compile_price_terms, load_price_preset, resolve_price_rules
from .quantity_presets import load_quantity_preset, resolve_quantity_rules
from .rules import RuleConfiguration

DEFAULT_PRESET_VERSION = "2026.2.0"
_SHANGHAI_SHENZHEN_PRESET_VERSION = "2026.1.0"


class CashEquityProfileRequest(Contract):
    venue_id: Venue | None = None
    start: CalendarDate
    end: CalendarDate
    broker_policy: BrokerFeePolicy
    preset_version: Key | None = None
    instrument_classes: tuple[CNEquityClass, ...] | None = Field(default=None, min_length=1)
    calendar_source: Literal["bars", "preset"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    profile_format: Literal[
        "tivelo-cn-profile-v3", "tivelo-cn-profile-v4", "tivelo-cn-profile-v5"
    ] = "tivelo-cn-profile-v3"
    observed_book_max_age_ms: ObservedBookAgeMs | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.end < self.start:
            raise ValueError("profile range is reversed")
        if self.instrument_classes is not None:
            if self.venue_id is not None and any(
                item.venue_id != self.venue_id for item in self.instrument_classes
            ):
                raise ValueError("all instrument classes must be for the selected venue")
            if len(set(self.instrument_classes)) != len(self.instrument_classes):
                raise ValueError("profile instrument classes must be unique")
        if (self.profile_format == "tivelo-cn-profile-v5") != (
            self.observed_book_max_age_ms is not None
        ):
            raise ValueError("maximum observed book age is required only for profile v5")
        if self.profile_format != "tivelo-cn-profile-v3" and self.venue_id not in ("XSHG", "XSHE"):
            raise ValueError("continuous auction profiles require XSHG or XSHE")
        if self.profile_format != "tivelo-cn-profile-v3" and self.calendar_source == "bars":
            raise ValueError("continuous auction profiles require declared sessions")
        return self

    @property
    def selected_preset_version(self) -> str:
        if self.preset_version is not None:
            return self.preset_version
        if self.venue_id in ("XSHG", "XSHE"):
            return _SHANGHAI_SHENZHEN_PRESET_VERSION
        return DEFAULT_PRESET_VERSION


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time(), CN_TIMEZONE).astimezone(UTC)


def _base_terms(quantities, prices, classification, *, start, end):
    first, stop = _midnight(start), _midnight(end + timedelta(days=1))
    sources = {source.source_id: source.available_at for source in quantities.contents.sources}
    boundaries = {first, stop}
    for rule in quantities.contents.schedules:
        if (rule.venue_id, rule.board) == (classification.venue_id, classification.board):
            active = max(_midnight(rule.effective_from), *(sources[key] for key in rule.source_ids))
            if first < active < stop:
                boundaries.add(active)

    terms = []
    for since, until in pairwise(sorted(boundaries)):
        quantity = resolve_quantity_rules(quantities, classification, at=since)
        price = resolve_price_rules(prices, classification, at=since)
        limit = next(item for item in quantity.order_quantities if item.order_type == "limit")
        terms.append(
            TradingTerms(
                spec_revision=classification.spec_revision,
                market_id="cn.equity",
                venue_id=classification.venue_id,
                asset_class="equity",
                instrument_type=classification.instrument_type,
                currency="CNY",
                quantity_unit="share",
                price_tick=price.price_tick,
                price_limit_mode=price.price_limit_mode,
                limit_up_ratio=price.limit_up_ratio,
                limit_down_ratio=price.limit_down_ratio,
                order_quantity_rules=quantity.order_quantities,
                sell_lag_sessions=1,
                source_id=quantities.contents.preset_id,
                source_reference=f"{quantity.rule_id}:{quantity.preset_version_id}:next-session-research",
                valid_from=since,
                valid_to=until,
                available_at=quantity.available_at,
                revision_id=f"{quantity.rule_id}:{since.isoformat()}",
                **limit.model_dump(exclude={"order_type"}),
            )
        )
    return tuple(terms)


def compile_cash_equity_profile(
    request: CashEquityProfileRequest, *, calendar: CalendarVersion | None = None
) -> CNMarketProfile:
    """Use versioned market inputs and a declared broker policy in the existing archive."""
    request = CashEquityProfileRequest.model_validate_json(request.model_dump_json())
    if request.venue_id is None:
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Specify a venue for a single-venue profile")
    version = request.selected_preset_version
    interval = dict(venue_id=request.venue_id, start=request.start, end=request.end)
    if calendar is None:
        calendar = compile_calendar(load_calendar_preset(version=version), **interval)
    fees = compile_fee_rules(load_fee_preset(version=version), request.broker_policy, **interval)
    if request.broker_policy.available_at > _midnight(request.start):
        raise TiveloError(ErrorCode.RULES_UNAVAILABLE, "Initial broker policy was not yet known")
    quantities = load_quantity_preset(version=version)
    if (
        not quantities.contents.coverage_start
        <= request.start
        <= request.end
        <= quantities.contents.coverage_end
    ):
        raise TiveloError(
            ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Range is outside quantity coverage"
        )
    prices = load_price_preset(version=version)
    classes = request.instrument_classes or (
        CNEquityClass(
            venue_id=request.venue_id,
            board="beijing" if request.venue_id == "XBSE" else "main",
            risk_status="normal",
            listing_phase="regular",
        ),
    )
    classes = tuple(sorted(classes, key=lambda item: item.spec_revision))
    compilations = tuple(
        compile_price_terms(
            prices,
            classification,
            _base_terms(quantities, prices, classification, start=request.start, end=request.end),
        )
        for classification in classes
    )
    continuous = (
        continuous_limit_reference(version=version)
        if request.profile_format != "tivelo-cn-profile-v3"
        else None
    )
    return CNMarketProfile(
        profile_format=request.profile_format,
        calendar=calendar,
        fee_compilation=fees,
        price_compilations=compilations,
        continuous_limit=continuous,
        observed_book_max_age_ms=request.observed_book_max_age_ms,
        configuration=RuleConfiguration(
            schema_version="1.3",
            profile_id=f"cn.equity.{request.venue_id.lower()}.cash-research",
            profile_version=version,
            currency="CNY",
            instrument_classes=classes,
            terms=tuple(term for compilation in compilations for term in compilation.terms),
            required_fee_ids=fees.required_fee_ids,
            fee_rules=fees.fee_rules,
        ),
    )


def compile_market_adapter(request: CashEquityProfileRequest) -> MarketAdapterSpec:
    request = CashEquityProfileRequest.model_validate_json(request.model_dump_json())
    use_bars = (
        request.profile_format == "tivelo-cn-profile-v3" and request.calendar_source != "preset"
    )
    if use_bars or request.venue_id is None:
        from .portfolio import compile_portfolio_profile

        configuration = compile_portfolio_profile(request, use_bars=use_bars)
    else:
        configuration = compile_cash_equity_profile(request)
    return MarketAdapterSpec(
        adapter_id="tivelo.market.cn.cash-equity",
        distribution="tivelo-market-cn",
        configuration_json=configuration.model_dump_json(),
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("profile-schema", help="Print the market profile request JSON Schema")
    build = commands.add_parser("profile", help="Compile a locked market adapter specification")
    build.add_argument("--request", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "profile-schema":
            print(json.dumps(CashEquityProfileRequest.model_json_schema(), sort_keys=True))
        else:
            request = CashEquityProfileRequest.model_validate_json(args.request.read_bytes())
            print(canonical_bytes(compile_market_adapter(request)).decode())
        return 0
    except TiveloError as exc:
        code, message = exc.code, str(exc)
    except (OSError, ValueError):
        code, message = ErrorCode.CONTRACT_INVALID, "Invalid or unreadable market profile request"
    print(json.dumps({"code": code, "message": message}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
