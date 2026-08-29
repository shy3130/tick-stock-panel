from __future__ import annotations

from datetime import date

import pytest

from app.services.hold_firm_patterns.adapters import (
    PinnedPresenceUniverseReader,
    presence_universe_identity,
)
from app.services.hold_firm_patterns.models import PitUniverseStatus, UniverseIdentity
from app.services.universe_presence_history import (
    PresenceDaySnapshot,
    PresenceHistoryError,
)

DAY = date(2022, 3, 4)
SOURCE = {
    "artifact": "fstore_snapshot",
    "generation": "20220304T120000",
    "manifest_sha256": "a" * 64,
}


class _Presence:
    def __init__(self, snapshot: PresenceDaySnapshot, *, manifest: dict[str, object] | None = None):
        self.snapshot_value = snapshot
        self.manifest = manifest or {
            "generation": "20220304T120000Z-" + "b" * 16,
            "schema_version": 2,
            "artifact": "universe_presence",
            "rule_version": "presence_v1",
            "retrospective": True,
            "status_filter": "daily_market_row_present_exact_day",
            "source": SOURCE,
        }
        self.requested: list[date] = []

    def prefetch_presence_days(self, days):
        self.requested.extend(days)
        return {day: self.snapshot_value for day in days}

    def source_manifest(self):
        return self.manifest


def test_presence_reader_pins_exact_days_and_only_present_membership():
    snapshot = PresenceDaySnapshot(DAY, ("600000.SH",), 1, True, "c" * 64)
    source = _Presence(snapshot)
    reader = PinnedPresenceUniverseReader(source, (DAY,))

    assert source.requested == [DAY]
    assert reader.membership("600000.SH", DAY) is PitUniverseStatus.IN_POOL
    identity = reader.identity()
    assert identity.rule_version == "presence_v1"
    assert identity.schema_version == 2
    assert identity.artifact == "universe_presence"
    assert identity.retrospective is True
    assert identity.status_filter == "daily_market_row_present_exact_day"
    assert identity.source_generation == SOURCE["generation"]
    assert identity.source_manifest_sha256 == SOURCE["manifest_sha256"]
    assert identity.day_identities[0].day == DAY
    assert identity.day_identities[0].content_hash == "c" * 64


def test_not_observed_and_absence_fail_closed_never_not_in_pool():
    not_observed = PresenceDaySnapshot(DAY, (), 0, False, "d" * 64)
    with pytest.raises(PresenceHistoryError, match="not observed"):
        PinnedPresenceUniverseReader(_Presence(not_observed), (DAY,))

    observed = PinnedPresenceUniverseReader(
        _Presence(PresenceDaySnapshot(DAY, ("600000.SH",), 1, True, "c" * 64)), (DAY,)
    )
    with pytest.raises(PresenceHistoryError, match="cannot prove"):
        observed.membership("600001.SH", DAY)


def test_presence_identity_rejects_wrong_rule_and_short_hash():
    manifest = dict(_Presence(PresenceDaySnapshot(DAY, ("600000.SH",), 1, True, "c" * 64)).manifest)
    manifest["rule_version"] = "eligible_v1"
    with pytest.raises(ValueError, match="rule_version"):
        presence_universe_identity(manifest)

    with pytest.raises(ValueError):
        UniverseIdentity(
            generation="20220304T120000Z-" + "b" * 16,
            manifest_sha256="b" * 16,
            schema_version=2,
            artifact="universe_presence",
            rule_version="presence_v1",
            retrospective=True,
            status_filter="daily_market_row_present_exact_day",
            source_artifact="fstore_snapshot",
            source_generation="20220304T120000",
            source_manifest_sha256="a" * 64,
            day_identities=(),
        )

    manifest["rule_version"] = "presence_v1"
    manifest["status_filter"] = "eligible_stock_status"
    with pytest.raises(ValueError, match="status_filter"):
        presence_universe_identity(manifest)

    manifest["status_filter"] = "daily_market_row_present_exact_day"
    manifest["schema_version"] = 2.0
    with pytest.raises(ValueError, match="schema_version"):
        presence_universe_identity(manifest)

    manifest["schema_version"] = 2
    manifest["source"] = {**SOURCE, "artifact": "eligible_universe"}
    with pytest.raises(ValueError, match="fstore source"):
        presence_universe_identity(manifest)


def test_old_eligible_reader_shape_is_not_accepted():
    class EligibleOnly:
        def prefetch_event_days(self, days):
            return {}

    with pytest.raises(AttributeError):
        PinnedPresenceUniverseReader(EligibleOnly(), (DAY,))  # type: ignore[arg-type]
