"""Capture current TickFlow stock ex-rights events for an explicit Beijing date range."""

import math
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from importlib.metadata import version

from tivelo_contracts.errors import ErrorCode, TiveloError

from .adapters.cn_daily import CN, CNFactorRequest
from .adapters.cn_factor_observation import CNFactorObservation, collect_events
from .tickflow_connection import TickFlowConnection, TickFlowDailyConfig

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MILLISECOND = timedelta(milliseconds=1)


def _day_ms(day):
    return (datetime.combine(day, time.min, tzinfo=CN) - EPOCH) // MILLISECOND


def _events(payload, lower, upper):
    if not isinstance(payload, dict):
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid TickFlow factor payload")
    for symbol, rows in payload.items():
        if not isinstance(rows, list):
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Expected a list of stock factor events")
        if not rows:
            yield symbol, None, None
        for row in rows:
            if not isinstance(row, dict) or type(row.get("timestamp")) is not int:
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID, "Invalid stock factor event timestamp"
                )
            stamp, value = row["timestamp"], row.get("ex_factor")
            if not lower <= stamp < upper or row.get("symbol", symbol) != symbol:
                raise TiveloError(
                    ErrorCode.DATA_CONFLICT, "Stock factor falls outside source scope"
                )
            if (
                type(value) not in (int, float)
                or value <= 0
                or (isinstance(value, float) and not math.isfinite(value))
            ):
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID, "Stock factor must be a positive number"
                )
            day = (EPOCH + stamp * MILLISECOND).astimezone(CN).date()
            ratio = Decimal(str(value))
            yield symbol, day, ratio


class TickFlowFactorProvider(CNFactorObservation):
    def __init__(
        self,
        source: CNFactorRequest,
        config: TickFlowDailyConfig,
        *,
        resolve_token: Callable[[str], str | None] | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        if source.expected_provider != "tickflow":
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Source differs from TickFlow")
        if config.mode != "paid":
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "TickFlow factor access requires a paid connection",
            )
        self.connection = TickFlowConnection(config, resolve_token)
        sdk = version("tickflow")
        super().__init__(
            source,
            batch=config.batch,
            now=now,
            version="ex-factors.1.sdk." + sdk,
            baseline_prefix="tickflow:ex-factors",
            declaration={
                **config.model_dump(exclude={"endpoint"}),
                "endpoint": config.base_url,
                "sdk": sdk,
            },
        )

    def check_ready(self, context):
        super().check_ready(context)
        self.connection.open(context)

    def _read_window(self, symbols, lower, upper, context):
        start_ms, end_ms = _day_ms(lower), _day_ms(upper)
        with self.connection.request(context) as klines:
            # One SDK chunk: its multi-chunk aggregator suppresses failed requests.
            payload = klines.ex_factors(
                list(symbols),
                start_time=start_ms,
                end_time=end_ms - 1,
                batch_size=len(symbols),
                max_workers=1,
                as_dataframe=False,
            )
        context.check_cancelled()
        return collect_events(_events(payload, start_ms, end_ms), symbols, lower, upper, context)

    def close(self):
        try:
            super().close()
        finally:
            self.connection.close()
