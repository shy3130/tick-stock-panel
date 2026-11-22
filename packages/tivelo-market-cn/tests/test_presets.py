from datetime import UTC, date, datetime
from importlib import metadata

import pytest
from pydantic import ValidationError
from test_rules import instrument, rules
from tivelo_contracts.base import canonical_bytes
from tivelo_contracts.errors import ErrorCode, TiveloError

from tivelo_market_cn.adapter import CNMarketProfile
from tivelo_market_cn.calendar import CNCalendar
from tivelo_market_cn.presets import (
    CalendarPreset,
    CalendarPresetContents,
    compile_calendar,
    installed_calendar_presets,
    load_calendar_preset,
)


def utc(value):
    return datetime.fromisoformat(value).astimezone(UTC)


def test_installed_calendar_catalog_contains_loadable_pinned_presets():
    catalog = installed_calendar_presets()
    versions = [p.contents.preset_version for p in catalog]
    assert "2026.1.0" in versions and len(versions) == len(set(versions))
    assert [p.contents.verified_on for p in catalog] == sorted(
        (p.contents.verified_on for p in catalog), reverse=True
    )
    for preset in catalog:
        assert load_calendar_preset(
            version=preset.contents.preset_version, expected_sha256=preset.version_id
        ) == preset


def compiled(venue="XSHG", start="2024-01-01", end="2026-12-31"):
    return compile_calendar(
        load_calendar_preset(version="2026.1.0"),
        venue_id=venue,
        start=date.fromisoformat(start),
        end=date.fromisoformat(end),
    )


@pytest.mark.parametrize("venue", ["XSHG", "XSHE"])
@pytest.mark.parametrize(
    "day,is_open",
    [
        ("2024-02-08", True),
        ("2024-02-09", False),
        ("2024-02-18", False),
        ("2024-02-19", True),
        ("2024-04-07", False),
        ("2024-09-14", False),
        ("2025-01-27", True),
        ("2025-01-28", False),
        ("2025-02-04", False),
        ("2025-02-05", True),
        ("2025-02-08", False),
        ("2025-06-02", False),
        ("2025-10-08", False),
        ("2025-10-09", True),
        ("2026-01-04", False),
        ("2026-02-14", False),
        ("2026-02-23", False),
        ("2026-02-24", True),
        ("2026-09-20", False),
        ("2026-09-25", False),
        ("2026-10-07", False),
        ("2026-10-08", True),
    ],
)
def test_announced_closures_and_working_weekends(venue, day, is_open):
    calendar = CNCalendar(compiled(venue, day, day))
    assert calendar.is_open(date.fromisoformat(day), as_of=utc(day + "T09:30:00+08:00")) is is_open


@pytest.mark.parametrize("venue", ["XSHG", "XSHE"])
def test_regular_equity_auction_sessions_and_daily_convention(venue):
    calendar = CNCalendar(compiled(venue, "2026-07-03", "2026-07-06"))
    for day in ("2026-07-03", "2026-07-06"):
        for clock, expected in (
            ("09:14", None),
            ("09:15", "auction"),
            ("09:25", None),
            ("09:30", "continuous"),
            ("11:30", None),
            ("12:59", None),
            ("13:00", "continuous"),
            ("14:57", "auction"),
            ("15:00", None),
            ("15:05", None),
        ):
            moment = utc(f"{day}T{clock}:00+08:00")
            session = calendar.session_at(moment, as_of=moment)
            assert (session.kind if session else None) == expected
        observed = calendar.trading_days(
            date.fromisoformat(day), date.fromisoformat(day), as_of=utc(day + "T00:00:00+08:00")
        )[0]
        assert observed.open_at == utc(day + "T09:30:00+08:00")
        assert observed.close_at == utc(day + "T15:00:00+08:00")
        assert observed.bar_convention_id == "cn.equity.regular-auction.daily.v1"
        assert calendar.daily_bounds(observed.trading_date, as_of=observed.close_at)[1] == utc(
            day + "T14:57:00+08:00"
        )


def test_each_year_has_its_own_conservative_announcement_boundary():
    calendar = CNCalendar(compiled())
    assert calendar.is_open(date(2024, 12, 24), as_of=utc("2024-12-23T12:00:00+08:00"))
    for day, published in (("2025-01-02", "2024-12-23"), ("2026-01-05", "2025-12-22")):
        with pytest.raises(TiveloError) as error:
            calendar.is_open(date.fromisoformat(day), as_of=utc(published + "T23:59:59+08:00"))
        assert error.value.code == ErrorCode.FUTURE_INFORMATION
    assert calendar.is_open(date(2025, 1, 2), as_of=utc("2024-12-24T00:00:00+08:00"))
    assert calendar.is_open(date(2026, 1, 5), as_of=utc("2025-12-23T00:00:00+08:00"))


def test_cross_year_and_holiday_t_plus_one_are_calendar_based():
    calendar = CNCalendar(compiled())
    assert calendar.next_trading_open(date(2024, 12, 31), as_of=utc("2024-12-31T07:00:00Z")) == utc(
        "2025-01-02T09:30:00+08:00"
    )
    assert calendar.next_trading_open(date(2026, 2, 13), as_of=utc("2026-02-13T07:00:00Z")) == utc(
        "2026-02-24T09:30:00+08:00"
    )
    with pytest.raises(TiveloError) as error:
        calendar.next_trading_open(date(2024, 12, 31), as_of=utc("2024-12-23T12:00:00+08:00"))
    assert error.value.code == ErrorCode.FUTURE_INFORMATION
    with pytest.raises(TiveloError) as error:
        calendar.next_trading_open(date(2026, 12, 31), as_of=utc("2026-12-31T07:00:00Z"))
    assert error.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"start": date(2023, 12, 31)}, ErrorCode.DATA_COVERAGE_INSUFFICIENT),
        ({"end": date(2027, 1, 1)}, ErrorCode.DATA_COVERAGE_INSUFFICIENT),
        ({"venue_id": "XHKG"}, ErrorCode.UNSUPPORTED_ASSET),
        ({"start": date(2026, 1, 3), "end": date(2026, 1, 2)}, ErrorCode.CONTRACT_INVALID),
    ],
)
def test_unverified_scope_cannot_silently_use_weekday_fallback(changes, code):
    arguments = dict(venue_id="XSHG", start=date(2024, 1, 1), end=date(2026, 12, 31))
    arguments.update(changes)
    with pytest.raises(TiveloError) as error:
        compile_calendar(load_calendar_preset(version="2026.1.0"), **arguments)
    assert error.value.code == code


def test_identity_and_explicit_version_pin_are_deterministic():
    preset = load_calendar_preset(version="2026.1.0")
    assert load_calendar_preset(version="2026.1.0", expected_sha256=preset.version_id) == preset
    restored = CalendarPreset.model_validate_json(canonical_bytes(preset))
    assert canonical_bytes(
        compile_calendar(restored, venue_id="XSHG", start=date(2024, 1, 1), end=date(2024, 12, 31))
    ) == canonical_bytes(compiled(end="2024-12-31"))
    with pytest.raises(TiveloError) as error:
        load_calendar_preset(version="2026.1.0", expected_sha256="0" * 64)
    assert error.value.code == ErrorCode.ARTIFACT_CORRUPT
    for version in ("latest", "2027.1.0", "../../README.md"):
        with pytest.raises(TiveloError) as error:
            load_calendar_preset(version=version)
        assert error.value.code == ErrorCode.RULES_UNAVAILABLE
    changed = preset.model_dump(mode="json")
    changed["contents"]["years"][0]["closures"][0]["end"] = "2024-01-02"
    with pytest.raises(ValidationError):
        CalendarPreset.model_validate(changed)


def test_declared_sources_and_history_are_present():
    preset = load_calendar_preset(version="2026.1.0")
    assert len(preset.contents.years) == 6
    assert len(preset.contents.schedules) == 4
    assert all(source.url.startswith("https://") for source in preset.contents.sources)
    for schedule in preset.contents.schedules:
        assert schedule.clauses
        assert schedule.effective_from in (date(2023, 4, 10), date(2026, 7, 6))
    compiled_days = compiled("XSHG", "2026-07-03", "2026-07-06").contents.days
    assert compiled_days[0].available_at == utc("2025-12-23T00:00:00+08:00")
    assert compiled_days[-1].available_at == utc("2026-04-25T00:00:00+08:00")


@pytest.mark.parametrize("venue", ["XSHG", "XSHE"])
def test_complete_years_include_leap_day_and_have_expected_session_counts(venue):
    calendar = CNCalendar(compiled(venue))
    assert len(calendar.version.contents.days) == 1096
    assert calendar.is_open(date(2024, 2, 29), as_of=utc("2024-02-29T00:00:00Z"))
    # Weekdays less the announced weekday closures: 262-20, 261-18, 261-19.
    for year, count in ((2024, 242), (2025, 243), (2026, 242)):
        assert (
            len(
                calendar.trading_days(
                    date(year, 1, 1), date(year, 12, 31), as_of=utc(f"{year}-12-31T07:00:00Z")
                )
            )
            == count
        )


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["sources"].append(d["sources"][0]),
        lambda d: d["years"][0].update(source_ids=["missing-source"]),
        lambda d: d["years"].pop(1),
        lambda d: d["years"][0]["closures"][0].update(end="2024-02-10"),
        lambda d: d["schedules"][0]["sessions"][0].update(end="25:00"),
        lambda d: d["schedules"][0]["sessions"][1].update(start="09:20"),
        lambda d: d["schedules"][0].update(weekdays=[0, 0, 1]),
        lambda d: d["schedules"][0].update(weekdays=[False, 1]),
        lambda d: d.update(verified_on="2023-01-01"),
        lambda d: d.update(scope="all_assets_all_sessions"),
    ],
)
def test_invalid_catalog_metadata_cannot_be_resealed_as_valid(change):
    values = load_calendar_preset(version="2026.1.0").contents.model_dump(mode="json")
    change(values)
    with pytest.raises(ValidationError):
        CalendarPresetContents.model_validate(values)


@pytest.mark.parametrize("payload", [None, "{", '{"preset_format":"unknown"}'])
def test_missing_or_corrupt_installed_resource_fails_closed(monkeypatch, tmp_path, payload):
    import tivelo_market_cn.presets as presets

    if payload is not None:
        folder = tmp_path / "calendar_presets"
        folder.mkdir()
        (folder / "2026.1.0.json").write_text(payload)
    monkeypatch.setattr(presets.resources, "files", lambda name: tmp_path)
    with pytest.raises(TiveloError) as error:
        load_calendar_preset(version="2026.1.0")
    assert error.value.code == ErrorCode.ARTIFACT_CORRUPT


def test_resealed_mutation_cannot_replace_installed_release(monkeypatch, tmp_path):
    import tivelo_market_cn.presets as presets

    original = load_calendar_preset(version="2026.1.0")
    values = original.contents.model_dump(mode="json")
    values["years"][0]["closures"][0]["end"] = "2024-01-02"
    modified = CalendarPreset.create(CalendarPresetContents.model_validate(values))
    assert modified.version_id != original.version_id
    folder = tmp_path / "calendar_presets"
    folder.mkdir()
    (folder / "2026.1.0.json").write_bytes(canonical_bytes(modified))
    monkeypatch.setattr(presets.resources, "files", lambda name: tmp_path)
    with pytest.raises(TiveloError) as error:
        load_calendar_preset(version="2026.1.0")
    assert error.value.code == ErrorCode.ARTIFACT_CORRUPT
    # A task holding the original immutable catalog remains reproducible.
    calendar = CNCalendar(
        compile_calendar(original, venue_id="XSHE", start=date(2024, 1, 2), end=date(2024, 1, 2))
    )
    assert calendar.is_open(date(2024, 1, 2), as_of=utc("2024-01-02T01:30:00Z"))


def test_loading_requires_no_network_and_full_calendar_fits_run_configuration(monkeypatch):
    import socket

    from tivelo_contracts.execution import MarketAdapterSpec

    def no_network(*args, **kwargs):
        raise AssertionError("calendar loading must not access a network")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    calendar = compiled()
    profile = CNMarketProfile(configuration=rules().configuration, calendar=calendar)
    spec = MarketAdapterSpec(
        adapter_id="tivelo.market.cn.cash-equity",
        distribution="tivelo-market-cn",
        configuration_json=profile.model_dump_json(),
    )
    assert len(spec.configuration_json.encode()) < 1_048_576
    assert CNMarketProfile.model_validate_json(spec.configuration_json).calendar == calendar


def test_installed_factory_consumes_compiled_calendar_without_changing_legacy_profile():
    original = rules()
    calendar = compiled(start="2025-01-01", end="2025-02-28")
    profile = CNMarketProfile(configuration=original.configuration, calendar=calendar)
    entry = next(
        item
        for item in metadata.entry_points(group="tivelo.market_rules.v1")
        if item.name == "tivelo.market.cn.cash-equity"
    )
    restored = entry.load()(configuration_json=profile.model_dump_json())
    security = instrument(calendar_id=calendar.contents.calendar_id)
    assert restored.sellable_at(security, utc("2025-01-27T09:30:00+08:00")) == utc(
        "2025-02-05T09:30:00+08:00"
    )
    assert restored.reference != original.reference
    assert restored.calendar.version == calendar
    old_profile = CNMarketProfile(
        configuration=original.configuration, calendar=original.calendar.version
    )
    assert (
        entry.load()(configuration_json=old_profile.model_dump_json()).reference
        == original.reference
    )
