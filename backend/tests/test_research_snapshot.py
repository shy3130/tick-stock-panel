from __future__ import annotations

from datetime import UTC, datetime

import polars as pl

from app.data_providers.trust import DataAudit, write_latest_audit
from app.services import strategy_cache

AS_OF = "2026-07-31"


def _seed_valid_inputs(tmp_path) -> None:
    recorded_at = datetime(2026, 7, 31, 8, 10, tzinfo=UTC)
    audits = [
        DataAudit(
            provider="tushare",
            dataset="instruments",
            status="ok",
            row_count=5537,
            returned_symbols=("600000.SH",),
            missing_symbols=(),
            coverage_ratio=1.0,
            observed_start=AS_OF,
            observed_end=AS_OF,
        ),
        DataAudit(
            provider="tushare",
            dataset="daily",
            status="ok",
            row_count=1,
            returned_symbols=("600000.SH",),
            missing_symbols=(),
            coverage_ratio=1.0,
            observed_start=AS_OF,
            observed_end=AS_OF,
        ),
        DataAudit(
            provider="tushare",
            dataset="adj_factor",
            status="ok",
            row_count=1,
            returned_symbols=("600000.SH",),
            missing_symbols=(),
            coverage_ratio=1.0,
            observed_start="2026-07-01",
            observed_end="2026-07-30",
        ),
        DataAudit(
            provider="derived",
            dataset="daily_enriched",
            status="ok",
            row_count=1,
            returned_symbols=("600000.SH",),
            missing_symbols=(),
            coverage_ratio=1.0,
            observed_start=AS_OF,
            observed_end=AS_OF,
        ),
    ]
    for audit in audits:
        write_latest_audit(tmp_path, audit, recorded_at=recorded_at)

    strategy_cache.write_cache(
        tmp_path,
        AS_OF,
        {
            "bullish_alignment": {
                "as_of": AS_OF,
                "total": 1,
                "rows": [
                    {
                        "symbol": "600000.SH",
                        "name": "浦发银行",
                        "close": 10.2,
                        "change_pct": 0.02,
                        "score": 80.0,
                    }
                ],
            }
        },
    )
    for dataset in ("kline_daily", "kline_daily_enriched"):
        path = tmp_path / dataset / f"date={AS_OF}" / "part.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(
            {
                "symbol": ["600000.SH"],
                "date": [AS_OF],
                "close": [10.2],
            }
        ).write_parquet(path)


def test_publish_research_snapshot_freezes_one_consistent_research_bundle(tmp_path):
    """Later receipt/cache writes must not mutate the already published bundle."""
    from app.services.research_snapshot import (
        load_latest_research_snapshot,
        publish_research_snapshot,
    )

    _seed_valid_inputs(tmp_path)

    published = publish_research_snapshot(tmp_path)

    assert published["schema_version"] == 2
    assert published["as_of"] == AS_OF
    assert len(published["snapshot_id"]) == 64
    assert published["strategy_cache"]["as_of"] == AS_OF
    assert {item["dataset"] for item in published["audits"]} == {
        "instruments",
        "daily",
        "adj_factor",
        "daily_enriched",
    }

    strategy_cache.write_cache(
        tmp_path,
        "2026-08-01",
        {
            "new_day": {
                "as_of": "2026-08-01",
                "total": 0,
                "rows": [],
            }
        },
    )

    loaded = load_latest_research_snapshot(tmp_path)
    assert loaded == published
    assert loaded["strategy_cache"]["as_of"] == AS_OF
    assert set(loaded["source_evidence"]) == {
        "kline_daily",
        "kline_daily_enriched",
    }


def test_republishing_same_semantic_bundle_reuses_immutable_archive(tmp_path):
    from app.services.research_snapshot import publish_research_snapshot

    _seed_valid_inputs(tmp_path)
    first = publish_research_snapshot(tmp_path)

    strategy_cache.write_cache(
        tmp_path,
        AS_OF,
        first["strategy_cache"]["results"],
    )
    second = publish_research_snapshot(tmp_path)

    archive_dir = tmp_path / "research_snapshots" / f"date={AS_OF}"
    assert second == first
    assert list(archive_dir.glob("*.json")) == [
        archive_dir / f"{first['snapshot_id']}.json"
    ]


def test_rejected_publish_keeps_previous_snapshot_visible(tmp_path):
    """A newer but inconsistent receipt set must not replace a trusted snapshot."""
    from app.services.research_snapshot import (
        ResearchSnapshotRejectedError,
        load_latest_research_snapshot,
        publish_research_snapshot,
    )

    _seed_valid_inputs(tmp_path)
    first = publish_research_snapshot(tmp_path)

    write_latest_audit(
        tmp_path,
        DataAudit(
            provider="tushare",
            dataset="daily",
            status="ok",
            row_count=1,
            returned_symbols=("600000.SH",),
            missing_symbols=(),
            coverage_ratio=1.0,
            observed_start="2026-08-01",
            observed_end="2026-08-01",
        ),
        recorded_at=datetime(2026, 8, 1, 8, 10, tzinfo=UTC),
    )

    try:
        publish_research_snapshot(tmp_path)
    except ResearchSnapshotRejectedError as exc:
        assert "策略结果仍为 2026-07-31" in str(exc)
    else:
        raise AssertionError("inconsistent inputs were published")

    assert load_latest_research_snapshot(tmp_path) == first


def test_snapshot_history_is_date_ordered_validated_and_excludes_current(tmp_path):
    import json

    from app.services.research_snapshot import (
        _snapshot_id,
        load_research_snapshot_history,
        publish_research_snapshot,
    )

    _seed_valid_inputs(tmp_path)
    current = publish_research_snapshot(tmp_path)
    previous = {
        **current,
        "schema_version": 1,
        "as_of": "2026-07-30",
        "published_at": "2026-07-30T08:10:00+00:00",
        "strategy_cache": {
            **current["strategy_cache"],
            "as_of": "2026-07-30",
            "results": {
                key: {**value, "as_of": "2026-07-30"}
                for key, value in current["strategy_cache"]["results"].items()
            },
        },
    }
    previous.pop("source_evidence")
    previous["snapshot_id"] = _snapshot_id(
        previous["as_of"], previous["audits"], previous["strategy_cache"]
    )
    archive = (
        tmp_path
        / "research_snapshots"
        / "date=2026-07-30"
        / f"{previous['snapshot_id']}.json"
    )
    archive.parent.mkdir(parents=True)
    archive.write_text(json.dumps(previous), encoding="utf-8")
    (archive.parent / "corrupt.json").write_text("not-json", encoding="utf-8")

    history = load_research_snapshot_history(
        tmp_path,
        before_as_of="2026-07-31",
        limit=10,
    )

    assert [snapshot["as_of"] for snapshot in history] == ["2026-07-30"]
    assert history[0]["snapshot_id"] == previous["snapshot_id"]


def test_published_snapshot_detects_source_partition_drift(tmp_path):
    from app.services.research_snapshot import (
        publish_research_snapshot,
        research_snapshot_source_problem,
    )

    _seed_valid_inputs(tmp_path)
    published = publish_research_snapshot(tmp_path)
    path = tmp_path / "kline_daily" / f"date={AS_OF}" / "part.parquet"
    pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "date": [AS_OF],
            "close": [11.2],
        }
    ).write_parquet(path)

    problem = research_snapshot_source_problem(tmp_path, published)

    assert problem["code"] == "RESEARCH_SOURCE_DRIFT_AFTER_PUBLICATION"
    assert "重新运行" in problem["next_action"]


def test_published_snapshot_seals_same_day_research_until_source_changes(tmp_path):
    """A valid same-day bundle must stop post-close realtime persistence."""
    from app.services.research_snapshot import (
        is_research_date_sealed,
        publish_research_snapshot,
    )

    _seed_valid_inputs(tmp_path)
    publish_research_snapshot(tmp_path)

    assert is_research_date_sealed(tmp_path, AS_OF) is True

    path = tmp_path / "kline_daily" / f"date={AS_OF}" / "part.parquet"
    pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "date": [AS_OF],
            "close": [11.2],
        }
    ).write_parquet(path)

    assert is_research_date_sealed(tmp_path, AS_OF) is False


def test_legacy_snapshot_fails_closed_when_same_day_source_is_newer(tmp_path):
    from app.services.research_snapshot import (
        _snapshot_id,
        publish_research_snapshot,
        research_snapshot_source_problem,
    )

    _seed_valid_inputs(tmp_path)
    published = publish_research_snapshot(tmp_path)
    legacy = {
        key: value
        for key, value in published.items()
        if key != "source_evidence"
    }
    legacy["schema_version"] = 1
    legacy["published_at"] = "2020-01-01T00:00:00+00:00"
    legacy["snapshot_id"] = _snapshot_id(
        legacy["as_of"], legacy["audits"], legacy["strategy_cache"]
    )

    problem = research_snapshot_source_problem(tmp_path, legacy)

    assert problem["code"] == "RESEARCH_SOURCE_DRIFT_AFTER_PUBLICATION"
