"""Declared HTTP stock ex-rights event lists for the independent data engine."""

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from tivelo_contracts.errors import ErrorCode, TiveloError

from ..adapters.cn_factor_observation import CNFactorObservation, collect_events
from .client import CheckedHTTPClient
from .config import CN_EX_FACTOR_PROFILE


def _events(rows, fields):
    for row in rows:
        symbol = row.get(fields["symbol"])
        day, ratio = row.get(fields["trade_date"]), row.get(fields["ex_factor"])
        if not isinstance(symbol, str) or not symbol:
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Stock factor has no source symbol")
        status = row.get(fields["event_status"], "event") if "event_status" in fields else "event"
        if status == "no_events" and day is None and ratio is None:
            yield symbol, None, None
            continue
        if status != "event" or not isinstance(day, str) or type(ratio) not in (str, int, Decimal):
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Invalid stock factor event or empty marker"
            )
        try:
            effective = date.fromisoformat(day)
            if effective.isoformat() != day:
                raise ValueError
            value = Decimal(ratio)
        except (ValueError, InvalidOperation):
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Invalid stock ex-date or event ratio"
            ) from None
        yield symbol, effective, value


class CNHTTPFactorProvider(CNFactorObservation):
    def __init__(self, source, config, *, resolve_token=None, now=None):
        if config.name != source.expected_provider:
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Source differs from the requested provider")
        dataset = config.datasets.get("adj_factor")
        if dataset is None or dataset.research_profile != CN_EX_FACTOR_PROFILE:
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "Source has no stock ex-rights event declaration"
            )
        fields = {target: name for name, target in dataset.field_map.items()}
        if (
            not dataset.url
            or dataset.method not in ("GET", "POST")
            or dataset.transforms
            or not {"symbol", "trade_date", "ex_factor"} <= fields.keys()
            or len(fields) != len(dataset.field_map)
            or len({dataset.symbols_param, dataset.start_param, dataset.end_param}) != 3
            or (dataset.batch is not None and dataset.batch < 1)
            or (dataset.rpm is not None and dataset.rpm < 1)
        ):
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Invalid stock factor HTTP mapping or limits"
            )
        self.fields = fields
        self.config = replace(config, datasets={"adj_factor": deepcopy(dataset)})
        self.resolve_token = resolve_token
        self.client = None
        super().__init__(
            source,
            batch=min(dataset.batch or 100, 100),
            now=now,
            version="http.ex-factors.1",
            baseline_prefix=f"http:{config.name}:ex-factors",
            declaration={"dataset": asdict(dataset), "auth": asdict(config.auth)},
        )

    def check_ready(self, context):
        super().check_ready(context)
        resolve = self.resolve_token or (lambda ref: context.resolve_credentials(ref).get("token"))
        self.client = CheckedHTTPClient(self.config, resolve)

    def _read_window(self, symbols, lower, upper, context):
        # Match v1 HTTP requests: naive Beijing wall clock with both bounds inclusive.
        rows = self.client.get_checked_rows(
            "adj_factor",
            list(symbols),
            datetime.combine(lower, time.min),
            datetime.combine(upper, time.min) - timedelta(microseconds=1),
            checkpoint=context.check_cancelled,
        )
        context.check_cancelled()
        return collect_events(_events(rows, self.fields), symbols, lower, upper, context)

    def close(self):
        try:
            super().close()
        finally:
            if self.client is not None:
                self.client.close()
                self.client = None
