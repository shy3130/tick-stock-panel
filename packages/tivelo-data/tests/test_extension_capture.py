import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tivelo_contracts.errors import ErrorCode, TiveloError

from tivelo_data import extension_capture as capture


def setup(root, *, market=False, mode="timeseries", dtype="int"):
    table = root / "ext_data/ranks"
    table.mkdir(parents=True)
    schema = {
        "id": "ranks",
        "mode": mode,
        "market_level": market,
        "fields": [{"name": "rank", "dtype": dtype}],
        "pull": {"auth": {"key": "test-secret"}},
    }
    (table / "config.json").write_text(json.dumps(schema))
    path = table / (
        "part.parquet" if mode == "snapshot" else "timeseries/date=2025-01-03/part.parquet"
    )
    path.parent.mkdir(exist_ok=True, parents=True)
    mapping = capture.TSPFieldCapture(
        dataset_id="ranking",
        table_id="ranks",
        field_id="rank",
        field_version="1",
        unit="rank",
        source_id="tsp.sample",
        symbols={} if market else {"600000.SH": "A", "600001.SH": "B"},
        start="2025-01-01T00:00:00Z",
        end="2025-01-10T00:00:00Z",
    )
    return path, mapping


def observe(root, mapping):
    result = capture.observe_tsp_field(root / "journal", root, mapping, root / "inputs")
    artifact = root / "inputs" / result["input"]["path"]
    rows = [json.loads(line) for line in artifact.read_text().splitlines()]
    assert "test-secret" not in artifact.read_text() + json.dumps(result)
    return result, rows


def test_witnessed_only_revisions_and_tombstones_are_immutable(tmp_path, monkeypatch):
    path, mapping = setup(tmp_path)
    instant = datetime(2025, 1, 4, 6, tzinfo=UTC)
    monkeypatch.setattr(capture, "_stamp", lambda: instant)
    pq.write_table(pa.table({"symbol": ["600000.SH", "600001.SH"], "rank": [1, 4]}), path)
    original, rows = observe(tmp_path, mapping)
    assert {r["available_at"] for r in rows} == {"2025-01-04T06:00:00Z"}
    assert {r["effective_at"] for r in rows} == {"2025-01-02T16:00:00Z"}
    assert {r["valid_until"] for r in rows} == {"2025-01-03T16:00:00Z"}
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert all(
            r == (original, rows)
            for r in executor.map(lambda _: observe(tmp_path, mapping), range(2))
        )
    old_bytes = (tmp_path / "inputs" / original["input"]["path"]).read_bytes()
    instant = instant.replace(hour=7)
    pq.write_table(pa.table({"symbol": ["600000.SH"], "rank": [9]}), path)
    revised, latest = observe(tmp_path, mapping)
    assert original["field_reference"] == revised["field_reference"]
    assert original["input"]["path"] != revised["input"]["path"]
    assert [(v["instrument_id"], v["int_value"]) for v in latest] == [
        ("A", 1),
        ("A", 9),
        ("B", 4),
        ("B", None),
    ]
    assert (tmp_path / "inputs" / original["input"]["path"]).read_bytes() == old_bytes
    assert observe(tmp_path, mapping) == (revised, latest)


def test_market_snapshot_uses_witness_clock_and_recovers_after_duplicate(tmp_path, monkeypatch):
    path, mapping = setup(tmp_path, market=True, mode="snapshot", dtype="bool")
    instant = datetime(2025, 1, 3, 6, tzinfo=UTC)
    monkeypatch.setattr(capture, "_stamp", lambda: instant)
    pq.write_table(pa.table({"rank": [False, True]}), path)
    with pytest.raises(TiveloError) as exc:
        observe(tmp_path, mapping)
    assert exc.value.code == ErrorCode.DATA_CONFLICT
    pq.write_table(pa.table({"rank": [False]}), path)
    result, rows = observe(tmp_path, mapping)
    assert result["input"]["request"]["instrument_ids"] == []
    assert len(rows) == 1 and rows[0]["instrument_id"] is None and rows[0]["bool_value"] is False
    assert rows[0]["effective_at"] == rows[0]["available_at"] == "2025-01-03T06:00:00Z"
    pq.write_table(pa.table({"rank": [True]}), path)
    with pytest.raises(TiveloError):
        observe(tmp_path, mapping)
    instant = instant.replace(hour=7)
    _, rows = observe(tmp_path, mapping)
    assert [r["bool_value"] for r in rows] == [False, True]


@pytest.mark.parametrize("label", ["09:30", "2025-01-03 09:30:00", "2025-01-03T09:30:00+08:00"])
def test_legacy_intraday_time_uses_declared_zone_and_separate_witness_time(
    tmp_path, monkeypatch, label
):
    path, mapping = setup(tmp_path)
    config = tmp_path / "ext_data/ranks/config.json"
    schema = json.loads(config.read_text())
    schema["pull"]["time_field"] = "time"
    config.write_text(json.dumps(schema))
    monkeypatch.setattr(capture, "_stamp", lambda: datetime(2025, 1, 3, 6, tzinfo=UTC))
    pq.write_table(pa.table({"symbol": ["600000.SH"], "rank": [4], "time": [label]}), path)
    _, rows = observe(tmp_path, mapping)
    assert rows[0]["effective_at"] == "2025-01-03T01:30:00Z"
    assert rows[0]["available_at"] == "2025-01-03T06:00:00Z"
