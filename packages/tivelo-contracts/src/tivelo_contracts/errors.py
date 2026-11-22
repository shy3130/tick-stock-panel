"""Stable public error codes, with retry policy independent of exception text."""

from enum import StrEnum


class ErrorCode(StrEnum):
    CONTRACT_INVALID = "CONTRACT_INVALID"
    SCHEMA_UNSUPPORTED = "SCHEMA_UNSUPPORTED"
    DATA_CONFLICT = "DATA_CONFLICT"
    DATA_COVERAGE_INSUFFICIENT = "DATA_COVERAGE_INSUFFICIENT"
    SNAPSHOT_NOT_FOUND = "SNAPSHOT_NOT_FOUND"
    SNAPSHOT_CORRUPT = "SNAPSHOT_CORRUPT"
    AMBIGUOUS_INSTRUMENT = "AMBIGUOUS_INSTRUMENT"
    INSTRUMENT_NOT_FOUND = "INSTRUMENT_NOT_FOUND"
    RAW_PRICE_REQUIRED = "RAW_PRICE_REQUIRED"
    FACTOR_INCOMPATIBLE = "FACTOR_INCOMPATIBLE"
    PRICE_BASIS_UNPROVEN = "PRICE_BASIS_UNPROVEN"
    FUTURE_INFORMATION = "FUTURE_INFORMATION"
    UNSUPPORTED_ASSET = "UNSUPPORTED_ASSET"
    UNSUPPORTED_MARKET = "UNSUPPORTED_MARKET"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    RULES_UNAVAILABLE = "RULES_UNAVAILABLE"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    LICENSE_REQUIRED = "LICENSE_REQUIRED"
    LICENSE_EXPIRED = "LICENSE_EXPIRED"
    EXTENSION_INCOMPATIBLE = "EXTENSION_INCOMPATIBLE"
    RESOURCE_BUSY = "RESOURCE_BUSY"
    REFERENCED_ARTIFACT = "REFERENCED_ARTIFACT"
    CANCELLED = "CANCELLED"
    STRATEGY_FAILED = "STRATEGY_FAILED"
    DEFINITION_NOT_FOUND = "DEFINITION_NOT_FOUND"
    DEFINITION_CORRUPT = "DEFINITION_CORRUPT"
    DEFINITION_CONFLICT = "DEFINITION_CONFLICT"
    RUNTIME_MISMATCH = "RUNTIME_MISMATCH"
    ARTIFACT_CORRUPT = "ARTIFACT_CORRUPT"
    PUBLICATION_FAILED = "PUBLICATION_FAILED"


class TiveloError(Exception):
    """Only use safe public messages here; supplier responses may contain secrets."""

    def __init__(self, code: ErrorCode, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.code = ErrorCode(code)
        self.retryable = self.code in {
            ErrorCode.RATE_LIMITED,
            ErrorCode.SOURCE_UNAVAILABLE,
            ErrorCode.RESOURCE_BUSY,
        }
        if retry_after is not None and (not self.retryable or not 0 <= retry_after < float("inf")):
            raise ValueError(
                "retry_after requires a retryable error and a finite nonnegative delay"
            )
        self.retry_after = retry_after


def require_api_major(actual: int, supported: int = 2) -> None:
    if type(actual) is not int or actual != supported:
        raise TiveloError(ErrorCode.SCHEMA_UNSUPPORTED, f"API major {actual!r} is not supported")
