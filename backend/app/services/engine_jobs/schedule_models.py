"""Immutable workbench schedule requests and dispatch times."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal, Self

from pydantic import ConfigDict, Field, model_validator
from tivelo_contracts.base import CalendarDate, Contract, Digest, Key, UTCDateTime
from tivelo_contracts.data import DatasetReference
from tivelo_data.adapters.cn_daily import CN
from tivelo_data.native_source import NativeDataInput

from .models import ExecutionBudget, JobRequest, NodeId, RequestKey, ResearchNode
from .preparation_models import DataInput, RepositoryDailyInput, RoutedDailyInput


@dataclass(frozen=True)
class DispatchTime:
    due_at: datetime
    next_due_at: datetime | None
    missed_count: int
    trading_date: date | None = None


class IntervalTrigger(Contract):
    kind: Literal["utc_interval"] = "utc_interval"
    starts_at: UTCDateTime
    interval_seconds: int = Field(strict=True, ge=60, le=31 * 86400)
    ends_at: UTCDateTime | None = None

    @model_validator(mode="after")
    def bounds(self) -> Self:
        if self.starts_at.microsecond or (self.ends_at and self.ends_at.microsecond):
            raise ValueError("schedule boundaries require whole seconds")
        if self.ends_at is not None and self.ends_at < self.starts_at:
            raise ValueError("schedule end precedes start")
        return self

    def latest_due(self, at):
        end = min(at, self.ends_at) if self.ends_at is not None else at
        if end < self.starts_at:
            return None
        step = timedelta(seconds=self.interval_seconds)
        return self.starts_at + ((end - self.starts_at) // step) * step

    def following(self, due):
        try:
            result = due + timedelta(seconds=self.interval_seconds)
        except OverflowError:
            return None
        return None if self.ends_at is not None and result > self.ends_at else result

    def occurrence(self, at: datetime, cursor: datetime) -> DispatchTime | None:
        due = self.latest_due(at)
        if due is None or due < cursor:
            return None
        missed = (due - cursor) // timedelta(seconds=self.interval_seconds)
        return DispatchTime(due, self.following(due), missed)


class TradingDayTrigger(Contract):
    kind: Literal["trading_day"]
    market_id: Literal["cn.equity"] = "cn.equity"
    venue_id: Literal["XSHG", "XSHE"] = "XSHG"
    calendar_version: Key
    preset_sha256: Digest | None = None
    start_date: CalendarDate
    end_date: CalendarDate
    anchor: Literal["open", "close"] = "close"
    offset_minutes: int = Field(default=0, strict=True, ge=-1440, le=1440)
    daily_capture_nodes: tuple[NodeId, ...] = Field(
        default=(), max_length=16, exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end_date < self.start_date:
            raise ValueError("trading-date range is reversed")
        if self.daily_capture_nodes != tuple(sorted(set(self.daily_capture_nodes))):
            raise ValueError("daily capture node IDs must be unique and sorted")
        return self


class PublicationTrigger(Contract):
    kind: Literal["data_publication"]
    dataset_id: Key


class ResearchTemplate(Contract):
    nodes: tuple[ResearchNode, ...] = Field(min_length=1, max_length=16)
    budget: ExecutionBudget = Field(default_factory=ExecutionBudget)

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        # Keep graph, preparation and budget validation in the existing request contract.
        self.request("schedule-template")
        return self

    def request(self, key):
        return JobRequest(idempotency_key=key, nodes=self.nodes, budget=self.budget)


def supports_daily_capture(source: DataInput, market_id: str) -> bool:
    request = source.request
    return (
        isinstance(source, (RoutedDailyInput, RepositoryDailyInput, NativeDataInput))
        and request.market_id == market_id
        and request.asset_class == "equity"
        and request.record_type == "bar"
        and request.timeframe == "1d"
    )


def trading_day_capture_nodes(nodes: tuple[ResearchNode, ...], market_id: str = "cn.equity"):
    return tuple(
        node.node_id
        for node in nodes
        if node.capture
        and node.capture.datasets
        and not node.capture.versions
        and all(supports_daily_capture(s, market_id) for s in node.capture.datasets)
    )


def publication_datasets(nodes):
    datasets = set()
    for node in nodes:
        if spec := node.capture or node.prepare:
            datasets.update(s.request.dataset_id for s in spec.datasets)
            datasets.update(ref.dataset_id for ref in spec.versions)
    return tuple(sorted(datasets))


def _describe_schedule_policies(schema):
    for name in ("misfire_policy", "overlap_policy"):
        schema["properties"][name].pop("default", None)

    def policies(misfire, overlap):
        return {
            "properties": {
                "misfire_policy": {"const": misfire, "default": misfire},
                "overlap_policy": {"const": overlap, "default": overlap},
            }
        }

    schema["allOf"] = [
        {
            "if": {
                "properties": {"schedule_version": {"const": "1.2"}},
                "required": ["schedule_version"],
            },
            "then": policies("all_publications", "queue"),
            "else": policies("latest_only", "skip"),
        }
    ]


class ScheduleRequest(Contract):
    model_config = ConfigDict(json_schema_extra=_describe_schedule_policies)
    schedule_version: Literal["1.0", "1.1", "1.2"] = "1.0"
    schedule_key: RequestKey
    trigger: IntervalTrigger | TradingDayTrigger | PublicationTrigger
    template: ResearchTemplate
    misfire_policy: Literal["latest_only", "all_publications"] = "latest_only"
    overlap_policy: Literal["skip", "queue"] = "skip"

    @model_validator(mode="before")
    @classmethod
    def publication_defaults(cls, value):
        if isinstance(value, dict) and value.get("schedule_version") == "1.2":
            return {"misfire_policy": "all_publications", "overlap_policy": "queue", **value}
        return value

    @model_validator(mode="after")
    def supported_trigger(self) -> Self:
        versions = {"utc_interval": "1.0", "trading_day": "1.1", "data_publication": "1.2"}
        if self.schedule_version != versions[self.trigger.kind]:
            raise ValueError("schedule version does not match its trigger")
        if isinstance(self.trigger, PublicationTrigger):
            if (self.misfire_policy, self.overlap_policy) != ("all_publications", "queue"):
                raise ValueError("publication subscriptions process every event in order")
            if self.trigger.dataset_id not in publication_datasets(self.template.nodes):
                raise ValueError("a capture or preparation step must consume the watched dataset")
        elif (self.misfire_policy, self.overlap_policy) != ("latest_only", "skip"):
            raise ValueError("clock schedules use latest_only and skip")
        if isinstance(self.trigger, TradingDayTrigger):
            eligible = set(trading_day_capture_nodes(self.template.nodes, self.trigger.market_id))
            if not set(self.trigger.daily_capture_nodes) <= eligible:
                raise ValueError(
                    "a trading-date window needs a capture node containing only CN daily sources"
                )
        return self

    def job_request(
        self, key: str, trading_date: date | None, publication: DatasetReference | None = None
    ) -> JobRequest:
        request = self.template.request(key)
        if isinstance(self.trigger, PublicationTrigger):
            if publication is None or publication.dataset_id != self.trigger.dataset_id:
                raise ValueError("publication does not identify the subscribed dataset")
            body = request.model_dump(mode="json")
            for node in body["nodes"]:
                spec = node.get("capture") or node.get("prepare")
                if spec is None:
                    continue
                sources = [
                    s
                    for s in spec["datasets"]
                    if s["request"]["dataset_id"] != publication.dataset_id
                ]
                pins = {p["dataset_id"]: p for p in spec.get("versions", [])}
                if len(sources) != len(spec["datasets"]) or publication.dataset_id in pins:
                    pins[publication.dataset_id] = publication.model_dump(mode="json")
                    spec.update(datasets=sources, versions=[pins[k] for k in sorted(pins)])
            return JobRequest.model_validate(body)
        if not isinstance(self.trigger, TradingDayTrigger) or not self.trigger.daily_capture_nodes:
            return request
        if trading_date is None:
            raise ValueError("a trading-date window requires a locked occurrence")
        start = datetime.combine(trading_date, time.min, CN)
        end = datetime.combine(trading_date + timedelta(days=1), time.min, CN)
        body = request.model_dump(mode="json")
        for node in body["nodes"]:
            if node["node_id"] in self.trigger.daily_capture_nodes:
                for source in node["capture"]["datasets"]:
                    source["request"].update(start=start.isoformat(), end=end.isoformat())
        return JobRequest.model_validate(body)


class ScheduleControl(Contract):
    enabled: bool = Field(strict=True)
