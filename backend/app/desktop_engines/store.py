"""Verified release combinations and offline maintenance for the frozen desktop."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from contextlib import closing, contextmanager, suppress
from pathlib import Path
from typing import Literal

from packaging.specifiers import SpecifierSet
from pydantic import Field, TypeAdapter, model_validator
from tivelo_contracts.base import Contract, Digest, canonical_bytes, parse_json
from tivelo_contracts.signatures import MAX_ENVELOPE, decode_base64url, verify_envelope

from .bundle import ENGINES, Wheel, digest, load_bundle, read_limited, version_tree, wheel_files


class CoreRelease(Contract):
    format: Literal["tivelo-desktop-core-release-v1"]
    baseline_id: Digest
    wheels: tuple[Wheel, Wheel]

    @model_validator(mode="after")
    def engine_pair(self):
        if tuple(item.name for item in self.wheels) != ENGINES:
            raise ValueError("核心发行清单需要按 data、backtest 顺序声明认证组合")
        return self

    @property
    def id(self):
        return digest(canonical_bytes(self))


class CoreTrust(Contract):
    format: Literal["tivelo-desktop-core-trust-v1"]
    keys: dict[str, str] = Field(max_length=32)


def verify_release(envelope):
    location = os.environ.get("TIVELO_CORE_TRUST_FILE")
    if not location:
        raise ValueError("没有配置桌面核心发行公钥")
    trust = CoreTrust.model_validate(
        parse_json(read_limited(Path(location), MAX_ENVELOPE).decode("utf-8"))
    )
    verified = verify_envelope(
        envelope,
        purpose="desktop-core-release",
        public_keys={kid: decode_base64url(key, size=32) for kid, key in trust.keys.items()},
    )
    return CoreRelease.model_validate(verified.claims)


def publish_file(path, payload):
    if not path.exists():
        with tempfile.TemporaryDirectory(prefix=".stage-", dir=path.parent) as temporary:
            staged = Path(temporary) / "artifact"
            with staged.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            with suppress(FileExistsError):
                os.link(staged, path)
    if path.is_symlink() or not path.is_file() or read_limited(path) != payload:
        raise ValueError("已有引擎制品与预期不同")


class DesktopStore:
    def __init__(self, bundle, data_dir):
        self.bundle = bundle
        self.data_dir = Path(data_dir).resolve()
        self.baseline_id, self.lock = load_bundle(bundle)
        self.bundled = {item.name: item for item in self.lock.wheels}
        self.baseline = CoreRelease(
            format="tivelo-desktop-core-release-v1",
            baseline_id=self.baseline_id,
            wheels=tuple(self.bundled[name] for name in ENGINES),
        )
        self.root = self.data_dir / "tivelo-desktop"
        self.root.mkdir(parents=True, exist_ok=True)
        for folder in (self.root, self.root / "artifacts", self.root / "certificates"):
            if folder.is_symlink():
                raise ValueError("核心发行存储不能使用符号链接")
            folder.mkdir(exist_ok=True)
        self.database = self.root / "combinations.sqlite3"
        if self.database.is_symlink():
            raise ValueError("核心发行状态路径无效")

    @contextmanager
    def guard(self, *, maintenance=False, coordinator=False):
        # Rollback-mode SQLite holds a shared OS lock across the complete process
        # lifetime (GUI, CLI or spawned worker). Process death releases it on all OSes.
        from .workspaces import draft_installation, logical_root, register_original

        managed = (
            logical_root(self.data_dir) != self.data_dir
            or draft_installation(self.data_dir) is not None
        )
        with closing(sqlite3.connect(self.database, timeout=3, isolation_level=None)) as db:
            try:
                if db.execute("PRAGMA journal_mode").fetchone() != ("delete",):
                    raise ValueError("核心发行状态库锁模式不受支持")
                if db.execute("PRAGMA user_version").fetchone() == (0,):
                    db.execute("BEGIN EXCLUSIVE")
                    if db.execute("PRAGMA user_version").fetchone() == (0,):
                        db.execute(
                            "CREATE TABLE IF NOT EXISTS active (baseline TEXT PRIMARY KEY, id TEXT NOT NULL, revision INTEGER NOT NULL)"
                        )
                        db.execute(
                            "CREATE TABLE IF NOT EXISTS history (baseline TEXT, id TEXT, PRIMARY KEY(baseline,id))"
                        )
                        db.execute(
                            "CREATE TABLE IF NOT EXISTS wheels (baseline TEXT, name TEXT, version TEXT, sha TEXT NOT NULL, PRIMARY KEY(baseline,name,version))"
                        )
                        if managed:
                            # A predecessor pointed directly at the physical copy must be fenced too.
                            db.execute("PRAGMA user_version=3")
                        else:
                            register_original(db, self.baseline_id)
                            db.execute("PRAGMA user_version=1")
                    db.commit()
                db.execute("PRAGMA busy_timeout=0")
                db.execute("BEGIN EXCLUSIVE" if maintenance else "BEGIN")
                supported = (3,) if managed else ((1, 2) if coordinator else (1,))
                if db.execute("PRAGMA user_version").fetchone()[0] not in supported:
                    raise ValueError("当前程序不支持此核心发行状态库版本")
                yield db
                if maintenance:
                    db.commit()
            except sqlite3.OperationalError as exc:
                if getattr(exc, "sqlite_errorcode", 0) & 255 in (
                    sqlite3.SQLITE_BUSY,
                    sqlite3.SQLITE_LOCKED,
                ):
                    raise ValueError(
                        "当前组合正在使用或维护，请在任务完成并退出工作台后重试"
                    ) from exc
                raise

    def active(self, db):
        row = db.execute(
            "SELECT id,revision FROM active WHERE baseline=?", (self.baseline_id,)
        ).fetchone()
        return row or (self.baseline.id, 0)

    def retained(self, db, identity):
        if identity == self.baseline.id:
            return True
        return (
            db.execute(
                "SELECT 1 FROM history WHERE baseline=? AND id=?", (self.baseline_id, identity)
            ).fetchone()
            is not None
        )

    def release(self, identity):
        TypeAdapter(Digest).validate_python(identity)
        if identity == self.baseline.id:
            return self.baseline
        path = self.root / "certificates" / (identity + ".json")
        if path.is_symlink():
            raise ValueError("核心发行清单路径无效")
        release = verify_release(read_limited(path, MAX_ENVELOPE))
        if release.id != identity or release.baseline_id != self.baseline_id:
            raise ValueError("发行清单与当前宿主或运行环境代际不兼容")
        return release

    def version_paths(self, release):
        paths = {}
        for wheel in release.wheels:
            if wheel == self.bundled[wheel.name]:
                path = self.bundle / wheel.filename
            else:
                path = self.root / "artifacts" / (wheel.sha256 + ".whl")
                if path.is_symlink():
                    raise ValueError("已安装的核心发行文件路径无效")
            paths[wheel.name] = version_tree(self.root, wheel, read_limited(path)).resolve()
        return paths

    def mount(self, release):
        paths = self.version_paths(release)
        if any(name.replace("-", "_") in sys.modules for name in ENGINES):
            raise ValueError("进程已加载引擎，切换版本需要新的进程")
        sys.dont_write_bytecode = True
        sys.path[:0] = list(map(str, paths.values()))
        return paths

    def stage(self, db, current, envelope, binary):
        candidate = verify_release(envelope)
        if candidate.baseline_id != self.baseline_id:
            raise ValueError("更新包面向其他宿主或运行环境代际")
        if any(
            not SpecifierSet(self.lock.engine_requirements[w.name]).contains(
                w.version, prereleases=True
            )
            for w in candidate.wheels
        ):
            raise ValueError("引擎发行版本超出宿主兼容范围")
        changed = [
            wheel
            for wheel, previous in zip(candidate.wheels, current.wheels, strict=True)
            if wheel != previous
        ]
        if len(changed) != 1:
            raise ValueError("每次安装必须只更新一个核心引擎，组合需要发行者认证")
        (wheel,) = changed
        previous = db.execute(
            "SELECT sha FROM wheels WHERE baseline=? AND name=? AND version=?",
            (self.baseline_id, wheel.name, wheel.version),
        ).fetchone()
        bundled = self.bundled[wheel.name]
        if (previous and previous[0] != wheel.sha256) or (
            bundled.version == wheel.version and bundled.sha256 != wheel.sha256
        ):
            raise ValueError("同一发行版本已有不同制品")
        if binary.name != wheel.filename:
            raise ValueError("所选文件不属于已签名的核心更新")
        data = read_limited(binary)
        wheel_files(wheel, data)
        publish_file(self.root / "artifacts" / (wheel.sha256 + ".whl"), data)
        publish_file(self.root / "certificates" / (candidate.id + ".json"), envelope)
        return candidate

    def activate(self, candidate, revision, *, restoring=False):
        from app.services.mining_process_lock import MiningProcessLock

        with self.guard(maintenance=True) as db:
            current, actual = self.active(db)
            if revision != actual:
                raise ValueError("核心组合状态已经改变，请刷新 revision")
            if self.release(candidate.id) != candidate or (
                restoring and not self.retained(db, candidate.id)
            ):
                raise ValueError("目标组合未获认证或尚未安装")
            if current == candidate.id:
                return actual
            self.version_paths(candidate)
            # Also honor a source-mode workbench using the same durable research workspace.
            folder = self.data_dir / "research" / "engine" / "owner"
            if any(
                path.is_symlink()
                for path in (
                    folder.parent.parent,
                    folder.parent,
                    folder,
                    folder / ".mining_process.lock",
                )
            ):
                raise ValueError("研究队列锁路径无效")
            owner = MiningProcessLock(folder)
            owner.acquire(role="desktop-engine-maintenance")
            try:
                self.require_idle_jobs()
                for wheel in (*self.baseline.wheels, *candidate.wheels):
                    db.execute(
                        "INSERT OR IGNORE INTO wheels VALUES (?,?,?,?)",
                        (self.baseline_id, wheel.name, wheel.version, wheel.sha256),
                    )
                    if db.execute(
                        "SELECT sha FROM wheels WHERE baseline=? AND name=? AND version=?",
                        (self.baseline_id, wheel.name, wheel.version),
                    ).fetchone() != (wheel.sha256,):
                        raise ValueError("同一发行版本已有不同制品")
                db.execute(
                    "INSERT OR IGNORE INTO history VALUES (?,?)", (self.baseline_id, candidate.id)
                )
                db.execute(
                    "INSERT OR REPLACE INTO active VALUES (?,?,?)",
                    (self.baseline_id, candidate.id, actual + 1),
                )
                db.commit()
            finally:
                owner.release()
            return actual + 1

    def require_idle_jobs(self):
        root = self.data_dir / "research" / "engine"
        checks = (
            (
                "jobs.sqlite3",
                "SELECT 1 FROM job_catalog WHERE json_extract(state,'$.status') "
                "IN ('queued','running','publishing','cancelling') LIMIT 1",
                "仍有排队或执行中的研究任务，请用原组合完成后再切换",
            ),
            (
                "schedules.sqlite3",
                "SELECT 1 FROM deliveries WHERE json_extract(state,'$.status') "
                "IN ('pending','prepared') AND plan IS NOT NULL LIMIT 1",
                "仍有锁定待提交的计划任务，请先用原组合完成派发",
            ),
        )
        for filename, sql, message in checks:
            path = root / filename
            if path.is_symlink() or root.is_symlink() or root.parent.is_symlink():
                raise ValueError("研究队列路径无效")
            if path.exists():
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                    if db.execute("PRAGMA user_version").fetchone() != (1,):
                        raise ValueError("无法确认此版本的研究队列是否已经清空")
                    if (
                        filename == "jobs.sqlite3"
                        and not db.execute(
                            "SELECT 1 FROM sqlite_schema WHERE name='job_catalog'"
                        ).fetchone()
                    ):
                        if db.execute(
                            "SELECT 1 FROM sqlite_schema WHERE name='pinned_jobs'"
                        ).fetchone():
                            raise ValueError("研究队列索引不完整")
                        sql = sql.replace("job_catalog", "jobs")
                    if db.execute(sql).fetchone():
                        raise ValueError(message)
