"""Paging and coverage of regular mainland stock bars."""

from datetime import timedelta

from tivelo_contracts.data import Coverage, CoverageIssue
from tivelo_contracts.errors import ErrorCode, TiveloError

from ..providers import FetchPage
from .cn_daily import CN


def _groups(items, size):
    for index in range(0, len(items), size):
        yield items[index : index + size]


class CNBarObservation:
    def __init__(self, source, now, *, symbols_per_page):
        self.source = source
        self._now = now
        self.preset = None
        if source.calendar_version is not None:
            try:
                from tivelo_market_cn.presets import load_calendar_preset
            except ModuleNotFoundError as exc:
                if exc.name != "tivelo_market_cn":
                    raise
                raise TiveloError(
                    ErrorCode.UNSUPPORTED_CAPABILITY,
                    "Install tivelo-market-cn to use a declared calendar preset",
                ) from None

            self.preset = load_calendar_preset(version=source.calendar_version)
        self.symbols_per_page = symbols_per_page
        self.index = self.requested = self.completed = 0
        self.missing = {}
        self.pages = self.next_page = None

    def check_ready(self, context):
        context.check_cancelled()
        self.close()
        self.index = self.requested = self.completed = 0
        self.missing.clear()
        self.pages = self._pages(context)
        self.next_page = next(self.pages, None)

    def _record_coverage(self, bindings, sessions, seen):
        self.requested += len(sessions) * len(bindings)
        self.completed += len(seen)
        for symbol, instrument in bindings.items():
            for day in sessions:
                if (symbol, day) not in seen:
                    self.missing.setdefault(instrument, day)
                    break

    def fetch(self, request, cursor, context):
        context.check_cancelled()
        if (
            request != self.source.request
            or cursor != (str(self.index) if self.index else None)
            or self.pages is None
        ):
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, "Bar page does not match the active observation"
            )
        records = ()
        if self.next_page is not None:
            records = self._read(*self.next_page, context)
            self.index += 1
            self.next_page = next(self.pages, None)
            if self.next_page is not None:
                return FetchPage(records, str(self.index))
        self.pages = None
        issues = tuple(
            CoverageIssue(
                instrument_id=instrument,
                reason="missing",
                detail=f"No bar returned for {date} in requested sessions",
            )
            for instrument, date in sorted(self.missing.items())
        )
        if not self.requested:
            if self.preset is None:
                issue = CoverageIssue(
                    reason="missing",
                    detail="No stored daily partition in the requested interval",
                )
            else:
                issue = CoverageIssue(
                    reason="no_trades",
                    detail="No eligible trading session opens in the requested interval",
                )
            issues = (issue,)
        status = "empty" if not self.completed else ("partial" if issues else "complete")
        return FetchPage(
            records,
            None,
            Coverage(
                status=status,
                requested_units=self.requested or len(request.instrument_ids),
                completed_units=self.completed,
                row_count=self.completed,
                issues=issues,
            ),
        )

    def _read(self, days, symbols, context):
        raise NotImplementedError

    def close(self):
        self.pages = self.next_page = None


class CNDailyObservation(CNBarObservation):
    def __init__(self, source, now, *, days_per_page, symbols_per_page):
        super().__init__(source, now, symbols_per_page=symbols_per_page)
        self.days_per_page = days_per_page

    def _bounds(self, day):
        return day.open_at, day.close_at, day.bar_convention_id

    def _pages(self, context):
        from tivelo_market_cn.calendar import CNCalendar
        from tivelo_market_cn.presets import compile_calendar

        request = self.source.request
        start = request.start.astimezone(CN).date()
        end = (request.end - timedelta(microseconds=1)).astimezone(CN).date()
        observed = self._now()
        for venue in sorted({s.venue_id for s in self.source.symbols}):
            context.check_cancelled()
            calendar = CNCalendar(
                compile_calendar(self.preset, venue_id=venue, start=start, end=end)
            )
            dates = []
            for day in calendar.trading_days(start, end, as_of=observed):
                start_at, end_at, _ = self._bounds(day)
                if end_at <= request.start or start_at >= request.end:
                    continue
                if start_at < request.start or end_at > request.end:
                    raise TiveloError(
                        ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                        "Daily capture requires whole sessions",
                    )
                if end_at > observed:
                    raise TiveloError(
                        ErrorCode.FUTURE_INFORMATION, "The requested daily bar is unfinished"
                    )
                dates.append(day)
            symbols = tuple(s for s in self.source.symbols if s.venue_id == venue)
            for days in _groups(dates, self.days_per_page):
                for batch in _groups(symbols, self.symbols_per_page):
                    yield days, batch
