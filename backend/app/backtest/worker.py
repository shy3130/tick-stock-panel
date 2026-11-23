"""Spawn-isolated strategy backtest and optimizer task runner."""
from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import queue
import threading
import time
import traceback
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

import psutil

from app.services.engine_jobs import ENGINE_NODE_PROCESS_NAME

logger = logging.getLogger(__name__)


class BacktestWorkerError(RuntimeError):
    """Raised when a spawned worker fails before returning a task result."""

    def __init__(self, message: str, *, code: str = "WORKER_INTERRUPTED") -> None:
        super().__init__(message)
        self.code = code


_CANCEL_GRACE_SECONDS = 5.0


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


# worker RSS 上限 (issue #516): 超限 terminate 并返回明确错误, 而不是把整机
# 拖进 swap/内存节流假死。默认 6GiB; 0 = 不限制。刻意不用 RLIMIT_AS ——
# polars/numpy 的虚拟内存映射远大于实际 RSS, AS 上限会大量误杀。
_RSS_CHECK_INTERVAL_S = 2.0


def _effective_rss_limit_bytes() -> int:
    """本次任务生效的 worker RSS 上限 (字节)。

    优先级: 环境变量 TICK_WORKER_RSS_LIMIT_BYTES 显式设置时最高 (部署级覆盖,
    含 0=不限制, 非法值回退内置默认); 否则用设置页偏好 (GiB, 0=不限制)。
    每任务解析一次而非模块导入时求值 —— 设置页保存后对下一个回测/优化任务
    即生效, 无需重启; 且运行中任务的上限保持确定, 不受中途改设置影响。
    """
    raw = os.environ.get("TICK_WORKER_RSS_LIMIT_BYTES", "").strip()
    if raw:
        return _env_int("TICK_WORKER_RSS_LIMIT_BYTES", 6 * 1024**3)
    # 函数内导入: spawn 子进程会重导入本模块, 保持模块级 import 轻量
    from app.services import preferences

    return preferences.get_backtest_worker_rss_limit_gib() * 1024**3


def _process_rss_bytes(pid: int | None) -> int:
    if pid is None:
        return 0
    try:
        return psutil.Process(pid).memory_info().rss
    except psutil.Error:
        return 0


def _native_tree_rss(pid):
    """Include the data source process in the research node's existing RSS budget."""
    total = _process_rss_bytes(pid)
    if total:
        try:
            for child in psutil.Process(pid).children(recursive=True):
                with suppress(psutil.Error):
                    total += child.memory_info().rss
        except psutil.Error:
            pass
    return total


def _terminate_task(process, *, native_data):
    descendants = []
    if native_data and process.pid:
        with suppress(psutil.Error):
            descendants = psutil.Process(process.pid).children(recursive=True)
    process.terminate()
    if descendants:
        # Give a native worker monitoring its owner time to remove its private SDK settings.
        _, remaining = psutil.wait_procs(descendants, timeout=0.6)
        for child in reversed(remaining):
            with suppress(psutil.Error):
                child.kill()


class _PeakRssSampler:
    """Track whole-task and resettable phase RSS peaks with one sampling thread."""

    def __init__(self, interval_seconds: float = 0.05, *, native_data: bool = False) -> None:
        if interval_seconds <= 0:
            raise ValueError("RSS sample interval must be positive")
        self._process = psutil.Process(os.getpid())
        self._native_data = native_data
        self._interval_seconds = float(interval_seconds)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self._lock = threading.Lock()
        self._started = False
        current = self._current_rss()
        self.peak_rss_bytes = current
        self._phase_peak_rss_bytes = current

    def _current_rss(self):
        return _native_tree_rss(self._process.pid) if self._native_data else int(self._process.memory_info().rss)

    def start(self) -> None:
        if self._started:
            raise RuntimeError("RSS sampler has already started")
        self._started = True
        self._thread.start()

    def stop(self) -> int:
        if self._started:
            self._stop.set()
            self._thread.join(timeout=1.0)
        self._record_current()
        return self.peak_rss_bytes

    def reset_phase(self) -> None:
        current = self._current_rss()
        with self._lock:
            self._phase_peak_rss_bytes = current

    def phase_peak_rss_bytes(self) -> int:
        self._record_current()
        with self._lock:
            return self._phase_peak_rss_bytes

    def _record_current(self) -> None:
        current = self._current_rss()
        with self._lock:
            self.peak_rss_bytes = max(self.peak_rss_bytes, current)
            self._phase_peak_rss_bytes = max(self._phase_peak_rss_bytes, current)

    def _sample(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            self._record_current()


def _rss_bytes() -> int:
    return int(psutil.Process(os.getpid()).memory_info().rss)


def _strategy_dirs(data_dir: Path) -> list[Path]:
    app_dir = Path(__file__).resolve().parents[1]
    return [
        app_dir / "strategy" / "research",
        data_dir / "strategies" / "custom",
        data_dir / "strategies" / "ai",
        data_dir / "strategies" / "composite",
    ]


def _decode_backtest_config(payload: dict[str, Any]):
    from app.backtest.strategy import StrategyBacktestConfig

    values = dict(payload)
    values["start"] = date.fromisoformat(values["start"])
    values["end"] = date.fromisoformat(values["end"])
    if values.get("snapshot") is not None:
        from app.services.engine_jobs.daily_snapshot import DailySnapshotRef

        values["snapshot"] = DailySnapshotRef.model_validate(values["snapshot"])
    if values.get("entry_filters") is not None:
        from app.backtest.entry_inputs import EntryFilters

        values["entry_filters"] = EntryFilters.model_validate(values["entry_filters"])
    return StrategyBacktestConfig(**values)


def _decode_robust_config(payload: dict[str, Any]):
    from app.backtest.robust_optimizer import RobustOptimizeConfig

    values = dict(payload)
    values["start"] = date.fromisoformat(values["start"])
    values["end"] = date.fromisoformat(values["end"])
    return RobustOptimizeConfig(**values)


def _decode_walkforward_config(payload: dict[str, Any]):
    from app.backtest.walkforward import WalkForwardConfig

    values = dict(payload)
    values["start"] = date.fromisoformat(values["start"])
    values["end"] = date.fromisoformat(values["end"])
    return WalkForwardConfig(**values)


def encode_backtest_config(config) -> dict[str, Any]:
    payload = asdict(config)
    payload["start"] = config.start.isoformat()
    payload["end"] = config.end.isoformat()
    snapshot = payload.pop("snapshot", None)
    if snapshot is not None:
        payload["snapshot"] = snapshot.model_dump()
    filters = payload.pop("entry_filters", None)
    if filters is not None:
        payload["entry_filters"] = filters.model_dump()
    if payload.get("filter_data_version") is None:
        payload.pop("filter_data_version", None)
    return payload


def make_worker_task(kind: str, data_dir: Path, config) -> dict[str, Any]:
    if kind == "backtest":
        encoded = encode_backtest_config(config)
    elif kind in ("walkforward", "robust_optimize"):
        encoded = asdict(config)
        encoded["start"] = config.start.isoformat()
        encoded["end"] = config.end.isoformat()
    elif kind == "mining":
        if not isinstance(config, dict):
            raise TypeError("mining worker config must be a dict")
        encoded = dict(config)
    elif kind == "guardian":
        if not isinstance(config, dict):
            raise TypeError("guardian worker config must be a dict")
        encoded = dict(config)
    else:
        raise ValueError(f"unsupported worker task kind: {kind}")
    return {
        "kind": kind,
        "data_dir": str(data_dir.resolve()),
        "config": encoded,
    }


def _attach_worker_metrics(
    kind: str,
    result: dict[str, Any],
    metrics: dict[str, Any],
) -> None:
    if kind == "backtest":
        result.setdefault("stats", {})["worker"] = metrics
    else:
        result["worker"] = metrics


def _error_message(exc: BaseException) -> str:
    """任务级错误文案: enriched 发布类失败对用户是"稍后再试", 不透出原始异常。"""
    from app.enriched_generation import EnrichedGenerationUnavailableError

    if isinstance(exc, EnrichedGenerationUnavailableError):
        return "指标数据正在发布更新，请稍后重试"
    return str(exc)


def _worker_entry(task: dict[str, Any], event_queue, cancel_event) -> None:
    if task.get("kind") == "engine_node":
        # Locked engine work must not initialize the legacy repository or user registries.
        from app.services.engine_jobs.worker import worker_entry

        worker_entry(task, event_queue, cancel_event)
        return
    sampler = _PeakRssSampler()
    sampler.start()
    started = time.perf_counter()
    store = None
    try:
        from app.backtest.engine import BacktestEngine
        from app.backtest.strategy import StrategyBacktestService
        from app.strategy import config as strategy_config
        from app.strategy.engine import StrategyEngine
        from app.tickflow.repository import DataStore, KlineRepository

        data_dir = Path(task["data_dir"])
        store = DataStore(data_dir, defer_database=task["kind"] == "backtest")
        repo = KlineRepository(store)
        # 子进程不继承主进程的因子注册表; 自定义/复合因子 (uf_/cf_) 在任何
        # 涉及因子物化的 worker 任务里都依赖注册表, 启动时从存储加载。
        # 单个加载失败只跳过 (fail-open 跳过该因子), 与主进程启动行为一致。
        from app.factors.store import load_runtime_registry

        load_runtime_registry(data_dir)
        strategy_engine = StrategyEngine(
            strategy_dirs=_strategy_dirs(data_dir),
            override_loader=lambda sid: strategy_config.load_override(data_dir, sid),
        )
        service = StrategyBacktestService(BacktestEngine(repo), strategy_engine)

        def _progress(message: dict) -> None:
            event_queue.put({"type": "progress", "payload": message})

        kind = task["kind"]
        if kind == "backtest":
            config = _decode_backtest_config(task["config"])
            if config.snapshot is not None:
                from app.backtest.snapshot import SnapshotDailySource

                try:
                    snapshot = SnapshotDailySource.open(data_dir, config.snapshot)
                except ValueError as exc:
                    event_queue.put({"type": "error", "code": "SNAPSHOT_UNAVAILABLE", "message": str(exc)})
                    return
                service = StrategyBacktestService(BacktestEngine(repo, snapshot=snapshot), strategy_engine)
            result = asdict(service.run_with_evidence(config, _progress, cancel_event))
        elif kind == "robust_optimize":
            from app.backtest.robust_optimizer import RobustOptimizeService

            config = _decode_robust_config(task["config"])
            robust = RobustOptimizeService(service, strategy_engine)
            result = robust.robust_optimize(config, _progress, cancel_event)
        elif kind == "walkforward":
            from app.backtest.optimizer import StrategyOptimizer
            from app.backtest.walkforward import WalkForwardService

            config = _decode_walkforward_config(task["config"])
            optimizer = StrategyOptimizer(service, strategy_engine)
            walkforward = WalkForwardService(optimizer, service, strategy_engine)
            result = walkforward.run(config, _progress, cancel_event)
        elif kind == "mining":
            from app.backtest.mining_runtime import run_mining_runtime

            result = run_mining_runtime(
                task["config"],
                data_dir=data_dir,
                service=service,
                strategy_engine=strategy_engine,
                progress_cb=_progress,
                cancel_check=cancel_event,
                rss_sampler=sampler,
            )
        elif kind == "guardian":
            from app.backtest.guardian_runtime import run_guardian_runtime

            payload = dict(task["config"])
            result = run_guardian_runtime(
                {**payload.get("request", {}), "run_id": payload.get("run_id")},
                data_dir=data_dir,
                service=service,
                strategy_engine=strategy_engine,
                progress_cb=_progress,
                cancel_check=cancel_event,
            )
        else:
            raise ValueError(f"unsupported worker task kind: {kind}")

        serialization_started = time.perf_counter()
        serialized_bytes = len(
            json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")
        )
        serialization_ms = round(
            (time.perf_counter() - serialization_started) * 1000,
            1,
        )
        peak_rss = sampler.stop()
        metrics = {
            "pid": os.getpid(),
            "peak_rss_bytes": peak_rss,
            "final_rss_bytes": _rss_bytes(),
            "serialization_ms": serialization_ms,
            "serialized_result_bytes": serialized_bytes,
            "task_elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        _attach_worker_metrics(kind, result, metrics)
        event_queue.put({"type": "result", "payload": result})
    except BaseException as exc:
        with suppress(Exception):
            sampler.stop()
        event_queue.put({
            "type": "error",
            "message": _error_message(exc),
            "traceback": traceback.format_exc(),
        })
    finally:
        if store is not None:
            with suppress(Exception):
                store.close()
        # 终态消息已入队: 显式冲刷队列后立即退出。put 只是入队, 实际写管道的
        # 是后台 feeder 线程, close+join_thread 保证消息完整落管 (否则父进程误判
        # "exited without result"); 大数据量任务再跳过解释器 teardown (GC、DuckDB
        # 线程 join、DLL 卸载), 否则收尾可达数十秒, 撞上父进程 10s 退出预算。
        with suppress(Exception):
            event_queue.close()
            event_queue.join_thread()
        os._exit(0)


def run_worker_task(
    task: dict[str, Any],
    progress_cb: Callable[[dict], None] | None = None,
    cancel_event: threading.Event | None = None,
    *,
    rss_limit_bytes: int | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Run one complete task in a spawned process and wait for deterministic exit."""
    if rss_limit_bytes is None:
        rss_limit_bytes = _effective_rss_limit_bytes()
    elif type(rss_limit_bytes) is not int or rss_limit_bytes < 0:
        raise ValueError("invalid worker RSS limit")
    if timeout_seconds is not None and (type(timeout_seconds) is not int or timeout_seconds <= 0):
        raise ValueError("invalid worker time limit")
    context = mp.get_context("spawn")
    events = context.Queue()
    process_cancel = context.Event()
    process = context.Process(
        name=ENGINE_NODE_PROCESS_NAME if task.get("kind") == "engine_node" else None,
        target=_worker_entry,
        args=(task, events, process_cancel),
        daemon=False,
    )
    native_data = task.get("native_data", False) and task["kind"] == "engine_node"
    sample_rss = _native_tree_rss if native_data else _process_rss_bytes
    parent_rss_before = _rss_bytes()
    try:
        process.start()
    except BaseException:
        events.close()
        events.join_thread()
        raise
    result: dict[str, Any] | None = None
    failure: dict[str, Any] | None = None
    ipc_started = time.perf_counter()
    cancel_started: float | None = None
    last_rss_check = 0.0
    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None

    try:
        while result is None and failure is None:
            # 每轮只取一次时钟 (取消宽限与 RSS 检查共用), 测试用脚本时钟锁定调用量
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                raise BacktestWorkerError("研究任务超过锁定的执行时限", code="RESOURCE_LIMIT")
            if cancel_event is not None and cancel_event.is_set():
                process_cancel.set()
                if cancel_started is None:
                    cancel_started = now
                elif now - cancel_started >= _CANCEL_GRACE_SECONDS:
                    _terminate_task(process, native_data=native_data)
                    process.join(timeout=5.0)
                    raise BacktestWorkerError(
                        "backtest worker did not stop within 5 seconds after cancellation"
                    )
            if (
                rss_limit_bytes > 0
                and now - last_rss_check >= _RSS_CHECK_INTERVAL_S
            ):
                last_rss_check = now
                # getattr: 测试替身进程可能没有 pid 属性, 缺失时按 0 处理 (不触发)
                rss = sample_rss(getattr(process, "pid", None))
                if rss > rss_limit_bytes:
                    logger.error(
                        "backtest worker rss %.2f GiB exceeds limit %.2f GiB, terminating",
                        rss / 1024**3, rss_limit_bytes / 1024**3,
                    )
                    _terminate_task(process, native_data=native_data)
                    process.join(timeout=5.0)
                    raise BacktestWorkerError(
                        f"回测任务内存超过上限 {rss_limit_bytes / 1024**3:.1f}GiB"
                        f" (当前 {rss / 1024**3:.1f}GiB), 已中止。"
                        "可在 设置 → 系统设置 → 性能与内存 调高「回测内存上限」"
                        "(0 = 不限制), 或缩小回测时间范围/标的数量。"
                        "(环境变量 TICK_WORKER_RSS_LIMIT_BYTES 优先于界面设置)",
                        code="RESOURCE_LIMIT",
                    )
            try:
                message = events.get(timeout=0.1)
            except queue.Empty:
                if not process.is_alive():
                    break
                continue

            message_type = message.get("type")
            if message_type == "progress":
                if progress_cb is not None:
                    progress_cb(message["payload"])
            elif message_type == "result":
                result = message["payload"]
            elif message_type == "error":
                failure = message

        # 子进程退出后, 队列读线程可能尚未把管道尾部的 result/error 搬进本地缓冲
        # (0.1s 轮询在系统高负载下会先看到 Empty+进程已死)。join 后做一次兜底排空,
        # 只要消息完整刷入过管道就一定能取到。
        if result is None and failure is None:
            for _ in range(2):
                try:
                    message = events.get(timeout=1.0)
                except queue.Empty:
                    break
                message_type = message.get("type")
                if message_type == "result":
                    result = message["payload"]
                elif message_type == "error":
                    failure = message

        process.join(timeout=10.0)
        worker_exit_forcibly = False
        if process.is_alive():
            # 终态消息 (result/error) 已完整送达, 子进程只是退出收尾慢:
            # 强制结束并继续走结果/错误处理, 不把已送达的成功结果当失败丢弃。
            _terminate_task(process, native_data=native_data)
            process.join(timeout=5.0)
            worker_exit_forcibly = True
            logger.warning(
                "%s worker delivered its terminal message but did not exit within "
                "10s; terminated forcibly (exitcode=%s)",
                task["kind"],
                process.exitcode,
            )
        if failure is not None:
            if task["kind"] == "engine_node":
                raise BacktestWorkerError("研究节点执行失败", code=failure.get("code", "ENGINE_EXECUTION_FAILED"))
            if failure.get("code") == "SNAPSHOT_UNAVAILABLE":
                raise BacktestWorkerError(failure["message"], code="SNAPSHOT_UNAVAILABLE")
            raise BacktestWorkerError(
                f"{failure.get('message', 'worker failed')}\n{failure.get('traceback', '')}".rstrip()
            )
        if result is None:
            raise BacktestWorkerError(
                f"backtest worker exited without result (exitcode={process.exitcode})"
            )

        parent_metrics = {
            "ipc_elapsed_ms": round((time.perf_counter() - ipc_started) * 1000, 1),
            "parent_rss_before_bytes": parent_rss_before,
            "parent_rss_after_worker_exit_bytes": _rss_bytes(),
            "worker_exitcode": process.exitcode,
            "worker_exit_forcibly": worker_exit_forcibly,
        }
        kind = task["kind"]
        if kind == "backtest":
            result.setdefault("stats", {}).setdefault("worker", {}).update(parent_metrics)
        else:
            result.setdefault("worker", {}).update(parent_metrics)
        return result
    finally:
        if task["kind"] == "engine_node":
            process_cancel.set()
        if process.is_alive():
            _terminate_task(process, native_data=native_data)
            process.join(timeout=5.0)
            if task["kind"] == "engine_node" and process.is_alive():
                process.kill()
                process.join(timeout=5.0)
        events.close()
        events.join_thread()
