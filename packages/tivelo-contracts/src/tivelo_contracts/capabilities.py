"""One deterministic resolution of all runtime capability constraints."""

from typing import Literal

from pydantic import StrictBool

from .base import Contract, Key

CapabilityStatus = Literal[
    "available",
    "not_installed",
    "disabled",
    "incompatible",
    "needs_configuration",
    "needs_license",
    "license_expired",
    "missing_data",
    "unsupported_market",
    "pending_restart",
    "load_failed",
]


class CapabilityFacts(Contract):
    feature_id: Key
    installed: StrictBool
    trusted: StrictBool
    compatible: StrictBool
    enabled: StrictBool
    configured: StrictBool
    licensed: StrictBool
    license_expired: StrictBool
    supports_market: StrictBool
    data_ready: StrictBool
    pending_restart: StrictBool
    load_failed: StrictBool


class Capability(Contract):
    feature_id: Key
    status: CapabilityStatus
    reasons: tuple[Key, ...]


def resolve_capability(facts: CapabilityFacts) -> Capability:
    failures: list[tuple[CapabilityStatus, str]] = [
        (status, reason)
        for failed, status, reason in (
            (not facts.installed, "not_installed", "package_missing"),
            (not facts.trusted, "incompatible", "package_untrusted"),
            (not facts.compatible, "incompatible", "runtime_or_api_mismatch"),
            (facts.load_failed, "load_failed", "registration_failed"),
            (not facts.enabled, "disabled", "feature_disabled"),
            (facts.pending_restart, "pending_restart", "activation_required"),
            (not facts.configured, "needs_configuration", "configuration_incomplete"),
            (facts.license_expired, "license_expired", "lease_expired"),
            (not facts.licensed, "needs_license", "entitlement_missing"),
            (not facts.supports_market, "unsupported_market", "market_not_supported"),
            (not facts.data_ready, "missing_data", "data_requirements_unsatisfied"),
        )
        if failed
    ]
    return Capability(
        feature_id=facts.feature_id,
        status=failures[0][0] if failures else "available",
        reasons=tuple(reason for _, reason in failures),
    )
