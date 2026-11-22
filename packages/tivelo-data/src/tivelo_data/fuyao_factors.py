"""Read the Fuyao full-market event release and match it to unadjusted stock closes."""

import hashlib
import re
from collections import defaultdict
from datetime import date, time, timedelta
from tempfile import TemporaryFile
from urllib.parse import urlsplit

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
from tivelo_contracts.errors import ErrorCode, TiveloError

from ._http_request import checked_request_logging, retry_after
from .adapters.cn_daily import CN
from .adapters.cn_ex_rights import CNExRightsObservation, event_ratios, terms, traded_close
from .fuyao_daily import _EPOCH, _ms, fuyao_failure
from .legacy.fuyao_history import FuyaoError, FuyaoHistoryClient, FuyaoUnknownSymbolError
from .legacy.pacing import RequestPacer

_DUMP_PATH = "/api/dump/market-dumps/adjustment-factors/download-url"
_EVENTS_PATH = "/api/a-share/corporate-actions/adjustment-factors"
_COLUMNS = (
    "thscode",
    "ticker",
    "currency",
    "ex_date_ms",
    "dividend_per_share",
    "per_share_bonus",
    "allotment_ratio",
    "allotment_price",
)


def _day(stamp):
    if type(stamp) is int:
        try:
            dt = (_EPOCH + timedelta(milliseconds=stamp)).astimezone(CN)
            if dt.time() == time.min:
                return dt.date()
        except (OverflowError, ValueError):
            pass
    raise TiveloError(ErrorCode.CONTRACT_INVALID, "Expected Fuyao Beijing midnight in milliseconds")


def _dump_release(url):
    if not isinstance(url, str):
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Missing Fuyao event download URL")
    address = urlsplit(url)
    match = re.search(r"/releases/([0-9]{8})/", address.path)
    if (
        match
        and address.hostname
        and not address.username
        and not address.password
        and not address.fragment
        and (
            address.scheme == "https"
            or (address.scheme == "http" and address.hostname in {"127.0.0.1", "::1", "localhost"})
        )
    ):
        try:
            return date.fromisoformat(match[1])
        except ValueError:
            pass
    raise TiveloError(
        ErrorCode.CONTRACT_INVALID, "Fuyao event URL lacks a dated release or valid origin"
    )


class FuyaoFactorProvider(CNExRightsObservation):
    def __init__(self, source, config, *, resolve_token=None, now=None):
        super().__init__(
            source, config, name="fuyao", transport="httpx", resolve_token=resolve_token, now=now
        )
        self.pacer = RequestPacer()
        self.client = None

    def _pace(self, context):
        self.pacer.wait("fuyao", self.config.rpm, context.check_cancelled)
        context.check_cancelled()

    def check_ready(self, context):
        super().check_ready(context)
        self.client = FuyaoHistoryClient(self.credential(context), self.config.endpoint, exact=True)
        self.known = set()
        self.events = defaultdict(lambda: defaultdict(list))
        self._pace(context)
        try:
            info = self.client._get(_DUMP_PATH, {})
        except FuyaoError as exc:
            raise fuyao_failure(exc) from None
        url = info.get("presigned_url") if isinstance(info, dict) else None
        self.release = _dump_release(url)
        end = self.source.request.end.astimezone(CN).date()
        if end - timedelta(days=1) > self.release:
            raise TiveloError(
                ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                f"Fuyao event release {self.release} predates the requested interval end",
            )
        # Never send the API key to the object store or retain its presigned URL.
        try:
            with TemporaryFile() as archive:
                with checked_request_logging(), httpx.stream("GET", url, timeout=90) as response:
                    if response.status_code == 429:
                        raise TiveloError(
                            ErrorCode.RATE_LIMITED,
                            "Fuyao event download rate limited",
                            retry_after=retry_after(response.headers.get("Retry-After", "")),
                        )
                    if response.status_code != 200:
                        raise TiveloError(
                            ErrorCode.SOURCE_UNAVAILABLE, "Fuyao event download failed"
                        )
                    digest = hashlib.sha256()
                    for chunk in response.iter_bytes(1 << 20):
                        context.check_cancelled()
                        if archive.tell() + len(chunk) > 128 << 20:
                            raise TiveloError(
                                ErrorCode.CONTRACT_INVALID, "Fuyao event file exceeds size limit"
                            )
                        archive.write(chunk)
                        digest.update(chunk)
                archive.seek(0)
                self._read_archive(archive, end, context)
                self.archive_digest = digest.hexdigest()
        except httpx.HTTPError:
            raise TiveloError(ErrorCode.SOURCE_UNAVAILABLE, "Fuyao event download failed") from None
        except pa.ArrowException:
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Invalid Fuyao event Parquet file"
            ) from None

    def _read_archive(self, file, end, context):
        wanted = {s.symbol for s in self.source.symbols}
        start = self.source.baseline_date or self.source.request.start.astimezone(CN).date()
        parquet = pq.ParquetFile(file)
        if not parquet.metadata.num_rows or not set(_COLUMNS).issubset(parquet.schema_arrow.names):
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Fuyao full event file is empty or missing columns"
            )
        for batch in parquet.iter_batches(batch_size=2048, columns=_COLUMNS):
            context.check_cancelled()
            for row in batch.to_pylist():
                symbol = row["thscode"]
                if symbol not in wanted:
                    continue
                if row["currency"] != "CNY" or row["ticker"] != symbol[:6]:
                    raise TiveloError(
                        ErrorCode.DATA_CONFLICT, f"Fuyao currency or ticker differs for {symbol}"
                    )
                self.known.add(symbol)
                day = _day(row["ex_date_ms"])
                if start <= day < end:
                    part = terms(*(row[k] for k in _COLUMNS[4:]))
                    if any(part):
                        self.events[symbol][day].append(part)

    def _unknown_history(self, symbol, lower, upper, context):
        self._pace(context)
        try:
            reply = self.client._get(
                _EVENTS_PATH,
                {
                    "thscode": symbol,
                    "from": lower.isoformat(),
                    "to": (upper - timedelta(days=1)).isoformat(),
                },
            )
        except FuyaoError as exc:
            if str(exc.code) == "1002" and exc.api_message.strip() == f"Unknown thscode: {symbol}":
                return {}
            raise
        if (
            not isinstance(reply, dict)
            or reply.get("thscode") != symbol
            or not isinstance(reply.get("item"), list)
        ):
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Fuyao event service did not confirm the stock"
            )
        if reply["item"]:
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, f"Fuyao full file omits reported stock events for {symbol}"
            )
        return {symbol: {}}

    def _closes(self, symbol, start, end, context):
        self._pace(context)
        rows = self.client.historical_kline(symbol, _ms(start), _ms(end), checked=True)
        closes, seen = {}, set()
        for bar in rows:
            context.check_cancelled()
            if not isinstance(bar, dict):
                raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid Fuyao raw daily row")
            day = _day(bar.get("date_ms"))
            if (
                not start <= day <= end
                or day in seen
                or bar.get("thscode", symbol) != symbol
                or bar.get("currency", "CNY") != "CNY"
                or bar.get("adjust", "none") != "none"
                or bar.get("adjusted", "none") != "none"
                or bar.get("interval", "1d") != "1d"
            ):
                raise TiveloError(
                    ErrorCode.DATA_CONFLICT, "Fuyao raw daily bar outside price request"
                )
            seen.add(day)
            try:
                close = traded_close(
                    *(
                        bar[k]
                        for k in ("open_price", "high_price", "close_price", "volume", "turnover")
                    )
                )
            except KeyError:
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID, "Fuyao raw daily bar lacks price or volume"
                ) from None
            if close is not None:
                closes[day] = close
        return closes

    def _read_window(self, symbols, lower, upper, context):
        (symbol,) = symbols
        try:
            if symbol not in self.known:
                return self._unknown_history(symbol, lower, upper, context)
            events = {
                day: value for day, value in self.events[symbol].items() if lower <= day < upper
            }
            if not events:
                return {symbol: {}}
            first = min(events)
            start = date.fromordinal(max(1, first.toordinal() - 32))
            closes = self._closes(symbol, start, max(events), context)
            if not any(day < first for day in closes) and start > date.min:
                # A suspended security may have a preceding trade before the initial lookback.
                earlier = date.fromordinal(max(1, start.toordinal() - 3650))
                closes.update(self._closes(symbol, earlier, start - timedelta(days=1), context))
            return {symbol: event_ratios(symbol, events, closes)}
        except FuyaoUnknownSymbolError:
            raise TiveloError(
                ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                f"Fuyao has events but no raw trading history for {symbol} {first}",
            ) from None
        except FuyaoError as exc:
            raise fuyao_failure(exc) from None

    def _revision(self, observed):
        return f"fuyao-{self.release}-{self.archive_digest}-{super()._revision(observed)}"

    def close(self):
        try:
            if self.client is not None:
                self.client.close()
                self.client = None
        finally:
            self.events = {}
            self.known = set()
            super().close()
