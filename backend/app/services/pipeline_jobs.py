"""异步盘后管道任务注册表 — 每个 job 独立 JSON 文件。

设计:
  - job_store/ 文件夹,每个 job 一个 {id}.json,最多保留 max_jobs 个文件
  - create()/start() 即落盘 pending/running 快照(进度只更新内存) ——
    进程意外死亡(uvicorn --reload 热重载 / 被 kill)时记录不蒸发
  - succeeded/failed 后写入终态并从内存释放
  - 实例化时扫描磁盘,把上个进程遗留的 pending/running 孤儿记录补标为
    failed(中断);finished_at 取文件 mtime(最后已知存活时刻),不用下次
    开机时间虚增时长
  - 列表查询 = 内存中的活跃 job + 磁盘文件扫描,按时间排序
  - 单个查询 = 内存优先,没有则读磁盘
  - 创建新 job 前检查文件数量,>= max_jobs 时删除最老的文件
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeVar

logger = logging.getLogger(__name__)

JobStatus = Literal["pending", "running", "succeeded", "failed"]

# 卡死判定阈值(秒)。语义是「进度停滞」而非「总时长」:
# running 期间只要 progress() 还在上报(每个分块都会回调), 就永远不算卡死 ——
# 慢带宽/高延迟环境下的冷启动全市场拉取可能远超 20 分钟, 但只要分块在推进,
# 不应被误杀(reap 旧实现按总时长一刀切, 导致 UI 标记失败而拉取线程仍在写盘)。
#
# 阈值按任务类型区分,可在 Web 数据源设置中调整:
#   - 普通任务(日K管道/扩展/修正/重算): 1200s (20 分钟无进度)
#   - 长任务(分钟K全市场同步,数据量是日K的 ~240 倍): 1800s (30 分钟无进度)
DEFAULT_JOB_TIMEOUT_S = 1200
LONG_JOB_TIMEOUT_S = 1800
# 总时长硬上限(兜底): 进度回调持续上报但永不结束的病态循环无法靠停滞判定捕获,
# 超过该值无条件终止。取 12h(最长合法任务分钟K补齐的历史量级远小于此)。
HARD_JOB_TIMEOUT_S = 12 * 3600
# 向后兼容: 旧调用方引用 STALE_JOB_TIMEOUT_S
STALE_JOB_TIMEOUT_S = DEFAULT_JOB_TIMEOUT_S


class JobCancelledError(BaseException):
    """任务已被取消(reap 判定卡死后自动取消,手动取消,或排队等槽超时)。

    继承 BaseException 而非 Exception(对齐 asyncio.CancelledError 的设计):
    同步循环内部的分块异常隔离(``except Exception: continue``)不得吞掉取消信号,
    它必须从 executor 线程一路传播回 API 边界的 task()。API 层应有独立
    ``except JobCancelledError`` 分支(job 此时已被 reap 标记 failed,无需再写状态)。
    """

    def __init__(self, job_id: str) -> None:
        super().__init__(f"job {job_id} 已取消")
        self.job_id = job_id


# ── 取消标志注册表 ──────────────────────────────────────────────────────
# reap 终止 job 时置位; 僵尸线程随后每次 progress() 回调检查到即抛
# JobCancelledError 自行退出。flag 独立于 job 记录存活 —— fail() 会把记录从
# _active_jobs 弹出, 但僵尸线程仍需通过 flag 感知取消。
# 有界(最多 _CANCEL_FLAG_MAX 条, 淘汰最老): 真卡死的僵尸永远不会回来清 flag。
_CANCEL_FLAG_MAX = 32
_CANCEL_FLAGS: dict[str, threading.Event] = {}
_CANCEL_FLAGS_LOCK = threading.Lock()


def request_cancel(job_id: str) -> bool:
    """请求取消指定 job。返回是否存在该 job 的 flag。"""
    with _CANCEL_FLAGS_LOCK:
        ev = _CANCEL_FLAGS.get(job_id)
        if ev is None:
            return False
        ev.set()
        return True


def is_cancelled(job_id: str) -> bool:
    ev = _CANCEL_FLAGS.get(job_id)
    return ev is not None and ev.is_set()


def _register_cancel_flag(job_id: str) -> None:
    with _CANCEL_FLAGS_LOCK:
        if job_id not in _CANCEL_FLAGS:
            _CANCEL_FLAGS[job_id] = threading.Event()
        # 有界淘汰最老(当前活跃 job 总是最新注册, 不会被误淘汰)
        while len(_CANCEL_FLAGS) > _CANCEL_FLAG_MAX:
            oldest = next(iter(_CANCEL_FLAGS))
            _CANCEL_FLAGS.pop(oldest)


def _default_store_dir() -> Path:
    from app.config import settings
    return settings.data_dir / "job_store"


_STORE_DIR = _default_store_dir()


class JobStore:
    def __init__(self, max_jobs: int = 50, store_dir: Path = _STORE_DIR) -> None:
        self._max_jobs = max_jobs
        self._store_dir = store_dir
        self._active_jobs: dict[str, dict[str, Any]] = {}   # running/pending
        self._active_id: str | None = None
        self._lock = threading.Lock()
        self._store_dir.mkdir(parents=True, exist_ok=True)
        self._reap_orphans()

    # ===== persistence =====

    def _write_file(self, job: dict[str, Any]) -> None:
        """将 job 快照写入独立 JSON 文件(create/start/终态均落盘)。

        临时文件 + os.replace 原子替换: get()/list_recent() 等无锁读者
        不会读到写一半的 JSON —— 否则 _read_file 解析失败按"文件不存在"
        返回 None, 轮询方会瞬时看到任务凭空消失。
        """
        path = self._store_dir / f"{job['id']}.json"
        tmp = self._store_dir / f"{job['id']}.json.tmp"
        try:
            tmp.write_text(
                json.dumps(job, ensure_ascii=False, indent=None),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except Exception:
            logger.warning("failed to write job file %s", path)
            try:
                tmp.unlink()
            except OSError:
                pass

    def _read_file(self, job_id: str) -> dict[str, Any] | None:
        """从磁盘读取单个 job 文件。"""
        path = self._store_dir / f"{job_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text("utf-8"))
        except Exception:
            logger.warning("failed to read job file %s", path)
            return None

    def _delete_oldest(self) -> None:
        """删除最老的 job 文件,保持文件数量 < max_jobs。"""
        try:
            files = sorted(self._store_dir.glob("*.json"), key=lambda f: f.stat().st_mtime)
        except Exception:
            return
        while len(files) >= self._max_jobs:
            oldest = files.pop(0)
            try:
                oldest.unlink()
            except Exception:
                logger.warning("failed to delete old job file %s", oldest)

    def _job_files_sorted(self) -> list[dict[str, Any]]:
        """扫描磁盘上所有 job 文件,按 started_at 从新到旧排序。"""
        jobs: list[dict[str, Any]] = []
        for f in self._store_dir.glob("*.json"):
            try:
                jobs.append(json.loads(f.read_text("utf-8")))
            except Exception:
                continue
        jobs.sort(key=lambda j: j.get("started_at") or "", reverse=True)
        return jobs

    def _reap_orphans(self) -> None:
        """启动补录: 把上个进程遗留的 pending/running 记录标为中断。

        单例在进程启动时实例化,此时磁盘上的 pending/running 必然来自已死
        亡的进程(uvicorn --reload 热重载 / 被 kill)—— 不补录则这些记录
        永远停留在「运行中」, 同步历史里既看不到结果也看不到失败, 即
        「数据在、记录丢」。finished_at 取文件 mtime(最后一次落盘 = 最后
        已知存活时刻), 避免拿下次开机时间虚增 duration。
        """
        for f in self._store_dir.glob("*.json"):
            try:
                j = json.loads(f.read_text("utf-8"))
            except Exception:
                continue
            orig_status = j.get("status")
            if not j.get("id") or orig_status not in ("pending", "running"):
                continue
            j["status"] = "failed"
            j["error"] = "后端重启,任务中断(启动时补录)"
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=UTC)
                end = mtime.strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                end = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            # mtime 早于 started_at(时钟回拨等)时夹住, 避免 duration 为负
            if j.get("started_at") and end < j["started_at"]:
                end = j["started_at"]
            j["finished_at"] = end
            j["duration_s"] = _duration_s(j)
            logger.warning(
                "job_store: 补录中断任务 %s (上个进程遗留 %s 记录)",
                j["id"],
                orig_status,
            )
            self._write_file(j)

    # ===== lifecycle =====

    def create(
        self,
        timeout_s: int | None = None,
        *,
        long_running: bool = False,
    ) -> tuple[str, bool]:
        """单飞创建任务。返回 (job_id, is_new)。

        去重条件为 **pending ∨ running**(而非仅 running):`/run` 先 create() 再在
        后台任务里 start() 置 running,两者之间存在 pending 窗口。旧实现只在 running 时
        复用,两次快速点击时首个 job 仍是 pending → 第二次绕过去重、另起并发任务、覆盖
        _active_id,导致两条全市场拉取同时读改写同一 parquet。纳入 pending 后该窗口关闭。

        is_new=False 表示复用了已有活跃任务,调用方**不得**再调度新的后台任务。

        timeout_s: reap_stale 判定「进度停滞卡死」的阈值。None 时读取用户配置。
        long_running: timeout_s 为 None 时,是否读取长任务配置;普通任务默认
            1200s,分钟K全市场同步等长任务默认 1800s。
        """
        if timeout_s is None:
            from app.services import preferences
            if long_running:
                timeout_s = preferences.get_data_source_long_job_timeout_s()
            else:
                timeout_s = preferences.get_data_source_job_timeout_s()

        with self._lock:
            if self._active_id:
                active = self._active_jobs.get(self._active_id)
                if active and active.get("status") in ("pending", "running"):
                    return self._active_id, False

            job_id = uuid.uuid4().hex[:10]
            # last_progress_at 锚定创建时刻: pending 阶段(executor 排队/等重任务槽)
            # 也要有「已知存活」基准, 否则从未上报进度的排队任务永远不进 reap 视野。
            created_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            job = {
                "id": job_id,
                "status": "pending",
                "stage": "init",
                "progress": 0,
                "stage_pct": 0,
                "log": [],
                "started_at": None,
                "last_progress_at": created_at,
                "finished_at": None,
                "duration_s": None,
                "result": None,
                "error": None,
                "timeout_s": timeout_s,
            }
            self._active_jobs[job_id] = job
            self._active_id = job_id
            # pending 即落盘: 进程在 start() 前死亡时记录也不丢
            self._delete_oldest()
            self._write_file(job)
        _register_cancel_flag(job_id)
        return job_id, True

    def start(self, job_id: str) -> None:
        with self._lock:
            j = self._active_jobs.get(job_id)
            if not j:
                return
            j["status"] = "running"
            j["started_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            # 心跳基准初始化为启动时刻: start() 到首次 progress() 之间的
            # 初始化阶段(解析标的池等)同样计入停滞计时。
            j["last_progress_at"] = j["started_at"]
            # running 快照落盘: 进程死亡后由下次启动的 _reap_orphans 补录
            self._write_file(j)

    def succeed(self, job_id: str, result: Any) -> None:
        with self._lock:
            j = self._active_jobs.pop(job_id, None)
            if not j:
                return
            j["status"] = "succeeded"
            j["finished_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            j["progress"] = 100
            j["result"] = result
            j["duration_s"] = _duration_s(j)
            if self._active_id == job_id:
                self._active_id = None
            self._delete_oldest()
            self._write_file(j)

    def fail(self, job_id: str, error: str) -> None:
        with self._lock:
            j = self._active_jobs.pop(job_id, None)
            if not j:
                return
            j["status"] = "failed"
            j["finished_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            j["error"] = error
            j["duration_s"] = _duration_s(j)
            if self._active_id == job_id:
                self._active_id = None
            self._delete_oldest()
            self._write_file(j)

    # ===== progress =====

    def progress(self, job_id: str, stage: str, pct: int, msg: str,
                 stage_pct: int | None = None, skip_log: bool = False) -> None:
        with self._lock:
            j = self._active_jobs.get(job_id)
            if not j:
                # 记录已不在(通常是被 reap 终止后 fail() 弹出)。
                # 僵尸线程仍需感知取消 —— flag 检查不能依赖记录存在。
                cancelled = _CANCEL_FLAGS.get(job_id)
                if cancelled is not None and cancelled.is_set():
                    raise JobCancelledError(job_id)
                return
            j["stage"] = stage
            j["progress"] = max(0, min(100, int(pct)))
            j["last_progress_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            if stage_pct is not None:
                j["stage_pct"] = max(0, min(100, int(stage_pct)))
            elif j["stage"] != stage:
                j["stage_pct"] = 0
            entry = {
                "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "stage": stage,
                "msg": msg,
            }
            if skip_log:
                entry["_skip"] = True
            if skip_log and j["log"] and j["log"][-1].get("stage") == stage and j["log"][-1].get("_skip"):
                j["log"][-1] = entry
            else:
                j["log"].append(entry)
                if len(j["log"]) > 200:
                    j["log"] = j["log"][-200:]
        # 锁外检查取消: reap 可能在本次更新刚结束后置位,下一次回调必然命中;
        # 在这里立即检查可以把终止延迟压缩到当次回调。
        ev = _CANCEL_FLAGS.get(job_id)
        if ev is not None and ev.is_set():
            raise JobCancelledError(job_id)

    # ===== query =====

    def get(self, job_id: str) -> dict[str, Any] | None:
        # 取锁与 succeed()/fail() 的「pop 出内存 → 终态落盘」串行,
        # 避免轮询方恰好落在两者之间, 瞬时看到任务凭空消失。
        with self._lock:
            # 内存中的活跃 job 优先
            j = self._active_jobs.get(job_id)
            if j:
                return j
            # 否则从磁盘读
            return self._read_file(job_id)

    def list_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        # 合并: 内存中的活跃 job + 磁盘文件
        all_jobs: list[dict[str, Any]] = list(self._active_jobs.values())
        all_jobs.extend(self._job_files_sorted())
        # 按 started_at 从新到旧排序,去重(理论上不会有重复)
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for j in sorted(all_jobs, key=lambda x: x.get("started_at") or "", reverse=True):
            jid = j["id"]
            if jid in seen:
                continue
            seen.add(jid)
            result.append(_summary(j))
            if len(result) >= limit:
                break
        return result

    def active_id(self) -> str | None:
        return self._active_id

    def reap_stale(self, timeout_s: int | None = None) -> None:
        """回收卡死的 running job, 以及排队超时的 pending job。判定:

        1. 进度停滞(主判定): 距上次 progress() 上报超过阈值秒数。
           慢带宽环境下任务只要仍在分块推进就不会被误杀 —— 这正是旧的
           总时长判定的问题(冷启动全市场拉取 >20min 就被标死,线程却还在写盘)。
        2. 总时长硬上限(兜底): 进度回调持续但永不结束的病态循环。
        3. pending 排队停滞: 任务卡在「等待其他计算任务完成…」(重任务槽被
           僵尸线程/长回测占住)超过阈值。旧实现只回收 running, 排队任务
           无限空转且无任何兜底(用户侧「卡死空转」体感)。排队任务尚未开跑,
           无数据写入, 取消零代价; 总时长硬上限对 pending 不适用(无 started_at)。

        在 /run 和 /jobs/{id} 轮询端点都会调用 — 保证卡死后任意轮询都能自愈,
        无需用户再次手动触发同步。reload 后的孤儿 task(内存里已无 job 记录)
        不在此处理:它们没有 active_id,只能靠 executor 线程自然结束或进程重启。

        终止是**协作式**的: 置 cancel flag → 僵尸线程在下一个分块进度回调处
        抛 JobCancelledError 自行退出(BaseException,不会被分块异常隔离吞掉);
        排队中的线程则在 limiter 的 cancel_event 轮询处立即退出。
        线程真正退出前,由所有权 token 保证它误释放不了新任务的执行槽。

        timeout_s: 显式覆盖停滞阈值。None 时用 job 自身 create() 时存的 timeout_s,
        缺失则回退 DEFAULT_JOB_TIMEOUT_S。分钟K长任务在 create 时存了更大阈值,
        不被普通任务的 1200s 误杀。
        """
        with self._lock:
            jid = self._active_id
            if not jid:
                return
            j = self._active_jobs.get(jid)
            status = j.get("status") if j else None
            if status not in ("pending", "running"):
                return
            started = j.get("started_at")
            last_alive = j.get("last_progress_at") or started
            if not last_alive:
                return
            # 优先用显式传入, 其次 job 自身阈值, 最后默认值
            effective_timeout = timeout_s if timeout_s is not None else j.get("timeout_s", DEFAULT_JOB_TIMEOUT_S)
            timeout_s = effective_timeout
            started_at = started
            last_alive_at = last_alive
            queued = status == "pending"
        # 时间计算放到锁外(避免 datetime 解析持锁)。
        # started_at 形如 "2026-07-04T12:00:00Z"(start() 用 datetime.utcnow 存)。
        # 两端都用 timezone-aware UTC 比较,避免 naive/aware 混用导致 TypeError。
        try:
            alive_dt = _parse_utc(last_alive_at)
            start_dt = _parse_utc(started_at) if started_at else None
            now = datetime.now(alive_dt.tzinfo)
            stalled_s = (now - alive_dt).total_seconds()
            total_s = (now - start_dt).total_seconds() if start_dt else 0.0
        except Exception:  # noqa: BLE001
            return
        if stalled_s > timeout_s:
            if queued:
                logger.warning(
                    "reap_stale: 取消排队 job %s (等待重任务槽 %.0fs > 阈值 %ss)",
                    jid, stalled_s, timeout_s)
                self.terminate(
                    jid,
                    f"超时自动取消: 排队等待重任务执行槽 {int(stalled_s)}s 超过阈值 {timeout_s}s — "
                    "槽位可能被长回测或未正常退出的残留任务占用, 请稍后重试; 若持续出现请重启后端")
            else:
                logger.warning(
                    "reap_stale: 强制取消卡死 job %s (进度停滞 %.0fs > 阈值 %ss, 总运行 %.0fs)",
                    jid, stalled_s, timeout_s, total_s)
                self.terminate(jid, f"超时自动取消: 进度停滞 {int(stalled_s)}s 超过阈值 {timeout_s}s,已请求终止")
        elif not queued and total_s > HARD_JOB_TIMEOUT_S:
            logger.warning(
                "reap_stale: 强制取消 job %s (总运行 %.0fs 超过硬上限 %ss)",
                jid, total_s, HARD_JOB_TIMEOUT_S)
            self.terminate(jid, f"超时自动取消: 总运行 {int(total_s)}s 超过硬上限,已请求终止")

    def terminate(self, job_id: str, message: str) -> None:
        """标记失败 + 请求协作式终止 + 强制释放执行槽(带所有权)。

        reap_stale(判定卡死)与手动取消端点共用。
        """
        # 先置 cancel flag 再标失败: fail() 弹出记录后,僵尸线程的 progress()
        # 依赖 flag(而非记录)感知取消。
        request_cancel(job_id)
        self.fail(job_id, message)
        # 强制释放重任务槽(按所有权): 卡死线程可能永远回不来释放。
        # job 已标记 failed 且已请求终止; 僵尸线程即使后续短暂写盘,
        # 也会在下一个分块回调处自行退出, 下次拉取会覆盖, 安全。
        release_run_slot(job_id)

    def clear(self) -> None:
        """清空所有任务（内存 + 磁盘文件 + 取消标志）。"""
        with self._lock:
            self._active_jobs.clear()
            self._active_id = None
            for f in self._store_dir.glob("*.json"):
                try:
                    f.unlink()
                except Exception:
                    pass
        with _CANCEL_FLAGS_LOCK:
            _CANCEL_FLAGS.clear()


def _summary(j: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": j["id"],
        "status": j["status"],
        "stage": j["stage"],
        "progress": j["progress"],
        "stage_pct": j.get("stage_pct", 0),
        "started_at": j["started_at"],
        "finished_at": j["finished_at"],
        "duration_s": j["duration_s"],
        "result": j["result"],
        "error": j["error"],
    }


def _duration_s(j: dict[str, Any]) -> float | None:
    if not j.get("started_at") or not j.get("finished_at"):
        return None
    try:
        s = datetime.fromisoformat(j["started_at"])
        e = datetime.fromisoformat(j["finished_at"])
        return round((e - s).total_seconds(), 2)
    except Exception:  # noqa: BLE001
        return None


# 进程内单例
job_store = JobStore()

_Result = TypeVar("_Result")


def run_with_capacity(job_id: str, fn: Callable[[], _Result]) -> _Result:
    """Wait in the worker, keeping the reservation until its real execution ends.

    排队等待有上界(取 job 自身 timeout_s): 重任务槽被长回测或未正常退出的
    残留线程长期占住时, 有限等待后以可读错误失败, 而不是无限空转
    (「分钟K同步卡死空转」的排队侧修复; 持有侧由 kline_sync 的 SDK deadline 兜底)。
    """
    from app.services.heavy_job_limiter import (
        HeavyJobCancelledError,
        HeavyJobLimitTimeoutError,
        shared_heavy_job_limiter,
    )

    job_store.progress(job_id, "init", 0, "等待其他计算任务完成…")
    with _CANCEL_FLAGS_LOCK:
        cancel_event = _CANCEL_FLAGS.get(job_id)
    wait_s: float = DEFAULT_JOB_TIMEOUT_S
    job = job_store.get(job_id)
    if isinstance(job, dict):
        configured = job.get("timeout_s")
        if isinstance(configured, (int, float)) and configured > 0:
            wait_s = configured
    try:
        with shared_heavy_job_limiter.slot("exclusive", timeout=wait_s, cancel_event=cancel_event):
            if is_cancelled(job_id):
                raise JobCancelledError(job_id)
            job_store.start(job_id)
            return fn()
    except HeavyJobCancelledError as exc:
        raise JobCancelledError(job_id) from exc
    except HeavyJobLimitTimeoutError:
        # 排队超时: fn 尚未执行, 无数据写入; 直接标记失败并按「已终止」上报,
        # API 层的 except JobCancelledError 分支无需(也无法)再改写状态。
        job_store.fail(
            job_id,
            f"排队等待重任务执行槽超时({int(wait_s)}s): 槽位可能被长回测或未正常退出的"
            "残留任务占用, 请稍后重试; 若持续出现请重启后端")
        raise JobCancelledError(job_id)


# ================================================================
# 重任务互斥执行槽 — 防「僵尸并发」, 带所有权 token
# ================================================================
# create() 的单飞去重能挡住 pending/running 窗口内的重复点击, 但挡不住
# reap_stale 把卡死 job 标记 failed、清掉 _active_id 之后 —— 此时 executor
# 线程可能仍在跑(线程无法被硬中断), 下一次 /run 会视作无活跃任务而另起一条,
# 与僵尸线程并发读改写同一 parquet。
#
# 该槽绑定「实际执行体」的生命周期而非 job 状态: 每个重任务在真正开跑前
# try_acquire_run_slot(job_id), 结束(含异常)在 finally 里 release_run_slot(job_id)。
#
# 所有权 token 修复的竞态: 旧实现用裸 threading.Lock + 无参 release ——
# 僵尸线程最终结束时会在 finally 里误释放**新任务**正持有的锁(Lock 允许
# 任意线程 release), 第三次点击又能插入并发。现在 release 必须携带持有者
# job_id, 非持有者的释放一律忽略; reap 的强制释放也走同一入口。
# 代价: 真卡死且协作式终止不生效(如卡在单个无限阻塞的网络读里)时,
# 需重启进程才能再次跑重任务(优先保证数据不损坏)。
_run_slot_lock = threading.Lock()
_run_slot_owner: str | None = None


def try_acquire_run_slot(owner: str = "") -> bool:
    """尝试占用重任务执行槽(非阻塞)。成功返回 True 并记录持有者。

    owner: 持有者标识(调用方传 job_id), 供 release_run_slot 校验所有权。
    """
    global _run_slot_owner
    with _run_slot_lock:
        if _run_slot_owner is not None:
            return False
        _run_slot_owner = owner
        return True


def release_run_slot(owner: str | None = None) -> None:
    """释放重任务执行槽。

    owner=None 时无条件释放(兼容旧调用/测试);
    owner 非 None 时仅当它是当前持有者才释放 —— 僵尸线程 finally 里的
    误释放(持有者已换成新 job 或槽已被 reap 释放)会被忽略, 幂等不抛。
    """
    global _run_slot_owner
    with _run_slot_lock:
        if owner is not None and _run_slot_owner is not None and _run_slot_owner != owner:
            return
        _run_slot_owner = None


def _parse_utc(ts: str) -> datetime:
    """解析 start()/progress() 存的 "2026-07-04T12:00:00Z" 形式时间戳。"""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))
