"""Raw Shanghai and Shenzhen stock daily observations through the TickFlow SDK."""

import hashlib
import json
import math
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from importlib.metadata import version

from pydantic import ValidationError
from tivelo_contracts.errors import ErrorCode, TiveloError

from .adapters.cn_daily import CN, CNRawDailyRequest
from .adapters.cn_observation import CNDailyObservation
from .adapters.v1 import V1BarAdapter
from .legacy.frame import halted_daily_values
from .providers import ProviderCapability, ProviderDescriptor
from .tickflow_connection import FREE_ENDPOINT as FREE_ENDPOINT
from .tickflow_connection import PAID_ENDPOINT as PAID_ENDPOINT
from .tickflow_connection import TickFlowConnection, TickFlowDailyConfig

BAR_CONVENTION = "cn.equity.tickflow.beijing-date"
FIELDS = ("timestamp", "open", "high", "low", "close", "volume", "amount")
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class TickFlowDailyProvider(CNDailyObservation):
    def __init__(
        self,
        source: CNRawDailyRequest,
        config: TickFlowDailyConfig,
        *,
        resolve_token: Callable[[str], str | None] | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        if source.expected_provider != "tickflow":
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Source differs from TickFlow")
        super().__init__(
            source,
            now or (lambda: datetime.now(UTC)),
            days_per_page=32,
            symbols_per_page=config.batch,
        )
        self.config = config
        self.connection = TickFlowConnection(config, resolve_token)
        self.sdk_version = version("tickflow")
        declaration = {
            **config.model_dump(exclude={"endpoint"}),
            "endpoint": config.base_url,
            "sdk": self.sdk_version,
            "calendar": self.preset.version_id,
            "bindings": [entry.model_dump() for entry in source.symbols],
        }
        self.configuration_digest = hashlib.sha256(
            json.dumps(declaration, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def describe(self):
        return ProviderDescriptor(
            provider_id="tickflow",
            version="daily.1.sdk." + self.sdk_version,
            source_ids=("tickflow",),
            capabilities=(
                ProviderCapability(
                    market_id="cn.equity",
                    asset_class="equity",
                    record_type="bar",
                    timeframes=("1d",),
                    price_bases=("raw",),
                    knowledge_basis="latest_reconstruction",
                    normalization_version="tickflow.daily.raw.lot100.v1",
                    bar_convention_id=BAR_CONVENTION,
                ),
            ),
        )

    def _bounds(self, day):
        start = datetime.combine(day.trading_date, time.min, tzinfo=CN)
        return start, start + timedelta(days=1), BAR_CONVENTION

    def check_ready(self, context):
        super().check_ready(context)
        if self.next_page is None:
            return
        self.connection.open(context)

    def _request(self, days, bindings, context):
        start, _, _ = self._bounds(days[0])
        _, end, _ = self._bounds(days[-1])
        with self.connection.request(context) as klines:
            # One SDK chunk per observed page. Its multi-chunk path discards failed chunks.
            return klines.batch(
                list(bindings),
                period="1d",
                adjust="none",
                count=len(days),
                start_time=(start - EPOCH) // timedelta(milliseconds=1),
                end_time=(end - EPOCH) // timedelta(milliseconds=1) - 1,
                batch_size=len(bindings),
                max_workers=1,
                as_dataframe=False,
            )

    def _read(self, days, symbols, context):
        bindings = {entry.symbol: entry.instrument_id for entry in symbols}
        sessions = {day.trading_date: day for day in days}
        payload = self._request(days, bindings, context)
        context.check_cancelled()
        if not isinstance(payload, dict):
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid TickFlow daily payload")
        if payload.keys() - bindings.keys():
            raise TiveloError(ErrorCode.DATA_CONFLICT, "TickFlow returned an unrequested symbol")
        stamp = self._now()
        adapter = V1BarAdapter(
            "tickflow",
            "daily_raw_v1",
            "raw",
            "1d",
            "CNY",
            "share",
            "lot",
            lambda symbol, _: bindings[symbol],
            stamp,
            lot_size=lambda *_: Decimal(100),
            daily_bounds=lambda day: self._bounds(sessions[day]),
        )
        revision = "observation-" + stamp.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        records, seen = [], set()
        for symbol, columns in payload.items():
            context.check_cancelled()
            dates = set()
            for row in _compact_rows(columns, len(days)):
                day = row["date"]
                if day not in sessions or day in dates:
                    raise TiveloError(
                        ErrorCode.DATA_CONFLICT, "Duplicate or out-of-range TickFlow bar"
                    )
                dates.add(day)
                if halted_daily_values(row["open"], row["high"], row["volume"], row["amount"]):
                    continue
                if min(row[k] for k in ("open", "high", "low", "close")) <= 0:
                    raise TiveloError(ErrorCode.CONTRACT_INVALID, "Equity prices must be positive")
                try:
                    records.append(adapter.convert({**row, "symbol": symbol}, revision_id=revision))
                except ValidationError:
                    raise TiveloError(
                        ErrorCode.CONTRACT_INVALID, "Invalid TickFlow daily bar"
                    ) from None
                seen.add((symbol, day))
        self._record_coverage(bindings, sessions, seen)
        return tuple(records)

    def close(self):
        try:
            self.connection.close()
        finally:
            super().close()


def _compact_rows(columns, limit):
    if not isinstance(columns, dict) or any(not isinstance(columns.get(k), list) for k in FIELDS):
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "TickFlow daily columns are missing")
    size = len(columns["timestamp"])
    if size > limit or any(len(columns[k]) != size for k in FIELDS):
        raise TiveloError(
            ErrorCode.CONTRACT_INVALID, "TickFlow daily column lengths differ from the request"
        )
    for values in zip(*(columns[k] for k in FIELDS), strict=True):
        row = dict(zip(FIELDS, values, strict=True))
        if type(row["timestamp"]) is not int or type(row["volume"]) is not int:
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "TickFlow timestamp and lot count must be integers"
            )
        if (
            any(
                type(value) not in (int, float)
                or (type(value) is float and not math.isfinite(value))
                or value < 0
                for value in values[1:]
            )
            or row["high"] < max(row["low"], row["open"], row["close"])
            or row["low"] > min(row["open"], row["close"])
        ):
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Invalid TickFlow OHLC, volume or turnover"
            )
        try:
            row["date"] = (EPOCH + timedelta(milliseconds=row["timestamp"])).astimezone(CN).date()
        except OverflowError:
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid TickFlow timestamp") from None
        yield row
