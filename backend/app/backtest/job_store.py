"""回测任务的磁盘持久化 (DurableJob)。

存储契约 (schema_version=1):
- 目录: {data_dir}/research/backtest_jobs/{job_id}.json
- job_id 严格白名单 (^[0-9A-Za-z][0-9A-Za-z_-]{0,63}$), 防路径穿越;
  内存 job_key 中的 `factor:` 命名空间换算为 `factor-`
- 原子写: 同目录 mkstemp + fsync + os.replace, 崩溃只可能留下 .tmp 孤儿
- 单文件 1 MiB 上限; 非有限数值经 json_safe 转 null
- lease_owner = "{pid}:{BOOT_ID}", 用于区分本进程与历史进程的记录

服务重启后由 job_recovery.recover_stale_backtest_jobs 把遗留的
queued/running (lease 不属于本进程) 标为 interrupted。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.json_safe import json_safe

SCHEMA_VERSION = 1
MAX_JOB_FILE_BYTES = 1024 * 1024  # 1 MiB
JOB_KINDS: tuple[str, ...] = ("strategy", "factor")
# 与 run_store 的 RUN_ID_RE 同口径: 首字符字母数字, 其后 _ -, 长度 1~64。
JOB_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{0,63}$")

# 进程启动标识: 同一进程多次 import 不变, 重启后必变。
BOOT_ID = uuid.uuid4().hex[:8]
LEASE_OWNER = f"{os.getpid()}:{BOOT_ID}"

_WRITE_LOCK = threading.RLock()


class JobIdError(ValueError):
    """job_id 不在白名单内 (路径穿越/非法字符)。"""


class JobTooLargeError(ValueError):
    """序列化后超过单文件 1 MiB 上限。"""


def check_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
        raise JobIdError(f"invalid job_id: {job_id!r}")
    return job_id


def job_id_for_key(job_key: str) -> str:
    """内存 job_key → 文件名安全的 job_id。

    - `factor:{md5}` → `factor-{md5}` (`:` 不允许出现在文件名白名单里)
    - 策略 key 为纯 md5 hex, 原样可用
    - 其余异常 key 退化为内容哈希, 保证确定性 (同一 key 稳定映射)
    """
    candidate = str(job_key).replace(":", "-")
    if JOB_ID_RE.match(candidate):
        return candidate
    digest = hashlib.sha1(str(job_key).encode("utf-8")).hexdigest()[:16]
    return f"job-{digest}"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class DurableJob(BaseModel):
    """磁盘上的回测任务记录; extra=ignore 保证旧文件/新版本字段互相兼容。"""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION
    job_id: str
    job_key: str
    kind: Literal["strategy", "factor"]
    status: str  # queued | running | interrupted | completed | failed | cancelled
    created_at: str = ""
    updated_at: str = ""
    heartbeat_at: str | None = None
    lease_owner: str = ""
    attempt: int = 0
    request: dict[str, Any] = {}
    # 只存最近一条进度, 不存全量列表 (体积受 1 MiB 约束)。
    progress: dict[str, Any] | None = None
    error: str | None = None
    run_id: str | None = None
    resumed_from_interrupt: bool = False


def new_job(
    *,
    job_key: str,
    kind: str,
    request: dict[str, Any] | None = None,
    status: str = "running",
) -> DurableJob:
    """新建本进程持有的 DurableJob (lease_owner=LEASE_OWNER, 时间戳=now)。"""
    now = now_iso()
    return DurableJob(
        job_id=job_id_for_key(job_key),
        job_key=job_key,
        kind=kind,  # type: ignore[arg-type]
        status=status,
        created_at=now,
        updated_at=now,
        heartbeat_at=now,
        lease_owner=LEASE_OWNER,
        attempt=0,
        request=request or {},
    )


class BacktestJobStore:
    """backtest_jobs 目录的读写入口。"""

    def __init__(self, data_dir: Path | str) -> None:
        self.job_dir = Path(data_dir) / "research" / "backtest_jobs"

    def _path(self, job_id: str) -> Path:
        return self.job_dir / f"{check_job_id(job_id)}.json"

    def serialize(self, job: DurableJob) -> bytes:
        """规范序列化: 非有限数值转 null + 体积上限检查。"""
        check_job_id(job.job_id)
        encoded = json.dumps(
            json_safe(job.model_dump()),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_JOB_FILE_BYTES:
            raise JobTooLargeError(
                f"job {job.job_id} 序列化后 {len(encoded)} 字节，超过 {MAX_JOB_FILE_BYTES} 上限"
            )
        return encoded

    def save(self, job: DurableJob) -> DurableJob:
        """原子覆盖写 (新建与状态更新同一路径)。

        取消是终态: 磁盘上已 cancelled 的任务不接受非 cancelled 回写
        (心跳 get→mutate→save 的竞态不得复活任务), 此时返回磁盘上的记录;
        目标文件存在但解析失败时同样保守不覆盖, 返回 incoming。
        cancelled → cancelled 仍允许 (更新 error/heartbeat 等)。
        """
        data = self.serialize(job)
        with _WRITE_LOCK:
            self.job_dir.mkdir(parents=True, exist_ok=True)
            target = self._path(job.job_id)
            if job.status != "cancelled" and target.exists():
                on_disk = self.get(job.job_id)
                if on_disk is None:
                    # 文件在但解析失败: 保守放弃本次覆盖。
                    return job
                if on_disk.status == "cancelled":
                    return on_disk
            fd, tmp_name = tempfile.mkstemp(dir=self.job_dir, prefix=f".{job.job_id}.", suffix=".tmp")
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, target)
            except BaseException:
                tmp_path.unlink(missing_ok=True)
                raise
        return job

    def get(self, job_id: str) -> DurableJob | None:
        """读取单个任务; 不存在或损坏 (旧 schema/半文件) 返回 None。"""
        path = self._path(job_id)
        if not path.exists():
            return None
        try:
            return DurableJob.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, TypeError, ValueError):
            return None

    def get_by_key(self, job_key: str) -> DurableJob | None:
        return self.get(job_id_for_key(job_key))

    def delete(self, job_id: str) -> bool:
        path = self._path(job_id)
        with _WRITE_LOCK:
            try:
                path.unlink()
            except FileNotFoundError:
                return False
            return True
