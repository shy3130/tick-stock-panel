import polars as pl
import pytest
from test_daily import at, bar, factors, opening, security
from test_snapshot import query, write_bundle
from tivelo_contracts.data import EventQuery, HistoryQuery
from tivelo_contracts.errors import ErrorCode, TiveloError

from tivelo_backtest.snapshot import PortableSnapshotReader


class Observer:
    stop = False

    def cancelled(self):
        return self.stop

    def progress(self, done, total, phase):
        pass


def test_prepared_queries_share_fact_buffers_and_preserve_as_of_selection(tmp_path, monkeypatch):
    root = tmp_path / "bundle"
    base = bar(2, "10")
    revision = bar(2, "12", available_at=at(4), ingested_at=at(4), revision_id="later")
    reference, _ = write_bundle(
        root,
        bars=(base, revision),
        instruments=(security(),),
        openings=(opening(2, "10"),),
        factors=factors(),
    )
    with PortableSnapshotReader(root) as reader:
        assert reader.prepare(max_bytes=100000) > 0
        identities = tuple(id(value[1]) for value in reader._prepared.values())
        monkeypatch.setattr(
            pl, "scan_parquet", lambda *args, **kwargs: pytest.fail("trial rescanned facts")
        )
        for _ in range(2):
            for day in (2, 3, 4, 6):
                assert reader.bars(query(day)) == reference.bars(query(day))
                request = HistoryQuery(
                    instrument_ids=("A",), effective_at=at(day, 8), as_of=at(day, 8)
                )
                assert reader.instruments(request) == reference.instruments(request)
                assert reader.factors(request) == reference.factors(request)
            event = EventQuery(instrument_ids=("A",), start=at(2), end=at(3), as_of=at(3))
            assert reader.openings(event) == reference.openings(event)
        assert tuple(id(value[1]) for value in reader._prepared.values()) == identities
        assert reader.prepare(max_bytes=100000) == reader.prepared_fact_bytes
    assert reader.prepared_fact_bytes == 0


def test_prepared_selection_checks_conflicts_before_unfinished_filter(tmp_path):
    root = tmp_path / "bundle"
    write_bundle(root, bars=(bar(2, "10"), bar(2, "12", revision_id="conflict", is_final=False)))
    with PortableSnapshotReader(root) as reader:
        reader.prepare(max_bytes=100000)
        with pytest.raises(TiveloError) as error:
            reader.bars(query())
        assert error.value.code == ErrorCode.DATA_CONFLICT


def test_prepared_file_mutation_is_detected_before_cached_answers(tmp_path):
    root = tmp_path / "bundle"
    write_bundle(root, bars=(bar(2, "10"),))
    with PortableSnapshotReader(root) as reader:
        reader.prepare(max_bytes=100000)
        path = next((root / "datasets").rglob("*.parquet"))
        path.write_bytes(path.read_bytes() + b"corrupt")
        with pytest.raises(TiveloError) as error:
            reader.bars(query())
        assert error.value.code == ErrorCode.SNAPSHOT_CORRUPT


@pytest.mark.parametrize("cancel", [False, True])
def test_failed_preparation_stops_stream_and_never_publishes_partial_buffers(
    tmp_path, monkeypatch, cancel
):
    root = tmp_path / "bundle"
    write_bundle(root, bars=(bar(2, "10"),))
    original = pl.LazyFrame.sink_batches
    calls = []
    observer = Observer()

    def sink(frame, consume, **kwargs):
        def callback(batch):
            if cancel:
                observer.stop = True
            stop = consume(batch)
            calls.append(stop)
            return stop

        return original(frame, callback, **kwargs)

    monkeypatch.setattr(pl.LazyFrame, "sink_batches", sink)
    with PortableSnapshotReader(root) as reader:
        with pytest.raises(TiveloError) as error:
            reader.prepare(max_bytes=1 if not cancel else 100000, observer=observer)
        assert error.value.code == (ErrorCode.CANCELLED if cancel else ErrorCode.CONTRACT_INVALID)
        assert calls == [True]
        assert reader.prepared_fact_bytes == 0 and not reader._prepared
        assert reader.bars(query())[0].close == 10


def test_cancel_before_preparation_does_not_start_stream(tmp_path, monkeypatch):
    root = tmp_path / "bundle"
    write_bundle(root, bars=(bar(2, "10"),))
    with PortableSnapshotReader(root) as reader:
        monkeypatch.setattr(
            pl.LazyFrame,
            "sink_batches",
            lambda *args, **kwargs: pytest.fail("started after cancel"),
        )
        observer = Observer()
        observer.stop = True
        with pytest.raises(TiveloError) as error:
            reader.prepare(max_bytes=100000, observer=observer)
        assert error.value.code == ErrorCode.CANCELLED
