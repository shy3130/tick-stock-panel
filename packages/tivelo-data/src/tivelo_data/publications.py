"""A resumable feed of committed dataset versions in one local data store."""

import sqlite3
from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import Field, TypeAdapter, model_validator
from tivelo_contracts.base import Contract, Digest, Key, UTCDateTime
from tivelo_contracts.errors import ErrorCode, TiveloError

if TYPE_CHECKING:
    from .storage import SnapshotStore

ReceiptId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Sequence = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
_DATASET = TypeAdapter(Key)


class PublicationCursor(Contract):
    format: Literal["tivelo-publication-cursor-v1"] = "tivelo-publication-cursor-v1"
    stream_id: ReceiptId
    sequence: Sequence
    event_id: ReceiptId | None
    dataset_id: Key | None

    @model_validator(mode="after")
    def receipt_at_checkpoint(self) -> Self:
        if (self.sequence == 0) != (self.event_id is None):
            raise ValueError("only the initial cursor has no event receipt")
        return self


class Publication(Contract):
    sequence: Sequence = Field(gt=0)
    event_id: ReceiptId
    dataset_id: Key
    version_id: Digest
    recorded_at: UTCDateTime


class PublicationPage(Contract):
    format: Literal["tivelo-publications-v1"] = "tivelo-publications-v1"
    items: tuple[Publication, ...]
    cursor: PublicationCursor
    has_more: bool


def initialize_journal(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO metadata VALUES ('publication_stream', lower(hex(randomblob(16))))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS publication_events ("
        "sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL, "
        "dataset_id TEXT NOT NULL, version_id TEXT UNIQUE NOT NULL REFERENCES versions(version_id), "
        "recorded_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS publication_dataset ON publication_events(dataset_id,sequence)"
    )
    # A previous writer also uses this table. A failed or repeated insert never publishes an event.
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS record_dataset_publication AFTER INSERT ON versions BEGIN "
        "INSERT INTO publication_events(event_id,dataset_id,version_id,recorded_at) "
        "VALUES(lower(hex(randomblob(16))),NEW.dataset_id,NEW.version_id,"
        "strftime('%Y-%m-%dT%H:%M:%fZ','now')); END"
    )


def read_publications(
    store: "SnapshotStore",
    *,
    after: PublicationCursor | None,
    dataset_id: str | None,
    limit: int,
    tail: bool,
) -> PublicationPage:
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("publication page limit must be between 1 and 500")
    if type(tail) is not bool or (tail and after is not None):
        raise ValueError("tail and a saved publication cursor cannot be combined")
    if dataset_id is not None:
        dataset_id = _DATASET.validate_python(dataset_id)
    if after is not None:
        after = PublicationCursor.model_validate(after)
        if dataset_id is not None and dataset_id != after.dataset_id:
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, "Publication cursor belongs to another dataset filter"
            )
        dataset_id = after.dataset_id

    with store.connection() as db, db:
        # Use one read snapshot for cursor validation, the high watermark and the page.
        db.execute("BEGIN")
        identity = db.execute(
            "SELECT value FROM metadata WHERE key='publication_stream'"
        ).fetchone()
        if identity is None:
            raise TiveloError(ErrorCode.SNAPSHOT_CORRUPT, "Publication store identity is missing")
        (stream,) = identity
        if after is not None:
            if after.stream_id != stream:
                raise TiveloError(
                    ErrorCode.DATA_CONFLICT, "Publication cursor belongs to another store"
                )
            if after.sequence:
                previous = db.execute(
                    "SELECT event_id FROM publication_events WHERE sequence=?", (after.sequence,)
                ).fetchone()
                if previous != (after.event_id,):
                    raise TiveloError(
                        ErrorCode.DATA_CONFLICT, "Publication checkpoint is missing or changed"
                    )
        head = db.execute(
            "SELECT sequence,event_id FROM publication_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone() or (0, None)
        rows = []
        if not tail:
            where = "sequence>?"
            params: list[int | str] = [after.sequence if after is not None else 0]
            if dataset_id is not None:
                where += " AND dataset_id=?"
                params.append(dataset_id)
            rows = db.execute(
                "SELECT sequence,event_id,dataset_id,version_id,recorded_at FROM publication_events "
                f"WHERE {where} ORDER BY sequence LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
    more = len(rows) > limit
    checkpoint = rows[limit - 1][:2] if more else head
    return PublicationPage(
        items=tuple(
            Publication(
                sequence=seq,
                event_id=event,
                dataset_id=dataset,
                version_id=version,
                recorded_at=when,
            )
            for seq, event, dataset, version, when in rows[:limit]
        ),
        cursor=PublicationCursor(
            stream_id=stream, sequence=checkpoint[0], event_id=checkpoint[1], dataset_id=dataset_id
        ),
        has_more=more,
    )
