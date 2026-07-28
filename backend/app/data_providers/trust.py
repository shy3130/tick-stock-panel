"""Auditable market-data ingestion boundaries.

The module records what a selected provider actually returned.  It never
substitutes another provider and never manufactures rows.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import polars as pl

AuditStatus = Literal["ok", "partial", "empty", "invalid", "error"]
_ALLOWED_AUDIT_STATUSES = {"ok", "partial", "empty", "invalid", "error"}
_SAFE_DATASET = re.compile(r"^[a-z0-9_]+$")
_REQUIRED_COLUMNS = {
    "daily": {"symbol", "date", "open", "high", "low", "close", "volume", "amount"},
    "daily_enriched": {
        "symbol",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    },
    "adj_factor": {"symbol", "trade_date", "ex_factor"},
}


class DataProviderUnavailable(RuntimeError):  # noqa: N818
    """The explicitly selected provider cannot serve the requested dataset."""

    def __init__(self, provider: str, dataset: str, reason: str) -> None:
        self.provider = provider
        self.dataset = dataset
        self.reason = reason
        super().__init__(f"{provider} cannot provide {dataset}: {reason}")


class DataProviderFetchFailed(RuntimeError):  # noqa: N818
    """The selected provider failed; no alternate provider was attempted."""

    def __init__(self, provider: str, dataset: str, cause: Exception) -> None:
        self.provider = provider
        self.dataset = dataset
        self.cause = cause
        super().__init__(f"{provider} {dataset} fetch failed: {cause}")


class DataQualityRejected(RuntimeError):  # noqa: N818
    """Provider rows failed the persistence quality gate."""

    def __init__(self, audit: DataAudit) -> None:
        self.audit = audit
        detail = ", ".join(audit.issues) or audit.status
        super().__init__(f"{audit.provider} {audit.dataset} rejected: {detail}")


@dataclass(frozen=True)
class DataAudit:
    provider: str
    dataset: str
    status: AuditStatus
    row_count: int
    returned_symbols: tuple[str, ...]
    missing_symbols: tuple[str, ...]
    coverage_ratio: float
    fallback_used: bool = False
    synthetic: bool = False
    issues: tuple[str, ...] = ()
    observed_start: str | None = None
    observed_end: str | None = None

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "provider": self.provider,
            "dataset": self.dataset,
            "status": self.status,
            "row_count": self.row_count,
            "returned_symbols": list(self.returned_symbols),
            "missing_symbols": list(self.missing_symbols),
            "coverage_ratio": self.coverage_ratio,
            "fallback_used": self.fallback_used,
            "synthetic": self.synthetic,
            "issues": list(self.issues),
            "observed_start": self.observed_start,
            "observed_end": self.observed_end,
        }


def audit_market_frame(
    *,
    provider: str,
    dataset: str,
    frame: pl.DataFrame,
    requested_symbols: list[str],
    requested_end: date | datetime | None = None,
) -> DataAudit:
    """Summarize provider coverage without filling any missing observations."""
    issues: list[str] = []
    required_columns = _REQUIRED_COLUMNS.get(dataset, set())
    if dataset.startswith("financial_"):
        required_columns = {"symbol", "period_end", "announce_date"}
    if dataset == "financial_shares":
        required_columns = {"symbol", "period_end"}
    missing_columns = sorted(required_columns - set(frame.columns))
    if missing_columns and not frame.is_empty():
        issues.append(f"{dataset}.missing_columns:{','.join(missing_columns)}")
    if dataset in {"daily", "daily_enriched"} and {
        "open",
        "high",
        "low",
        "close",
    } <= set(frame.columns):
        invalid_ohlc = frame.filter(
            (pl.col("high") < pl.max_horizontal("open", "close", "low"))
            | (pl.col("low") > pl.min_horizontal("open", "close", "high"))
        ).height
        if invalid_ohlc:
            issues.append(f"{dataset}.invalid_ohlc:{invalid_ohlc}")
    if (
        dataset in {"daily", "daily_enriched"}
        and requested_end is not None
        and "date" in frame.columns
    ):
        end_date = requested_end.date() if isinstance(requested_end, datetime) else requested_end
        after_end = frame.filter(
            pl.col("date").cast(pl.Date, strict=False) > end_date
        ).height
        if after_end:
            issues.append(f"{dataset}.after_requested_end:{after_end}")
    requested = tuple(dict.fromkeys(requested_symbols))
    observed_column = {
        "daily": "date",
        "daily_enriched": "date",
        "adj_factor": "trade_date",
        "instruments": "as_of",
    }.get(dataset)
    if dataset.startswith("financial_"):
        observed_column = "period_end"
    observed_start = None
    observed_end = None
    if observed_column in frame.columns and not frame.is_empty():
        observed_start = _iso_value(frame[observed_column].min())
        observed_end = _iso_value(frame[observed_column].max())
    returned = (
        tuple(sorted(str(value) for value in frame["symbol"].drop_nulls().unique()))
        if not frame.is_empty() and "symbol" in frame.columns
        else ()
    )
    returned_set = set(returned)
    missing = tuple(symbol for symbol in requested if symbol not in returned_set)
    coverage = (len(requested) - len(missing)) / len(requested) if requested else 1.0
    status: AuditStatus
    if issues:
        status = "invalid"
    elif frame.is_empty():
        status = "empty"
    elif missing:
        status = "partial"
    else:
        status = "ok"
    return DataAudit(
        provider=provider,
        dataset=dataset,
        status=status,
        row_count=frame.height,
        returned_symbols=returned,
        missing_symbols=missing,
        coverage_ratio=coverage,
        issues=tuple(issues),
        observed_start=observed_start,
        observed_end=observed_end,
    )


def audit_market_error(
    *,
    provider: str,
    dataset: str,
    requested_symbols: list[str],
    error: Exception,
) -> DataAudit:
    requested = tuple(dict.fromkeys(requested_symbols))
    return DataAudit(
        provider=provider,
        dataset=dataset,
        status="error",
        row_count=0,
        returned_symbols=(),
        missing_symbols=requested,
        coverage_ratio=0.0 if requested else 1.0,
        issues=(f"{dataset}.fetch_error:{type(error).__name__}:{error}",),
    )


def write_latest_audit(
    data_dir: Path,
    audit: DataAudit,
    *,
    recorded_at: datetime | None = None,
) -> Path:
    """Atomically persist the latest receipt for one dataset."""
    if not _SAFE_DATASET.fullmatch(audit.dataset):
        raise ValueError(f"invalid dataset name: {audit.dataset!r}")
    out_dir = Path(data_dir) / "data_quality"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{audit.dataset}.json"
    temporary = out_dir / f".{audit.dataset}.{uuid4().hex}.tmp"
    payload = audit.to_dict()
    payload["recorded_at"] = (recorded_at or datetime.now(UTC)).isoformat()
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(out)
    return out


def load_latest_audits(data_dir: Path) -> list[dict]:
    """Load latest receipts and preserve field-level schema failures."""
    out_dir = Path(data_dir) / "data_quality"
    if not out_dir.exists():
        return []
    audits: list[dict] = []
    for path in sorted(out_dir.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict):
            value = dict(value)
            value.pop("schema_errors", None)
            schema_errors = validate_audit_receipt(value)
            if schema_errors:
                value["schema_errors"] = list(schema_errors)
            audits.append(value)
    return audits


def validate_audit_receipt(receipt: dict) -> tuple[str, ...]:
    """Return human-readable schema errors without coercing corrupt values."""
    errors: list[str] = []
    provider = receipt.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        errors.append("provider 必须是非空字符串")
    if receipt.get("status") not in _ALLOWED_AUDIT_STATUSES:
        errors.append("status 必须是 ok、partial、empty、invalid 或 error")
    coverage = receipt.get("coverage_ratio")
    valid_coverage = (
        not isinstance(coverage, bool)
        and (
            (isinstance(coverage, int) and 0 <= coverage <= 1)
            or (
                isinstance(coverage, float)
                and math.isfinite(coverage)
                and 0.0 <= coverage <= 1.0
            )
        )
    )
    if not valid_coverage:
        errors.append("coverage_ratio 必须是 0 到 1 之间的有限数值")
    for field in ("fallback_used", "synthetic"):
        if not isinstance(receipt.get(field), bool):
            errors.append(f"{field} 必须是布尔值")
    return tuple(errors)


def record_daily_enriched_audit(
    data_dir: Path,
    *,
    requested_symbols: list[str],
) -> DataAudit:
    """Record coverage for the actual latest persisted enriched partition."""
    enriched_dir = Path(data_dir) / "kline_daily_enriched"
    partitions = sorted(enriched_dir.glob("date=*"))
    if not partitions:
        raise FileNotFoundError("no persisted daily_enriched partition")
    parquet_files = sorted(partitions[-1].glob("*.parquet"))
    if not parquet_files:
        raise FileNotFoundError("latest daily_enriched partition has no parquet data")
    frame = pl.read_parquet(parquet_files)
    audit = audit_market_frame(
        provider="derived",
        dataset="daily_enriched",
        frame=frame,
        requested_symbols=requested_symbols,
    )
    write_latest_audit(data_dir, audit)
    return audit


def _iso_value(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
