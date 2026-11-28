"""Bind a committed Tivelo Data event to an immutable workbench delivery."""

from typing import Self

from pydantic import model_validator
from tivelo_contracts.base import Contract, Digest, fingerprint, utc_datetime
from tivelo_contracts.data import DatasetReference
from tivelo_data.publications import Publication, PublicationCursor, ReceiptId

from .schedule_models import PublicationTrigger, ScheduleRequest
from .storage import JobError


class FrozenPublicationSchedule(Contract):
    request: ScheduleRequest
    origin: PublicationCursor

    @model_validator(mode="after")
    def same_dataset(self) -> Self:
        if (
            not isinstance(self.request.trigger, PublicationTrigger)
            or self.origin.dataset_id != self.request.trigger.dataset_id
        ):
            raise ValueError("subscription and initial cursor must select the same dataset")
        return self

    def cursor(self, raw):
        cursor = PublicationCursor.model_validate(raw)
        if (
            cursor.stream_id != self.origin.stream_id
            or cursor.dataset_id != self.origin.dataset_id
            or cursor.sequence < self.origin.sequence
            or (cursor.sequence == self.origin.sequence and cursor != self.origin)
        ):
            raise JobError("DATA_CONFLICT", "订阅游标与创建时的数据流不一致")
        return cursor


class PublicationReceipt(Contract):
    schedule_id: Digest
    stream_id: ReceiptId
    publication: Publication

    def checkpoint(self, origin):
        return origin.model_copy(
            update={"sequence": self.publication.sequence, "event_id": self.publication.event_id}
        )

    @property
    def reference(self):
        return DatasetReference(
            dataset_id=self.publication.dataset_id, version_id=self.publication.version_id
        )


def check_delivery(delivery, frozen):
    receipt = PublicationReceipt(
        schedule_id=delivery["schedule_id"],
        stream_id=frozen.origin.stream_id,
        publication=delivery.get("publication"),
    )
    event = receipt.publication
    if (
        event.dataset_id != frozen.origin.dataset_id
        or event.sequence <= frozen.origin.sequence
        or utc_datetime(delivery["due_at"]) != event.recorded_at
        or fingerprint(receipt) != delivery["delivery_id"]
        or delivery["request_key"] != "schedule." + delivery["delivery_id"]
    ):
        raise JobError("ARTIFACT_CORRUPT", "发布事件与订阅派发记录不一致")
    return receipt
