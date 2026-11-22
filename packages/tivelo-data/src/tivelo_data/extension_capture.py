"""Observe a TSP extension field without changing the source table or assuming prior vintages."""

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
from contextlib import closing
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pyarrow.parquet as pq
from pydantic import Field, model_validator
from tivelo_contracts.base import (
    Contract,
    Key,
    UTCDateTime,
    canonical_bytes,
    fingerprint,
    parse_json,
    utc_datetime,
)
from tivelo_contracts.data import AssetClass
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.extension_fields import ExtensionField, ExtensionValue
from tivelo_contracts.providers import SyncRequest

from .legacy.repository_files import changed, date_partition_files, identity
from .storage import _sync_directory, durable_mkdir, file_digest


class TSPFieldCapture(Contract):
    dataset_id: Key
    source_id: Key
    table_id: str = Field(strict=True, pattern=r"^[a-zA-Z0-9_]+$", max_length=255)
    field_id: Key
    field_version: Key
    unit: Key
    market_id: Key = "cn.equity"
    asset_class: AssetClass = "equity"
    timezone: Key = "Asia/Shanghai"
    start: UTCDateTime
    end: UTCDateTime
    symbols: dict[Key, Key]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.start >= self.end or len(set(self.symbols.values())) != len(self.symbols):
            raise ValueError("an observation needs a nonempty interval and unique symbol bindings")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("unknown observation timezone") from exc
        return self


def _stamp():
    return datetime.now(UTC)


def _encoded(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def _descriptor(config, mapping):
    mode = config["mode"]
    market = config.get("market_level", False)
    if (
        config["id"] != mapping.table_id
        or mode not in ("timeseries", "snapshot")
        or type(market) is not bool
    ):
        raise ValueError("invalid TSP table configuration")
    fields = {f["name"]: f.get("dtype", "string") for f in config["fields"]}
    if (
        len(fields) != len(config["fields"])
        or mapping.field_id not in fields
        or market == bool(mapping.symbols)
    ):
        raise ValueError("field or instrument mapping does not match the TSP table")
    time_field = (config.get("pull") or {}).get("time_field") if mode == "timeseries" else None
    # Credentials, jobs, labels and modification times are not part of the research schema.
    structure = {
        "mode": mode,
        "market_level": market,
        "fields": fields,
        "time_field": time_field,
        "symbol_map": config.get("symbol_map") or {},
        "code_map": config.get("code_map") or {},
    }
    field = ExtensionField(
        table_id=mapping.table_id,
        table_version=hashlib.sha256(_encoded(structure).encode()).hexdigest(),
        field_id=mapping.field_id,
        field_version=mapping.field_version,
        value_type=fields[mapping.field_id],
        unit=mapping.unit,
        granularity="market" if market else "instrument",
        time_basis="witnessed_snapshot"
        if mode == "snapshot"
        else f"tsp_partition.{mapping.timezone}",
    )
    return field, mode, time_field


def _inventory(root, mode, mapping, zone):
    if mode == "snapshot":
        path = root / "part.parquet"
        if path.is_symlink():
            raise ValueError("invalid TSP snapshot file")
        if not path.exists():
            return {}
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("invalid TSP snapshot file")
        return {path: identity(info)}
    parts = root / "timeseries"
    if parts.is_symlink():
        raise ValueError("invalid TSP timeseries directory")
    if not parts.exists():
        return {}
    selected = date_partition_files(
        parts, mapping.start.astimezone(zone).date(), mapping.end.astimezone(zone).date()
    )
    return {path: info for path, info in selected.items() if path.name == "part.parquet"}


def _local_instant(day, clock, zone):
    wall = datetime.combine(day, clock)
    instant = wall.replace(tzinfo=zone)
    if (
        instant.utcoffset() != instant.replace(fold=1).utcoffset()
        or instant.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != wall
    ):
        raise ValueError("ambiguous local time in TSP table; use an offset-aware timestamp")
    return instant.astimezone(UTC)


def _slot(day, raw_time, zone):
    if raw_time is None:
        return _local_instant(day, time.min, zone)
    if isinstance(raw_time, str):
        if re.fullmatch(r"\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?", raw_time):
            raw_time = time.fromisoformat(raw_time)
        elif re.fullmatch(
            r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?",
            raw_time,
        ):
            raw_time = datetime.fromisoformat(raw_time)
    if isinstance(raw_time, time):
        raw_time = datetime.combine(day, raw_time)
    if isinstance(raw_time, datetime) and raw_time.tzinfo is None:
        result = _local_instant(raw_time.date(), raw_time.time(), zone)
    else:
        result = utc_datetime(raw_time)
    if result.astimezone(zone).date() != day:
        raise ValueError("intraday observation disagrees with the TSP partition date")
    return result


def _stage(connection, paths, mapping, field, mode, time_field, zone):
    columns = [mapping.field_id]
    if field.granularity == "instrument":
        columns.append("symbol")
    if time_field:
        columns.append(time_field)
    for path in sorted(paths):
        day = date.fromisoformat(path.parent.name[5:]) if mode == "timeseries" else None
        expiry = (
            _local_instant(day + timedelta(days=1), time.min, zone).isoformat() if day else None
        )
        parquet = pq.ParquetFile(path)
        if not set(columns).issubset(parquet.schema_arrow.names):
            raise ValueError("configured column is missing from TSP Parquet")
        for batch in parquet.iter_batches(batch_size=2048, columns=list(dict.fromkeys(columns))):
            rows = []
            for item in batch.to_pylist():
                subject = mapping.symbols.get(item["symbol"]) if mapping.symbols else ""
                if subject is None:
                    continue
                slot = "live"
                if day:
                    if time_field and item[time_field] is None:
                        raise ValueError("missing intraday observation time")
                    effective = _slot(day, item.get(time_field), zone)
                    if not mapping.start <= effective < mapping.end:
                        continue
                    slot = effective.isoformat()
                raw = item[mapping.field_id]
                if field.value_type == "float" and raw is not None:
                    if isinstance(raw, bool) or not isinstance(raw, (int, float, Decimal)):
                        raise ValueError("TSP decimal field contains nonnumeric data")
                    raw = str(raw)
                rows.append((subject, slot, expiry, _encoded(raw)))
            try:
                connection.executemany("INSERT INTO sample VALUES (?,?,?,?)", rows)
            except sqlite3.IntegrityError as exc:
                raise TiveloError(
                    ErrorCode.DATA_CONFLICT,
                    "TSP table has multiple rows for one subject and instant",
                ) from exc


def _append(connection, stream, request, source_id, observed):
    field = request.extension_field
    digest = fingerprint(field)
    stamp = observed.isoformat(timespec="microseconds")
    previous = connection.execute("SELECT stamp FROM clocks WHERE stream=?", (stream,)).fetchone()
    if previous and stamp < previous[0]:
        raise TiveloError(
            ErrorCode.DATA_CONFLICT, "Observation clock must advance between captures"
        )
    # Keep the diff fixed while advancing heads. SQLite can spill large captures to disk.
    connection.execute(
        """CREATE TEMP TABLE delta AS
        SELECT s.subject,s.slot,s.expiry,s.token FROM sample s LEFT JOIN heads h
          ON h.stream=? AND h.subject=s.subject AND h.slot=s.slot WHERE s.token IS NOT h.token
        UNION ALL
        SELECT h.subject,h.slot,h.expiry,NULL FROM heads h WHERE h.stream=? AND h.token IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM sample s WHERE s.subject=h.subject AND s.slot=h.slot)
        ORDER BY 1,2
    """,
        (stream, stream),
    )
    column = {
        "int": "int_value",
        "float": "decimal_value",
        "string": "text_value",
        "bool": "bool_value",
    }[field.value_type]
    if (
        previous
        and stamp == previous[0]
        and connection.execute("SELECT 1 FROM delta LIMIT 1").fetchone()
    ):
        raise TiveloError(
            ErrorCode.DATA_CONFLICT, "Changed observations need distinct capture times"
        )
    for subject, slot, expiry, token in connection.execute(
        "SELECT * FROM delta ORDER BY subject,slot"
    ):
        effective = observed if slot == "live" else utc_datetime(slot)
        if not request.start <= effective < request.end:
            raise ValueError("live snapshot observation is outside the requested interval")
        row = ExtensionValue(
            field_sha256=digest,
            instrument_id=subject or None,
            effective_at=effective,
            valid_until=expiry,
            available_at=observed,
            ingested_at=observed,
            revision_id=stamp,
            source_id=source_id,
            **{column: json.loads(token) if token is not None else None},
        )
        connection.execute(
            "INSERT INTO events VALUES (?,?,?,?,?)",
            (stream, subject, slot, stamp, canonical_bytes(row)),
        )
        connection.execute(
            "INSERT OR REPLACE INTO heads VALUES (?,?,?,?,?)",
            (stream, subject, slot, expiry, token),
        )
    connection.execute("INSERT OR REPLACE INTO clocks VALUES (?,?)", (stream, stamp))


def _export(connection, stream, output):
    durable_mkdir(output)
    with tempfile.NamedTemporaryFile(dir=output, prefix=".ext-", delete=False) as file:
        temporary = Path(file.name)
    try:
        with temporary.open("wb") as file:
            for (row,) in connection.execute(
                "SELECT body FROM events WHERE stream=? ORDER BY subject,slot,stamp", (stream,)
            ):
                file.write(row + b"\n")
            file.flush()
            os.fsync(file.fileno())
        digest = file_digest(temporary)
        target = output / f"ext-{digest}.jsonl"
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.is_symlink() or file_digest(target) != digest:
                raise TiveloError(
                    ErrorCode.DATA_CONFLICT, "Captured JSONL bytes differ from their identity"
                ) from None
        _sync_directory(output)
        return target.name
    finally:
        temporary.unlink(missing_ok=True)


def observe_tsp_field(root: Path, repository: Path, mapping: TSPFieldCapture, output: Path):
    """Append actually witnessed changes, then emit an immutable local_jsonl workbench input."""
    root, repository, output = (Path(p).resolve() for p in (root, repository, output))
    mapping = TSPFieldCapture.model_validate_json(canonical_bytes(mapping))
    source = repository / "ext_data" / mapping.table_id
    config_path = source / "config.json"
    if root.is_relative_to(repository / "ext_data") or output.is_relative_to(
        repository / "ext_data"
    ):
        raise ValueError("journal or exported observations overlap TSP source tables")
    if any(p.is_symlink() for p in (source.parent, source, config_path)):
        raise ValueError("TSP table must be a local directory with regular schema and data files")
    durable_mkdir(root)
    zone = ZoneInfo(mapping.timezone)
    with (
        closing(sqlite3.connect(root / "extension-observations.sqlite3", timeout=30)) as connection,
        connection,
    ):
        connection.execute("BEGIN IMMEDIATE")
        for ddl in (
            "CREATE TABLE IF NOT EXISTS heads(stream,subject,slot,expiry,token,PRIMARY KEY(stream,subject,slot))",
            "CREATE TABLE IF NOT EXISTS events(stream,subject,slot,stamp,body BLOB,PRIMARY KEY(stream,subject,slot,stamp))",
            "CREATE TABLE IF NOT EXISTS clocks(stream PRIMARY KEY,stamp)",
            "CREATE TEMP TABLE sample(subject,slot,expiry,token,PRIMARY KEY(subject,slot))",
        ):
            connection.execute(ddl)
        config_identity = identity(config_path.stat())
        config = parse_json(config_path.read_text(encoding="utf-8"))
        try:
            field, mode, time_field = _descriptor(config, mapping)
        except (KeyError, AttributeError, TypeError) as exc:
            raise ValueError("invalid TSP extension table schema") from exc
        request = SyncRequest(
            dataset_id=mapping.dataset_id,
            record_type="extension_value",
            market_id=mapping.market_id,
            asset_class=mapping.asset_class,
            instrument_ids=tuple(sorted(mapping.symbols.values())),
            start=mapping.start,
            end=mapping.end,
            extension_field=field,
        )
        paths = _inventory(source, mode, mapping, zone)
        _stage(connection, paths, mapping, field, mode, time_field, zone)
        if (
            identity(config_path.stat()) != config_identity
            or _inventory(source, mode, mapping, zone) != paths
        ):
            raise changed()
        stream = hashlib.sha256(
            canonical_bytes(mapping) + canonical_bytes(field) + os.fsencode(source)
        ).hexdigest()
        _append(connection, stream, request, mapping.source_id, _stamp())
        name = _export(connection, stream, output)
    return {
        "input": {
            "kind": "local_jsonl",
            "request": request.model_dump(mode="json"),
            "path": name,
            "source_id": mapping.source_id,
            "knowledge_basis": "historical_versions",
        },
        "field_reference": {"dataset_id": request.dataset_id, "field_sha256": fingerprint(field)},
    }
