"""Checked HTTP observations of raw Shanghai and Shenzhen daily equity bars."""

import hashlib
import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, time
from decimal import Decimal

from pydantic import ValidationError
from tivelo_contracts.errors import ErrorCode, TiveloError

from ..adapters.cn_daily import CNRawDailyRequest
from ..adapters.cn_observation import CNDailyObservation
from ..adapters.v1 import V1BarAdapter
from ..legacy.normalizer import normalize_daily
from ..providers import ProviderCapability, ProviderDescriptor
from .client import DAILY_FIELDS, CheckedDailyClient
from .config import CN_RAW_DAILY_PROFILE, CustomSourceConfig


class CNDailyProvider(CNDailyObservation):
    def __init__(
        self,
        source: CNRawDailyRequest,
        config: CustomSourceConfig,
        *,
        resolve_token: Callable[[str], str | None] | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        if config.name != source.expected_provider:
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Source differs from the requested provider")
        dataset = config.datasets.get("daily")
        if dataset is None:
            raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Source has no daily dataset")
        if dataset.research_profile != CN_RAW_DAILY_PROFILE:
            raise TiveloError(
                ErrorCode.PRICE_BASIS_UNPROVEN, "Source has no raw daily research declaration"
            )
        fields = tuple(dataset.field_map.values())
        if (
            not dataset.url
            or not DAILY_FIELDS.issubset(fields)
            or len(fields) != len(set(fields))
            or len({dataset.symbols_param, dataset.start_param, dataset.end_param}) != 3
        ):
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid daily source mapping")
        # Source reloads or caller mutations cannot change the declaration after planning.
        self.config = replace(config, datasets={"daily": deepcopy(dataset)})
        self._resolve_token = resolve_token
        super().__init__(
            source,
            now or (lambda: datetime.now(UTC)),
            days_per_page=32,
            symbols_per_page=min(dataset.batch or 128, 128),
        )
        self.client = None
        self.dataset = self.config.datasets["daily"]
        if self.dataset.batch is not None and self.dataset.batch < 1:
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid daily source batch size")
        # Only the digest enters the task. Secret values resolved at fetch time never enter it.
        declaration = {
            "dataset": asdict(self.dataset),
            "auth": asdict(config.auth),
            "calendar": self.preset.version_id,
            "bindings": [s.model_dump() for s in source.symbols],
        }
        self.configuration_digest = hashlib.sha256(
            json.dumps(
                declaration,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()

    def describe(self):
        return ProviderDescriptor(
            provider_id=self.source.expected_provider,
            version="tsp.daily.1",
            source_ids=(self.source.expected_provider,),
            capabilities=(
                ProviderCapability(
                    market_id="cn.equity",
                    asset_class="equity",
                    record_type="bar",
                    timeframes=("1d",),
                    price_bases=("raw",),
                    knowledge_basis="latest_reconstruction",
                    normalization_version=CN_RAW_DAILY_PROFILE,
                    bar_convention_id=self.preset.contents.bar_convention_id,
                ),
            ),
        )

    def check_ready(self, context):
        super().check_ready(context)
        if self.next_page is not None:
            resolve = self._resolve_token or (
                lambda ref: context.resolve_credentials(ref).get("token")
            )
            self.client = CheckedDailyClient(self.config, resolve)

    def _read(self, days, symbols, context):
        bindings = {entry.symbol: entry.instrument_id for entry in symbols}
        sessions = {day.trading_date: day for day in days}
        frame = self.client.get_daily_checked(
            list(bindings),
            datetime.combine(days[0].trading_date, time.min),
            datetime.combine(days[-1].trading_date, time.max),
            checkpoint=context.check_cancelled,
        )
        context.check_cancelled()
        stamp = self._now()
        adapter = V1BarAdapter(
            source_id=self.source.expected_provider,
            input_schema="daily_raw_v1",
            price_basis="raw",
            timeframe="1d",
            currency="CNY",
            quantity_unit="share",
            volume_unit="lot",
            lot_size=lambda instrument, day: Decimal(100),
            resolve_instrument=lambda symbol, day: bindings[symbol],
            ingested_at=stamp,
            daily_bounds=lambda date: (
                sessions[date].open_at,
                sessions[date].close_at,
                sessions[date].bar_convention_id,
            ),
        )
        revision = "observation-" + stamp.strftime("%Y%m%dT%H%M%S%fZ")
        keys = set()
        for row in frame.iter_rows(named=True):
            key = row["symbol"], row["date"]
            if key in keys or key[0] not in bindings or key[1] not in sessions:
                raise TiveloError(
                    ErrorCode.DATA_CONFLICT,
                    "Daily source returned duplicate or out-of-scope observations",
                )
            keys.add(key)
        # Legacy halt placeholders count as missing. Validate their scope before filtering them.
        records, seen = [], set()
        for row in normalize_daily(frame, source=self.source.expected_provider).iter_rows(
            named=True
        ):
            if min(row[name] for name in ("open", "high", "low", "close")) <= 0:
                raise TiveloError(ErrorCode.CONTRACT_INVALID, "Equity prices must be positive")
            seen.add((row["symbol"], row["date"]))
            try:
                records.append(adapter.convert(row, revision_id=revision))
            except ValidationError:
                raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid daily observation") from None
        self._record_coverage(bindings, sessions, seen)
        return tuple(records)

    def close(self):
        if self.client is not None:
            self.client.close()
            self.client = None
        super().close()
