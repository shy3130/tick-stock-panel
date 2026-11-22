"""Independent Fuyao historical A-share daily acquisition."""

from datetime import UTC, datetime, time, timedelta
from importlib.metadata import version
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from tivelo_contracts.base import Contract
from tivelo_contracts.errors import ErrorCode, TiveloError

from .adapters.cn_daily import CN
from .adapters.stock_history import StockHistoryObservation
from .legacy.fuyao_history import (
    BASE_URL,
    HISTORICAL_BATCH_LIMIT,
    FuyaoError,
    FuyaoHistoryClient,
    FuyaoUnknownSymbolError,
)
from .legacy.pacing import RequestPacer

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class FuyaoDailyConfig(Contract):
    credential_ref: str = Field(default="FUYAO_API_KEY", pattern=r"^[A-Za-z_][A-Za-z_0-9]{0,127}$")
    endpoint: str = Field(default=BASE_URL, strict=True, max_length=2048)
    rpm: int = Field(default=120, strict=True, ge=1, le=60000)

    @field_validator("endpoint")
    @classmethod
    def https_or_loopback(cls, value):
        value = value.rstrip("/")
        url = urlsplit(value)
        if (
            not url.hostname
            or url.username is not None
            or url.password is not None
            or url.port == 0
            or url.query
            or url.fragment
            or url.path
            or (
                url.scheme != "https"
                and not (url.scheme == "http" and url.hostname in ("127.0.0.1", "::1", "localhost"))
            )
            or any(c.isspace() or ord(c) < 32 for c in value)
        ):
            raise ValueError("Supply a credential-free HTTPS origin or local HTTP origin")
        return value


def _ms(day):
    return (datetime.combine(day, time.min, tzinfo=CN) - _EPOCH) // timedelta(milliseconds=1)


def fuyao_failure(exc):
    if exc.status in (401, 403) or str(exc.code) in ("1001", "2001", "2002"):
        code = ErrorCode.AUTHENTICATION_FAILED
    elif exc.status == 429 or str(exc.code) == "4001":
        code = ErrorCode.RATE_LIMITED
    elif exc.transport or (exc.status is not None and exc.status >= 500):
        code = ErrorCode.SOURCE_UNAVAILABLE
    else:
        code = ErrorCode.CONTRACT_INVALID
    return TiveloError(
        code,
        "Fuyao stock data request failed",
        retry_after=exc.retry_after if code == ErrorCode.RATE_LIMITED else None,
    )


class FuyaoDailyProvider(StockHistoryObservation):
    turnover_multiplier = 1

    def __init__(self, source, config, *, resolve_token=None, now=None):
        super().__init__(
            source,
            config,
            name="fuyao",
            runtime="httpx." + version("httpx"),
            batch=HISTORICAL_BATCH_LIMIT,
            resolve_token=resolve_token,
            now=now,
        )
        self.pacer = RequestPacer()
        self.client = None

    def check_ready(self, context):
        super().check_ready(context)
        if self.next_page is not None:
            self.client = FuyaoHistoryClient(
                self.credential(context), self.config.endpoint, exact=True
            )

    def history_rows(self, days, symbols, context):
        dates = {_ms(d.trading_date): d.trading_date for d in days}
        start, end = min(dates), max(dates)
        try:
            self.pacer.wait("history", self.config.rpm, context.check_cancelled)
            context.check_cancelled()
            try:
                groups = self.client.historical_kline_batch(list(symbols), start, end)
            except FuyaoError as exc:
                exact_unknown = str(exc.code) == "1002" and exc.api_message.strip() in {
                    f"Unknown thscode: {symbol}" for symbol in symbols
                }
                if exc.status not in (404, 405) and not exact_unknown:
                    raise
                groups = {}
            # Only a successful empty group or an explicitly unknown security proves absence.
            for symbol in symbols:
                if symbol not in groups:
                    self.pacer.wait("history", self.config.rpm, context.check_cancelled)
                    context.check_cancelled()
                    try:
                        groups[symbol] = self.client.historical_kline(
                            symbol, start, end, checked=True
                        )
                    except FuyaoUnknownSymbolError:
                        groups[symbol] = []
        except FuyaoError as exc:
            raise fuyao_failure(exc) from None
        rows = []
        for symbol, bars in groups.items():
            if len(bars) > len(days):
                raise TiveloError(ErrorCode.DATA_CONFLICT, "Too many Fuyao daily bars")
            for bar in bars:
                if not isinstance(bar, dict):
                    raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid Fuyao daily bar")
                stamp = bar.get("date_ms")
                if (
                    type(stamp) is not int
                    or stamp not in dates
                    or bar.get("thscode", symbol) != symbol
                ):
                    raise TiveloError(
                        ErrorCode.DATA_CONFLICT,
                        "Fuyao daily bar outside requested symbol or sessions",
                    )
                try:
                    row = {k: bar[k + "_price"] for k in ("open", "high", "low", "close")}
                    rows.append(
                        dict(
                            row,
                            symbol=symbol,
                            date=dates[stamp],
                            volume=bar["volume"],
                            amount=bar["turnover"],
                        )
                    )
                except KeyError:
                    raise TiveloError(
                        ErrorCode.CONTRACT_INVALID, "Missing Fuyao daily field"
                    ) from None
        return rows

    def close(self):
        try:
            if self.client is not None:
                self.client.close()
        finally:
            self.client = None
            super().close()
