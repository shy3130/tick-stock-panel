"""Current observations from explicitly labeled raw Shanghai/Shenzhen minute feeds."""

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import ValidationError
from tivelo_contracts.base import utc_datetime
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_market_cn.calendar import CNCalendar
from tivelo_market_cn.presets import compile_calendar

from ..adapters.cn_daily import CN
from ..adapters.cn_minute import regular_slots
from ..adapters.cn_observation import CNBarObservation, _groups
from ..adapters.v1 import V1BarAdapter, legacy_decimal
from ..providers import ProviderCapability, ProviderDescriptor
from .client import CheckedHTTPClient
from .config import (
    CN_RAW_MINUTE_PROFILES,
    MINUTE_NUMERIC_SCALES,
    MINUTE_PRICES,
    minute_mapping_error,
)

WALL_MINUTE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}(?::00(?:\.0{1,6})?)?")


class CNHTTPMinuteProvider(CNBarObservation):
    def __init__(self, source, config, *, resolve_token=None, now=None):
        if config.name != source.expected_provider:
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Source differs from the requested provider")
        dataset = config.datasets.get("minute")
        if dataset is None or dataset.research_profile not in CN_RAW_MINUTE_PROFILES:
            raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Source has no raw minute research declaration")
        error = minute_mapping_error(dataset, config.auth)
        if error:
            raise TiveloError(ErrorCode.CONTRACT_INVALID, error)
        super().__init__(source, now or (lambda: datetime.now(UTC)), symbols_per_page=min(dataset.batch or 128, 128))
        self.config = replace(config, datasets={"minute": deepcopy(dataset)})
        self.profile = dataset.research_profile
        self.label = CN_RAW_MINUTE_PROFILES[self.profile]
        self.fields = {target: name for name, target in dataset.field_map.items()}
        self.scales = {name: MINUTE_NUMERIC_SCALES[expr.strip()] for name, expr in dataset.transforms.items()}
        self.resolve_token = resolve_token
        self.client = None
        declaration = {
            "dataset": asdict(dataset),
            "auth": asdict(config.auth),
            "calendar": self.preset.version_id,
            "bindings": [entry.model_dump() for entry in source.symbols],
        }
        self.configuration_digest = hashlib.sha256(
            json.dumps(declaration, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()

    def describe(self):
        return ProviderDescriptor(
            provider_id=self.source.expected_provider, version="http.minute.1",
            source_ids=(self.source.expected_provider,),
            capabilities=(ProviderCapability(
                market_id="cn.equity", asset_class="equity", record_type="bar",
                timeframes=("1m",), price_bases=("raw",), knowledge_basis="latest_reconstruction",
                normalization_version=self.profile, bar_convention_id=self.profile,
            ),),
        )

    def _pages(self, context):
        request = self.source.request
        self.observed = utc_datetime(self._now())
        if request.end > self.observed:
            raise TiveloError(ErrorCode.FUTURE_INFORMATION, "Minute capture extends into the future")
        first = request.start.astimezone(CN).date()
        last = (request.end - timedelta(microseconds=1)).astimezone(CN).date()
        for venue in sorted({entry.venue_id for entry in self.source.symbols}):
            calendar = CNCalendar(compile_calendar(self.preset, venue_id=venue, start=first, end=last))
            if not callable(getattr(calendar, "regular_sessions", None)):
                raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Market package lacks regular minute sessions")
            symbols = tuple(entry for entry in self.source.symbols if entry.venue_id == venue)
            for day in calendar.trading_days(first, last, as_of=self.observed):
                context.check_cancelled()
                for session in calendar.regular_sessions(day.trading_date, as_of=self.observed):
                    slots, _ = regular_slots((session,), request, self.label)
                    if slots:
                        for batch in _groups(symbols, min(self.symbols_per_page, 2048 // len(slots))):
                            yield slots, batch

    def check_ready(self, context):
        super().check_ready(context)
        if self.next_page is not None:
            resolve = self.resolve_token or (lambda key: context.resolve_credentials(key).get("token"))
            self.client = CheckedHTTPClient(self.config, resolve)

    def _number(self, row, name):
        value = legacy_decimal(row.get(self.fields[name]))
        if name in self.scales and value:
            # Move the exponent without ever rounding through float or the decimal context.
            sign, digits, exponent = value.as_tuple()
            value = Decimal((sign, digits, exponent + self.scales[name]))
        return value

    def _read(self, slots, symbols, context):
        bindings = {entry.symbol: entry.instrument_id for entry in symbols}
        rows = self.client.get_checked_rows(
            "minute", list(bindings), min(slots), max(slots), checkpoint=context.check_cancelled
        )
        context.check_cancelled()
        received = utc_datetime(self._now())
        if received < self.observed:
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Clock moved backwards during minute capture")
        self.observed = received
        if len(rows) > len(bindings) * len(slots):
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Minute source returned too many observations")
        adapter = V1BarAdapter(
            self.source.expected_provider, "minute_raw_v1", "raw", "1m", "CNY", "share", "lot",
            lambda symbol, _: bindings[symbol], received,
            lot_size=lambda *_: Decimal(100), minute_label=self.label,
        )
        revision = "observation-" + received.strftime("%Y%m%dT%H%M%S%fZ")
        keys, traded, records = set(), set(), []
        for row in rows:
            context.check_cancelled()
            symbol, label = row.get(self.fields["symbol"]), row.get(self.fields["datetime"])
            if not isinstance(symbol, str) or not isinstance(label, str) or not WALL_MINUTE.fullmatch(label):
                raise TiveloError(ErrorCode.CONTRACT_INVALID, "Expected a stock symbol and a whole Beijing wall minute")
            try:
                stamp = datetime.fromisoformat(label)
            except ValueError:
                raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid Beijing minute label") from None
            key = (symbol, stamp)
            if symbol not in bindings or stamp not in slots or key in keys:
                raise TiveloError(ErrorCode.DATA_CONFLICT, "Duplicate or out-of-scope stock minute")
            keys.add(key)
            lots = self._number(row, "volume")
            amount = self._number(row, "amount") if row.get(self.fields.get("amount")) is not None else None
            if lots < 0 or (not lots and amount not in (None, 0)):
                raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid minute volume or turnover")
            if not lots:
                continue
            prices = {name: self._number(row, name) for name in MINUTE_PRICES}
            if min(prices.values()) <= 0 or (amount is not None and amount <= 0):
                raise TiveloError(ErrorCode.CONTRACT_INVALID, "Traded stock prices and reported turnover must be positive")
            try:
                bar = adapter.convert(
                    {**prices, "symbol": symbol, "datetime": stamp, "volume": lots, "amount": amount},
                    revision_id=revision, minute_session_id=slots[stamp],
                )
            except (ArithmeticError, ValidationError):
                raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid stock minute observation") from None
            if bar.volume != bar.volume.to_integral_value():
                raise TiveloError(ErrorCode.CONTRACT_INVALID, "Stock minute volume must resolve to whole shares")
            traded.add(key)
            records.append(bar)
        self._record_coverage(bindings, slots, traded)
        return tuple(records)

    def close(self):
        if self.client is not None:
            self.client.close()
            self.client = None
        super().close()
