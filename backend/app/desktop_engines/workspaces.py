"""Select a release-specific workspace. The original core DB fences older launchers."""

import argparse
import json
import os
import secrets
import sqlite3
import sys
import time
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path

from .bundle import digest
from .store import DesktopStore
from .workspace_copy import quiescent_copy, sync_dir


def register_original(db, baseline):
    db.execute(
        "CREATE TABLE IF NOT EXISTS workspace_selection (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
        "original TEXT NOT NULL, active TEXT NOT NULL, revision INTEGER NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS workspace_hosts (baseline TEXT PRIMARY KEY, slot TEXT, image TEXT)"
    )
    db.execute("INSERT OR IGNORE INTO workspace_selection VALUES(1,?,?,0)", (baseline, baseline))
    db.execute("INSERT OR IGNORE INTO workspace_hosts VALUES(?,NULL,NULL)", (baseline,))


def current_workspace(anchor, db):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if not db.execute("SELECT 1 FROM sqlite_schema WHERE name='workspace_selection'").fetchone():
        if version != 1:
            raise ValueError("无法识别工作区选择记录")
        return {"original": None, "active": None, "revision": 0, "hosts": {}}
    original, active, revision = db.execute(
        "SELECT original,active,revision FROM workspace_selection WHERE singleton=1"
    ).fetchone()
    hosts = {
        key: {"slot": slot, "image": json.loads(image) if image else None}
        for key, slot, image in db.execute("SELECT baseline,slot,image FROM workspace_hosts")
    }
    if (
        version != (1 if original == active else 2)
        or original not in hosts
        or active not in hosts
        or hosts[original]["slot"] is not None
    ):
        raise ValueError("工作区选择记录与宿主索引不一致")
    return {"original": original, "active": active, "revision": revision, "hosts": hosts}


def _private_dir(parent, name, *, create=False):
    path = parent / name
    if path.is_symlink():
        raise ValueError("托管工作区路径含符号链接")
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    if not path.is_dir():
        raise ValueError("托管工作区不存在")
    return path


def _physical(anchor, state, host):
    if host == state["original"] or host is None:
        return anchor.data_dir
    slot = state["hosts"][host]["slot"]
    if (
        not isinstance(slot, str)
        or len(slot) != 64
        or any(c not in "0123456789abcdef" for c in slot)
    ):
        raise ValueError("托管工作区编号无效")
    slots = _private_dir(anchor.root, "workspaces")
    return _private_dir(_private_dir(slots, slot), "data")


def logical_root(directory):
    path = Path(directory).resolve()
    if (
        path.name == "data"
        and path.parent.parent.name == "workspaces"
        and path.parent.parent.parent.name == "tivelo-desktop"
    ):
        return path.parents[3]
    return path


def draft_installation(directory):
    path = Path(directory).resolve()
    if (
        path.name == "data"
        and path.parent.parent.name == "workspace-staging"
        and path.parent.parent.parent.name == "tivelo-desktop"
        and len(path.parent.name) == 64
        and not set(path.parent.name) - set("0123456789abcdef")
    ):
        return path.parents[3]
    return None


def _draft_database(draft):
    path = draft.parent / "verification.sqlite3"
    if any(Path(str(path) + suffix).is_symlink() for suffix in ("", "-journal", "-shm", "-wal")):
        raise ValueError("工作区核验记录路径无效")
    return path


def claim_draft(draft):
    """An orphaned validator holds a read lease. Late children must present the latest ticket."""
    ticket = secrets.token_hex(32)
    with closing(sqlite3.connect(_draft_database(draft), timeout=0, isolation_level=None)) as db:
        db.execute("BEGIN EXCLUSIVE")
        db.execute("CREATE TABLE IF NOT EXISTS verification(ticket TEXT NOT NULL)")
        db.execute("DELETE FROM verification")
        db.execute("INSERT INTO verification VALUES(?)", (ticket,))
        db.commit()
    return ticket


@contextmanager
def draft_reader(draft):
    ticket = os.environ.get("TSP_WORKSPACE_VERIFY_TICKET")
    if not ticket or draft_installation(draft) is None:
        raise ValueError("工作区核验仅限迁移中的私有副本")
    path = _draft_database(draft)
    with closing(
        sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0, isolation_level=None)
    ) as db:
        db.execute("BEGIN")
        if db.execute("SELECT ticket FROM verification").fetchall() != [(ticket,)]:
            raise ValueError("工作区核验已由新的迁移接管")
        yield


_ADOPT_RETRY_DELAYS = (0.05, 0.1, 0.2)


def _is_busy_error(exc):
    cause = exc.__cause__
    return isinstance(cause, sqlite3.OperationalError) and (
        getattr(cause, "sqlite_errorcode", 0) & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    )


def _can_adopt_inplace_upgrade(anchor, db, state):
    for folder in ("workspaces", "workspace-staging"):
        path = anchor.root / folder
        if path.exists() or path.is_symlink():
            return False
    return (
        state["original"] is not None
        and state["original"] == state["active"]
        and state["revision"] == 0
        and len(state["hosts"]) == 1
        and state["hosts"][state["original"]]["slot"] is None
        and state["hosts"][state["original"]]["image"] is None
        and db.execute("SELECT 1 FROM active LIMIT 1").fetchone() is None
        and db.execute("SELECT 1 FROM history LIMIT 1").fetchone() is None
        and db.execute("SELECT 1 FROM wheels LIMIT 1").fetchone() is None
    )


def _adopt_inplace_upgrade(anchor):
    """Re-bind an un-migrated desktop workspace after an in-place overwrite upgrade.

    Standard desktop upgrades (Windows Inno Setup overwrite, macOS .app replacement,
    Linux tarball update) replace the shipped executable in place while preserving
    ``data/``. When the workspace has never forked into managed copies and has no
    standalone installed engine overrides, promote the original host baseline once
    research queues are idle.
    """
    from app.services.mining_process_lock import MiningProcessLockError

    with anchor.guard(maintenance=True, coordinator=True) as db, ExitStack() as locks:
        state = current_workspace(anchor, db)
        if state["active"] == anchor.baseline_id:
            return
        if not _can_adopt_inplace_upgrade(anchor, db, state):
            raise ValueError("当前可执行文件与活动工作区不匹配，请用 workspace status 查看配套宿主")
        research = anchor.data_dir / "research/engine"
        if research.exists() or research.is_symlink():
            if (
                research.is_symlink()
                or research.parent.is_symlink()
                or (research / "owner").is_symlink()
            ):
                raise ValueError("研究工作区路径无效")
            try:
                _owner(research / "owner", locks)
            except MiningProcessLockError as exc:
                raise ValueError("研究工作区正在被其他进程使用，请退出后重试") from exc
        anchor.require_idle_jobs()
        previous = state["original"]
        db.execute("DELETE FROM workspace_hosts WHERE baseline=?", (previous,))
        db.execute("INSERT INTO workspace_hosts VALUES(?,NULL,NULL)", (anchor.baseline_id,))
        db.execute(
            "UPDATE workspace_selection SET original=?,active=? WHERE singleton=1",
            (anchor.baseline_id, anchor.baseline_id),
        )
        db.commit()


@contextmanager
def route(anchor, requested=None):
    if draft_installation(anchor.data_dir) is not None:
        # The caller holds draft_reader for the entire private worker lifetime.
        yield anchor
        return
    with ExitStack() as locks:
        db = locks.enter_context(anchor.guard(coordinator=True))
        state = current_workspace(anchor, db)
        if state["active"] is None:
            raise ValueError("旧工作区尚未登记宿主，请用 workspace migrate 指定原桌面程序")
        if state["active"] != anchor.baseline_id:
            if not _can_adopt_inplace_upgrade(anchor, db, state):
                raise ValueError("当前可执行文件与活动工作区不匹配，请用 workspace status 查看配套宿主")
            # Release the shared read lock before upgrading to an exclusive transaction.
            locks.close()
            for delay in (*_ADOPT_RETRY_DELAYS, None):
                try:
                    _adopt_inplace_upgrade(anchor)
                    break
                except ValueError as exc:
                    if not _is_busy_error(exc):
                        raise
                    try:
                        with anchor.guard(coordinator=True) as check_db:
                            if current_workspace(anchor, check_db)["active"] == anchor.baseline_id:
                                break
                    except ValueError as check_exc:
                        if not _is_busy_error(check_exc):
                            raise
                    if delay is None:
                        raise
                    time.sleep(delay)
            db = locks.enter_context(anchor.guard(coordinator=True))
            state = current_workspace(anchor, db)
            if state["active"] != anchor.baseline_id:
                raise ValueError("当前可执行文件与活动工作区不匹配，请用 workspace status 查看配套宿主")
        physical = _physical(anchor, state, state["active"])
        if requested is not None and Path(requested).resolve() not in (anchor.data_dir, physical):
            raise ValueError("指定的工作区副本当前未启用")
        if physical == anchor.data_dir:
            # Normal core maintenance takes its own exclusive transaction on this DB.
            # If activation wins this gap, the original runner sees the version-2 fence.
            locks.close()
            yield anchor
        else:
            yield DesktopStore(anchor.bundle, physical)


def _owner(lock_root, stack):
    from app.services.mining_process_lock import MiningProcessLock

    if (lock_root / ".mining_process.lock").is_symlink():
        raise ValueError("工作区进程锁路径无效")
    lock = MiningProcessLock(lock_root)
    lock.acquire(role="desktop-workspace-maintenance")
    stack.callback(lock.release)


def _quiesce(anchor, root, coordinator, locks):
    store = DesktopStore(anchor.bundle, root)
    db = (
        coordinator
        if root == anchor.data_dir
        else locks.enter_context(store.guard(maintenance=True))
    )
    _owner(root, locks)
    research = root / "research/engine"
    if research.exists():
        if (
            research.is_symlink()
            or research.parent.is_symlink()
            or (research / "owner").is_symlink()
        ):
            raise ValueError("研究工作区路径无效")
        _owner(research / "owner", locks)
    store.require_idle_jobs()
    return db


def _set_active(db, state, target):
    db.execute(
        "UPDATE workspace_selection SET active=?,revision=revision+1 WHERE singleton=1", (target,)
    )
    # An old launcher that read version 1 just before the exclusive transaction must also
    # fail its next SELECT against active, even when it supplied an explicit combination.
    if state["active"] == state["original"]:
        db.execute("ALTER TABLE active RENAME TO workspace_retired_active")
    elif target == state["original"]:
        db.execute("ALTER TABLE workspace_retired_active RENAME TO active")
    db.execute("PRAGMA user_version=1" if target == state["original"] else "PRAGMA user_version=2")


def _location(anchor, state):
    return {
        "original": state["original"],
        "active": state["active"],
        "revision": state["revision"],
        "current_executable": anchor.baseline_id,
        "retained": {host: str(_physical(anchor, state, host)) for host in state["hosts"]},
        "registered": state["active"] is not None,
    }


def manage(anchor, arguments):
    from .workspace_verify import certify, check_clone, record_deliveries, suspend_rewound_schedules

    parser = argparse.ArgumentParser(prog="TSP --engine workspace")
    sub = parser.add_subparsers(dest="operation", required=True)
    sub.add_parser("status")
    migrate = sub.add_parser("migrate")
    migrate.add_argument("--previous-executable", required=True, type=Path)
    restore = sub.add_parser("restore")
    restore.add_argument("--to", required=True)
    restore.add_argument("--executable", required=True, type=Path)
    for action in (migrate, restore):
        action.add_argument("--revision", required=True, type=int)
    args = parser.parse_args(arguments)
    if args.operation == "status":
        with anchor.guard(coordinator=True) as db:
            return _location(anchor, current_workspace(anchor, db))
    with anchor.guard(maintenance=True, coordinator=True) as db, ExitStack() as locks:
        state = current_workspace(anchor, db)
        if args.revision != state["revision"]:
            raise ValueError("工作区版本已改变，请刷新 revision")
        current = state["active"]
        source = _physical(anchor, state, current)
        source_db = _quiesce(anchor, source, db, locks)
        if args.operation == "restore":
            if args.to not in state["hosts"] or args.to == current:
                raise ValueError("请选择已保留的其他宿主和工作区")
            target = _physical(anchor, state, args.to)
            target_db = _quiesce(anchor, target, db, locks)
            proof = certify(args.executable, target, target_db, expected=args.to)
            if proof["image"] != state["hosts"][args.to]["image"]:
                raise ValueError("配套宿主映像与迁移时的记录不一致")
            record_deliveries(db, source)
            paused = suspend_rewound_schedules(db, target)
            _set_active(db, state, args.to)
            receipt = {
                **_location(anchor, current_workspace(anchor, db)),
                "paused_schedules": paused,
            }
            db.commit()
            return receipt
        previous = certify(args.previous_executable, source, source_db, expected=current)
        if current is None:
            current = previous["baseline"]
            register_original(db, current)
            state = current_workspace(anchor, db)
        candidate = anchor.baseline_id
        if candidate == current:
            # A previously unregistered alpha directory can be explicitly bound to the same image.
            if (
                state["revision"] != 0
                or previous["image"]
                != certify(Path(sys.executable), source, source_db, expected=candidate)["image"]
            ):
                raise ValueError("工作区已经使用此宿主或可执行映像不匹配")
            db.execute(
                "UPDATE workspace_hosts SET image=? WHERE baseline=?",
                (json.dumps(previous["image"]), current),
            )
            db.commit()
            return _location(anchor, state)
        if candidate in state["hosts"]:
            raise ValueError("该宿主已有自己的工作区，请使用 workspace restore")
        proof = certify(Path(sys.executable), None, None, expected=candidate)
        slot = digest(f"{current}/{state['revision']}/{candidate}".encode())
        staging = _private_dir(anchor.root, "workspace-staging", create=True)
        committed = _private_dir(anchor.root, "workspaces", create=True)
        pending, final = staging / slot, committed / slot
        if pending.is_symlink() or final.is_symlink():
            raise ValueError("工作区副本目录无效")
        if final.exists():
            # The directory rename is durable before the selector commits. Reconcile a lost commit.
            if pending.exists():
                raise ValueError("存在两个未确认的工作区副本")
            final.rename(pending)
        folder = _private_dir(staging, slot, create=True)
        draft = _private_dir(folder, "data", create=True)
        ticket = claim_draft(draft)
        with quiescent_copy(source, draft, hold_until=locks) as copied:
            checked = check_clone(draft, anchor.data_dir, ticket)
        record_deliveries(db, source)
        _draft_database(draft).unlink()
        folder.rename(final)
        sync_dir(committed)
        sync_dir(staging)
        db.execute(
            "UPDATE workspace_hosts SET image=? WHERE baseline=?",
            (json.dumps(previous["image"]), current),
        )
        db.execute(
            "INSERT INTO workspace_hosts VALUES(?,?,?)",
            (candidate, slot, json.dumps(proof["image"])),
        )
        _set_active(db, state, candidate)
        receipt = {
            **_location(anchor, current_workspace(anchor, db)),
            "copy": copied,
            "verified": checked,
        }
        db.commit()
        return receipt
