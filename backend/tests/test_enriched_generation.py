from __future__ import annotations

import json
import os
import threading
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.backtest.engine import BacktestEngine, PanelCache
from app.enriched_generation import (
    EnrichedGenerationUnavailableError,
    EnrichedPublication,
    get_enriched_generation,
)
from app.tickflow.repository import DataStore, KlineRepository


def _frame(value: float = 10.0) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["000001.SZ"],
        "date": [date(2026, 8, 14)],
        "open": [value],
        "high": [value],
        "low": [value],
        "close": [value],
        "volume": [1_000.0],
    })


def test_repository_enriched_noop_does_not_bump_generation(tmp_path) -> None:
    repo = KlineRepository(DataStore(tmp_path))
    frame = _frame()

    repo.append_enriched(frame)
    first = repo.get_matrix_data_generation("stock")
    repo.append_enriched(frame)

    assert repo.get_matrix_data_generation("stock") == first


def test_commit_twice_is_idempotent_issue417(tmp_path) -> None:
    """Issue #417 回归: 有变化分区提交后再遇到无变化分区的空提交,
    必须幂等返回而非抛 ownership was lost (修复前第二次 commit 抛错)。"""
    publication = EnrichedPublication(tmp_path, recover=True)
    first = tmp_path / "kline_daily_enriched" / "date=2026-08-13" / "part.parquet"
    second = tmp_path / "kline_daily_enriched" / "date=2026-08-14" / "part.parquet"
    publication.write_parquet(_frame(10.0), first)
    assert publication.commit() is not None   # 分区1: 有变化, 正常发布
    assert publication.commit() is None       # 分区2: 无变化, 空提交幂等
    # 已提交过的发布对象继续写入仍可用 (write 重新认领)
    publication.write_parquet(_frame(11.0), second)
    assert publication.commit() is not None


def test_repository_changed_partition_before_unchanged_issue417(tmp_path) -> None:
    """Issue #417 回归: 一次写多个日期分区且「有变化在前、无变化在后」,
    修复前第二个 (无变化) 分区的空提交必然抛 ownership was lost, 整批失败。"""
    repo = KlineRepository(DataStore(tmp_path))
    d1, d2 = date(2026, 8, 13), date(2026, 8, 14)
    base = {"symbol": ["000001.SZ"] * 2, "date": [d1, d2]}
    repo.append_enriched(pl.DataFrame({
        **base,
        "open": [10.0, 10.0], "high": [10.0, 10.0], "low": [10.0, 10.0],
        "close": [10.0, 10.0], "volume": [1_000.0, 1_000.0],
    }))
    first = repo.get_matrix_data_generation("stock")
    # 第二批: d1 有变化, d2 与库中内容一致 (无变化分区排在其后)
    repo.append_enriched(pl.DataFrame({
        **base,
        "open": [10.5, 10.0], "high": [10.5, 10.0], "low": [10.5, 10.0],
        "close": [10.5, 10.0], "volume": [1_000.0, 1_000.0],
    }))
    assert repo.get_matrix_data_generation("stock") != first
    assert repo.get_matrix_data_generation("stock") != first  # 幂等不抛


def test_failed_multi_partition_publication_remains_fail_closed(
    tmp_path,
    monkeypatch,
) -> None:
    publication = EnrichedPublication(tmp_path, recover=True)
    first = tmp_path / "kline_daily_enriched" / "date=2026-08-13" / "part.parquet"
    second = tmp_path / "kline_daily_enriched" / "date=2026-08-14" / "part.parquet"
    publication.write_parquet(_frame(10.0), first)

    original_write = pl.DataFrame.write_parquet

    def fail_second(self, path, *args, **kwargs):
        if "2026-08-14" in str(path):
            raise OSError("injected write failure")
        return original_write(self, path, *args, **kwargs)

    monkeypatch.setattr(pl.DataFrame, "write_parquet", fail_second)
    with pytest.raises(OSError, match="injected"):
        publication.write_parquet(_frame(11.0), second)

    marker = json.loads(
        (tmp_path / ".matrix_generation_stock.json").read_text(encoding="utf-8")
    )
    assert marker["state"] == "publishing"
    assert first.is_file()
    assert not second.is_file()
    with pytest.raises(EnrichedGenerationUnavailableError, match="being published"):
        get_enriched_generation(tmp_path, "stock")


def test_recovery_replaces_stale_publication_but_not_active_owner(tmp_path) -> None:
    first = EnrichedPublication(tmp_path, recover=True)
    out = tmp_path / "kline_daily_enriched" / "date=2026-08-14" / "part.parquet"
    first.write_parquet(_frame(10.0), out)

    with pytest.raises(EnrichedGenerationUnavailableError, match="active"):
        EnrichedPublication(tmp_path, recover=True).write_parquet(_frame(11.0), out)

    del first
    recovered = EnrichedPublication(tmp_path, recover=True)
    recovered.write_parquet(_frame(12.0), out)
    recovered_generation = recovered.commit()

    assert recovered_generation == get_enriched_generation(tmp_path, "stock")
    assert pl.read_parquet(out)["close"].item() == pytest.approx(12.0)


def test_recovery_takes_over_when_owner_pid_is_dead_on_windows(tmp_path, monkeypatch) -> None:
    # 跨进程孤儿锁: 属主进程已死, 但 Windows 的 os.kill(pid, 0) 对不存在的 pid
    # 抛 WinError 87 (ERROR_INVALID_PARAMETER) 而非 ProcessLookupError,
    # 存活探测若把它当"存活", recover 将永远报 another publication is active。
    stale = {
        "state": "publishing",
        "generation": "stale-generation",
        "publication_id": "stale-publication",
        "owner_pid": 12345,
        "updated_at_ns": 0,
    }
    (tmp_path / ".matrix_generation_stock.json").write_text(
        json.dumps(stale), encoding="utf-8"
    )

    def probe(_pid: int, _sig: int) -> None:
        error = OSError()
        error.winerror = 87
        raise error

    monkeypatch.setattr("app.enriched_generation.os.kill", probe)

    out = tmp_path / "kline_daily_enriched" / "date=2026-08-14" / "part.parquet"
    recovered = EnrichedPublication(tmp_path, recover=True)
    recovered.write_parquet(_frame(10.0), out)
    recovered.commit()

    marker = json.loads(
        (tmp_path / ".matrix_generation_stock.json").read_text(encoding="utf-8")
    )
    assert marker["state"] == "ready"
    assert get_enriched_generation(tmp_path, "stock") == marker["generation"]
    assert pl.read_parquet(out)["close"].item() == pytest.approx(10.0)


def test_panel_cache_generation_change_forces_recompute() -> None:
    cache = PanelCache()
    calls: list[int] = []
    args = (["000001.SZ"], date(2026, 8, 13), date(2026, 8, 14), None)

    def compute(*_args):
        calls.append(len(calls) + 1)
        return pl.DataFrame({"value": [calls[-1]]})

    first = cache.get_or_compute(*args, compute, "stock", "generation-a")
    second = cache.get_or_compute(*args, compute, "stock", "generation-b")

    assert first["value"].item() == 1
    assert second["value"].item() == 2
    assert cache.stats()["compute_count"] == 2


def test_panel_reader_retries_when_generation_changes_during_scan(tmp_path) -> None:
    generations = iter(["generation-a", "generation-b", "generation-b", "generation-b"])
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_matrix_data_generation=lambda _asset_type: next(generations),
    )
    engine = BacktestEngine(repo)
    calls: list[str] = []

    def load(*_args):
        calls.append("scan")
        return _frame(float(len(calls)))

    engine._load_panel_inner = load
    panel = engine.load_panel(
        None,
        date(2026, 8, 14),
        date(2026, 8, 14),
        columns=["symbol", "date", "close"],
    )

    assert calls == ["scan", "scan"]
    assert panel["close"].item() == pytest.approx(2.0)


def test_matrix_reader_retries_when_generation_changes_during_build(
    tmp_path,
    monkeypatch,
) -> None:
    generations = iter(["generation-a", "generation-b", "generation-b", "generation-b"])
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_matrix_data_generation=lambda _asset_type: next(generations),
        get_instruments_asset=lambda _asset_type: pl.DataFrame(),
    )
    engine = BacktestEngine(repo)
    calls: list[str | None] = []
    expected = SimpleNamespace(
        execution_backend="matrix_native",
        base_columns={"open", "high", "low", "close", "volume"},
        instrument_columns=set(),
        matrix_columns=set(),
    )
    market = SimpleNamespace()

    def load_matrix(*_args, source_generation=None, **_kwargs):
        calls.append(source_generation)
        return market

    monkeypatch.setattr("app.backtest.engine.load_market_data_matrix_from_parquet", load_matrix)

    result = engine.load_market_data_matrix_for_backtest(
        None,
        date(2026, 8, 14),
        date(2026, 8, 14),
        expected,
    )

    assert result is market
    assert calls == ["generation-a", "generation-b"]


def test_live_flush_write_recovers_stale_marker_from_dead_process(
    tmp_path, monkeypatch
) -> None:
    """实时 enriched 落盘(repository 路径)遇到僵死 publishing 标记应接管自愈,
    而非持续抛错直到下一次盘后管道。"""
    from app.tickflow.repository import DataStore, KlineRepository

    (tmp_path / ".matrix_generation_stock.json").write_text(
        json.dumps({
            "state": "publishing",
            "generation": "stale-generation",
            "publication_id": "stale-publication",
            "owner_pid": 999999999,
            "updated_at_ns": 0,
        }),
        encoding="utf-8",
    )

    repo = KlineRepository(DataStore(tmp_path))
    repo.append_enriched(_frame(10.0))

    marker = json.loads(
        (tmp_path / ".matrix_generation_stock.json").read_text(encoding="utf-8")
    )
    assert marker["state"] == "ready"


def _stale_publishing_marker(tmp_path, owner_pid: int = 999999999) -> None:
    (tmp_path / ".matrix_generation_stock.json").write_text(
        json.dumps({
            "state": "publishing",
            "generation": "stale-generation",
            "publication_id": "stale-publication",
            "owner_pid": owner_pid,
            "updated_at_ns": 0,
        }),
        encoding="utf-8",
    )


def test_reader_self_heals_stale_publishing_marker_from_dead_owner(tmp_path) -> None:
    """读取方遇到属主已死的 publishing 标记应就地恢复 ready, 而非持续失败
    直到某个写入方碰巧接管 (dev 热重载杀掉发布进程即产生这种孤儿)。"""
    _stale_publishing_marker(tmp_path)

    generation = get_enriched_generation(tmp_path, "stock")

    marker = json.loads(
        (tmp_path / ".matrix_generation_stock.json").read_text(encoding="utf-8")
    )
    assert marker["state"] == "ready"
    # 恢复时换新 generation: 磁盘可能残留部分替换的文件, 按代缓存需要失效。
    assert generation not in ("", "stale-generation")
    assert generation == marker["generation"]
    assert get_enriched_generation(tmp_path, "stock") == generation


def test_reader_still_fails_closed_while_owner_is_alive(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("app.enriched_generation._process_is_alive", lambda pid: True)
    _stale_publishing_marker(tmp_path, owner_pid=os.getpid() + 1)

    with pytest.raises(EnrichedGenerationUnavailableError, match="being published"):
        get_enriched_generation(tmp_path, "stock")
    marker = json.loads(
        (tmp_path / ".matrix_generation_stock.json").read_text(encoding="utf-8")
    )
    assert marker["state"] == "publishing"  # 标记未被读取方改动


def test_reader_respects_active_in_process_publication(tmp_path) -> None:
    publication = EnrichedPublication(tmp_path, recover=True)
    publication.begin()
    try:
        with pytest.raises(EnrichedGenerationUnavailableError, match="being published"):
            get_enriched_generation(tmp_path, "stock")
    finally:
        publication.abandon()

    assert _is_ready_marker(tmp_path)


def _is_ready_marker(tmp_path) -> bool:
    marker = json.loads(
        (tmp_path / ".matrix_generation_stock.json").read_text(encoding="utf-8")
    )
    return marker["state"] == "ready"


def test_data_generation_await_retries_during_publication(tmp_path, monkeypatch) -> None:
    engine = BacktestEngine(KlineRepository(DataStore(tmp_path)))
    calls = {"n": 0}

    def flaky(asset_type: str = "stock"):
        calls["n"] += 1
        if calls["n"] < 3:
            raise EnrichedGenerationUnavailableError(
                "enriched data is being published; retry after the update finishes"
            )
        return "gen-final"

    monkeypatch.setattr(engine, "data_generation", flaky)
    monkeypatch.setattr("app.backtest.engine._GENERATION_POLL_S", 0.001)

    assert engine.data_generation_await("stock") == "gen-final"
    assert calls["n"] == 3


def test_data_generation_await_times_out_and_respects_cancel(
    tmp_path, monkeypatch
) -> None:
    engine = BacktestEngine(KlineRepository(DataStore(tmp_path)))
    monkeypatch.setattr("app.backtest.engine._GENERATION_POLL_S", 0.001)

    def always_publishing(asset_type: str = "stock"):
        raise EnrichedGenerationUnavailableError(
            "enriched data is being published; retry after the update finishes"
        )

    monkeypatch.setattr(engine, "data_generation", always_publishing)

    with pytest.raises(EnrichedGenerationUnavailableError):
        engine.data_generation_await("stock", timeout_s=0.01)

    cancel = threading.Event()
    cancel.set()
    with pytest.raises(EnrichedGenerationUnavailableError):
        engine.data_generation_await("stock", cancel_event=cancel, timeout_s=30.0)


def test_worker_error_message_translates_publishing_error() -> None:
    from app.backtest.worker import _error_message
    from app.enriched_generation import EnrichedGenerationUnavailableError

    translated = _error_message(
        EnrichedGenerationUnavailableError(
            "enriched data is being published; retry after the update finishes"
        )
    )
    assert translated == "指标数据正在发布更新，请稍后重试"
    assert _error_message(ValueError("boom")) == "boom"
