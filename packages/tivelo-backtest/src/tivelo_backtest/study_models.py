"""Bounded, immutable optimization and walk-forward requests and artifacts."""

from __future__ import annotations

import itertools
import json
from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator
from tivelo_contracts.artifacts import ArchiveContents
from tivelo_contracts.base import (
    CalendarDate,
    Contract,
    Digest,
    ExactDecimal,
    Key,
    ParametersJSON,
    RelativePath,
    UTCDateTime,
    canonical_json,
    fingerprint,
)
from tivelo_contracts.execution import DefinitionReference
from tivelo_contracts.market import TradingDay

from .archives import MAX_ARCHIVE_BYTES, RunMetrics

Scalar = (
    StrictBool | StrictInt | Annotated[StrictFloat, Field(allow_inf_nan=False)] | StrictStr | None
)


class SearchAxis(Contract):
    name: Key
    values: tuple[Scalar, ...] = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def unique(self) -> Self:
        encoded = [canonical_json(json.dumps({"value": value})) for value in self.values]
        if len(set(encoded)) != len(encoded):
            raise ValueError("axis values must be unique JSON scalars")
        return self


class SearchSpace(Contract):
    target: Literal["strategy_parameters", "controlled_program"]
    axes: tuple[SearchAxis, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def unique(self) -> Self:
        names = [axis.name for axis in self.axes]
        if names != sorted(set(names)):
            raise ValueError("search axes must have unique, sorted names")
        return self

    def expand(self, maximum: int) -> tuple[str, ...]:
        count = 1
        for axis in self.axes:
            count *= len(axis.values)
            if count > maximum:
                raise ValueError("search grid exceeds the candidate budget")
        names = [axis.name for axis in self.axes]
        return tuple(
            canonical_json(json.dumps(dict(zip(names, values, strict=True))))
            for values in itertools.product(*(axis.values for axis in self.axes))
        )


class StudyObjective(Contract):
    metric: Literal["total_return", "maximum_drawdown"] = "total_return"
    min_filled_orders: int = Field(default=1, strict=True, ge=0)

    @property
    def direction(self):
        # The strict ledger metric is a positive drawdown magnitude.
        return "min" if self.metric == "maximum_drawdown" else "max"


class StudyLimits(Contract):
    max_candidates: int = Field(default=128, strict=True, ge=1, le=512)
    max_executions: int = Field(default=256, strict=True, ge=1, le=512)
    max_prepared_bytes: int = Field(default=256 * 1024 * 1024, strict=True, ge=1)
    max_archive_bytes: int = Field(
        default=MAX_ARCHIVE_BYTES, strict=True, ge=1, le=MAX_ARCHIVE_BYTES
    )


class WalkForwardWindows(Contract):
    train_sessions: int = Field(strict=True, ge=1, le=100000)
    test_sessions: int = Field(strict=True, ge=1, le=100000)
    step_sessions: int = Field(strict=True, ge=1, le=100000)
    gap_sessions: int = Field(default=0, strict=True, ge=0, le=100000)
    warmup_sessions: int = Field(default=0, strict=True, ge=0, le=100000)
    training: Literal["rolling", "expanding"] = "rolling"

    @model_validator(mode="after")
    def separate_tests(self) -> Self:
        if self.step_sessions < self.test_sessions:
            raise ValueError("walk-forward test windows must not overlap")
        return self


class StudyRequest(Contract):
    study_version: Literal["1.0"] = "1.0"
    mode: Literal["grid", "walk_forward"]
    search: SearchSpace
    objective: StudyObjective = Field(default_factory=StudyObjective)
    walk_forward: WalkForwardWindows | None = None
    limits: StudyLimits = Field(default_factory=StudyLimits)
    account_policy: Literal["reset_each_window"] = "reset_each_window"
    failure_policy: Literal["reject_study"] = "reject_study"

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.mode == "walk_forward") != (self.walk_forward is not None):
            raise ValueError("walk-forward mode requires its window settings")
        self.search.expand(self.limits.max_candidates)
        return self


class StudyCandidate(Contract):
    candidate_id: Digest
    overrides_json: ParametersJSON
    strategy: DefinitionReference

    @model_validator(mode="after")
    def identity(self) -> Self:
        if self.candidate_id != fingerprint(self.strategy):
            raise ValueError("candidate fingerprint differs from its strategy reference")
        return self


class StudyWindow(Contract):
    index: int = Field(strict=True, ge=0)
    train_start: CalendarDate
    train_end: CalendarDate
    train_warmup_start: CalendarDate | None
    selection_at: UTCDateTime
    test_start: CalendarDate | None = None
    test_end: CalendarDate | None = None
    test_warmup_start: CalendarDate | None = None
    test_open_at: UTCDateTime | None = None

    @model_validator(mode="after")
    def separated(self) -> Self:
        if self.train_end < self.train_start or (
            self.train_warmup_start is not None and self.train_warmup_start > self.train_start
        ):
            raise ValueError("invalid training or warmup interval")
        if self.test_start is None:
            if (
                self.test_end is not None
                or self.test_warmup_start is not None
                or self.test_open_at is not None
            ):
                raise ValueError("partial test interval")
        elif (
            self.test_end is None
            or not self.train_end < self.test_start <= self.test_end
            or (self.test_warmup_start is not None and self.test_warmup_start > self.test_start)
            or self.test_open_at is None
            or self.selection_at >= self.test_open_at
        ):
            raise ValueError("training/test intervals overlap or are invalid")
        return self


class StudyPlan(Contract):
    plan_version: Literal["1.0"] = "1.0"
    base_lock_id: Digest
    request: StudyRequest
    calendar_days: tuple[TradingDay, ...] = Field(min_length=1, max_length=100000)
    candidates: tuple[StudyCandidate, ...] = Field(min_length=1, max_length=512)
    windows: tuple[StudyWindow, ...] = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        ids = [candidate.candidate_id for candidate in self.candidates]
        dates = [day.trading_date for day in self.calendar_days]
        if dates != sorted(set(dates)) or any(
            day.available_at > day.open_at for day in self.calendar_days
        ):
            raise ValueError("study calendar must be ordered, unique and known before each opening")
        if ids != sorted(set(ids)) or len(ids) > self.request.limits.max_candidates:
            raise ValueError("candidate identities must be unique, sorted and bounded")
        if [window.index for window in self.windows] != list(range(len(self.windows))):
            raise ValueError("study windows must have consecutive indices")
        if self.request.mode == "grid" and (
            len(self.windows) != 1 or self.windows[0].test_start is not None
        ):
            raise ValueError("grid search has one training interval and no test interval")
        if self.request.mode == "walk_forward" and any(
            window.test_start is None for window in self.windows
        ):
            raise ValueError("walk-forward windows need test intervals")
        if any(
            a.test_end >= b.test_start for a, b in zip(self.windows, self.windows[1:], strict=False)
        ):
            raise ValueError("test intervals overlap")
        count = len(self.windows) * (len(self.candidates) + (self.request.mode == "walk_forward"))
        if count > self.request.limits.max_executions:
            raise ValueError("study exceeds its execution budget")
        return self

    @property
    def study_id(self):
        return fingerprint(self)


class StudyTrial(Contract):
    candidate_id: Digest
    lock_id: Digest
    result_archive_id: Digest
    path: RelativePath
    metrics: RunMetrics


class WindowResult(Contract):
    index: int = Field(strict=True, ge=0)
    training: tuple[StudyTrial, ...] = Field(min_length=1, max_length=512)
    winner_candidate_id: Digest
    test: StudyTrial | None = None


class StudyReport(Contract):
    report_version: Literal["1.0"] = "1.0"
    study_id: Digest
    windows: tuple[WindowResult, ...] = Field(min_length=1, max_length=512)
    aggregate_method: Literal["none", "independent_fold_return_product"]
    oos_compounded_return: ExactDecimal | None
    prepared_fact_bytes: int = Field(strict=True, ge=0)


class StudyArchiveContents(ArchiveContents):
    study_id: Digest


class StudyArchive(Contract):
    format: Literal["tivelo-study-archive"] = "tivelo-study-archive"
    format_version: Literal["1.0"] = "1.0"
    archive_id: Digest
    contents: StudyArchiveContents

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.archive_id != fingerprint(self.contents):
            raise ValueError("study archive fingerprint mismatch")
        if not {"study-plan.json", "study-report.json"} <= {
            item.path for item in self.contents.files
        }:
            raise ValueError("study archive is incomplete")
        return self

    @classmethod
    def create(cls, contents):
        return cls(archive_id=fingerprint(contents), contents=contents)
