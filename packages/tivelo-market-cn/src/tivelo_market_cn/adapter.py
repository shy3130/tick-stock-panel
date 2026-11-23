"""Explicit CN profile loading, with no implicit holidays, fees or classifications."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Self

from pydantic import Field, TypeAdapter, model_validator
from tivelo_contracts.base import Contract
from tivelo_contracts.execution import DefinitionReference

from .calendar import CalendarVersion, CNCalendar
from .fee_presets import CompiledFees, compile_fee_rules, load_fee_preset
from .order_prices import CNContinuousLimitOrders, ObservedBookAgeMs
from .price_presets import CompiledPriceTerms, compile_price_terms, load_price_preset
from .rules import CNTradingRules, RuleConfiguration

if TYPE_CHECKING:
    from .portfolio import CNPortfolioProfile, CNPortfolioRules

_PROFILE_PAYLOAD = TypeAdapter(dict[str, Any])


class CNMarketProfile(Contract):
    profile_format: Literal[
        "tivelo-cn-profile-v1",
        "tivelo-cn-profile-v2",
        "tivelo-cn-profile-v3",
        "tivelo-cn-profile-v4",
        "tivelo-cn-profile-v5",
    ] = "tivelo-cn-profile-v1"
    configuration: RuleConfiguration
    calendar: CalendarVersion
    fee_compilation: CompiledFees | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    price_compilations: tuple[CompiledPriceTerms, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    continuous_limit: DefinitionReference | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    observed_book_max_age_ms: ObservedBookAgeMs | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def coherent(self) -> Self:
        continuous = self.profile_format in ("tivelo-cn-profile-v4", "tivelo-cn-profile-v5")
        includes_prices = self.profile_format == "tivelo-cn-profile-v3" or continuous
        if not includes_prices and self.price_compilations:
            raise ValueError("archived price compilation requires profile format v3 or later")
        if continuous != (self.continuous_limit is not None):
            raise ValueError("profile v4 or v5 requires one continuous limit-price preset pin")
        if (self.profile_format == "tivelo-cn-profile-v5") != (
            self.observed_book_max_age_ms is not None
        ):
            raise ValueError("maximum observed book age is required only for profile v5")
        if self.profile_format == "tivelo-cn-profile-v1":
            if self.fee_compilation is not None:
                raise ValueError("archived fee compilation requires profile format v2")
            return self
        if self.fee_compilation is None:
            raise ValueError("profile v2 or later requires the original fee compilation inputs")
        compiled = self.fee_compilation
        inputs = compiled.inputs
        expected = compile_fee_rules(
            load_fee_preset(
                version=inputs.preset_version, expected_sha256=inputs.preset_version_id
            ),
            inputs.policy,
            venue_id=inputs.venue_id,
            start=inputs.start,
            end=inputs.end,
        )
        if compiled != expected:
            raise ValueError("compiled fees differ from their pinned preset and billing policy")
        if (
            self.configuration.required_fee_ids != compiled.required_fee_ids
            or self.configuration.fee_rules != compiled.fee_rules
        ):
            raise ValueError("configured fees differ from the archived compilation")
        if self.calendar.contents.venue_id != inputs.venue_id or any(
            term.venue_id != inputs.venue_id
            or term.asset_class != "equity"
            or term.instrument_type != "common_stock"
            for term in self.configuration.terms
        ):
            raise ValueError("fee preset applies only to the declared venue's common stocks")
        if includes_prices:
            if not self.price_compilations:
                raise ValueError(
                    "profile v3 or later requires the original price compilation inputs"
                )
            classes = tuple(p.inputs.classification for p in self.price_compilations)
            if len({c.spec_revision for c in classes}) != len(classes) or set(classes) != set(
                self.configuration.instrument_classes
            ):
                raise ValueError("compiled price classes differ from configured classes")
            price_terms = []
            for compiled_prices in self.price_compilations:
                source = compiled_prices.inputs
                expected_prices = compile_price_terms(
                    load_price_preset(
                        version=source.preset_version, expected_sha256=source.preset_version_id
                    ),
                    source.classification,
                    source.base_terms,
                )
                if compiled_prices != expected_prices:
                    raise ValueError("compiled prices differ from their pinned inputs")
                price_terms.extend(compiled_prices.terms)

            def key(term):
                return (term.spec_revision, term.valid_from, term.available_at, term.revision_id)

            if sorted(price_terms, key=key) != sorted(self.configuration.terms, key=key):
                raise ValueError("configured price terms differ from the archived compilation")
        return self


def create_rules(
    *, configuration_json: str
) -> CNTradingRules | CNContinuousLimitOrders | CNPortfolioRules | CNPortfolioProfile:
    payload = _PROFILE_PAYLOAD.validate_json(configuration_json)
    if payload.get("profile_format") in ("tivelo-cn-portfolio-v1", "tivelo-cn-bars-v1"):
        from .portfolio import CNPortfolioProfile, load_portfolio

        profile = CNPortfolioProfile.model_validate(payload)
        return profile if profile.uses_bars else load_portfolio(profile)
    profile = CNMarketProfile.model_validate(payload)
    market = CNTradingRules(profile.configuration, CNCalendar(profile.calendar))
    if profile.continuous_limit is not None:
        return CNContinuousLimitOrders(
            market,
            profile.continuous_limit,
            observed_book_max_age_ms=profile.observed_book_max_age_ms,
        )
    return market
