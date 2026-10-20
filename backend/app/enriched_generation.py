from __future__ import annotations

import json
import os
import threading
import time
import uuid
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

import polars as pl


class EnrichedGenerationUnavailableError(RuntimeError):
    """The enriched dataset has no stable generation available for readers."""


_WRITER_LOCKS_GUARD = threading.Lock()
_WRITER_LOCKS: dict[tuple[str, str], threading.RLock] = {}
_ACTIVE_PUBLICATIONS: weakref.WeakValueDictionary[str, EnrichedPublication] = (
    weakref.WeakValueDictionary()
)
# 认领冲突的退避间隔: 实时行情一轮发布只持有几毫秒到几百毫秒, 0.2s 足够错开
_CLAIM_RETRY_BACKOFF_S = 0.2
# 发布心跳: 长任务每写一个分区刷新一次 updated_at_ns, 避免被误判为僵死。
_HEARTBEAT_INTERVAL_S = 30.0
# 发布超时: 超过该时长没有任何心跳, 视为发布方已死或已放弃, 允许读取方接管。
# 没有这层兜底时, 一次异常中断留下的 publishing 标记会永久驻留 —— 所有读取
# 持续 fail-closed, 而停止实时行情 (运行期唯一频繁的写入方) 反而移除了自愈路径。
CLAIM_STALE_SECONDS = 600.0


def _marker_path(data_dir: Path, asset_type: str) -> Path:
    return Path(data_dir) / f".matrix_generation_{asset_type}.json"


def _writer_lock(data_dir: Path, asset_type: str) -> threading.RLock:
    key = (str(Path(data_dir).resolve()), asset_type)
    with _WRITER_LOCKS_GUARD:
        return _WRITER_LOCKS.setdefault(key, threading.RLock())


def _read_marker(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EnrichedGenerationUnavailableError(
            "enriched data generation marker is invalid"
        ) from exc
    if not isinstance(payload, dict):
        raise EnrichedGenerationUnavailableError(
            "enriched data generation marker is invalid"
        )
    return payload


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_marker(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _unlock_file(stream: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _try_lock_file(stream: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise EnrichedGenerationUnavailableError(
                "another enriched publication is active"
            ) from exc
        return
    import fcntl

    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise EnrichedGenerationUnavailableError(
            "another enriched publication is active"
        ) from exc


@contextmanager
def _exclusive_generation_lock(data_dir: Path, asset_type: str) -> Iterator[None]:
    lock_path = Path(data_dir) / f".matrix_generation_{asset_type}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with (
        _writer_lock(data_dir, asset_type),
        lock_path.open("a+b") as stream,
    ):
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        _try_lock_file(stream)
        try:
            yield
        finally:
            _unlock_file(stream)


def _process_is_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, PermissionError) as exc:
        # Windows 对不存在的 pid 返回 WinError 87 (ERROR_INVALID_PARAMETER),
        # 不会映射为 ProcessLookupError; 按存活处理会让孤儿发布锁永远无法恢复。
        if getattr(exc, "winerror", None) == 87:
            return False
        return True
    return True


def _ready_payload(generation: str) -> dict[str, Any]:
    return {
        "state": "ready",
        "generation": generation,
        "updated_at_ns": time.time_ns(),
    }


def _is_ready_payload(payload: dict[str, Any]) -> bool:
    generation = payload.get("generation")
    return (
        payload.get("state", "ready") == "ready"
        and isinstance(generation, str)
        and bool(generation)
    )


def _claim_age_seconds(payload: dict[str, Any]) -> float | None:
    """publishing 标记距上次心跳的秒数; 标记没有时间戳时返回 None。

    旧版本/手工写入的标记可能缺 updated_at_ns: 此时无法判断存活, 交由
    属主进程探测兜底, 不在此处直接判死。
    """
    updated = payload.get("updated_at_ns")
    if not isinstance(updated, int) or updated <= 0:
        return None
    return max(0.0, (time.time_ns() - updated) / 1_000_000_000)


def _claim_is_stale(payload: dict[str, Any]) -> bool:
    age = _claim_age_seconds(payload)
    return age is not None and age >= CLAIM_STALE_SECONDS


def _publication_claim_is_running(payload: dict[str, Any]) -> bool:
    """标记指向的发布是否仍在推进: 进程内活跃对象存在, 或属主进程仍存活。

    owner_pid 等于当前进程但无活跃对象视为可接管 (同进程上一次尝试的遗留),
    与写入方 recover 接管的判定一致。

    进程内有活跃对象时一律视为在推进 —— 长任务可能在两批写入之间计算
    十几分钟, 仅凭心跳时间戳会误判为僵死并放行第二个写入方。属主进程存活
    但没有活跃对象时, 心跳超时才说明它确实已停止推进 (异常中断后对象仍被
    traceback 引用的情形)。
    """
    if _ACTIVE_PUBLICATIONS.get(str(payload.get("publication_id"))) is not None:
        return True
    if _claim_is_stale(payload):
        return False
    owner_pid = payload.get("owner_pid")
    return owner_pid != os.getpid() and _process_is_alive(owner_pid)


def _orphaned_publishing_claim(payload: dict[str, Any]) -> bool:
    """标记是否指向确定已死的发布: 属主是其他进程且已退出, 或心跳已超时。

    owner_pid 等于当前进程但无活跃对象且心跳未超时时保守不判孤儿 ——
    同进程异常遗留的 publishing 标记意味着磁盘可能处于部分修改状态
    (如清库删了一半), 读取方恢复 ready 会放行读取半修改数据; 必须由下一个
    写入方接管重发布。心跳超时后数据已不可能继续推进, 此时允许恢复 (下面
    get_enriched_generation 会换新 generation, 让按代缓存整体失效)。
    """
    if _ACTIVE_PUBLICATIONS.get(str(payload.get("publication_id"))) is not None:
        return False
    owner_pid = payload.get("owner_pid")
    if (
        isinstance(owner_pid, int)
        and owner_pid > 0
        and owner_pid != os.getpid()
        and not _process_is_alive(owner_pid)
    ):
        return True
    return _claim_is_stale(payload)


def get_enriched_generation(
    data_dir: Path,
    asset_type: str = "stock",
    *,
    initialize: bool = True,
) -> str:
    path = _marker_path(data_dir, asset_type)
    payload = _read_marker(path)
    if payload is None:
        if not initialize:
            raise EnrichedGenerationUnavailableError(
                "enriched data generation marker is unavailable"
            )
    elif _is_ready_payload(payload):
        return payload["generation"]
    elif not _orphaned_publishing_claim(payload):
        # 发布仍在推进, 或为同进程异常遗留 (无法证明属主已死): 读取保持 fail-closed。
        raise EnrichedGenerationUnavailableError(
            "enriched data is being published; retry after the update finishes"
        )
    # 指向已死发布的僵死标记: 在独占锁内二次确认后恢复 ready。
    with _exclusive_generation_lock(data_dir, asset_type):
        payload = _read_marker(path)
        if payload is None:
            generation = uuid.uuid4().hex
            _write_marker(path, _ready_payload(generation))
            return generation
        if _is_ready_payload(payload):
            return payload["generation"]
        if not _orphaned_publishing_claim(payload):
            raise EnrichedGenerationUnavailableError(
                "enriched data is being published; retry after the update finishes"
            )
        # 属主已死的 publishing 标记永远不会 commit, 读取方持续失败直到某个
        # 写入方碰巧接管 (dev 热重载杀掉发布进程即产生这种孤儿)。恢复为 ready
        # 并换新 generation: 磁盘可能残留部分替换的文件, 新 generation 让按代
        # 缓存全部失效, 避免把混合状态混入旧快照 —— 与写入方 recover 接管同语义。
        generation = uuid.uuid4().hex
        _write_marker(path, _ready_payload(generation))
        return generation


def recover_stale_publications(
    data_dir: Path,
    asset_types: tuple[str, ...] = ("stock", "etf"),
) -> list[str]:
    """启动自检: 恢复上次进程异常退出留下的 publishing 标记。

    本进程启动时不可能有活跃发布对象, 因此标记指向的属主要么是上一个
    (已退出) 的进程, 要么是同进程残留 —— 后者由心跳超时判定。命中的资产
    类型走 get_enriched_generation 的锁内二次确认 + 换新 generation 恢复。

    返回实际恢复的资产类型列表; 仍有进程在推进的发布不受影响。
    """
    recovered: list[str] = []
    for asset_type in asset_types:
        path = _marker_path(data_dir, asset_type)
        try:
            payload = _read_marker(path)
        except EnrichedGenerationUnavailableError:
            continue
        if payload is None or _is_ready_payload(payload):
            continue
        if _ACTIVE_PUBLICATIONS.get(str(payload.get("publication_id"))) is not None:
            continue
        if not _orphaned_publishing_claim(payload):
            continue
        try:
            get_enriched_generation(data_dir, asset_type)
        except EnrichedGenerationUnavailableError:
            continue
        recovered.append(asset_type)
    return recovered


def enriched_publication_incomplete(
    data_dir: Path,
    asset_type: str = "stock",
) -> bool:
    try:
        payload = _read_marker(_marker_path(data_dir, asset_type))
    except EnrichedGenerationUnavailableError:
        return True
    if payload is None:
        return False
    return (
        payload.get("state", "ready") != "ready"
        or not isinstance(payload.get("generation"), str)
        or not payload["generation"]
    )


def bump_enriched_generation(data_dir: Path, asset_type: str = "stock") -> str:
    path = _marker_path(data_dir, asset_type)
    with _exclusive_generation_lock(data_dir, asset_type):
        current = _read_marker(path)
        if current is not None and current.get("state", "ready") != "ready":
            raise EnrichedGenerationUnavailableError(
                "cannot bump an incomplete enriched publication"
            )
        generation = uuid.uuid4().hex
        _write_marker(path, _ready_payload(generation))
        return generation


class EnrichedPublication:
    """Publish one logical enriched write batch under a stable generation token."""

    def __init__(
        self,
        data_dir: Path,
        asset_type: str = "stock",
        *,
        recover: bool = False,
        claim_wait_s: float = 0.0,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.asset_type = asset_type
        self.recover = recover
        self.claim_wait_s = max(0.0, float(claim_wait_s))
        self._publishing = False
        self._changed = False
        self._base_generation: str | None = None
        self._publication_id = uuid.uuid4().hex

    def _locked_claim(self, action):
        """在独占锁内认领发布并执行 action; 认领冲突时短暂让出重试。

        认领冲突的唯一常见来源是同进程的实时行情刷新 (每轮几毫秒到几百毫秒
        持有发布)。长任务 (盘后/手动全量同步) 撞上它就该等一会儿而不是整批
        失败 —— 现场表现为任务面板直接报 "another enriched publication is
        active", 同步 8 分钟后前功尽弃。

        等待必须在锁外: 持 flock 睡眠会把对方也堵死, 变成死锁。因此循环里
        每次重新获取锁, 只对「别的发布仍在推进」这一种可恢复错误重试; 属主
        已死仍由 recover 接管, 其他错误立即抛出。
        """
        deadline = time.monotonic() + self.claim_wait_s
        while True:
            try:
                with _exclusive_generation_lock(self.data_dir, self.asset_type):
                    self._claim_or_verify()
                    return action()
            except EnrichedGenerationUnavailableError as exc:
                if "another enriched publication is active" not in str(exc):
                    raise
                if time.monotonic() >= deadline:
                    raise
                time.sleep(_CLAIM_RETRY_BACKOFF_S)

    def begin(self) -> None:
        self._locked_claim(lambda: None)

    def mark_changed(self) -> None:
        if not self._publishing:
            raise RuntimeError("enriched publication has not started")
        self._changed = True

    def heartbeat(self) -> None:
        """发布进行中主动续期, 供长计算阶段调用。

        全量重建等长任务可能连续十几分钟只在算、不写分区; 期间没有任何
        write_parquet 触发续期, 别的进程会把心跳超时误判为「发布已死」并接手。
        拿不到锁时直接跳过 —— 下一次写入会再续期, 不能因为续期失败打断任务。
        """
        if not self._publishing:
            return
        path = _marker_path(self.data_dir, self.asset_type)
        try:
            with _exclusive_generation_lock(self.data_dir, self.asset_type):
                current = _read_marker(path)
                if (
                    current is not None
                    and current.get("publication_id") == self._publication_id
                ):
                    self._refresh_heartbeat(path, current)
        except EnrichedGenerationUnavailableError:
            return

    def abandon(self) -> None:
        """放弃本次发布, 尽力把标记恢复为可读状态。

        调用方在异常路径依赖它, 因此这里绝不能因为「已有分区写入」而直接
        早退: 那会把 publishing 标记永久留在磁盘上, 后续所有读写都 fail-closed
        (现场表现: 停掉实时行情后仍然一直报正在发布)。

        - 没有任何分区落盘: 数据未变化, 恢复 base generation 为 ready。
        - 已写入部分分区: 数据可能半新半旧, 保持 fail-closed 不写 ready,
          由心跳超时 (CLAIM_STALE_SECONDS) 后的读取方接管并换新 generation;
          同进程的下一个写入方仍可立即接管 (对象已从活跃表移除)。
        - 锁竞争拿不到: 同样交给超时机制兜底, 但必须复位内存态, 否则本对象
          会一直自认为「发布中」。
        """
        if not self._publishing:
            self._changed = False
            return
        release_ready = not self._changed
        path = _marker_path(self.data_dir, self.asset_type)
        try:
            if release_ready:
                deadline = time.monotonic() + max(self.claim_wait_s, 2.0)
                while True:
                    try:
                        with _exclusive_generation_lock(self.data_dir, self.asset_type):
                            current = _read_marker(path)
                            if (
                                current is not None
                                and current.get("publication_id") == self._publication_id
                            ):
                                _write_marker(
                                    path, _ready_payload(str(self._base_generation))
                                )
                        break
                    except EnrichedGenerationUnavailableError:
                        if time.monotonic() >= deadline:
                            break
                        time.sleep(_CLAIM_RETRY_BACKOFF_S)
        finally:
            self._publishing = False
            self._changed = False
            # 对象已放弃: 从活跃表移除, 允许同一进程内的下一个写入方
            # 通过 recover 接管 (否则弱引用表会一直把它当作「仍在发布」)。
            _ACTIVE_PUBLICATIONS.pop(self._publication_id, None)

    def write_parquet(self, df: pl.DataFrame, out: Path) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        temporary = out.with_name(f".{out.name}.{uuid.uuid4().hex}.tmp")

        def _replace() -> None:
            os.replace(temporary, out)
            _fsync_directory(out.parent)
            self._changed = True

        try:
            df.write_parquet(temporary)
            with temporary.open("r+b") as stream:
                stream.flush()
                os.fsync(stream.fileno())
            self._locked_claim(_replace)
        finally:
            temporary.unlink(missing_ok=True)

    def commit(self) -> str | None:
        if not self._changed:
            if self._publishing:
                # begin() 之后没有任何分区写入 (调用方提前 return 或全部无变化):
                # 必须让出认领, 否则 publishing 标记会一直挂着, 后续所有发布都
                # 报 "another enriched publication is active"。
                self.abandon()
            return None
        if not self._publishing:
            # 本次是同一发布对象的重复提交: 上一次 commit 已落盘并复位
            # _publishing, 标记也回到了 ready (ready payload 不含
            # publication_id)。写入方会把一个发布对象复用到多个分区
            # (如 ETF enriched 按完整本地历史一次写 245+ 个交易日分区),
            # 其中内容无变化的分区会直接走到这里 —— 此时没有任何待提交
            # 内容, 必须幂等返回, 否则会误判为 "ownership lost" 并打断
            # 整批写入。
            self._changed = False
            return None
        path = _marker_path(self.data_dir, self.asset_type)
        with _exclusive_generation_lock(self.data_dir, self.asset_type):
            current = _read_marker(path)
            if current is None or current.get("publication_id") != self._publication_id:
                raise EnrichedGenerationUnavailableError(
                    "enriched publication ownership was lost"
                )
            generation = uuid.uuid4().hex
            _write_marker(path, _ready_payload(generation))
        self._publishing = False
        # 待发布内容已全部落盘, 复位脏标记: 多日期分区写入复用同一发布对象,
        # 无变化分区会再调 commit() (空提交) — 不复位会以过期脏标记命中
        # ownership 校验而误抛 "ownership was lost" (Issue #417)。
        # 之后的写入会经 _claim_or_verify 重新认领并再次置位。
        self._changed = False
        return generation

    def _refresh_heartbeat(self, path: Path, current: dict[str, Any]) -> None:
        """发布进行中定期刷新标记时间戳, 供读取方区分「仍在推进」与「已放弃」。

        只在超过心跳间隔时重写, 避免每个分区都产生一次额外 fsync。
        """
        now_ns = time.time_ns()
        last = current.get("updated_at_ns")
        if (
            isinstance(last, int)
            and last > 0
            and now_ns - last < _HEARTBEAT_INTERVAL_S * 1_000_000_000
        ):
            return
        generation = current.get("generation")
        _write_marker(path, {
            "state": "publishing",
            "generation": (
                generation
                if isinstance(generation, str) and generation
                else self._base_generation
            ),
            "publication_id": self._publication_id,
            "owner_pid": os.getpid(),
            "updated_at_ns": now_ns,
        })

    def _claim_or_verify(self) -> None:
        path = _marker_path(self.data_dir, self.asset_type)
        try:
            current = _read_marker(path)
        except EnrichedGenerationUnavailableError:
            if not self.recover:
                raise
            current = None
        if self._publishing:
            if current is None or current.get("publication_id") != self._publication_id:
                raise EnrichedGenerationUnavailableError(
                    "enriched publication ownership was lost"
                )
            self._refresh_heartbeat(path, current)
            return
        _ACTIVE_PUBLICATIONS[self._publication_id] = self
        if current is not None and current.get("state", "ready") != "ready":
            if _publication_claim_is_running(current):
                raise EnrichedGenerationUnavailableError(
                    "another enriched publication is active"
                )
            if not self.recover:
                raise EnrichedGenerationUnavailableError(
                    "another enriched publication is incomplete"
                )
        generation = None if current is None else current.get("generation")
        if not isinstance(generation, str) or not generation:
            generation = uuid.uuid4().hex
        self._base_generation = generation
        _write_marker(path, {
            "state": "publishing",
            "generation": generation,
            "publication_id": self._publication_id,
            "owner_pid": os.getpid(),
            "updated_at_ns": time.time_ns(),
        })
        self._publishing = True
