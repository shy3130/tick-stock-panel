"""Shared conversion for suppliers that report mainland stock history in shares."""

import csv
import hashlib
import io
import json
from datetime import UTC, datetime, time, timedelta
from decimal import localcontext
from importlib.metadata import distribution, version
from importlib.resources import files
from pathlib import PurePosixPath

from pydantic import ValidationError
from tivelo_contracts.errors import ErrorCode, TiveloError

from ..legacy.frame import halted_daily_values
from ..providers import ProviderCapability, ProviderDescriptor
from .cn_daily import CN
from .cn_observation import CNDailyObservation
from .v1 import V1BarAdapter, legacy_decimal


def _supplier_artifacts(transport):
    # PyInstaller's Python modules live in PYZ. Python 3.12 distribution.files
    # drops RECORD paths absent on disk. The original wheel manifest still
    # identifies precisely the Python and native supplier code in the release.
    record = distribution(transport).read_text("RECORD")
    artifacts = []
    try:
        for path, digest, _size in csv.reader(io.StringIO(record or ""), strict=True):
            file = PurePosixPath(path)
            if file.parts[:1] != (transport,) or file.suffix.lower() not in {
                ".py",
                ".so",
                ".dylib",
                ".dll",
            }:
                continue
            algorithm, sep, value = digest.partition("=")
            if not (algorithm and sep and value):
                raise ValueError("Missing vendor wheel digest")
            artifacts.append((path, algorithm, value))
    except (ValueError, csv.Error):
        raise TiveloError(ErrorCode.RUNTIME_MISMATCH, "Invalid supplier wheel manifest") from None
    if not artifacts:
        raise TiveloError(
            ErrorCode.RUNTIME_MISMATCH, "Cannot identify the installed supplier runtime"
        )
    return sorted(artifacts)


def _release_digest(vendor, transport, *, factors=False):
    # Credentials, private INIs and v1 plugin installations are outside the software identity.
    sources = {
        "fuyao": ("fuyao_daily.py", "legacy/fuyao_history.py"),
        "tdxquant": (
            "tdx_daily.py",
            "tdx_connection.py",
            "legacy/tdx_runtime.py",
            "_source_process.py",
        ),
    }
    adapter_paths = sources[vendor]
    if factors:
        vendor_factors = (
            ("fuyao_factors.py", "_http_request.py") if vendor == "fuyao" else ("tdx_factors.py",)
        )
        adapter_paths += (
            *vendor_factors,
            "adapters/cn_ex_rights.py",
            "adapters/cn_factor_observation.py",
            "adapters/cn_factors.py",
        )
    root = files("tivelo_data")
    adapter = {}
    for path in (
        *adapter_paths,
        "adapters/stock_history.py",
        "adapters/cn_daily.py",
        "adapters/cn_observation.py",
        "adapters/v1.py",
        "legacy/frame.py",
        "legacy/pacing.py",
    ):
        adapter[path] = hashlib.sha256(root.joinpath(*path.split("/")).read_bytes()).hexdigest()
    return hashlib.sha256(
        json.dumps(
            {
                "data": version("tivelo-data"),
                "contracts": version("tivelo-contracts"),
                "market": version("tivelo-market-cn"),
                "adapter": adapter,
                "runtime": _supplier_artifacts(transport),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def source_credential(ref, resolve_token, context):
    value = resolve_token(ref) if resolve_token else context.resolve_credentials(ref).get("token")
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(ord(c) < 32 or c == "\x7f" for c in value)
    ):
        raise TiveloError(
            ErrorCode.AUTHENTICATION_FAILED, "Stock source requires a valid credential"
        )
    return value


class StockHistoryObservation(CNDailyObservation):
    def __init__(self, source, config, *, name, runtime, batch, resolve_token, now):
        if source.expected_provider != name:
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, "Requested provider differs from history source"
            )
        super().__init__(
            source, now or (lambda: datetime.now(UTC)), days_per_page=32, symbols_per_page=batch
        )
        self.name = name
        self.config = config
        self.resolve_token = resolve_token
        self.convention = f"cn.equity.{name}.beijing-date"
        self.descriptor = ProviderDescriptor(
            provider_id=name,
            version=f"daily.1.{runtime}",
            source_ids=(name,),
            capabilities=(
                ProviderCapability(
                    market_id="cn.equity",
                    asset_class="equity",
                    record_type="bar",
                    timeframes=("1d",),
                    price_bases=("raw",),
                    knowledge_basis="latest_reconstruction",
                    normalization_version=f"{name}.daily.raw.shares.v1",
                    bar_convention_id=self.convention,
                ),
            ),
        )
        declaration = {
            "source": self.descriptor.model_dump(),
            "release": _release_digest(name, "httpx" if name == "fuyao" else "tdxaidata"),
            "settings": config.model_dump(),
            "calendar": self.preset.version_id,
            "bindings": [s.model_dump() for s in source.symbols],
        }
        self.configuration_digest = hashlib.sha256(
            json.dumps(declaration, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()

    def describe(self):
        return self.descriptor

    def credential(self, context):
        return source_credential(self.config.credential_ref, self.resolve_token, context)

    def _bounds(self, day):
        start = datetime.combine(day.trading_date, time.min, tzinfo=CN)
        return start, start + timedelta(days=1), self.convention

    def _read(self, days, symbols, context):
        bindings = {s.symbol: s.instrument_id for s in symbols}
        sessions = {d.trading_date: d for d in days}
        rows = self.history_rows(days, tuple(bindings), context)
        stamp = self._now().astimezone(UTC)
        adapter = V1BarAdapter(
            self.name,
            "daily_raw_v1",
            "raw",
            "1d",
            "CNY",
            "share",
            "quantity",
            lambda symbol, _: bindings[symbol],
            stamp,
            daily_bounds=lambda day: self._bounds(sessions[day]),
        )
        revision = "observation-" + stamp.strftime("%Y%m%dT%H%M%S%fZ")
        returned, traded, records = set(), set(), []
        for row in rows:
            context.check_cancelled()
            cell = row["symbol"], row["date"]
            if cell[0] not in bindings or cell[1] not in sessions or cell in returned:
                raise TiveloError(ErrorCode.DATA_CONFLICT, "Duplicate or out-of-scope daily bar")
            returned.add(cell)
            numbers = {
                k: legacy_decimal(row[k])
                for k in ("open", "high", "low", "close", "volume", "amount")
            }
            volume, amount = numbers["volume"], numbers["amount"]
            low, high = numbers["low"], numbers["high"]
            if min(numbers.values()) < 0 or volume != volume.to_integral_value():
                raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid daily prices or quantities")
            if halted_daily_values(numbers["open"], high, volume, amount):
                continue
            if min(low, numbers["open"], numbers["close"]) <= 0 or not (
                low <= numbers["open"] <= high and low <= numbers["close"] <= high
            ):
                raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid daily OHLC range")
            try:
                with localcontext() as ctx:
                    ctx.prec = max(80, len(amount.as_tuple().digits))
                    numbers["amount"] = amount * self.turnover_multiplier
                records.append(adapter.convert({**row, **numbers}, revision_id=revision))
            except (ArithmeticError, ValidationError):
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID, "Unrepresentable supplier daily bar"
                ) from None
            traded.add(cell)
        self._record_coverage(bindings, sessions, traded)
        return tuple(records)
