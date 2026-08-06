"""Tests for resolving dated engine routes through the published catalog."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.data_providers.fquant import catalog_resolver as cr


def _write_generation(
    root: Path,
    generation: str,
    logical: str,
    file: str,
    payload: bytes = b"data",
) -> Path:
    generation_dir = root / generation
    generation_dir.mkdir(parents=True, exist_ok=True)
    db_path = generation_dir / file
    db_path.write_bytes(payload)
    (generation_dir / "manifest.json").write_text(
        json.dumps(
            {
                "generation": generation,
                "created_at": "2026-07-13T00:00:00Z",
                "entries": [{"logical": logical, "file": file, "size_bytes": len(payload)}],
            }
        ),
        encoding="utf-8",
    )
    return db_path


def _publish_catalog(tmp_path: Path, routes: list[dict[str, object]]) -> Path:
    """Build a catalog snapshot without invoking the engine binary."""
    import duckdb

    catalog_root = tmp_path / "catalog"
    generation = "20260713T120000"
    generation_dir = catalog_root / generation
    generation_dir.mkdir(parents=True)
    db_path = generation_dir / "catalog.duckdb"
    connection = duckdb.connect(str(db_path))
    connection.execute(
        """CREATE TABLE catalog_routes (
            route_key TEXT, market TEXT, start_date DATE, end_date DATE,
            root TEXT, generation TEXT, logical TEXT, file TEXT,
            size_bytes BIGINT, freshness_mode TEXT, priority INT,
            stage TEXT, coverage_date DATE, reconciled BOOLEAN, quality TEXT,
            reconciliation_ref TEXT, supersedes TEXT, preliminary_root_id TEXT,
            preliminary_generation TEXT, updated_at TIMESTAMP)"""
    )
    for route in routes:
        staged = route.get("stage")
        if staged is None and route["freshness_mode"] == cr.FRESHNESS_REQUIRE_CURRENT:
            staged = "final"
        coverage_date = route.get("coverage_date")
        if staged == "final" and coverage_date is None:
            coverage_date = "2026-07-13"
        reconciled = route.get("reconciled", staged == "final")
        quality = route.get("quality", "verified" if staged == "final" else "")
        reconciliation_ref = route.get("reconciliation_ref", "test-reconcile" if staged == "final" else "")
        connection.execute(
            "INSERT INTO catalog_routes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,now())",
            [
                route["route_key"],
                route["market"],
                route.get("start_date"),
                route.get("end_date"),
                route["root"],
                route["generation"],
                route["logical"],
                route["file"],
                route.get("size_bytes", 0),
                route["freshness_mode"],
                route.get("priority", 0),
                staged,
                coverage_date,
                reconciled,
                quality,
                reconciliation_ref,
                route.get("supersedes", ""),
                route.get("preliminary_root_id", ""),
                route.get("preliminary_generation", ""),
            ],
        )
    connection.close()
    _write_generation(
        catalog_root,
        generation,
        "duckdb_catalog",
        "catalog.duckdb",
        db_path.read_bytes(),
    )
    (catalog_root / "current.json").write_text(
        json.dumps({"generation": generation}), encoding="utf-8"
    )
    return catalog_root


def _route(**overrides: object) -> dict[str, object]:
    route: dict[str, object] = {
        "route_key": "tdx_trans",
        "market": "a",
        "start_date": "2026-01-01",
        "end_date": "2026-12-31",
        "root": "/Volumes/WD1/duckdb/snapshots/engine-a",
        "generation": "20260713T090000",
        "logical": "tdx_trans_2026",
        "file": "t.duckdb",
        "freshness_mode": "require_current",
    }
    route.update(overrides)
    return route


def test_resolve_route_require_current_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_root = tmp_path / "engine-a"
    expected = _write_generation(data_root, "20260713T090000", "tdx_trans_2026", "t.duckdb")
    (data_root / "current.json").write_text(
        json.dumps({"generation": "20260713T090000"}), encoding="utf-8"
    )
    catalog_root = _publish_catalog(tmp_path, [_route()])
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_CATALOG", str(catalog_root))
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_ENGINE_A", str(data_root))

    assert cr.resolve_route("tdx_trans", "a", date(2026, 7, 13)) == str(expected)


def test_resolve_route_require_current_stale_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "engine-a"
    _write_generation(data_root, "20260713T090000", "tdx_trans_2026", "t.duckdb")
    _write_generation(data_root, "20260714T090000", "tdx_trans_2026", "t.duckdb")
    (data_root / "current.json").write_text(
        json.dumps({"generation": "20260714T090000"}), encoding="utf-8"
    )
    catalog_root = _publish_catalog(tmp_path, [_route()])
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_CATALOG", str(catalog_root))
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_ENGINE_A", str(data_root))

    with pytest.raises(cr.StaleCatalogError) as exc_info:
        cr.resolve_route("tdx_trans", "a", date(2026, 7, 13))
    # A staged (final) stale route gets the accurate republishing guidance only;
    # it must NOT carry the legacy stage=NULL migration guidance.
    message = str(exc_info.value)
    assert "pins" in message
    assert "republish a catalog row that pins the root's current generation" in message
    assert "publish this route as staged" not in message


def test_resolve_route_pinned_immutable_ignores_newer_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "trans-archive"
    expected = _write_generation(archive, "20260101T000000", "tdx_trans_2015", "t15.duckdb")
    _write_generation(archive, "20260601T000000", "tdx_trans_2016", "t16.duckdb")
    (archive / "current.json").write_text(
        json.dumps({"generation": "20260601T000000"}), encoding="utf-8"
    )
    catalog_root = _publish_catalog(
        tmp_path,
        [
            _route(
                start_date="2015-01-01",
                end_date="2015-12-31",
                root="/Volumes/WD1/duckdb/snapshots/engine-a-trans-archive",
                generation="20260101T000000",
                logical="tdx_trans_2015",
                file="t15.duckdb",
                freshness_mode="pinned_immutable",
            )
        ],
    )
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_CATALOG", str(catalog_root))
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_ENGINE_A_TRANS_ARCHIVE", str(archive))

    assert cr.resolve_route("tdx_trans", "a", date(2015, 6, 1)) == str(expected)


def test_same_catalog_routes_different_years_to_different_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "trans-archive"
    old_path = _write_generation(archive, "20200101T000000", "tdx_trans_2019", "t19.duckdb")
    current = tmp_path / "engine-a"
    new_path = _write_generation(current, "20260713T090000", "tdx_trans_2026", "t26.duckdb")
    (current / "current.json").write_text(
        json.dumps({"generation": "20260713T090000"}), encoding="utf-8"
    )
    catalog_root = _publish_catalog(
        tmp_path,
        [
            _route(
                start_date="2019-01-01",
                end_date="2019-12-31",
                root="/Volumes/WD1/duckdb/snapshots/engine-a-trans-archive",
                generation="20200101T000000",
                logical="tdx_trans_2019",
                file="t19.duckdb",
                freshness_mode="pinned_immutable",
            ),
            _route(file="t26.duckdb"),
        ],
    )
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_CATALOG", str(catalog_root))
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_ENGINE_A_TRANS_ARCHIVE", str(archive))
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_ENGINE_A", str(current))

    assert cr.resolve_route("tdx_trans", "a", date(2019, 7, 10)) == str(old_path)
    assert cr.resolve_route("tdx_trans", "a", date(2026, 7, 10)) == str(new_path)


def test_resolve_route_no_match_raises_without_raw_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog_root = _publish_catalog(tmp_path, [_route()])
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_CATALOG", str(catalog_root))

    with pytest.raises(cr.RouteNotFoundError):
        cr.resolve_route("tdx_trans", "a", date(2019, 5, 1))


@pytest.mark.parametrize(
    ("field", "value"),
    [("generation", "../escape"), ("file", "../escape.duckdb")],
)
def test_resolve_route_rejects_path_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    catalog_root = _publish_catalog(tmp_path, [_route(**{field: value})])
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_CATALOG", str(catalog_root))

    with pytest.raises(cr.CatalogError, match="unsafe"):
        cr.resolve_route("tdx_trans", "a", date(2026, 7, 13))


def test_resolve_route_rejects_unknown_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog_root = _publish_catalog(tmp_path, [_route(root="/tmp/untrusted")])
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_CATALOG", str(catalog_root))

    with pytest.raises(cr.CatalogError, match="unknown catalog root"):
        cr.resolve_route("tdx_trans", "a", date(2026, 7, 13))


def test_resolve_route_date_sharded_requires_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog_root = _publish_catalog(tmp_path, [_route()])
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_CATALOG", str(catalog_root))

    with pytest.raises(cr.CatalogError, match="trade_date is required"):
        cr.resolve_route("tdx_trans", "a", None)


def test_exact_final_wins_over_same_day_preliminary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final_root = tmp_path / "engine-a"
    preliminary_root = tmp_path / "engine-a-preliminary"
    final = _write_generation(final_root, "20260713T090000", "tdx_trans_2026", "final.duckdb")
    _write_generation(preliminary_root, "20260713T080000", "tdx_trans_preliminary", "pre.duckdb")
    (final_root / "current.json").write_text(json.dumps({"generation": "20260713T090000"}), encoding="utf-8")
    (preliminary_root / "current.json").write_text(json.dumps({"generation": "20260713T080000"}), encoding="utf-8")
    catalog_root = _publish_catalog(
        tmp_path,
        [
            _route(stage="final", coverage_date="2026-07-13", reconciled=True, quality="verified", reconciliation_ref="r", generation="20260713T090000", file="final.duckdb"),
            _route(route_key="tdx_trans_preliminary", root="/Volumes/WD1/duckdb/snapshots/engine-a-preliminary", stage="preliminary", coverage_date="2026-07-13", reconciled=False, quality="preliminary", generation="20260713T080000", logical="tdx_trans_preliminary", file="pre.duckdb"),
        ],
    )
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_CATALOG", str(catalog_root))
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_ENGINE_A", str(final_root))
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_ENGINE_A_PRELIMINARY", str(preliminary_root))

    assert cr.resolve_route("tdx_trans", "a", date(2026, 7, 13)) == str(final)


def test_route_resolution_pins_catalog_generation_once_per_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "engine-a"
    _write_generation(data_root, "20260713T090000", "tdx_trans_2026", "final.duckdb")
    (data_root / "current.json").write_text(json.dumps({"generation": "20260713T090000"}), encoding="utf-8")
    catalog_root = _publish_catalog(
        tmp_path,
        [_route(stage="final", coverage_date="2026-07-13", reconciled=True, quality="verified", reconciliation_ref="r", file="final.duckdb")],
    )
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_CATALOG", str(catalog_root))
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_ENGINE_A", str(data_root))
    original = cr._catalog_db_path
    calls = 0

    def pinned_catalog_path() -> str:
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(cr, "_catalog_db_path", pinned_catalog_path)
    cr.resolve_route("tdx_trans", "a", date(2026, 7, 13))
    assert calls == 1


def test_exact_preliminary_fallback_uses_preliminary_root_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preliminary_root = tmp_path / "engine-a-preliminary"
    expected = _write_generation(preliminary_root, "20260713T080000", "tdx_trans_preliminary", "pre.duckdb")
    (preliminary_root / "current.json").write_text(json.dumps({"generation": "20260713T080000"}), encoding="utf-8")
    catalog_root = _publish_catalog(
        tmp_path,
        [_route(route_key="tdx_trans_preliminary", root="/Volumes/WD1/duckdb/snapshots/engine-a-preliminary", stage="preliminary", coverage_date="2026-07-13", reconciled=False, quality="preliminary", generation="20260713T080000", logical="tdx_trans_preliminary", file="pre.duckdb")],
    )
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_CATALOG", str(catalog_root))
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_ENGINE_A_PRELIMINARY", str(preliminary_root))

    assert cr.resolve_route("tdx_trans", "a", date(2026, 7, 13)) == str(expected)


def test_missing_exact_route_does_not_read_stale_final_for_current_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "engine-a"
    _write_generation(data_root, "20260713T090000", "tdx_trans_2026", "t.duckdb")
    _write_generation(data_root, "20260714T090000", "tdx_trans_2026", "t.duckdb")
    (data_root / "current.json").write_text(json.dumps({"generation": "20260714T090000"}), encoding="utf-8")
    catalog_root = _publish_catalog(
        tmp_path,
        [_route(stage="final", coverage_date="2026-07-13", reconciled=True, quality="verified", reconciliation_ref="r")],
    )
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_CATALOG", str(catalog_root))
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_ENGINE_A", str(data_root))

    with pytest.raises(cr.CatalogError):
        cr.resolve_route("tdx_trans", "a", date(2026, 7, 14))


def test_staged_current_route_is_historical_only_before_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "engine-a"
    expected = _write_generation(data_root, "20260713T090000", "tdx_trans_2026", "t.duckdb")
    (data_root / "current.json").write_text(json.dumps({"generation": "20260713T090000"}), encoding="utf-8")
    catalog_root = _publish_catalog(
        tmp_path,
        [_route(stage="final", coverage_date="2026-07-13", reconciled=True, quality="verified", reconciliation_ref="r")],
    )
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_CATALOG", str(catalog_root))
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_ENGINE_A", str(data_root))

    assert cr.resolve_route("tdx_trans", "a", date(2026, 7, 12)) == str(expected)
    with pytest.raises(cr.CatalogError):
        cr.resolve_route("tdx_trans", "a", date(2026, 7, 14))


def test_legacy_pinned_archive_allowed_but_legacy_current_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "trans-archive"
    expected = _write_generation(archive, "20200101T000000", "tdx_trans_2019", "t.duckdb")
    (archive / "current.json").write_text(json.dumps({"generation": "20200101T000000"}), encoding="utf-8")
    catalog_root = _publish_catalog(tmp_path, [_route(root="/Volumes/WD1/duckdb/snapshots/engine-a-trans-archive", generation="20200101T000000", logical="tdx_trans_2019", file="t.duckdb", start_date="2019-01-01", end_date="2019-12-31", freshness_mode=cr.FRESHNESS_PINNED_IMMUTABLE)])
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_CATALOG", str(catalog_root))
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_ENGINE_A_TRANS_ARCHIVE", str(archive))
    assert cr.resolve_route("tdx_trans", "a", date(2019, 7, 10)) == str(expected)

    current_root = tmp_path / "engine-a"
    _write_generation(current_root, "20260713T090000", "tdx_trans_2026", "t26.duckdb")
    (current_root / "current.json").write_text(json.dumps({"generation": "20260713T090000"}), encoding="utf-8")
    # stage="" forces a genuine legacy (stage=NULL) require_current row. The
    # written file matches the route's file so the ONLY failure reason is the
    # legacy-stage fail-closed rejection, not a manifest/file mismatch.
    catalog_root = _publish_catalog(tmp_path / "current-case", [_route(start_date="2026-01-01", end_date="2026-12-31", file="t26.duckdb", stage="")])
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_CATALOG", str(catalog_root))
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_ENGINE_A", str(current_root))
    with pytest.raises(cr.CatalogError) as exc_info:
        cr.resolve_route("tdx_trans", "a", date(2026, 7, 12))
    message = str(exc_info.value)
    assert "legacy" in message
    assert "publish this route as staged" in message


@pytest.mark.parametrize(
    "overrides",
    [
        {"preliminary_root_id": "/tmp/preliminary"},
        {"preliminary_generation": "20260712T090000"},
        {"supersedes": "not-a-generation"},
        {"supersedes": "20260713T090000"},
    ],
)
def test_staged_provenance_metadata_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, object]
) -> None:
    data_root = tmp_path / "engine-a"
    _write_generation(data_root, "20260713T090000", "tdx_trans_2026", "t.duckdb")
    (data_root / "current.json").write_text(json.dumps({"generation": "20260713T090000"}), encoding="utf-8")
    route = _route(
        stage="final", coverage_date="2026-07-13", reconciled=True,
        quality="verified", reconciliation_ref="r", **overrides,
    )
    catalog_root = _publish_catalog(tmp_path, [route])
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_CATALOG", str(catalog_root))
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_ENGINE_A", str(data_root))

    with pytest.raises(cr.CatalogError):
        cr.resolve_route("tdx_trans", "a", date(2026, 7, 13))


def test_legacy_require_current_route_fails_with_actionable_migration_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A legacy (stage=NULL) require_current row cannot prove which generation a
    # live read should pin, so it must stay fail-closed AND surface staged
    # migration guidance instead of a bare historical-fallback rejection.
    data_root = tmp_path / "engine-a"
    _write_generation(data_root, "20260713T090000", "tdx_trans_2026", "t.duckdb")
    (data_root / "current.json").write_text(
        json.dumps({"generation": "20260713T090000"}), encoding="utf-8"
    )
    catalog_root = _publish_catalog(tmp_path, [_route(stage="")])
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_CATALOG", str(catalog_root))
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_ENGINE_A", str(data_root))

    with pytest.raises(cr.CatalogError) as exc_info:
        cr.resolve_route("tdx_trans", "a", date(2026, 7, 13))
    message = str(exc_info.value)
    assert "legacy" in message
    # Distinctive marker of the staged-migration guidance, shared with the
    # legacy-current case in test_legacy_pinned_archive_allowed_but_legacy_current_rejected.
    assert "publish this route as staged" in message
    assert "republish a catalog row that pins" not in message
