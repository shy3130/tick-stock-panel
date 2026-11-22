"""Bounded physical locations that can be reported to a CLI or workbench."""

from typing import Annotated, Literal

from pydantic import Field
from tivelo_contracts.base import CalendarDate, Contract, Digest
from tivelo_contracts.errors import ErrorCode, TiveloError

Layout = Literal["daily_raw_v1", "enriched_v1"]
SourceSymbol = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
SourceColumn = Literal[
    "open",
    "high",
    "low",
    "close",
    "raw_open",
    "raw_high",
    "raw_low",
    "raw_close",
    "volume",
    "amount",
]
IssueReason = Literal["fractional_precision", "integral_range", "invalid_number"]
QuoteReason = Literal["quote_date_mismatch", "quote_before_finalization", "quote_after_observation"]


class RepositoryRowLocation(Contract):
    layout: Layout | Literal["event_factors_v1"]
    trading_date: CalendarDate
    symbol: SourceSymbol
    row_number: int = Field(strict=True, ge=1, le=2**53 - 1)
    file_sha256: Digest


class RepositoryDecimalIssue(RepositoryRowLocation):
    layout: Layout
    field: SourceColumn
    reason: IssueReason


class RepositoryQuoteIssue(RepositoryRowLocation):
    layout: Layout
    field: Literal["quote_ts"] = "quote_ts"
    reason: QuoteReason


class RepositoryFactorIssue(RepositoryRowLocation):
    layout: Literal["event_factors_v1"] = "event_factors_v1"
    field: Literal["ex_factor"] = "ex_factor"
    reason: Literal["fractional_precision", "integral_range"]


class RepositorySourceError(TiveloError):
    def __init__(
        self,
        issues: tuple[RepositoryDecimalIssue | RepositoryQuoteIssue | RepositoryFactorIssue, ...],
    ):
        if not issues:
            raise ValueError("A repository source error requires a physical location")
        if isinstance(issues[0], RepositoryQuoteIssue):
            super().__init__(
                ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                "Repository contains an unfinished or stale daily quote",
            )
        elif isinstance(issues[0], RepositoryFactorIssue):
            super().__init__(
                ErrorCode.CONTRACT_INVALID, "Legacy stock event ratio exceeds decimal128(38,12)"
            )
        else:
            super().__init__(
                ErrorCode.CONTRACT_INVALID, "Legacy daily fact exceeds decimal128(38,12)"
            )
        self.issues = issues
