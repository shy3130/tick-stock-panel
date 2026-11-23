"""Capture TdxAiData stock corporate actions and their unadjusted daily reference prices."""

import json
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from tivelo_contracts.errors import ErrorCode, TiveloError

from .adapters.cn_ex_rights import CNExRightsObservation, event_ratios, terms, traded_close
from .tdx_connection import TdxQuantConnection


def _payload(raw, symbol):
    try:
        data = json.loads(raw, parse_float=Decimal)
    except (ValueError, RecursionError):
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid native stock event JSON") from None
    if not isinstance(data, dict):
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid native stock event payload")
    if data.get("StockCode", symbol) != symbol:
        raise TiveloError(ErrorCode.DATA_CONFLICT, "Native response identified a different stock")
    return data


def _day(value):
    text = str(value).removesuffix(" 00:00:00")
    if type(value) in (int, str) and len(text) in (8, 10):
        try:
            day = date.fromisoformat(text)
            if text in (day.isoformat(), day.strftime("%Y%m%d")):
                return day
        except ValueError:
            pass
    raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid native stock event date")


def _events(raw, symbol, lower, upper):
    data = _payload(raw, symbol)
    # An explicit success with no data is the SDK's other representation of no events.
    if not (data.keys() - {"ErrorId", "Error", "ResultSetNum"}):
        return {} if str(data.get("ErrorId")) == "0" else None
    dates, values = data.get("Date"), data.get("Value")
    if not isinstance(dates, list) or not isinstance(values, list) or len(dates) != len(values):
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid native stock event columns")
    events = defaultdict(list)
    for stamp, row in zip(dates, values, strict=True):
        day = _day(stamp)
        # The native function can return all dates even when given inclusive bounds.
        if lower <= day < upper:
            if not isinstance(row, list) or len(row) != 4:
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID, f"Incomplete stock event terms for {symbol} {day}"
                )
            cash, price, bonus, rights = row
            part = terms(cash, bonus, rights, price, quantity_unit=10)
            if any(part):
                events[day].append(part)
    return events


def _closes(raw, symbol, end, count):
    data = _payload(raw, symbol)
    columns = data.get("Value", data)
    fields = ("Date", "Open", "High", "Close", "Volume", "Amount")
    if not isinstance(columns, dict) or any(not isinstance(columns.get(k), list) for k in fields):
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Missing raw TdxAiData daily columns")
    if columns.get("StockCode", symbol) != symbol:
        raise TiveloError(
            ErrorCode.DATA_CONFLICT, "Native daily prices identified a different stock"
        )
    n = len(columns["Date"])
    if n > count or any(len(columns[k]) != n for k in fields):
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Unexpected raw TdxAiData bar count")
    closes, seen = {}, set()
    for stamp, *bar in zip(*(columns[k] for k in fields), strict=True):
        day = _day(stamp)
        if day in seen or day > end:
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Unexpected or duplicate raw trading day")
        seen.add(day)
        close = traded_close(*bar)
        if close is not None:
            closes[day] = close
    return closes, n


class TdxQuantFactorProvider(CNExRightsObservation):
    def __init__(self, source, config, *, resolve_token=None, now=None):
        self.connection = TdxQuantConnection(config)
        super().__init__(
            source,
            config,
            name="tdxquant",
            transport="tdxaidata",
            resolve_token=resolve_token,
            now=now,
        )

    def check_ready(self, context):
        super().check_ready(context)
        self.connection.open(self.credential(context), context)

    def _read_window(self, symbols, lower, upper, context):
        (symbol,) = symbols
        raw = self.connection.request(
            "events",
            context,
            symbol=symbol,
            start=lower.isoformat(),
            end=(upper - timedelta(days=1)).isoformat(),
        )
        events = _events(raw, symbol, lower, upper)
        if events is None:
            return {}
        if not events:
            return {symbol: {}}
        first, last = min(events), max(events)
        # Even a daily series without weekend gaps needs at most this many bars to
        # cover the events and a preceding trade. A suspension can return older trades.
        count = (last - first).days + 2
        raw = self.connection.request(
            "history", context, symbol=symbol, end=last.isoformat(), count=count
        )
        closes, returned = _closes(raw, symbol, last, count)
        if returned == count and first in closes and min(closes) == first and first > date.min:
            # The count can be filled with halt placeholders. Ask for earlier raw trades.
            before = first - timedelta(days=1)
            raw = self.connection.request(
                "history", context, symbol=symbol, end=before.isoformat(), count=2600
            )
            earlier, _ = _closes(raw, symbol, before, 2600)
            closes.update(earlier)
        return {symbol: event_ratios(symbol, events, closes)}

    def close(self):
        try:
            self.connection.close()
        finally:
            super().close()
