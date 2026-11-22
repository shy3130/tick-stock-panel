"""Bounded record-stream paging, with final coverage and archive verification."""

from itertools import islice

import duckdb
import pyarrow as pa
from pydantic import ValidationError
from tivelo_contracts.errors import ErrorCode, TiveloError

from .providers import FetchPage


class PagedObservation:
    records = None
    invalid_data = (ArithmeticError, ValidationError, ValueError)
    invalid_message = "Invalid source observation"

    def _start_pages(self, records):
        self.close()
        self.records = records
        self.pending = None
        self.count = 0

    def fetch(self, request, cursor, context):
        context.check_cancelled()
        if (
            request != self.source.request
            or self.records is None
            or cursor != (str(self.count) if self.count else None)
        ):
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Page differs from active capture")
        try:
            head = (self.pending,) if self.pending is not None else ()
            page = head + tuple(islice(self.records, 2048 - len(head)))
            self.pending = next(self.records, None)
        except self.invalid_data:
            raise TiveloError(ErrorCode.CONTRACT_INVALID, self.invalid_message) from None
        self.count += len(page)
        if self.pending is not None:
            return FetchPage(page, str(self.count))
        self._finish_capture()
        return FetchPage(page, None, self._coverage())

    def _finish_capture(self):
        self.close()

    def close(self):
        if self.records is not None:
            self.records.close()
            self.records = None


class PagedRepositoryProvider(PagedObservation):
    invalid_data = (pa.ArrowException, duckdb.Error, *PagedObservation.invalid_data)
    invalid_message = "Invalid legacy archive observation"

    def _finish_capture(self):
        self._check_lock()
        self.close()
