"""strategy_run_queue 单测 — 耗时排序、耗时落盘、handle 状态、单飞管理器。"""
from __future__ import annotations

import threading
import time

from app.services import strategy_run_queue as q


def test_order_strategy_ids_fast_first_unknown_last():
    ids = ["slow_a", "mid_b", "fast_c", "new_d", "new_e"]
    timings = {"slow_a": 5000.0, "fast_c": 100.0, "mid_b": 1000.0}
    assert q.order_strategy_ids(ids, timings) == ["fast_c", "mid_b", "slow_a", "new_d", "new_e"]


def test_run_timings_record_merge_and_load(tmp_path):
    assert q.load_run_timings(tmp_path) == {}
    q.record_run_timings(tmp_path, {"a": 100.0})
    q.record_run_timings(tmp_path, {"b": 200.0, "a": 50.0})
    assert q.load_run_timings(tmp_path) == {"a": 50.0, "b": 200.0}


def test_run_timings_survives_corrupt_file(tmp_path):
    path = q._timings_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    assert q.load_run_timings(tmp_path) == {}
    q.record_run_timings(tmp_path, {"a": 1.0})
    assert q.load_run_timings(tmp_path) == {"a": 1.0}


def test_handle_snapshot_lifecycle():
    h = q.StrategyRunHandle(("k",), ["s1", "s2"])
    snap = h.snapshot()
    assert snap["pending"] == ["s1", "s2"]
    assert snap["done"] is False

    h.complete("s1", {"total": 3})
    snap = h.snapshot()
    assert snap["results"] == {"s1": {"total": 3}}
    assert snap["pending"] == ["s2"]

    h.fail("boom")
    snap = h.snapshot()
    assert snap["error"] == "boom"
    assert snap["done"] is True


def _wait_done(handle, timeout=5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if handle.snapshot()["done"]:
            return
        time.sleep(0.01)
    raise AssertionError("handle 未在时限内完成")


def test_manager_piggybacks_same_running_key():
    mgr = q.StrategyRunManager()
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def job(handle):
        calls.append(1)
        started.set()
        assert release.wait(timeout=5)

    h1 = mgr.get_or_submit(("k",), ["s"], job)
    assert started.wait(timeout=5)
    # 执行中: 相同 key 搭车, 不重复提交
    h2 = mgr.get_or_submit(("k",), ["s"], job)
    assert h2 is h1

    release.set()
    _wait_done(h1)
    assert calls == [1]

    # 已完成后: 同 key 再来 → 新执行 (重跑语义)
    h3 = mgr.get_or_submit(("k",), ["s"], job)
    assert h3 is not h1
    _wait_done(h3)
    assert calls == [1, 1]


def test_manager_piggybacks_same_queued_key():
    mgr = q.StrategyRunManager()
    started = threading.Event()
    release = threading.Event()

    def job(handle):
        started.set()
        assert release.wait(timeout=5)

    h1 = mgr.get_or_submit(("a",), ["s"], job)  # 占住唯一 worker
    assert started.wait(timeout=5)
    # key b 排队 (未开始), 此时 b 的重复请求应搭车排队中的 handle
    h2 = mgr.get_or_submit(("b",), ["s"], job)
    h3 = mgr.get_or_submit(("b",), ["s"], job)
    assert h2 is h3

    release.set()
    _wait_done(h1)
    _wait_done(h2, timeout=10)


def test_manager_serializes_different_keys():
    mgr = q.StrategyRunManager()
    lock = threading.Lock()
    active: list[str] = []
    overlap: list[list[str]] = []

    def make_job(name):
        def job(handle):
            with lock:
                active.append(name)
                if len(active) > 1:
                    overlap.append(list(active))
            time.sleep(0.1)
            with lock:
                active.remove(name)

        return job

    h1 = mgr.get_or_submit(("a",), ["s"], make_job("a"))
    h2 = mgr.get_or_submit(("b",), ["s"], make_job("b"))
    _wait_done(h1, timeout=10)
    _wait_done(h2, timeout=10)
    assert overlap == []


# ── 完成后保留 (TTL) + 重跑新执行 + 分钟摘要 ─────────────────────────

def test_done_handle_kept_for_polling_then_rerun_is_fresh():
    """完成后进暂存区供轮询; TTL 内重跑同 key 应是新执行 (旧行为不搭车)。"""
    mgr = q.StrategyRunManager()
    h1 = mgr.get_or_submit(("stock", "1d", "2026-03-02", ()), ["s1"], lambda handle: handle.complete("s1", {"total": 1}))
    _wait_done(h1)

    # 完成后仍可从 minute_summaries/暂存区读到 (key 非 1m 不导出摘要, 但 handle 在)
    h2 = mgr.get_or_submit(("stock", "1d", "2026-03-02", ()), ["s1"], lambda handle: handle.complete("s1", {"total": 2}))
    assert h2 is not h1, "已完成的重跑必须是新执行, 不能搭车旧结果"
    _wait_done(h2)


def test_minute_summaries_export_pending_and_results():
    mgr = q.StrategyRunManager()

    def job(handle):
        handle.complete("m1", {"total": 7, "as_of": "2026-03-02", "computed_at": 123})
        # m2 未完成 → pending

    h = mgr.get_or_submit(("stock", "1m", "2026-03-02", ("m1", "m2")), ["m1", "m2"], job)
    _wait_done(h, timeout=10)

    summaries = mgr.minute_summaries()
    minute = [s for s in summaries if s["as_of"] == "2026-03-02"]
    assert len(minute) == 1
    s = minute[0]
    assert s["results"]["m1"]["total"] == 7
    assert s["results"]["m1"]["computed_at"] == 123
    assert "m2" not in s["results"]
    assert s["complete"] is True  # job 正常返回 → done


def test_minute_summaries_only_exports_1m_handles():
    mgr = q.StrategyRunManager()
    mgr.get_or_submit(("stock", "1d", "2026-03-02", ()), [], lambda handle: None)
    assert mgr.minute_summaries() == []


def test_running_duplicate_still_rides_same_handle():
    """执行中的重复请求仍搭车同一 handle (回归: 暂存区不能破坏搭车)。"""
    mgr = q.StrategyRunManager()
    started = threading.Event()
    release = threading.Event()

    def job(handle):
        started.set()
        assert release.wait(timeout=5)

    h1 = mgr.get_or_submit(("stock", "1m", "2026-03-02", ()), ["s"], job)
    assert started.wait(timeout=5)
    h2 = mgr.get_or_submit(("stock", "1m", "2026-03-02", ()), ["s"], job)
    assert h2 is h1
    release.set()
    _wait_done(h1)
