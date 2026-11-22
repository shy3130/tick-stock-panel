"""Resolve only the explicitly selected installed market adapter; never install code."""

from dataclasses import dataclass
from importlib import metadata

from packaging.utils import canonicalize_name
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import MarketAdapterSpec
from tivelo_contracts.protocols import DailyExecutionRules

ENTRY_POINT_GROUP = "tivelo.market_rules.v1"


@dataclass(frozen=True)
class InstalledMarketAdapter:
    specification: MarketAdapterSpec
    entry_point: metadata.EntryPoint

    def _create(self):
        try:
            factory = self.entry_point.load()
            return factory(configuration_json=self.specification.configuration_json)
        except (Exception, SystemExit) as exc:
            raise TiveloError(
                ErrorCode.RULES_UNAVAILABLE, "Selected market adapter could not create rules"
            ) from exc

    def load_legacy_minute_limits(self):
        """Load the symbol, date and historical name lookup for legacy minute fills."""
        limits = self._create()
        if not callable(limits):
            raise TiveloError(
                ErrorCode.RULES_UNAVAILABLE, "Selected market adapter lacks legacy minute price limits"
            )
        return limits

    def load(self, *, continuous=False, reader=None, run=None):
        """Execute installed trusted code, after the caller verifies its runtime lock."""
        rules = self._create()
        if continuous:
            from .continuous import ContinuousTradeMarket

            if not isinstance(rules, ContinuousTradeMarket):
                raise TiveloError(
                    ErrorCode.UNSUPPORTED_CAPABILITY,
                    "Selected market lacks continuous auction admission and execution rules",
                )
            return rules
        bind_snapshot = getattr(rules, "bind_daily_snapshot", None)
        if callable(bind_snapshot):
            if reader is None or run is None:
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID, "This daily market profile needs the run snapshot"
                )
            rules = bind_snapshot(
                reader, universe=run.universe, start=run.warmup_start or run.start, end=run.end,
            )
        if getattr(rules, "requires_order_book", False) is True:
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "This market profile requires a current raw order book; daily data cannot validate its limit prices",
            )
        if not isinstance(rules, DailyExecutionRules):
            raise TiveloError(
                ErrorCode.RULES_UNAVAILABLE,
                "Selected market adapter does not implement daily execution rules",
            )
        return rules


def discover_market_adapter(specification: MarketAdapterSpec) -> InstalledMarketAdapter:
    specification = MarketAdapterSpec.model_validate_json(specification.model_dump_json())
    try:
        matches = [
            entry
            for entry in metadata.entry_points(group=ENTRY_POINT_GROUP)
            if entry.name == specification.adapter_id
        ]
        if len(matches) != 1 or canonicalize_name(
            matches[0].dist.metadata["Name"]
        ) != canonicalize_name(specification.distribution):
            raise ValueError("missing, duplicate or wrong distribution")
        return InstalledMarketAdapter(specification, matches[0])
    except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
        raise TiveloError(
            ErrorCode.RULES_UNAVAILABLE, "No unique installed market adapter matches the request"
        ) from exc
