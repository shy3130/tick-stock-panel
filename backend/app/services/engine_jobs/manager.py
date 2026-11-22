"""Single-owner durable research DAG orchestration, using the existing worker supervisor."""

import logging
import os
import sqlite3
import threading
import time
from datetime import UTC, datetime

import psutil
from tivelo_contracts.base import fingerprint
from tivelo_contracts.errors import TiveloError
from tivelo_data import DataEngine

from app.backtest.worker import _effective_rss_limit_bytes, run_worker_task
from app.services.heavy_job_limiter import (
    HeavyJobCancelledError,
    HeavyJobLimitTimeoutError,
    shared_heavy_job_limiter,
)
from app.services.mining_process_lock import MiningProcessLock, MiningProcessLockError
from app.services.shared_work import WorkBusyError

from .artifacts import (
    freeze_request,
    inspect_attempt,
    publish_document,
    read_document,
    read_input_run,
    verify_completed,
)
from .models import ArtifactReference, ExecutionBudget, JobRequest, PipelineManifest, stages
from .preparation_models import LockedPreparation
from .schedule_models import publication_datasets, trading_day_capture_nodes
from .storage import ACTIVE, JobError, JobStore

logger = logging.getLogger(__name__)


class _NeverCancel:
    def cancelled(self):
        return False

    def progress(self, *args):
        pass


class _Cancellation:
    def __init__(self, manager, job_id):
        self.manager, self.job_id = manager, job_id

    def is_set(self):
        return (
            self.manager._stop.is_set() or self.manager.store.get(self.job_id)["cancel_requested"]
        )


class EngineJobManager:
    def __init__(self, data_dir, *, worker_runner=run_worker_task, autostart=True):
        self.store = JobStore(data_dir)
        self.data_dir = str(self.store.data_dir)
        # Reuse the host's cross-platform OS lock; its diagnostic file is private to this root.
        self._owner = MiningProcessLock(self.store.path("owner"))
        self._owner.acquire(role="engine-scheduler")
        self._stop, self._wake = threading.Event(), threading.Event()
        self._work = threading.Lock()
        self._submission = threading.Lock()
        self._dispatch_lock = threading.Lock()
        self._thread = None
        self._worker = worker_runner
        self._schedule_wake = threading.Event()
        self.schedules = None
        try:
            from .schedules import ScheduleStore

            self.schedules = ScheduleStore(self.store)
        except (JobError, OSError, sqlite3.Error):
            # A missing/newer optional schedule index must not disable manual research jobs.
            logger.error("engine schedule index unavailable", exc_info=False)
        if autostart:
            self._thread = threading.Thread(target=self._loop, name="engine-jobs", daemon=True)
            self._thread.start()

    def preview(self, request):
        with self._submission:
            plan = self._plan_request(request)
            return {
                "plan_id": plan.plan_id,
                "plan": plan.model_dump(mode="json"),
                "trading_day_capture_nodes": trading_day_capture_nodes(plan.request.nodes),
                "publication_datasets": publication_datasets(plan.request.nodes)
                if hasattr(DataEngine, "publications")
                else (),
            }

    def submit(self, request, *, expected_plan_id=None):
        with self._submission:
            return self._submit(request, expected_plan_id=expected_plan_id)

    def _plan_request(self, request):
        if self._stop.is_set():
            raise JobError("UNAVAILABLE", "研究任务服务正在退出")
        request = JobRequest.model_validate_json(request.model_dump_json())
        existing = self.store.find_request(request.idempotency_key)
        if existing is not None:
            if existing.request != request:
                raise JobError("DATA_CONFLICT", "相同请求键已绑定其他参数，请创建新请求")
            budget = existing.effective_budget
        else:
            cap = _effective_rss_limit_bytes()
            memory = (
                min(cap, request.budget.rss_limit_bytes)
                if cap > 0
                else request.budget.rss_limit_bytes
            )
            if memory < 64 * 1024**2:
                raise JobError("RESOURCE_LIMIT", "部署内存上限不足以启动研究进程")
            budget = ExecutionBudget.model_validate(
                {**request.budget.model_dump(), "rss_limit_bytes": memory}
            )
        return freeze_request(self.store, request, budget=budget, prior=existing)

    def _submit(self, request, *, expected_plan_id=None):
        plan = self._plan_request(request)
        if expected_plan_id is not None and plan.plan_id != expected_plan_id:
            raise JobError("DATA_CONFLICT", "输入或执行预算已变化，请重新检查后提交")
        return self._submit_plan(plan)

    def _submit_plan(self, plan):
        if self._stop.is_set():
            raise JobError("UNAVAILABLE", "研究任务服务正在退出")
        from .preparation import persist_definitions

        persist_definitions(self.store, plan)
        state = self.store.create(plan)
        self._wake.set()
        return state

    def schedule_store(self):
        if self.schedules is None:
            raise JobError("UNAVAILABLE", "研究计划服务不可用，手动任务仍可使用")
        return self.schedules

    def create_schedule(self, request):
        with self._submission:
            if self._stop.is_set():
                raise JobError("UNAVAILABLE", "研究任务服务正在退出")
            return self.schedule_store().create(request)

    def set_schedule_enabled(self, identity, enabled):
        with self._submission:
            if self._stop.is_set():
                raise JobError("UNAVAILABLE", "研究任务服务正在退出")
            result = self.schedule_store().set_enabled(identity, enabled)
            self._schedule_wake.set()
            return result

    def dispatch_due(self, at=None):
        """Lock each occurrence before queuing; reconcile a lost receipt by request identity."""
        with self._dispatch_lock:
            return self._dispatch_due(at)

    def _dispatch_due(self, at):
        if self._stop.is_set() or self.schedules is None:
            return 0
        # A feed read can wait on a separate data index. Only admission shares the
        # submission lock with pause, and always rechecks the schedule after claiming.
        pending = self.schedules.claim_due(at if at is not None else datetime.now(UTC))
        count = 0
        for delivery in pending:
            with self._submission:
                if self._stop.is_set():
                    break
                if not self.schedules.get(delivery["schedule_id"])["enabled"]:
                    continue
                try:
                    if self._dispatch(delivery) is False:
                        continue
                except (JobError, TiveloError, ValueError, TypeError, OSError) as exc:
                    if self._stop.is_set():
                        break
                    # Reconcile an admission that committed before its acknowledgement failed.
                    # Database errors themselves propagate, leaving the outbox untouched.
                    existing = self.store.find_request(delivery["request_key"])
                    code = str(getattr(exc, "code", "INVALID_INPUT"))
                    if existing is not None and code not in {"DATA_CONFLICT", "ARTIFACT_CORRUPT"}:
                        current = self.schedules.delivery(delivery["delivery_id"])
                        if self.schedules.plan(current) == existing:
                            self.schedules.finish(current, "submitted", job_id=existing.plan_id)
                            count += 1
                            continue
                        code = "DATA_CONFLICT"
                    self.schedules.finish(delivery, "failed", code=code)
                count += 1
        return count

    def _dispatch(self, delivery):
        schedules = self.schedule_store()
        request = schedules.delivery_request(delivery)
        plan = schedules.plan(delivery)
        if plan is not None and plan.request != request:
            raise JobError("DATA_CONFLICT", "已锁定派发计划与触发配置不一致")
        existing = self.store.find_request(delivery["request_key"])
        if existing is not None:
            if plan is None or existing != plan:
                raise JobError("DATA_CONFLICT", "派发请求键已经绑定其他输入")
            schedules.finish(delivery, "submitted", job_id=existing.plan_id)
            return
        active = self.store.active_ids()
        if "publication" in delivery and len(active) >= 128:
            return False
        if schedules.has_active_job(delivery["schedule_id"], active):
            if "publication" in delivery:
                return False
            schedules.finish(delivery, "skipped_overlap")
            return
        if "publication" in delivery:
            schedules.check_publication_source(delivery)
        if plan is None:
            plan = self._plan_request(request)
            delivery = schedules.lock_plan(delivery, plan)
        state = self._submit_plan(plan)
        schedules.finish(delivery, "submitted", job_id=state["job_id"])

    def cancel(self, job_id):
        state = self.store.cancel(job_id)
        self._wake.set()
        return state

    def uninstall_price_extension(self, extension_id, generation, digests):
        from app.services.engine_package_store import EnginePackageStore

        with self._submission:
            for job_id in self.store.active_ids():
                for locked in self.store.plan(job_id).inputs:
                    if isinstance(locked.preparation, LockedPreparation):
                        pins = (locked.preparation.price_extension,)
                    elif locked.request.run is not None:
                        _, lock = read_input_run(self.store, locked.request.run)
                        pins = lock.contents.extensions
                    else:
                        continue
                    if any(pin and pin.extension_id == extension_id for pin in pins):
                        raise JobError("IN_USE", "研究任务正在使用 Tivelo Pro，请等任务结束后再卸载")
            return EnginePackageStore(self.store.data_dir).uninstall(extension_id, generation, digests)

    def retry(self, job_id):
        if self._stop.is_set():
            raise JobError("UNAVAILABLE", "研究任务服务正在退出")
        plan = self.store.plan(job_id)
        state = self.store.get(job_id)
        graph = stages(plan)
        for name, node in state["nodes"].items():
            if node["status"] == "succeeded":
                verify_completed(self.store, plan, name)
            elif node["attempts"] and graph[name][0] == "sync":
                source = graph[name][1].preparation.sources[int(name.rsplit("_", 1)[1])].source
                if source.kind != "local_jsonl":
                    raise JobError("DATA_CONFLICT", "上次采集未封存，重新观察行情需要新的任务")
        # Metadata and content references remain the original plan on every retry.
        if (
            freeze_request(self.store, plan.request, budget=plan.effective_budget, prior=plan)
            != plan
        ):
            raise JobError("DATA_CONFLICT", "原输入已改变，请创建新任务")
        from .preparation import checked_definitions

        for locked in plan.inputs:
            if locked.request.prepare is not None:
                checked_definitions(self.store, locked, plan_id=plan.plan_id)
        with self._submission:
            result = self.store.retry(job_id)
        self._wake.set()
        return result

    def shutdown(self, timeout=20):
        self._stop.set()
        self._wake.set()
        self._schedule_wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                return False
        else:
            deadline = time.monotonic() + timeout
            acquired = []
            try:
                for lock in (self._work, self._dispatch_lock, self._submission):
                    if not lock.acquire(timeout=max(0, deadline - time.monotonic())):
                        return False
                    acquired.append(lock)
            finally:
                for lock in reversed(acquired):
                    lock.release()
        self._owner.release()
        return True

    def _loop(self):
        trigger_thread = threading.Thread(
            target=self._trigger_loop, name="engine-triggers", daemon=True
        )
        trigger_thread.start()
        try:
            while not self._stop.is_set():
                try:
                    progressed = self.run_once()
                except Exception:  # noqa: BLE001 — keep the scheduler alive without exposing exception text
                    # No false terminal state when the persistence layer itself is unavailable.
                    logger.error("engine task persistence or scheduling failed", exc_info=False)
                    progressed = False
                if not progressed:
                    self._wake.wait(1)
                    self._wake.clear()
        finally:
            # Do not release ownership while a trigger is still persisting an admission.
            self._stop.set()
            self._schedule_wake.set()
            trigger_thread.join()
            self._owner.release()

    def _trigger_loop(self):
        while not self._stop.is_set():
            try:
                self.dispatch_due()
            except Exception:  # noqa: BLE001 — preserve outbox and suppress sensitive exception text
                logger.error("engine schedule dispatch unavailable", exc_info=False)
            self._schedule_wake.wait(1)
            self._schedule_wake.clear()

    def _finalize(self, plan):
        state = self.store.get(plan.plan_id)
        if not all(node["status"] == "succeeded" for node in state["nodes"].values()):
            return False
        outputs = {name: verify_completed(self.store, plan, name) for name in stages(plan)}
        manifest = PipelineManifest(plan_id=plan.plan_id, artifacts=outputs)
        path = self.store.path(f"jobs/{plan.plan_id}/manifest.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.store.transaction(plan.plan_id) as state:
            state["status"] = "publishing"
        # Every node has already committed; a late cancellation cannot erase its success.
        publish_document(self.store, path, manifest, _NeverCancel())
        actual = PipelineManifest.model_validate_json(read_document(path))
        if actual != manifest:
            raise JobError("ARTIFACT_CORRUPT", "流水线结果清单不一致")
        reference = ArtifactReference(
            kind="pipeline",
            identity=fingerprint(actual),
            path=path.relative_to(self.store.root).as_posix(),
        )
        with self.store.transaction(plan.plan_id) as state:
            state["artifact"] = reference.model_dump(mode="json")
            state["status"] = "succeeded"
        return True

    def recover(self, job_id):
        """Probe real per-attempt locks before treating any old dispatch as interrupted."""
        if not self._owner.acquired:
            raise JobError("UNAVAILABLE", "当前进程不拥有研究任务执行锁")
        plan = self.store.plan(job_id)
        state = self.store.get(job_id)
        for name, node in state["nodes"].items():
            if node["status"] not in {"running", "publishing", "cancelling"}:
                continue
            attempt = node["attempts"][-1]
            if not self._settle(plan, name, attempt, code="WORKER_INTERRUPTED"):
                return False
        self._finalize(plan)
        return True

    def _settle(self, plan, name, attempt, *, code=None, metrics=None):
        directory = self.store.attempt_directory(plan.plan_id, name, attempt["number"])
        directory.mkdir(parents=True, exist_ok=True)
        lock = MiningProcessLock(directory)
        try:
            lock.acquire(role="engine-recovery")
        except MiningProcessLockError:
            if code is not None:
                with self.store.transaction(plan.plan_id) as state:
                    current = state["nodes"][name]["attempts"][-1]
                    if current["token"] == attempt["token"] and (
                        code != "WORKER_INTERRUPTED" or "pending_error" not in current
                    ):
                        current["pending_error"] = code
            return False
        try:
            state, current = self.store.current(plan.plan_id, name, attempt["token"])
            code = current.get("pending_error", code)
            try:
                artifact = inspect_attempt(self.store, plan, name, attempt)
            except (JobError, TiveloError, ValueError, TypeError, OSError, KeyError) as exc:
                code = code or getattr(exc, "code", "ARTIFACT_CORRUPT")
                status = (
                    "interrupted"
                    if self._stop.is_set()
                    else (
                        "cancelled"
                        if state["cancel_requested"]
                        else ("interrupted" if code == "WORKER_INTERRUPTED" else "failed")
                    )
                )
                self.store.finish(
                    plan.plan_id, name, attempt["token"], status, error={"code": code}
                )
            else:
                from .preparation import release_export_reference

                release_export_reference(self.store, plan, name, artifact)
                self.store.finish(
                    plan.plan_id,
                    name,
                    attempt["token"],
                    "succeeded",
                    artifact=artifact.model_dump(mode="json"),
                    metrics=metrics,
                )
            return True
        finally:
            lock.release()

    def run_once(self):
        """Execute at most one node. Useful for a deterministic local maintenance runner."""
        with self._work:
            jobs = list(reversed(self.store.list(active=True, limit=128)))
            if not jobs or self._stop.is_set():
                return False
            job_id = jobs[0]["job_id"]
            if not self.recover(job_id):
                return False
            state, plan = self.store.get(job_id), self.store.plan(job_id)
            if state["status"] not in ACTIVE:
                return True
            if state["cancel_requested"]:
                self.store.cancel(job_id)
                return True
            graph = stages(plan)
            name = next(
                (
                    name
                    for name, (_, _, parents) in graph.items()
                    if state["nodes"][name]["status"] == "queued"
                    and all(state["nodes"][parent]["status"] == "succeeded" for parent in parents)
                ),
                None,
            )
            if name is None:
                return self._finalize(plan)
            cancel = _Cancellation(self, job_id)
            attempt = None
            try:
                with shared_heavy_job_limiter.slot("exclusive", cancel_event=cancel):
                    for parent in graph[name][2]:
                        verify_completed(self.store, plan, parent)
                    attempt = self.store.begin(job_id, name)
                    task = {
                        "kind": "engine_node",
                        "data_dir": self.data_dir,
                        "job_id": job_id,
                        "node_id": name,
                        "token": attempt["token"],
                        "owner_pid": os.getpid(),
                        "owner_started": psutil.Process().create_time(),
                    }
                    role, locked, _ = graph[name]
                    if role == "sync":
                        index = int(name.rsplit("_", 1)[1])
                        selected = locked.preparation.sources[index]
                        task["native_data"] = selected.plan.extension is not None or (
                            selected.source.kind == "tsp_daily" and selected.source.expected_provider == "tdxquant"
                        )
                    last_progress = [0.0, None]

                    def progress(message):
                        now = time.monotonic()
                        if (
                            now - last_progress[0] >= 0.1
                            or message.get("phase") != last_progress[1]
                            or message.get("done") == message.get("total")
                        ):
                            self.store.progress(job_id, name, attempt["token"], message)
                            last_progress[:] = [now, message.get("phase")]

                    result = self._worker(
                        task,
                        progress,
                        cancel,
                        rss_limit_bytes=plan.budget.rss_limit_bytes,
                        timeout_seconds=plan.budget.timeout_seconds,
                    )
                    self._settle(plan, name, attempt, metrics=result.get("worker"))
            except Exception as exc:  # noqa: BLE001 — worker/IPC boundary; persist only safe failure codes
                if attempt is None:
                    if isinstance(exc, (HeavyJobLimitTimeoutError, WorkBusyError)):
                        return False
                    if isinstance(exc, HeavyJobCancelledError):
                        if not self._stop.is_set():
                            self.store.cancel(job_id)
                        return False
                    with self.store.transaction(job_id) as state:
                        # Cancellation can arrive after admission/parent checks but before begin.
                        if state["cancel_requested"]:
                            state["status"] = "cancelled"
                        elif not self._stop.is_set():
                            state["status"] = "failed"
                            state["error"] = {"code": getattr(exc, "code", "RESOURCE_BUSY")}
                    return True
                # A committed marker wins a late cancel, failed IPC, or parent-side timeout.
                self._settle(
                    plan, name, attempt, code=getattr(exc, "code", "ENGINE_EXECUTION_FAILED")
                )
            self._finalize(plan)
            return True
