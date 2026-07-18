"""Stream normalized financial reports from PostgreSQL into ClickHouse."""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib import parse, request
from zoneinfo import ZoneInfo

CLICKHOUSE_DDL = """
CREATE TABLE IF NOT EXISTS {database}.lb_financial_report
(
    symbol String,
    market LowCardinality(String),
    report_type LowCardinality(String),
    report_period LowCardinality(String),
    fiscal_year UInt16,
    fp_end Nullable(Date),
    field LowCardinality(String),
    name String,
    value Nullable(Float64),
    ratio Nullable(Float64),
    yoy Nullable(Float64),
    percent Bool,
    industry_ranking String,
    ranking_code LowCardinality(String),
    currency LowCardinality(String),
    updated_at DateTime64(3, 'Asia/Shanghai')
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY fiscal_year
ORDER BY (symbol, report_type, field, fiscal_year, report_period)
"""

_SHANGHAI = ZoneInfo("Asia/Shanghai")

_SOURCE_COLUMNS = """
    id, symbol, market, report_type, report_period, fiscal_year, fp_end,
    field, name, value, ratio, yoy, percent, industry_ranking, ranking_code,
    currency, updated_at
"""


@dataclass(frozen=True)
class MigrationState:
    full_complete: bool = False
    last_id: int = 0
    updated_at: datetime | None = None


@dataclass(frozen=True)
class MigrationSummary:
    rows: int
    batches: int
    state: MigrationState


class FinancialMigration:
    def __init__(
        self,
        source: Any,
        write_rows: Any,
        checkpoint: Any,
        *,
        batch_size: int = 20_000,
        overlap: timedelta = timedelta(minutes=5),
    ) -> None:
        self._source = source
        self._write_rows = write_rows
        self._checkpoint = checkpoint
        self._batch_size = batch_size
        self._overlap = overlap

    def run(self) -> MigrationSummary:
        state = self._checkpoint.load()
        total_rows = 0
        batches = 0
        if state.full_complete:
            start = state.updated_at - self._overlap if state.updated_at else None
            iterator = self._source.iter_incremental(start, self._batch_size)
        else:
            iterator = self._source.iter_full(state.last_id, self._batch_size)

        for source_rows in iterator:
            if not source_rows:
                continue
            normalized = [normalize_source_row(row) for row in source_rows]
            self._write_rows(normalized)
            batches += 1
            total_rows += len(normalized)
            newest = _max_updated_at(source_rows, state.updated_at)
            state = MigrationState(
                full_complete=state.full_complete,
                last_id=max(state.last_id, max(int(row.get("id") or 0) for row in source_rows)),
                updated_at=newest,
            )
            self._checkpoint.save(state)

        if not state.full_complete:
            state = MigrationState(
                full_complete=True,
                last_id=state.last_id,
                updated_at=state.updated_at,
            )
            self._checkpoint.save(state)
        return MigrationSummary(rows=total_rows, batches=batches, state=state)


class JsonCheckpoint:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> MigrationState:
        if not self._path.exists():
            return MigrationState()
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        updated_at = _parse_datetime(raw.get("updated_at"))
        return MigrationState(
            full_complete=bool(raw.get("full_complete")),
            last_id=int(raw.get("last_id") or 0),
            updated_at=updated_at,
        )

    def save(self, state: MigrationState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "full_complete": state.full_complete,
            "last_id": state.last_id,
            "updated_at": state.updated_at.isoformat() if state.updated_at else None,
        }
        handle, temp_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=self._path.parent,
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as temp_file:
                json.dump(payload, temp_file, ensure_ascii=False)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_name, self._path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


class PostgresFinancialSource:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def iter_full(self, last_id: int, batch_size: int):
        sql = f"""
            SELECT {_SOURCE_COLUMNS}
            FROM public.lb_financial_report
            WHERE id > %s
            ORDER BY id
        """
        yield from self._iter(sql, (last_id,), batch_size)

    def iter_incremental(self, start: datetime | None, batch_size: int):
        if start is None:
            yield from self.iter_full(0, batch_size)
            return
        sql = f"""
            SELECT {_SOURCE_COLUMNS}
            FROM public.lb_financial_report
            WHERE updated_at >= %s
            ORDER BY updated_at, id
        """
        yield from self._iter(sql, (start,), batch_size)

    def _iter(self, sql: str, params: tuple[Any, ...], batch_size: int):
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.read_only = True
            with connection.cursor(name="financial_clickhouse_migration") as cursor:
                cursor.itersize = batch_size
                cursor.execute(sql, params)
                while rows := cursor.fetchmany(batch_size):
                    yield [dict(row) for row in rows]


class ClickHouseFinancialWriter:
    def __init__(
        self,
        url: str,
        database: str,
        *,
        user: str = "",
        password: str = "",
        timeout: float = 120.0,
    ) -> None:
        if not database.replace("_", "a").isalnum() or database[:1].isdigit():
            raise ValueError("invalid ClickHouse database identifier")
        self._url = url.rstrip("/")
        self._database = database
        self._user = user
        self._password = password
        self._timeout = timeout

    def ensure_table(self) -> None:
        self._request(CLICKHOUSE_DDL.format(database=self._database).encode("utf-8"))

    def write_rows(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        header = f"INSERT INTO {self._database}.lb_financial_report FORMAT JSONEachRow\n"
        body = header + "\n".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows
        )
        self._request(gzip.compress(body.encode("utf-8")), compressed=True)

    def _request(self, body: bytes, *, compressed: bool = False) -> bytes:
        endpoint = f"{self._url}/?database={parse.quote(self._database, safe='')}"
        req = request.Request(endpoint, data=body, method="POST")
        if self._user:
            req.add_header("X-ClickHouse-User", self._user)
        if self._password:
            req.add_header("X-ClickHouse-Key", self._password)
        if compressed:
            req.add_header("Content-Encoding", "gzip")
        with request.urlopen(req, timeout=self._timeout) as response:
            return response.read()


def normalize_source_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return the compact, JSONEachRow-safe ClickHouse representation."""

    return {
        "symbol": _text(row.get("symbol")).upper(),
        "market": _text(row.get("market")).lower(),
        "report_type": _text(row.get("report_type")).upper(),
        "report_period": _text(row.get("report_period")),
        "fiscal_year": int(row.get("fiscal_year") or 0),
        "fp_end": _date_text(row.get("fp_end")),
        "field": _text(row.get("field")),
        "name": _text(row.get("name")),
        "value": _number(row.get("value")),
        "ratio": _number(row.get("ratio")),
        "yoy": _number(row.get("yoy")),
        "percent": bool(row.get("percent")),
        "industry_ranking": _text(row.get("industry_ranking")),
        "ranking_code": _text(row.get("ranking_code")),
        "currency": _text(row.get("currency")).upper(),
        "updated_at": _datetime_text(row.get("updated_at")),
    }


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        value = float(value)
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _date_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def _datetime_text(value: Any) -> str:
    if not isinstance(value, datetime):
        return "1970-01-01 00:00:00.000"
    if value.tzinfo is not None:
        value = value.astimezone(_SHANGHAI).replace(tzinfo=None)
    return value.isoformat(sep=" ", timespec="milliseconds")


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_SHANGHAI)
    return parsed


def _max_updated_at(rows: list[dict[str, Any]], current: datetime | None) -> datetime | None:
    values = [_parse_datetime(row.get("updated_at")) for row in rows]
    candidates = [value for value in [current, *values] if value is not None]
    return max(candidates, key=lambda value: value.timestamp()) if candidates else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=Path(os.getenv("FINANCIAL_MIGRATION_CHECKPOINT", "data/sync_state/financial_clickhouse.json")),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(os.getenv("FINANCIAL_MIGRATION_BATCH_SIZE", "20000")),
    )
    args = parser.parse_args()
    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")

    postgres_dsn = os.getenv("POSTGRES_FINANCIAL_DSN", "").strip()
    clickhouse_url = os.getenv("CLICKHOUSE_URL", "").strip()
    if not postgres_dsn:
        parser.error("POSTGRES_FINANCIAL_DSN is required")
    if not clickhouse_url:
        parser.error("CLICKHOUSE_URL is required")

    writer = ClickHouseFinancialWriter(
        clickhouse_url,
        os.getenv("CLICKHOUSE_DATABASE", "longbridge"),
        user=os.getenv("CLICKHOUSE_USER", ""),
        password=os.getenv("CLICKHOUSE_PASSWORD", ""),
        timeout=float(os.getenv("CLICKHOUSE_WRITE_TIMEOUT_SECONDS", "120")),
    )
    writer.ensure_table()
    migration = FinancialMigration(
        PostgresFinancialSource(postgres_dsn),
        writer.write_rows,
        JsonCheckpoint(args.checkpoint),
        batch_size=args.batch_size,
    )
    summary = migration.run()
    print(json.dumps(asdict(summary), ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
