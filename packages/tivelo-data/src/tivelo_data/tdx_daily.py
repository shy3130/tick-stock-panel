"""Independent TdxAiData raw stock history in a per-observation native process."""

import json
from datetime import date
from decimal import Decimal

from tivelo_contracts.errors import ErrorCode, TiveloError

from .adapters.stock_history import StockHistoryObservation
from .tdx_connection import TdxQuantConnection
from .tdx_connection import TdxQuantDailyConfig as TdxQuantDailyConfig

_FIELDS = ("Date", "Open", "High", "Low", "Close", "Volume", "Amount")


def _daily_rows(raw, symbol, days):
    try:
        columns = json.loads(raw, parse_float=Decimal)
    except (ValueError, RecursionError):
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid TdxAiData history JSON") from None
    if isinstance(columns, dict) and "Value" in columns:
        columns = columns["Value"]
    if not isinstance(columns, dict) or any(not isinstance(columns.get(k), list) for k in _FIELDS):
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Missing TdxAiData history columns")
    count = len(columns["Date"])
    if count > len(days) or any(len(columns[k]) != count for k in _FIELDS):
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Unexpected TdxAiData column length")
    start = days[0].trading_date
    seen = set()
    rows = []
    for stamp, opening, high, low, close, volume, amount in zip(
        *(columns[k] for k in _FIELDS), strict=True
    ):
        compact = str(stamp)
        if (
            type(stamp) not in (int, str)
            or len(compact) != 8
            or not compact.isascii()
            or not compact.isdigit()
        ):
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid TdxAiData trading date")
        try:
            day = date.fromisoformat(compact)
        except ValueError:
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Invalid TdxAiData trading date"
            ) from None
        if day in seen:
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Duplicate TdxAiData trading date")
        seen.add(day)
        if day < start:
            # Count-based lookback may legitimately return older trades when a symbol suspended.
            continue
        rows.append(
            dict(
                symbol=symbol,
                date=day,
                open=opening,
                high=high,
                low=low,
                close=close,
                volume=volume,
                amount=amount,
            )
        )
    return rows


class TdxQuantDailyProvider(StockHistoryObservation):
    turnover_multiplier = 10000

    def __init__(self, source, config, *, resolve_token=None, now=None):
        self.connection = TdxQuantConnection(config)
        super().__init__(
            source,
            config,
            name="tdxquant",
            runtime="sdk." + self.connection.sdk,
            batch=1,
            resolve_token=resolve_token,
            now=now,
        )

    def check_ready(self, context):
        super().check_ready(context)
        if self.next_page is not None:
            self.connection.open(self.credential(context), context)

    def history_rows(self, days, symbols, context):
        (symbol,) = symbols
        raw = self.connection.request(
            "history",
            context,
            symbol=symbol,
            end=days[-1].trading_date.isoformat(),
            count=len(days),
        )
        return _daily_rows(raw, symbol, days)

    def close(self):
        try:
            self.connection.close()
        finally:
            super().close()
