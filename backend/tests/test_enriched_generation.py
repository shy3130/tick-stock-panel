from __future__ import annotations

import json
import os
import threading
import time
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.backtest.engine import BacktestEngine, PanelCache
from app.enriched_generation import (
    CLAIM_STALE_SECONDS,
    EnrichedGenerationUnavailableError,
    EnrichedPublication,
    enriched_publication_incomplete,
    get_enriched_generation,
    recover_stale_publications,
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


# ---------------------------------------------------------------------------
# 长任务与实时行情争抢发布代: 等待错开而不是整批失败
# ---------------------------------------------------------------------------

def test_long_task_waits_for_short_live_publication(tmp_path) -> None:
    """回归: 盘后/手动全量同步撞上实时行情的一轮发布 (几百毫秒) 时,
    修复前直接抛 "another enriched publication is active", 8 分钟任务白跑。"""
    out = tmp_path / "kline_daily_enriched" / "date=2026-08-14" / "part.parquet"
    live = EnrichedPublication(tmp_path, recover=True)
    live.begin()

    def release() -> None:
        time.sleep(0.4)
        live.write_parquet(_frame(10.0), out)
        live.commit()

    worker = threading.Thread(target=release, daemon=True)
    worker.start()

    long_task = EnrichedPublication(tmp_path, recover=True, claim_wait_s=10.0)
    started = time.monotonic()
    long_task.begin()
    waited = time.monotonic() - started
    long_task.write_parquet(_frame(20.0), out)
    long_task.commit()
    worker.join(timeout=5)

    assert waited >= 0.2, "应在锁外等待实时发布让出, 而不是立即失败"
    assert pl.read_parquet(out)["close"][0] == pytest.approx(20.0)


def test_claim_wait_times_out_with_original_error(tmp_path) -> None:
    """真有两个长任务时不能无限等: 超时仍抛同一错误。"""
    holder = EnrichedPublication(tmp_path, recover=True)
    holder.begin()
    try:
        waiter = EnrichedPublication(tmp_path, recover=True, claim_wait_s=0.3)
        with pytest.raises(EnrichedGenerationUnavailableError, match="another enriched publication is active"):
            waiter.begin()
    finally:
        holder.abandon()


def test_begin_without_write_releases_claim(tmp_path) -> None:
    """begin() 后一笔未写就 commit: 必须让出认领, 否则标记永久卡住。"""
    idle = EnrichedPublication(tmp_path, recover=True)
    idle.begin()
    assert idle.commit() is None

    # 后续发布应能正常认领 (修复前会一直报 active)
    follower = EnrichedPublication(tmp_path, recover=True, claim_wait_s=0.2)
    follower.begin()
    out = tmp_path / "kline_daily_enriched" / "date=2026-08-14" / "part.parquet"
    follower.write_parquet(_frame(), out)
    assert follower.commit() is not None


# ---------------------------------------------------------------------------
# 残留发布标记的自愈: 心跳超时接管 + 启动自检
# ---------------------------------------------------------------------------

def _write_stale_marker(tmp_path, *, owner_pid: int, age_seconds: float) -> str:
    generation = "base-generation"
    marker = {
        "state": "publishing",
        "generation": generation,
        "publication_id": "stale-publication",
        "owner_pid": owner_pid,
        "updated_at_ns": time.time_ns() - int(age_seconds * 1_000_000_000),
    }
    (tmp_path / ".matrix_generation_stock.json").write_text(
        json.dumps(marker), encoding="utf-8"
    )
    return generation


def test_abandon_without_writes_restores_ready(tmp_path) -> None:
    """发布过程中异常且尚未写任何分区: 必须回到 ready, 而不是把标记挂住。

    这是现场"停掉实时行情后仍然一直报正在发布"的主要来源 ——
    运行期唯一的频繁写入方停了, 就再没有人能接管 publishing 标记。
    """
    publication = EnrichedPublication(tmp_path, recover=True)
    publication.begin()
    marker_path = tmp_path / ".matrix_generation_stock.json"
    assert json.loads(marker_path.read_text())["state"] == "publishing"

    publication.abandon()

    marker = json.loads(marker_path.read_text())
    assert marker["state"] == "ready"
    assert marker["generation"]
    assert get_enriched_generation(tmp_path, "stock") == marker["generation"]
    assert publication._publishing is False


def test_abandon_after_partial_write_stays_fail_closed_until_stale(tmp_path) -> None:
    """已写入部分分区时数据可能半新半旧: 放弃不能直接放行读取。"""
    publication = EnrichedPublication(tmp_path, recover=True)
    out = tmp_path / "kline_daily_enriched" / "date=2026-08-14" / "part.parquet"
    publication.write_parquet(_frame(10.0), out)
    publication.abandon()

    assert publication._publishing is False
    marker = json.loads((tmp_path / ".matrix_generation_stock.json").read_text())
    assert marker["state"] == "publishing"
    with pytest.raises(EnrichedGenerationUnavailableError, match="being published"):
        get_enriched_generation(tmp_path, "stock")


def test_abandon_resets_state_even_if_lock_is_held(tmp_path, monkeypatch) -> None:
    """锁竞争拿不到时也必须复位内存态, 否则该对象永远自认"发布中"。"""
    publication = EnrichedPublication(tmp_path, recover=True)
    publication.begin()

    def always_busy(*_args, **_kwargs):
        raise EnrichedGenerationUnavailableError(
            "another enriched publication is active"
        )

    monkeypatch.setattr(
        "app.enriched_generation._exclusive_generation_lock", always_busy
    )
    publication.abandon()

    assert publication._publishing is False
    assert publication._changed is False


def test_stale_heartbeat_allows_takeover_even_if_owner_process_alive(
    tmp_path, monkeypatch
) -> None:
    """属主进程活着但很久没有心跳 (已停止发布): 应允许接管。

    现场: 后端进程没退出, 但发布对象已被丢弃; 停止实时行情后再无写入方,
    修复前读取会永久 fail-closed。
    """
    _write_stale_marker(
        tmp_path,
        owner_pid=os.getpid(),
        age_seconds=CLAIM_STALE_SECONDS + 60,
    )

    out = tmp_path / "kline_daily_enriched" / "date=2026-08-14" / "part.parquet"
    recovered = EnrichedPublication(tmp_path, recover=True, claim_wait_s=0.2)
    recovered.begin()
    recovered.write_parquet(_frame(10.0), out)
    generation = recovered.commit()

    marker = json.loads((tmp_path / ".matrix_generation_stock.json").read_text())
    assert marker["state"] == "ready"
    assert marker["generation"] == generation
    assert get_enriched_generation(tmp_path, "stock") == generation


def test_fresh_heartbeat_still_blocks_takeover(tmp_path) -> None:
    """心跳新鲜且属主进程存活: 发布仍在推进, 不得被抢占。

    长任务在两批写入之间可能计算十几分钟, 只靠时间戳会误判僵死; 因此只要
    属主进程存活且心跳未超时, 就保持 fail-closed。
    """
    owner_pid = os.getppid()  # 父进程存活, 且不是当前进程
    assert owner_pid > 0
    _write_stale_marker(tmp_path, owner_pid=owner_pid, age_seconds=5.0)

    out = tmp_path / "kline_daily_enriched" / "date=2026-08-14" / "part.parquet"
    with pytest.raises(
        EnrichedGenerationUnavailableError, match="another enriched publication is active"
    ):
        EnrichedPublication(tmp_path, recover=True, claim_wait_s=0.1).write_parquet(
            _frame(10.0), out
        )


def test_recover_stale_publications_restores_orphan_on_startup(tmp_path, monkeypatch) -> None:
    """启动自检: 上一个进程异常退出的 publishing 标记应直接恢复为 ready。"""
    _write_stale_marker(tmp_path, owner_pid=999_999, age_seconds=5.0)

    def probe(_pid: int, _sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr("app.enriched_generation.os.kill", probe)

    assert recover_stale_publications(tmp_path) == ["stock"]
    marker = json.loads((tmp_path / ".matrix_generation_stock.json").read_text())
    assert marker["state"] == "ready"
    assert enriched_publication_incomplete(tmp_path, "stock") is False


def test_recover_stale_publications_leaves_live_publication_alone(tmp_path) -> None:
    """启动自检不能打断另一个仍在运行的实例的发布。"""
    live = EnrichedPublication(tmp_path, recover=True)
    live.begin()
    try:
        assert recover_stale_publications(tmp_path) == []
        marker = json.loads((tmp_path / ".matrix_generation_stock.json").read_text())
        assert marker["state"] == "publishing"
    finally:
        live.abandon()


def test_heartbeat_renews_stale_marker(tmp_path) -> None:
    """长计算阶段调用 heartbeat: 标记时间戳刷新, 别的进程不再判为僵死。"""
    publication = EnrichedPublication(tmp_path, recover=True)
    publication.begin()
    path = tmp_path / ".matrix_generation_stock.json"
    before = json.loads(path.read_text())["updated_at_ns"]

    # 把时间戳改到很久以前, 模拟长时间计算未续期
    marker = json.loads(path.read_text())
    marker["updated_at_ns"] = time.time_ns() - int((CLAIM_STALE_SECONDS + 60) * 1e9)
    path.write_text(json.dumps(marker), encoding="utf-8")

    publication.heartbeat()

    after = json.loads(path.read_text())
    assert after["updated_at_ns"] > before
    assert after["state"] == "publishing"
    assert after["publication_id"] == publication._publication_id
    publication.abandon()


def test_heartbeat_is_noop_before_begin(tmp_path) -> None:
    publication = EnrichedPublication(tmp_path, recover=True)
    publication.heartbeat()
    assert not (tmp_path / ".matrix_generation_stock.json").exists()


def test_run_pipeline_releases_claim_when_compute_fails(tmp_path, monkeypatch) -> None:
    """管道在计算阶段抛异常: 不得留下任何发布认领。

    修复前 run_pipeline 没有任何 abandon 调用; 一旦在写过分区之后失败,
    publishing 标记会一直挂着, 后续所有读写持续报"数据正在发布"。
    这里断言失败后 generation 仍可读 (无标记或 ready)。
    """
    from app.indicators import pipeline as pipeline_module

    daily = tmp_path / "kline_daily" / "date=2026-08-14"
    daily.mkdir(parents=True)
    _frame().write_parquet(daily / "part.parquet")

    def boom(*_args, **_kwargs):
        raise RuntimeError("injected compute failure")

    monkeypatch.setattr(pipeline_module, "compute_enriched", boom)
    monkeypatch.setattr(pipeline_module, "_load_factors", lambda _p: pl.DataFrame())

    with pytest.raises(RuntimeError, match="injected"):
        pipeline_module.run_pipeline(data_dir=tmp_path, new_dates_only=True)

    assert enriched_publication_incomplete(tmp_path, "stock") is False
    assert get_enriched_generation(tmp_path, "stock")


def test_run_pipeline_abandons_claim_after_partial_write(tmp_path, monkeypatch) -> None:
    """已写入部分分区后失败: 保持 fail-closed, 但同一进程内可立即被接管。

    不能直接放行读取 (数据可能半新半旧), 也不能让对象永久自认"发布中" ——
    后者会让后续本进程的实时写入也报 active。
    """
    from app.indicators import pipeline as pipeline_module

    for ds in ("2026-08-13", "2026-08-14"):
        d = tmp_path / "kline_daily" / f"date={ds}"
        d.mkdir(parents=True)
        _frame().with_columns(pl.lit(date.fromisoformat(ds)).alias("date")).write_parquet(
            d / "part.parquet"
        )

    original_write = EnrichedPublication.write_parquet
    calls = {"n": 0}
    failing = {"on": True}

    def write_then_fail(self, df, out):
        calls["n"] += 1
        if failing["on"] and calls["n"] >= 2:
            raise RuntimeError("injected failure during write")
        return original_write(self, df, out)

    monkeypatch.setattr(EnrichedPublication, "write_parquet", write_then_fail)
    monkeypatch.setattr(pipeline_module, "_load_factors", lambda _p: pl.DataFrame())

    with pytest.raises(RuntimeError, match="injected"):
        pipeline_module.run_pipeline(data_dir=tmp_path, new_dates_only=True)

    failing["on"] = False
    assert calls["n"] >= 2, "应至少写入一个分区后才失败"
    marker = json.loads((tmp_path / ".matrix_generation_stock.json").read_text())
    assert marker["state"] == "publishing", "部分写入后必须保持 fail-closed"

    # 同一进程内的下一个写入方应能立即接管 (无需等心跳超时)
    out = tmp_path / "kline_daily_enriched" / "date=2026-08-14" / "part.parquet"
    follower = EnrichedPublication(tmp_path, recover=True, claim_wait_s=0.2)
    follower.write_parquet(_frame(10.0), out)
    assert follower.commit()
    assert get_enriched_generation(tmp_path, "stock") == json.loads(
        (tmp_path / ".matrix_generation_stock.json").read_text()
    )["generation"]
