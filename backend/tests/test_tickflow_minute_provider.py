"""TickFlow 历史分钟的请求分批/分段由数据源控制。"""
from __future__ import annotations

from datetime import datetime, timedelta

import polars as pl
import pytest

from app.data_providers import tickflow_provider as module
from app.data_providers.base import ProviderRequestError
from app.market_time import CN_TZ


class SDK:
    def __init__(self, fail_call=None):
        self.klines = self
        self.calls = []
        self.fail_call = fail_call

    def batch(self, symbols, **kwargs):
        self.calls.append((list(symbols), kwargs))
        if len(self.calls) == self.fail_call:
            raise TimeoutError("upstream failed")
        at = datetime(2026, 11, 6, 9, 31, tzinfo=CN_TZ)
        if kwargs.get("start_time"):
            first = datetime.fromtimestamp(kwargs["start_time"] / 1000, tz=CN_TZ)
            at = first.replace(hour=9, minute=31)
            if at < first:
                at += timedelta(days=1)
        return {symbol: {"timestamp": [int(at.timestamp() * 1000)], "open": [10.0],
                         "high": [10.2], "low": [9.9], "close": [10.1],
                         "volume": [123.0], "amount": [124230.0]} for symbol in symbols}


def provider(sdk, **kwargs):
    return module.TickFlowProvider(minute_client=sdk, minute_options=module.TickFlowMinuteOptions(**kwargs))


def test_tickflow_owns_request_batches_and_emits_bounded_frames():
    sdk = SDK()
    source = provider(sdk, batch_size=200, raw_basis=True)
    symbols = [str(i) for i in range(400)]
    iterator = source.iter_minute(symbols, datetime(2026, 11, 6), datetime(2026, 11, 6, 15))
    assert not sdk.calls
    first = next(iterator)
    assert first.height == 200 and len(sdk.calls) == 1
    rest = list(iterator)
    assert [len(call[0]) for call in sdk.calls] == [200, 200]
    df = pl.concat([first, *rest])
    assert df.height == 400
    assert df["datetime"][0] == datetime(2026, 11, 6, 9, 31)
    assert df["volume"][0] == 123.0 and df["amount"][0] == 124230.0
    assert all(call[1]["adjust"] == "none" for call in sdk.calls)


def test_tickflow_preserves_legacy_adjustment_and_time_segmentation():
    sdk = SDK()
    source = provider(sdk, batch_size=1, segment_trading_days=5, raw_basis=False)
    frames = list(source.iter_minute(["a"], datetime(2026, 10, 1), datetime(2026, 11, 6)))
    assert len(frames) == len(sdk.calls) > 1
    for _, kwargs in sdk.calls:
        assert kwargs["adjust"] == "forward" and kwargs["count"] == 10000
        assert kwargs["end_time"] - kwargs["start_time"] <= 7 * 86400000
    for (_, a), (_, b) in zip(sdk.calls, sdk.calls[1:], strict=False):
        assert a["end_time"] == b["start_time"]


def test_tickflow_failure_keeps_yielded_frames_and_marks_incomplete():
    sdk = SDK(fail_call=2)
    progress = []
    iterator = provider(sdk, batch_size=1).iter_minute(
        ["a", "b"], datetime(2026, 11, 6), datetime(2026, 11, 6, 15),
        on_chunk_done=lambda *args: progress.append(args),
    )
    assert next(iterator)["symbol"].to_list() == ["a"]
    with pytest.raises(ProviderRequestError, match="未完成"):
        list(iterator)
    assert progress[-1] == (2, 2)


def test_tickflow_progress_cancellation_stops_new_requests():
    sdk = SDK()

    def cancel(*_):
        raise RuntimeError("cancel")

    stream = provider(sdk, batch_size=1).iter_minute(["a", "b"], None, None, on_chunk_done=cancel)
    next(stream)
    with pytest.raises(RuntimeError, match="cancel"):
        next(stream)
    assert len(sdk.calls) == 1


def test_tickflow_collecting_api_reuses_stream(monkeypatch):
    source = provider(SDK())
    calls = []
    def stream(symbols, *args, **kwargs):
        calls.append(symbols)
        yield pl.DataFrame({"symbol": symbols})
    monkeypatch.setattr(source, "iter_minute", stream)
    assert source.get_minute(["a"], None, None)["symbol"].to_list() == ["a"]
    assert calls == [["a"]]
