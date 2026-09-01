from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from .job_store import RUN_ID_PATTERN

MAX_SUMMARY_BYTES = 20 * 1024 * 1024


def _write_bytes(path: Path, data: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


class RunStoreError(RuntimeError):
    pass


class FactorRunStore:
    def __init__(self, data_dir):
        self.root = Path(data_dir) / "research" / "factor_runs"
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, run_id):
        if RUN_ID_PATTERN.fullmatch(run_id) is None:
            raise ValueError("invalid run_id")
        return self.root / run_id

    def publish(self, run_id, summary, raw_result, events=None, series=None):
        target = self._dir(run_id)
        if target.exists():
            raise RunStoreError("run is immutable")
        staging = Path(tempfile.mkdtemp(prefix=f".staging-{run_id}-", dir=self.root))
        try:
            for name, value in (("summary.json", summary), ("raw-result.json", raw_result)):
                data = json.dumps(
                    value, ensure_ascii=False, default=str, separators=(",", ":")
                ).encode()
                if name == "summary.json" and len(data) > MAX_SUMMARY_BYTES:
                    raise RunStoreError("summary exceeds 20MiB")
                _write_bytes(staging / name, data)
            import polars as pl

            event_rows = []
            for row in events or []:
                event_rows.append(
                    {
                        "symbol": row.get("symbol"),
                        "arm": row.get("arm"),
                        "event_date": row.get("event_date"),
                        "qualified": row.get("qualified"),
                        "reachable": row.get("reachable"),
                        "censor_code": row.get("censor_code"),
                        "label": row.get("label"),
                        "detail": json.dumps(
                            row.get("detail") or {},
                            ensure_ascii=False,
                            default=str,
                            separators=(",", ":"),
                        ),
                    }
                )
            event_schema = {
                "symbol": pl.String,
                "arm": pl.String,
                "event_date": pl.String,
                "qualified": pl.Boolean,
                "reachable": pl.Boolean,
                "censor_code": pl.String,
                "label": pl.String,
                "detail": pl.String,
            }
            events_path = staging / "events.parquet"
            pl.DataFrame(event_rows, schema=event_schema).write_parquet(events_path)
            _fsync_file(events_path)
            series_rows = []
            for kind, points in (series or {}).items():
                for point in points:
                    series_rows.append(
                        {"kind": kind, "date": point.get("date"), "value": point.get("value")}
                    )
            series_path = staging / "series.parquet"
            pl.DataFrame(
                series_rows, schema={"kind": pl.String, "date": pl.String, "value": pl.Float64}
            ).write_parquet(series_path)
            _fsync_file(series_path)
            manifest = {"run_id": run_id, "files": {}}
            for path in staging.iterdir():
                data = path.read_bytes()
                rows = pl.read_parquet(path).height if path.suffix == ".parquet" else None
                manifest["files"][path.name] = {
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                    "rows": rows,
                }
            manifest_data = json.dumps(manifest, separators=(",", ":")).encode()
            _write_bytes(staging / "manifest.json", manifest_data)
            os.replace(staging, target)
            directory_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return manifest
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def read_summary(self, run_id):
        path = self._dir(run_id) / "summary.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def read_manifest(self, run_id):
        path = self._dir(run_id) / "manifest.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def read_events(self, run_id, cursor=0, limit=200, filters=None):
        if limit > 200:
            raise ValueError("limit must be <= 200")
        path = self._dir(run_id) / "events.parquet"
        if not path.exists():
            return []
        import polars as pl

        frame = pl.read_parquet(path)
        for key, value in (filters or {}).items():
            if value is not None and key in frame.columns:
                frame = frame.filter(pl.col(key) == value)
        rows = frame.slice(cursor, limit).to_dicts()
        for row in rows:
            detail = row.get("detail")
            if isinstance(detail, str):
                row["detail"] = json.loads(detail)
        return rows

    def events(self, run_id, cursor=0, limit=200):
        return self.read_events(run_id, cursor, limit)

    def read_series(self, run_id, kinds=None, max_points=2000):
        if max_points > 2000:
            raise ValueError("max_points must be <= 2000")
        path = self._dir(run_id) / "series.parquet"
        if not path.exists():
            return {}
        import polars as pl

        frame = pl.read_parquet(path)
        if kinds:
            frame = frame.filter(pl.col("kind").is_in(kinds))
        result = {}
        for key, part in frame.partition_by("kind", as_dict=True).items():
            name = key[0] if isinstance(key, tuple) else key
            rows = part.to_dicts()
            if len(rows) > max_points:
                stride = (len(rows) + max_points - 1) // max_points
                rows = rows[::stride][:max_points]
            result[str(name)] = rows
        return result

    def series(self, run_id, kind=None, max_points=2000):
        return self.read_series(run_id, [kind] if kind else None, max_points)
