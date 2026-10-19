"""策略 run_all 渐进式执行 — 单飞后台执行 + 快策略先返回。

页面进入策略页时 run_all 全量跑需要 ~2 分钟, 用户只能盯着空卡片等。此模块把
执行拆成「同步等一小段 + 后台继续算」:

- 全局同一时刻只执行一个 run_all (polars/Numba 并发跑两份有崩死风险),
  请求先到先得, 后来者排队; 相同 key (资产/周期/日期/策略集) 的重复请求
  直接搭车现有执行, 不重复算。
- 按历史耗时升序执行: 快策略 (秒级) 在首返时限内完成并随 HTTP 响应返回,
  慢策略 (分钟级) 留在后台慢慢算。
- 每个策略算完立刻增量写入 strategy_cache, 前端轮询 cached-summary
  逐个点亮卡片数字。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

_TIMINGS_FILENAME = "strategy_run_timings.json"
_timings_lock = threading.Lock()


def _timings_path(data_dir: Path) -> Path:
    return data_dir / "user_data" / _TIMINGS_FILENAME


def load_run_timings(data_dir: Path) -> dict[str, float]:
    """读取各策略上次执行耗时 (ms); 无文件/损坏时返回空。"""
    with _timings_lock:
        try:
            data = json.loads(_timings_path(data_dir).read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): float(v) for k, v in data.items() if isinstance(v, (int, float))}


def record_run_timings(data_dir: Path, elapsed_ms: dict[str, float]) -> None:
    """批量记录策略耗时 (ms), 与已有文件合并后原子重写。"""
    if not elapsed_ms:
        return
    with _timings_lock:
        path = _timings_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        merged: dict[str, float] = {}
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                merged = {str(k): float(v) for k, v in old.items() if isinstance(v, (int, float))}
        except (FileNotFoundError, ValueError, OSError):
            pass
        merged.update({sid: float(ms) for sid, ms in elapsed_ms.items()})
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)


def order_strategy_ids(all_ids: list[str], timings: dict[str, float]) -> list[str]:
    """快策略先算: 有历史耗时的按耗时升序, 未知耗时的保持原顺序排在后面。"""
    known = sorted(
        (timings[sid], i, sid) for i, sid in enumerate(all_ids) if sid in timings
    )
    known_ids = {sid for _, _, sid in known}
    unknown = [sid for sid in all_ids if sid not in known_ids]
    return [sid for _, _, sid in known] + unknown


class StrategyRunHandle:
    """一次 run_all 的执行状态; 端点线程 (读) 与后台执行线程 (写) 共享。"""

    def __init__(self, key: tuple, ordered_ids: list[str]) -> None:
        self.key = key
        self.started_at_ms = int(time.time() * 1000)
        self.finished_at_ms: int | None = None
        self._lock = threading.Lock()
        self._results: dict[str, dict] = {}
        self._remaining: list[str] = list(ordered_ids)
        self._errors: dict[str, str] = {}
        self._error: str | None = None
        self._done = False

    def complete(self, sid: str, payload: dict) -> None:
        with self._lock:
            self._results[sid] = payload
            if sid in self._remaining:
                self._remaining.remove(sid)

    def fail_one(self, sid: str, message: str) -> None:
        """单个策略失败: 记错误并移出待算队列, 不影响其余策略继续。"""
        with self._lock:
            self._errors[sid] = message
            if sid in self._remaining:
                self._remaining.remove(sid)

    def fail(self, message: str) -> None:
        with self._lock:
            self._error = message
            self._done = True

    def finish(self) -> None:
        with self._lock:
            self._done = True
            if self.finished_at_ms is None:
                self.finished_at_ms = int(time.time() * 1000)

    def snapshot(self) -> dict:
        """线程安全快照: 结果拷贝 + 剩余/逐策略错误/整体错误/完成状态。"""
        with self._lock:
            return {
                "results": dict(self._results),
                "pending": list(self._remaining),
                "errors": dict(self._errors),
                "error": self._error,
                "done": self._done,
                "started_at_ms": self.started_at_ms,
            }


class StrategyRunManager:
    """run_all 单飞管理器。

    - 相同 key 且仍在执行 (含排队中) 的重复请求搭车现有执行, 不重复算
      (页面 reload / StrictMode / 反复切换); 已完成的在 TTL 内仍可查询结果
      (分钟策略结果不落盘, 靠 handle 快照轮询点亮), 超过 TTL 后重跑即新执行。
    - 不同 key 在唯一 daemon 工作线程里排队; 端点在首返时限内等不到也只能
      先返回 pending, 前端靠轮询缓存拿最终结果。
    - 工作线程为 daemon: 进程退出不等待剩余计算 (缓存写入均为原子替换,
      中断只留部分结果, 下次进入页面补算)。
    """

    # 完成后 handle 保留时长: 前端 2s 轮询在几分钟内足够拿完全部结果
    KEEP_DONE_MS = 5 * 60 * 1000

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handles: dict[tuple, StrategyRunHandle] = {}
        # 已完成的 handle 暂存区: 分钟策略结果不落盘, 轮询方靠它读最终结果;
        # 与 _handles 分开存放, 保证「已完成不搭车, 重跑即新执行」语义不变
        self._done_recent: dict[tuple, StrategyRunHandle] = {}
        self._queue: queue.Queue[tuple[StrategyRunHandle, Callable]] = queue.Queue()
        self._worker: threading.Thread | None = None

    def get_or_submit(
        self,
        key: tuple,
        ordered_ids: list[str],
        job: Callable[[StrategyRunHandle], None],
    ) -> StrategyRunHandle:
        now_ms = time.time() * 1000
        with self._lock:
            # 顺手清理完成超过 TTL 的暂存 handle, 防止字典无限增长
            for store in (self._handles, self._done_recent):
                for k in [
                    k for k, h in store.items()
                    if h.snapshot()["done"] and h.finished_at_ms is not None
                    and now_ms - h.finished_at_ms > self.KEEP_DONE_MS
                ]:
                    del store[k]
            existing = self._handles.get(key)
            if existing is not None:
                return existing  # 执行中/排队中: 搭车, 不重复算
            done = self._done_recent.get(key)
            if done is not None:
                del self._done_recent[key]  # 已完成: 重跑即新执行 (旧行为)
            handle = StrategyRunHandle(key, ordered_ids)
            self._handles[key] = handle
        self._ensure_worker()
        self._queue.put((handle, job))
        return handle

    def _retire_if_done(self, handle: StrategyRunHandle) -> None:
        """worker 收尾后调用: 把 done handle 从执行区挪到暂存区供轮询。"""
        with self._lock:
            if self._handles.get(handle.key) is handle:
                del self._handles[handle.key]
                self._done_recent[handle.key] = handle

    def minute_summaries(self) -> list[dict]:
        """所有分钟 (timeframe='1m') handle 的结果摘要, 供 cached-summary 合并。

        分钟策略结果不落 strategy_cache (日线语义), 渐进式转后台后前端靠
        cached-summary 轮询点亮 — 这里把活跃/近完成分钟 handle 的 per-策略
        摘要 (total/computed_at) 按快照导出。
        """
        out: list[dict] = []
        with self._lock:
            handles = list(self._handles.values()) + list(self._done_recent.values())
        for h in handles:
            if len(h.key) < 2 or h.key[1] != "1m":
                continue
            snap = h.snapshot()
            out.append({
                "as_of": h.key[2] if len(h.key) > 2 else None,
                "results": {
                    sid: {
                        "total": int(payload.get("total") or 0),
                        "as_of": payload.get("as_of"),
                        "computed_at": payload.get("computed_at"),
                    }
                    for sid, payload in snap["results"].items()
                },
                "pending": snap["pending"],
                "complete": snap["done"] and not snap["error"],
            })
        return out

    def _run_loop(self) -> None:
        while True:
            handle, job = self._queue.get()
            try:
                job(handle)
            except Exception as e:
                logger.exception("run_all 后台执行失败: %s", e)
                handle.fail(str(e))
            else:
                handle.finish()
            self._retire_if_done(handle)

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._run_loop, name="runall", daemon=True
                )
                self._worker.start()


# 进程级单例: 与 strategy_cache 的模块级锁同风格, 生命周期跟随进程
MANAGER = StrategyRunManager()
