"""分钟请求批次属于 Provider;核心只消费有界帧。"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.data_providers.base import ProviderRequestError
from app.data_providers.custom.config import CustomSourceConfig, DatasetConfig
from app.data_providers.custom.managed import ManagedProvider
from app.data_providers.custom.provider import GenericHTTPProvider
from app.services import kline_sync


def frame(symbols):
    return pl.DataFrame({
        "symbol": symbols, "datetime": [datetime(2026, 11, 6, 9, 31)] * len(symbols),
        "open": [10.0] * len(symbols), "high": [10.2] * len(symbols),
        "low": [9.9] * len(symbols), "close": [10.1] * len(symbols),
        "volume": [100.0] * len(symbols), "amount": [101000.0] * len(symbols),
    })


class StreamSource:
    config = SimpleNamespace(datasets={"minute": None})
    name = "stream"

    def __init__(self, size=200, fail=False):
        self.size, self.fail = size, fail
        self.calls, self.closed, self.requests = [], False, []

    def iter_minute(self, symbols, *, on_chunk_done=None, **kwargs):
        self.calls.append((list(symbols), kwargs))
        try:
            for offset in range(0, len(symbols), self.size):
                if offset and self.fail:
                    raise ProviderRequestError("source request failed")
                batch = symbols[offset:offset + self.size]
                self.requests.append(batch)
                yield frame(batch)
                if on_chunk_done:
                    on_chunk_done(min(offset + self.size, len(symbols)), len(symbols))
        finally:
            self.closed = True

    def get_minute(self, *args, **kwargs):
        raise AssertionError("流式同步不能聚合完整结果")


def route(monkeypatch, provider):
    monkeypatch.setattr(kline_sync, "_selected_minute_provider_name", lambda: "stream")
    monkeypatch.setattr(kline_sync, "_resolve_minute_provider", lambda _: (provider, False, None))
    monkeypatch.setattr(kline_sync, "get_client", lambda: pytest.fail("不可跨源取数"))


def test_core_passes_full_universe_once_and_respects_provider_batch(monkeypatch):
    source = StreamSource()
    route(monkeypatch, source)
    symbols = [f"{i:06d}.SZ" for i in range(400)]
    frames, progress = [], []
    result = kline_sync.sync_minute_batch(
        symbols, start_time=datetime(2026, 11, 6), end_time=datetime(2026, 11, 6, 15),
        on_segment=frames.append, on_chunk_done=lambda *args: progress.append(args),
    )
    assert len(source.calls) == 1 and source.calls[0][0] == symbols
    assert [len(batch) for batch in source.requests] == [200, 200]
    assert [df.height for df in frames] == [200, 200]
    assert progress[-1] == (400, 400, "custom")
    assert result.is_empty() and source.closed


def test_late_source_failure_keeps_previous_frame_and_propagates(monkeypatch):
    source = StreamSource(size=2, fail=True)
    route(monkeypatch, source)
    frames = []
    with pytest.raises(ProviderRequestError, match="source request failed"):
        kline_sync.sync_minute_batch(["a", "b", "c"], on_segment=frames.append)
    assert len(frames) == 1 and frames[0]["symbol"].to_list() == ["a", "b"]
    assert source.closed


def test_write_failure_closes_source_without_fetching_remaining_frames(monkeypatch):
    source = StreamSource(size=1)
    route(monkeypatch, source)

    def persist(_):
        raise OSError("write failed")

    with pytest.raises(OSError, match="write failed"):
        kline_sync.sync_minute_batch(["a", "b"], on_segment=persist)
    assert len(source.requests) == 1 and source.closed


def test_progress_cancellation_propagates_and_closes_source(monkeypatch):
    source = StreamSource(size=1)
    route(monkeypatch, source)

    def cancel(*_):
        raise RuntimeError("cancel requested")

    with pytest.raises(RuntimeError, match="cancel requested"):
        kline_sync.sync_minute_batch(["a", "b"], on_segment=lambda _: None, on_chunk_done=cancel)
    assert source.closed and len(source.requests) <= 1


def test_managed_legacy_source_keeps_instance_until_stream_is_closed():
    calls, closed = [], []

    class Legacy:
        name = "legacy"
        config = SimpleNamespace(datasets={"minute": {"batch": 1}})

        def get_minute(self, symbols, **_kwargs):
            calls.append(list(symbols))
            return frame(symbols)

        def close(self):
            closed.append(self)

    old, new = Legacy(), Legacy()
    handle = ManagedProvider(old)
    stream = handle.iter_minute([str(i) for i in range(120)], start_time=None, end_time=None)
    handle.replace(new)
    assert not closed
    assert [part.height for part in stream] == [120]
    assert calls == [[str(i) for i in range(120)]]
    assert closed == [old]
    handle.close()
    assert closed == [old, new]


@pytest.mark.parametrize("result_type", ["none", "pandas"])
def test_legacy_stream_keeps_existing_empty_and_pandas_normalization(monkeypatch, result_type):
    source = SimpleNamespace(get_minute=lambda *_args, **_kwargs: None)
    expected = frame(["a"])
    if result_type == "pandas":
        source.get_minute = lambda *_args, **_kwargs: expected.to_pandas()
    route(monkeypatch, source)
    published = []
    kline_sync.sync_minute_batch(["a"], on_segment=published.append)
    if result_type == "none":
        assert published == []
    else:
        from polars.testing import assert_frame_equal

        assert len(published) == 1
        assert_frame_equal(published[0], expected)


def yaml_source(batch=200, rpm=None):
    columns = frame(["a"]).columns
    return GenericHTTPProvider(CustomSourceConfig(name="yaml", display_name="yaml", datasets={
        "minute": DatasetConfig(url="https://example.invalid", batch=batch, rpm=rpm,
                                field_map={name: name for name in columns}),
    }))


def test_yaml_stream_is_lazy_and_core_does_not_override_batch(monkeypatch):
    source = yaml_source()
    requests = []

    def fetch(_cfg, *, symbols, **_):
        requests.append(list(symbols))
        return frame(symbols).to_dicts()

    monkeypatch.setattr(source, "_request_rows", fetch)
    try:
        stream = source.iter_minute([str(i) for i in range(400)], None, None)
        assert not requests
        assert next(stream).height == 200 and len(requests) == 1
        stream.close()
        assert len(requests) == 1
        route(monkeypatch, source)
        requests.clear()
        out = []
        kline_sync.sync_minute_batch([str(i) for i in range(400)], on_segment=out.append)
        assert [len(part) for part in requests] == [200, 200]
        assert sum(part.height for part in out) == 400
    finally:
        source.close()


def test_yaml_request_limit_survives_separate_calls(monkeypatch):
    source = yaml_source(rpm=60)
    clock, waits = [100.0], []

    def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    monkeypatch.setattr("time.sleep", sleep)
    monkeypatch.setattr(source, "_request_rows", lambda *_args, **kwargs: [])
    try:
        source.get_minute(["a"], None, None)
        source.get_minute(["b"], None, None)
        assert sum(waits) == pytest.approx(1.0)
    finally:
        source.close()


def test_yaml_reload_defers_close_until_minute_stream_finishes(monkeypatch, tmp_path):
    from app.data_providers.custom import loader

    old, new = yaml_source(batch=1), yaml_source(batch=1)
    providers = iter([old, new])
    for source in (old, new):
        monkeypatch.setattr(source, "_request_rows", lambda *_args, **kwargs: frame(kwargs["symbols"]).to_dicts())
    monkeypatch.setattr(loader, "_PROVIDERS", {})
    monkeypatch.setattr(loader, "_LOAD_ERRORS", [])
    monkeypatch.setattr(loader, "_PLUGIN_STATUS", {})
    monkeypatch.setattr(loader, "_load_builtin_plugins", lambda: None)
    monkeypatch.setattr(loader, "load_config", lambda _: old.config)
    monkeypatch.setattr(loader, "GenericHTTPProvider", lambda _: next(providers))
    (tmp_path / "source.yaml").touch()
    try:
        loader.load_all(tmp_path)
        stream = loader.get_provider("yaml").iter_minute(["a", "b"], None, None)
        assert next(stream)["symbol"].to_list() == ["a"]
        loader.load_all(tmp_path)
        assert not old._client.is_closed
        assert loader.list_sources()[0]["name"] == "yaml"
        assert loader.get_config_dict("yaml")["datasets"]["minute"]["batch"] == 1
        assert [part["symbol"].to_list() for part in stream] == [["b"]]
        assert old._client.is_closed and not new._client.is_closed
    finally:
        old.close()
        new.close()


@pytest.mark.parametrize("bad_frame", [
    frame(["a"]).drop("volume"),
    frame(["a"]).drop("datetime"),
    frame(["a"]).with_columns(pl.lit(None, dtype=pl.Datetime("us")).alias("datetime")),
    frame(["a"]).with_columns(pl.lit(None, dtype=pl.String).alias("symbol")),
])
def test_invalid_stream_schema_is_rejected_before_publish(monkeypatch, bad_frame):
    source = StreamSource()
    closed, published = [], []

    def stream(*_args, **_kwargs):
        try:
            yield bad_frame
        finally:
            closed.append(True)

    monkeypatch.setattr(source, "iter_minute", stream)
    route(monkeypatch, source)
    with pytest.raises(ProviderRequestError, match="数据校验失败"):
        kline_sync.sync_minute_batch(["a"], on_segment=published.append)
    assert not published and closed == [True]


def test_stream_accepts_missing_amount_and_propagates_job_cancellation(monkeypatch):
    from app.services.pipeline_jobs import JobCancelledError

    source = StreamSource(size=1)
    route(monkeypatch, source)
    closed = []

    def stream(*_args, on_chunk_done, **_kwargs):
        try:
            yield frame(["a"]).drop("amount")
            on_chunk_done(1, 2)
            pytest.fail("取消后不可继续拉取")
        finally:
            closed.append(True)

    def cancel(*_args):
        raise JobCancelledError("cancelled-job")

    monkeypatch.setattr(source, "iter_minute", stream)
    published = []
    with pytest.raises(JobCancelledError):
        kline_sync.sync_minute_batch(["a", "b"], on_segment=published.append, on_chunk_done=cancel)
    assert published[0].columns == frame(["a"]).drop("amount").columns
    assert closed == [True]


def test_cancel_during_source_rate_wait_does_not_start_another_request(monkeypatch):
    from app.services.pipeline_jobs import JobCancelledError

    source = yaml_source(rpm=60)
    requests = []
    monkeypatch.setattr("time.monotonic", lambda: 100.0)
    monkeypatch.setattr(source, "_request_rows", lambda *_args, **kwargs: requests.append(kwargs["symbols"]) or [])

    def cancel(*_args):
        raise JobCancelledError("waiting-job")

    try:
        source.get_minute(["a"], None, None)
        stream = source.iter_minute(["b"], None, None, on_chunk_done=cancel)
        with pytest.raises(JobCancelledError):
            next(stream)
        assert requests == [["a"]]
    finally:
        source.close()


@pytest.mark.parametrize("asset_type", ["stock", "etf"])
def test_stream_failure_preserves_queryable_partitions_and_retry_is_idempotent(monkeypatch, tmp_path, asset_type):
    from polars.testing import assert_frame_equal

    from app.tickflow.capabilities import CapabilitySet
    from app.tickflow.repository import DataStore, KlineRepository

    repo = KlineRepository(DataStore(tmp_path))
    source = StreamSource(size=1, fail=True)
    route(monkeypatch, source)
    monkeypatch.setattr(kline_sync.preferences, "data_source_stamp", lambda: "same-source")
    monkeypatch.setattr(kline_sync.preferences, "get_minute_sync_segment_days", lambda: 20)
    other_table = "kline_minute" if asset_type == "etf" else "kline_etf_minute"
    kline_sync._write_minute_partition(frame(["other"]), tmp_path / other_table)
    other_file = next((tmp_path / other_table).rglob("*.parquet"))
    untouched = other_file.read_bytes()
    day = datetime(2026, 11, 6).date()
    capset = CapabilitySet()

    with pytest.raises(ProviderRequestError):
        kline_sync.sync_and_persist_minute(
            ["a", "b"], repo, capset, force_full_days=True, asset_type=asset_type,
        )
    assert_frame_equal(repo.get_minute("a", day, asset_type=asset_type), frame(["a"]))
    assert repo.get_minute("b", day, asset_type=asset_type).is_empty()
    source.fail = False
    kline_sync.sync_and_persist_minute(
        ["a", "b"], repo, capset, force_full_days=True, asset_type=asset_type,
    )
    for symbol in ("a", "b"):
        assert_frame_equal(repo.get_minute(symbol, day, asset_type=asset_type), frame([symbol]))
    assert other_file.read_bytes() == untouched
