"""Versioned local inputs for strategy entry filters and extension fields."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field
from tivelo_contracts.base import Digest, canonical_bytes
from tivelo_contracts.errors import TiveloError

from app.factors.ext_factors import ext_column_name
from app.market_time import CN_TZ
from app.services.ext_data import ExtConfig, ExtConfigStore
from app.services.fs_utils import atomic_write_text

_DATE = "_ext_date"
_KNOWN = "_available_at"
_BUILTIN = "builtin"
_CALENDAR = "calendar"


class ExtensionEntryCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_id: str = Field(min_length=1, max_length=120)
    field: str = Field(min_length=1, max_length=256)
    op: Literal[">", ">=", "<", "<=", "==", "!=", "contains"]
    value: str = Field(min_length=1, max_length=512)


class EntryFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: list[ExtensionEntryCondition] = Field(default_factory=list, max_length=8)
    stocks: list[ExtensionEntryCondition] = Field(default_factory=list, max_length=16)

    @property
    def enabled(self) -> bool:
        return bool(self.market or self.stocks)


class _InputFile(BaseModel):
    digest: Digest
    label: str
    config: dict | None = None


class _InputManifest(BaseModel):
    version: Literal[1] = 1
    start: date
    end: date
    symbols: tuple[str, ...] | None
    files: dict[str, _InputFile]


def _identity(path: Path) -> tuple[int, int, int]:
    st = path.stat()
    return st.st_ino, st.st_mtime_ns, st.st_size


def _digest(path: Path) -> str:
    if path.is_symlink():
        raise ValueError("过滤数据版本文件不能是符号链接")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _describe(config: ExtConfig) -> dict:
    # An input version contains table schema and observations only.
    return {
        "id": config.id, "label": config.label, "mode": config.mode,
        "market_level": config.market_level,
        "fields": [field.to_dict() for field in config.fields],
    }


def _known_time(column: str) -> pl.Expr:
    raw = pl.col(column).cast(pl.String).str.strip_chars()
    zoned = pl.when(raw.str.contains(r"(?:Z|[+-]\d{2}:?\d{2})$")).then(raw).otherwise(raw + "+08:00")
    return zoned.str.to_datetime(format="%+", time_zone="UTC", strict=False)


class EntryInputData:
    """Capture the consumed history once, or read the same content on replay."""

    def __init__(self, data_dir: Path, version: str | None = None):
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "backtest_filter_data"
        self.version = version
        self._manifest = None
        self._scope = None
        self._frames: dict[str, pl.DataFrame] = {}
        self._metadata: dict[str, tuple[str, dict | None]] = {}
        self._configs: list[ExtConfig] | None = None
        if version is not None:
            path = self._path(version, ".json")
            try:
                if _digest(path) != version:
                    raise ValueError("过滤数据版本清单校验失败")
                self._manifest = _InputManifest.model_validate_json(path.read_bytes())
            except OSError as exc:
                raise ValueError("找不到本次回测的过滤数据版本") from exc

    def set_scope(self, start: date, end: date, symbols: list[str] | None) -> None:
        selected = tuple(sorted(set(symbols))) if symbols else None
        if self._manifest is not None:
            locked = self._manifest
            if not locked.start <= start <= end <= locked.end or (
                locked.symbols is not None
                and (selected is None or set(selected) - set(locked.symbols))
            ):
                raise ValueError("回测范围超出已固定的过滤数据，请使用新数据版本")
        self._scope = (start, end, selected)

    def configs(self) -> list[ExtConfig]:
        if self._configs is None:
            if self._manifest is not None:
                self._configs = [
                    ExtConfig.from_dict(file.config)
                    for file in self._manifest.files.values() if file.config is not None
                ]
            else:
                self._configs = [
                    cfg for cfg in ExtConfigStore(self.data_dir).load_all()
                    if cfg.mode == "timeseries"
                ]
        return self._configs

    def _path(self, digest: str, suffix: str) -> Path:
        if self.root.is_symlink() or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("过滤数据版本标识无效")
        return self.root / (digest + suffix)

    def _read(self, key: str) -> pl.DataFrame:
        file = self._manifest.files.get(key)
        if file is None:
            raise ValueError("已固定版本中没有这项输入，请使用新数据版本")
        path = self._path(file.digest, ".parquet")
        try:
            if _digest(path) != file.digest:
                raise ValueError("过滤数据版本校验失败")
            return pl.read_parquet(path)
        except (OSError, pl.exceptions.PolarsError) as exc:
            raise ValueError("无法读取已固定的过滤数据") from exc

    def _load(self, key, label, reader, config=None) -> pl.DataFrame:
        if key not in self._frames:
            self._frames[key] = self._read(key) if self._manifest is not None else reader()
            self._metadata[key] = (label, config)
        return self._frames[key]

    def builtin(self) -> pl.DataFrame:
        from app.services.regime_builder import load_regime_history

        def read():
            start, end, _ = self._scope
            frame = load_regime_history(self.data_dir)
            return frame.filter(pl.col("date").is_between(start, end)) if "date" in frame.columns else frame

        return self._load(_BUILTIN, "内置市场环境", read)

    def calendar(self) -> pl.DataFrame:
        from tivelo_market_cn.calendar import CNCalendar
        from tivelo_market_cn.presets import compile_calendar, installed_calendar_presets

        def read():
            start, end, _ = self._scope
            lower = start - timedelta(days=32)
            covered = None
            rows = {}
            for preset in installed_calendar_presets():
                years = [year.year for year in preset.contents.years if year.venue_id == "XSHG"]
                if not years:
                    continue
                first = max(lower, date(min(years), 1, 1))
                last = min(end, date(max(years), 12, 31))
                if first > last:
                    continue
                try:
                    version = compile_calendar(preset, venue_id="XSHG", start=first, end=last)
                    calendar = CNCalendar(version)
                    rows = {
                        day.trading_date: day.close_at
                        for day in calendar.trading_days(first, last, as_of=datetime.now(UTC))
                    }
                    covered = (first, last)
                    break
                except TiveloError:
                    continue
            if covered != (lower, end):
                # Use recorded market-wide sessions only outside the installed calendar.
                for table in ("kline_daily", "kline_daily_enriched"):
                    for partition in (self.data_dir / table).glob("date=*"):
                        try:
                            day = date.fromisoformat(partition.name[5:])
                        except ValueError:
                            continue
                        if (lower <= day <= end and (covered is None or not covered[0] <= day <= covered[1])
                                and next(partition.glob("*.parquet"), None)):
                            rows[day] = datetime.combine(day, time(15), tzinfo=CN_TZ).astimezone(UTC)
            return pl.DataFrame(
                sorted(rows.items()), schema={"date": pl.Date, "decision_at": pl.Datetime("us", "UTC")},
                orient="row",
            ).with_columns(pl.col("date").shift(1).alias("previous_date"))

        return self._load(_CALENDAR, "市场交易日历", read)

    def table(self, table_id: str, *, market: bool) -> tuple[ExtConfig, pl.DataFrame]:
        config = next((cfg for cfg in self.configs() if cfg.id == table_id), None)
        if config is None or config.market_level != market:
            raise ValueError(f"扩展时序表「{table_id}」不可用，请选择匹配的数据范围")
        frame = self._load(
            "ext:" + table_id, config.label, lambda: self._capture_table(config), _describe(config)
        )
        return config, frame

    def _capture_table(self, config: ExtConfig) -> pl.DataFrame:
        start, end, selected = self._scope
        if config.market_level:
            calendar = self.calendar()
            if calendar.height:
                first = calendar.filter(pl.col("date") >= start)["previous_date"].min()
                if first is not None:
                    start = first
        directory = self.data_dir / "ext_data" / config.id / "timeseries"
        if not directory.resolve().is_relative_to((self.data_dir / "ext_data").resolve()):
            raise ValueError("扩展数据表目录无效")

        def inventory():
            return {
                path: _identity(path)
                for path in sorted(directory.glob("date=*/part.parquet"))
                if start.isoformat() <= path.parent.name[5:] <= end.isoformat()
            }

        try:
            files = inventory()
            parts = []
            fields = [field for field in config.fields if field.dtype in ("int", "float", "string")]
            for path in files:
                if path.is_symlink() or path.parent.is_symlink():
                    raise ValueError(f"扩展表「{config.label}」含有无效的历史分区路径")
                scan = pl.scan_parquet(path)
                schema = scan.collect_schema()
                columns = []
                if not config.market_level:
                    if "symbol" not in schema:
                        raise ValueError(f"扩展时序表「{config.label}」缺少标的列")
                    if selected:
                        scan = scan.filter(pl.col("symbol").is_in(selected))
                    columns.append(pl.col("symbol").cast(pl.String))
                for field in fields:
                    if field.name in schema:
                        dtype = pl.String if field.dtype == "string" else pl.Float64
                        columns.append(pl.col(field.name).cast(dtype).alias(ext_column_name(config.id, field.name)))
                if "available_at" in schema:
                    columns.extend((
                        _known_time("available_at").alias(_KNOWN),
                        pl.col("available_at").is_not_null().alias("_has_publication_time"),
                    ))
                if not columns:
                    continue
                part = scan.select(columns).collect()
                if "_has_publication_time" in part.columns:
                    if part.select((pl.col("_has_publication_time") & pl.col(_KNOWN).is_null()).any()).item():
                        raise ValueError(f"扩展表「{config.label}」在 {path.parent.name[5:]} 的可知时间无效")
                    part = part.drop("_has_publication_time")
                elif config.market_level:
                    part = part.tail(1)
                else:
                    part = part.unique(subset=["symbol"], keep="last", maintain_order=True)
                if not part.is_empty():
                    parts.append(part.with_columns(pl.lit(date.fromisoformat(path.parent.name[5:])).alias(_DATE)))
            current = ExtConfigStore(self.data_dir).get(config.id)
            if inventory() != files or current is None or _describe(current) != _describe(config):
                raise ValueError(f"扩展表「{config.label}」在固定版本时更新，请重新运行")
        except (OSError, pl.exceptions.PolarsError) as exc:
            raise ValueError(f"无法读取扩展表「{config.label}」的历史数据") from exc
        return pl.concat(parts, how="diagonal_relaxed") if parts else pl.DataFrame()

    def legacy_frame(self, config: ExtConfig) -> pl.DataFrame:
        _, frame = self.table(config.id, market=False)
        if frame.is_empty():
            return frame
        return (
            frame.drop(_KNOWN, strict=False)
            .unique(subset=["symbol", _DATE], keep="last", maintain_order=True)
            .with_columns(pl.col(_DATE).cast(pl.String))
        )

    def freeze(self) -> str | None:
        from app.tickflow.repository import replace_with_retry

        if not self._frames or self.version is not None:
            return self.version
        start, end, symbols = self._scope
        if self.root.is_symlink():
            raise ValueError("过滤数据版本目录不能是符号链接")
        self.root.mkdir(parents=True, exist_ok=True)
        files = {}
        with TemporaryDirectory(dir=self.root, prefix="publish-") as staging:
            temporary = Path(staging) / "input.parquet"
            for key, frame in sorted(self._frames.items()):
                frame.write_parquet(temporary)
                digest = _digest(temporary)
                target = self._path(digest, ".parquet")
                if not target.exists() or _digest(target) != digest:
                    replace_with_retry(temporary, target)
                label, config = self._metadata[key]
                files[key] = _InputFile(digest=digest, label=label, config=config)
            manifest = _InputManifest(start=start, end=end, symbols=symbols, files=files)
            payload = canonical_bytes(manifest)
            version = hashlib.sha256(payload).hexdigest()
            target = self._path(version, ".json")
            if not target.exists() or _digest(target) != version:
                staged_manifest = Path(staging) / "manifest.json"
                atomic_write_text(staged_manifest, payload.decode("utf-8"), durable=True)
                replace_with_retry(staged_manifest, target)
        self.version = version
        return version

    def sources(self) -> list[dict]:
        return [
            {"id": key, "label": label, "knowledge_basis": "latest_reconstruction"}
            for key, (label, _) in sorted(self._metadata.items()) if key != _CALENDAR
        ]
