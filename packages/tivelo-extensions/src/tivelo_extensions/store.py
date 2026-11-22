"""Immutable candidate packages. Staging never changes the active engine combination."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from tivelo_contracts.base import canonical_bytes, fingerprint
from tivelo_contracts.engine_releases import compatibility, release_trust, verify_release
from tivelo_contracts.errors import TiveloError
from tivelo_contracts.execution import ExtensionPin
from tivelo_contracts.signatures import MAX_ENVELOPE

from .activation import materialize_native, probe_release, verify_local_lease
from .installation import EngineInstallation


class EnginePackageStore:
    def __init__(self, data_dir: Path):
        self.root = Path(data_dir).resolve()

    def _database(self, *, create: bool):
        path = self.root
        for component in ("research", "engine", "extensions"):
            path /= component
            if path.is_symlink():
                raise ValueError("引擎扩展目录不可为符号链接")
            if create:
                path.mkdir(exist_ok=True)
        file = path / "candidates.sqlite3"
        if any(
            Path(str(file) + ending).is_symlink() for ending in ("", "-journal", "-wal", "-shm")
        ):
            raise ValueError("引擎候选索引的路径无效")
        if not create and not file.exists():
            return None
        connection = sqlite3.connect(
            file.as_uri() + ("?mode=rwc" if create else "?mode=ro"), uri=True, timeout=10
        )
        try:
            if create:
                connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE" if create else "BEGIN")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise ValueError("引擎候选索引版本不受支持")
            if create:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS candidates (id TEXT, version TEXT, digest TEXT UNIQUE NOT NULL, kid TEXT NOT NULL, manifest BLOB NOT NULL, archive BLOB NOT NULL, PRIMARY KEY(id,version))"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS capabilities (id TEXT PRIMARY KEY, package_id TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS active_releases (package_id TEXT PRIMARY KEY, active_digest TEXT, previous_digest TEXT, generation INTEGER NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS approved_releases (digest TEXT PRIMARY KEY)"
                )
                connection.execute("PRAGMA user_version=2")
            elif version == 0:
                # A first writer may be creating the schema in an uncommitted transaction.
                connection.close()
                return None
        except BaseException:
            connection.close()
            raise
        return connection

    @staticmethod
    def _state(connection, release):
        if connection is None:
            return 0, None, []
        revision = connection.execute("SELECT count(*) FROM candidates").fetchone()[0]
        row = connection.execute(
            "SELECT digest FROM candidates WHERE id=? AND version=?", (release.id, release.version)
        ).fetchone()
        provided = tuple(cap.id for cap in release.provides)
        placeholders = ",".join("?" for _ in provided)
        conflicts = connection.execute(
            f"SELECT id FROM capabilities WHERE package_id<>? AND id IN ({placeholders}) ORDER BY id",
            (release.id, *provided),
        ).fetchall()
        return revision, row[0] if row else None, [item[0] for item in conflicts]

    @staticmethod
    def _preview(release, kid, digest, trust, installed, issues, state):
        generation, existing, conflicts = state
        errors = list(issues)
        if existing and existing != digest:
            errors.append("同一扩展版本已经绑定其他制品")
        if conflicts:
            errors.append("声明的能力已由其他候选扩展持有")
        revision = hashlib.sha256(
            json.dumps(
                [generation, digest, kid, fingerprint(trust), installed, errors],
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        return {
            "manifest": release.model_dump(mode="json"),
            "kid": kid,
            "digest": digest,
            "revision": revision,
            "errors": errors,
            "state": "staged" if existing == digest else "inspected",
        }

    def inspect(self, data: bytes) -> dict:
        trust = release_trust()
        release, kid = verify_release(data, trust)
        installed, issues = compatibility(release)
        db = self._database(create=False)
        try:
            state = self._state(db, release)
        finally:
            if db is not None:
                db.close()
        return self._preview(
            release, kid, hashlib.sha256(data).hexdigest(), trust, installed, issues, state
        )

    def stage(self, data: bytes, expected_digest: str, revision: str) -> dict:
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected_digest:
            raise ValueError("上传内容与预检时的摘要不同")
        trust = release_trust()
        release, kid = verify_release(data, trust)
        installed, issues = compatibility(release)
        if issues:
            raise ValueError(issues[0])
        with closing(self._database(create=True)) as db, db:
            state = self._state(db, release)
            preview = self._preview(release, kid, digest, trust, installed, issues, state)
            if preview["errors"]:
                raise ValueError(preview["errors"][0])
            if preview["state"] == "staged":
                stored = db.execute(
                    "SELECT archive FROM candidates WHERE digest=?", (digest,)
                ).fetchone()[0]
                if hashlib.sha256(stored).hexdigest() != digest:
                    raise ValueError("已保存的候选制品校验失败")
                return preview
            if preview["revision"] != revision:
                raise ValueError("候选状态或信任策略已变化，请重新预检")
            db.execute(
                "INSERT INTO candidates VALUES (?,?,?,?,?,?)",
                (
                    release.id,
                    release.version,
                    digest,
                    kid,
                    canonical_bytes(release),
                    data,
                ),
            )
            db.executemany(
                "INSERT OR IGNORE INTO capabilities VALUES (?,?)",
                [(cap.id, release.id) for cap in release.provides],
            )
            return self._preview(
                release, kid, digest, trust, installed, issues, self._state(db, release)
            )

    def list_staged(self) -> list[dict]:
        db = self._database(create=False)
        if db is None:
            return []
        with closing(db):
            active = {}
            if db.execute("PRAGMA user_version").fetchone()[0] == 2:
                active = {
                    name: (current, previous, generation)
                    for name, current, previous, generation in db.execute(
                        "SELECT package_id,active_digest,previous_digest,generation FROM active_releases"
                    )
                }
            result = []
            for name, version, digest, kid, manifest in db.execute(
                "SELECT id,version,digest,kid,manifest FROM candidates ORDER BY id,version"
            ):
                item = dict(id=name, version=version, digest=digest, kid=kid, state="staged")
                claims = json.loads(manifest)
                if claims["kind"] == "data-provider":
                    item["provider_ids"] = [cap["id"] for cap in claims["provides"]]
                if name in active:
                    current, previous, generation = active[name]
                    item["generation"] = generation
                    if current == digest:
                        item.update(
                            state="active",
                            rollback_digest=previous,
                            license_kind=claims["license_kind"],
                        )
                    elif previous == digest:
                        item["state"] = "previous"
                result.append(item)
            return result

    def check_workspace_versions(self):
        """Recheck active native versions in an offline copy before its first launch."""
        result = []
        for item in self.list_staged():
            if item["state"] != "active":
                continue
            try:
                with closing(self._database(create=False)) as db:
                    data, release = self._verified(db, item["digest"])
                probe_release(data, release)
            except (ValueError, TiveloError) as exc:
                self.switch(item["digest"], "disable", item["generation"])
                result.append(
                    {"id": item["id"], "digest": item["digest"], "state": "disabled", "reason": str(exc)}
                )
            else:
                result.append({"id": item["id"], "digest": item["digest"], "state": "active"})
        return result

    @staticmethod
    def _selection(db, package):
        if db.execute("PRAGMA user_version").fetchone()[0] == 2:
            state = db.execute(
                "SELECT active_digest,previous_digest,generation FROM active_releases WHERE package_id=?",
                (package,),
            ).fetchone()
            if state:
                return state
        return None, None, 0

    @staticmethod
    def _verified(db, digest):
        row = db.execute(
            "SELECT id,archive,kid FROM candidates WHERE digest=?", (digest,)
        ).fetchone()
        if not row or hashlib.sha256(row[1]).hexdigest() != digest:
            raise ValueError("引擎制品缺失或摘要校验失败")
        release, kid = verify_release(row[1], release_trust())
        if (release.id, kid) != (row[0], row[2]):
            raise ValueError("引擎制品身份与保存记录不一致")
        _, errors = compatibility(release)
        if errors:
            raise ValueError(errors[0])
        return row[1], release

    def switch(self, digest, operation, generation):
        db = self._database(create=False)
        if db is None:
            raise ValueError("找不到已保存的引擎版本")
        with closing(db):
            row = db.execute("SELECT id FROM candidates WHERE digest=?", (digest,)).fetchone()
            if not row:
                raise ValueError("找不到已保存的引擎版本")
            package = row[0]
            current, previous, seen = self._selection(db, package)
            if generation != seen:
                raise ValueError("活动版本已变化，请刷新扩展清单")
            if operation == "activate":
                target = digest
            elif operation == "rollback" and digest == current and previous:
                target = previous
            elif operation == "disable" and digest == current:
                target = None
            else:
                raise ValueError("当前版本无法执行此操作")
            if target:
                data, release = self._verified(db, target)
            if target == current:
                return {"id": package, "active_digest": current, "generation": seen}
        if target:
            probe_release(data, release)
            installation = EngineInstallation(self.root)
            credential = (
                installation.saved_lease(package) if release.license_kind == "commercial" else None
            )
            if credential is not None:
                verify_local_lease(data, release, self.root, credential[0])
        with closing(self._database(create=True)) as db, db:
            if self._selection(db, package) != (current, previous, seen):
                raise ValueError("自检期间活动版本发生变化，请重新操作")
            if target:
                self._verified(db, target)
                if (
                    release.license_kind == "commercial"
                    and installation.saved_lease(package) != credential
                ):
                    raise ValueError("版本自检期间使用凭证发生变化，请重新操作")
                db.execute("INSERT OR IGNORE INTO approved_releases(digest) VALUES(?)", (target,))
            db.execute(
                "INSERT OR REPLACE INTO active_releases(package_id,active_digest,previous_digest,generation) VALUES(?,?,?,?)",
                (package, target, current, seen + 1),
            )
        return {"id": package, "active_digest": target, "generation": seen + 1}

    def uninstall(self, package, generation, expected_digests):
        """Remove every version approved by the caller's current installation snapshot."""
        with closing(self._database(create=True)) as db, db:
            _, _, seen = self._selection(db, package)
            digests = tuple(
                row[0] for row in db.execute(
                    "SELECT digest FROM candidates WHERE id=? ORDER BY digest", (package,)
                )
            )
            if not digests:
                raise ValueError("找不到要卸载的引擎扩展")
            if seen != generation or digests != tuple(sorted(expected_digests)):
                raise ValueError("已安装版本发生变化，请刷新扩展清单后重新确认")
            if any(not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in digests):
                raise ValueError("引擎版本摘要无效")
            self._remove_version_cache(digests)
            db.execute(
                "DELETE FROM approved_releases WHERE digest IN (SELECT digest FROM candidates WHERE id=?)",
                (package,),
            )
            db.execute("DELETE FROM capabilities WHERE package_id=?", (package,))
            db.execute("DELETE FROM candidates WHERE id=?", (package,))
            # Retain the revision so a stale confirmation cannot match a fresh reinstall.
            db.execute(
                "INSERT OR REPLACE INTO active_releases VALUES(?,NULL,NULL,?)",
                (package, seen + 1),
            )
        return {"id": package, "removed": len(digests)}

    def _remove_version_cache(self, digests):
        versions = self.root / "research/engine/extensions/versions"
        if versions.is_symlink() or (versions.exists() and not versions.is_dir()):
            raise ValueError("引擎版本目录无效")
        paths = [(versions / digest, versions / (".uninstall-" + digest)) for digest in digests]
        if any(
            path.is_symlink() or (path.exists() and not path.is_dir())
            for pair in paths for path in pair
        ):
            raise ValueError("引擎版本路径无效")
        for target, pending in paths:
            if pending.exists():
                shutil.rmtree(pending)
            if target.exists():
                # On cleanup failure the archived package can still recreate its cache.
                target.rename(pending)
                shutil.rmtree(pending)

    @staticmethod
    def _pin(release, digest, provider_id, kind):
        cap = next(
            (c for c in release.provides if c.id == provider_id and c.kind == kind), None
        )
        if not cap:
            raise ValueError("锁定的制品不提供指定能力")
        entry = next(item for item in release.files if item.path == release.entrypoint)
        return ExtensionPin(
            extension_id=release.id,
            version=release.version,
            kind=cap.kind,
            capability_id=cap.id,
            feature_id=cap.feature_id,
            archive_sha256=digest,
            entry_sha256=entry.sha256,
            api_version=cap.api_version,
            runtime_generation=release.requires.runtime_generation,
        )

    def resolve(self, provider_id, *, kind="price_view"):
        db = self._database(create=False)
        if db is not None:
            with closing(db):
                owner = db.execute(
                    "SELECT package_id FROM capabilities WHERE id=?", (provider_id,)
                ).fetchone()
                digest = self._selection(db, owner[0])[0] if owner else None
                if digest:
                    _, release = self._verified(db, digest)
                    return self._pin(release, digest, provider_id, kind)
        raise ValueError("指定能力缺少已启用的引擎扩展")

    def price_choices(self):
        """Read public capability status without importing native code or exposing device identity."""
        choices = {
            f"tivelo.{mode}": dict(provider_id=f"tivelo.{mode}", mode=mode, state="available")
            for mode in ("raw", "qfq", "hfq")
        }
        choices["tivelo.pro.dynamic"] = dict(
            provider_id="tivelo.pro.dynamic", mode="extension", state="not_installed"
        )
        db = self._database(create=False)
        if db is None:
            return list(choices.values())
        installation = EngineInstallation(self.root)
        now = datetime.now(UTC)
        with closing(db):
            for (package,) in db.execute("SELECT DISTINCT id FROM candidates ORDER BY id"):
                active, _, _ = self._selection(db, package)
                release = None
                state = "disabled"
                if active:
                    try:
                        _, release = self._verified(db, active)
                        state = "available"
                    except ValueError:
                        state = "incompatible"
                manifests = db.execute(
                    "SELECT manifest FROM candidates WHERE id=? ORDER BY version", (package,)
                )
                known = {
                    cap["id"]
                    for (raw,) in manifests
                    for cap in json.loads(raw)["provides"]
                    if cap["kind"] == "price_view"
                }
                selected = (
                    {cap.id for cap in release.provides if cap.kind == "price_view"}
                    if release
                    else set()
                )
                if release and release.license_kind == "commercial":
                    try:
                        lease = installation.lease_summary(package)
                        if lease is None:
                            state = "needs_license"
                        elif lease.lease_expires_at <= now or (
                            lease.usage_until and lease.usage_until <= now
                        ):
                            state = "license_expired"
                    except (OSError, ValueError, sqlite3.DatabaseError):
                        state = "incompatible"
                for capability in sorted(known):
                    choices[capability] = dict(
                        provider_id=capability,
                        mode="extension",
                        extension_id=package,
                        version=release.version if capability in selected else None,
                        state=state if not release or capability in selected else "disabled",
                    )
        return list(choices.values())

    def verified_pin(self, pin):
        db = self._database(create=False)
        if db is None:
            raise ValueError("找不到任务锁定的引擎版本")
        with closing(db):
            if (
                not self._selection(db, pin.extension_id)[0]
                or not db.execute(
                    "SELECT 1 FROM approved_releases WHERE digest=?", (pin.archive_sha256,)
                ).fetchone()
            ):
                raise ValueError("锁定的原生版本尚未获准运行或扩展已停用")
            data, release = self._verified(db, pin.archive_sha256)
            if self._pin(release, pin.archive_sha256, pin.capability_id, pin.kind) != pin:
                raise ValueError("任务制品引用与已签名清单不一致")
            return data, release

    def dataset_descriptor(self, pin):
        from .data_descriptors import data_descriptors

        if pin.kind != "dataset_provider":
            raise ValueError("需要已锁定的数据源扩展")
        data, release = self.verified_pin(pin)
        return data_descriptors(data, release)[pin.capability_id]

    def loadable_entry(self, pin):
        """Supply an immutable local file to a dedicated worker after re-verification."""
        data, release = self.verified_pin(pin)
        base = self.root / "research/engine/extensions/versions"
        if base.is_symlink():
            raise ValueError("引擎版本目录无效")
        base.mkdir(mode=0o700, exist_ok=True)
        target = base / pin.archive_sha256
        if target.is_symlink():
            raise ValueError("引擎版本路径无效")
        if not target.exists():
            with tempfile.TemporaryDirectory(dir=base, prefix=".install-") as temporary:
                draft = Path(temporary) / "version"
                draft.mkdir(mode=0o700)
                materialize_native(data, release, draft)
                try:
                    os.rename(draft, target)
                except OSError:
                    if not target.is_dir() or target.is_symlink():
                        raise
        expected = {item.path: item for item in release.files}
        directories = {parent for name in expected for parent in Path(name).parents}
        seen = set()
        for member in target.rglob("*"):
            relative = member.relative_to(target)
            if member.is_symlink():
                raise ValueError("引擎版本包含非法链接")
            if member.is_dir() and relative in directories:
                continue
            item = expected.get(relative.as_posix())
            if not item or not member.is_file() or member.stat().st_size != item.size_bytes:
                raise ValueError("引擎版本文件清单不一致")
            with member.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != item.sha256:
                    raise ValueError("引擎版本文件摘要不一致")
            seen.add(item.path)
        if seen != set(expected):
            raise ValueError("引擎版本缺少文件")
        return target / release.entrypoint, release

    def import_lease(self, digest, envelope, revision):
        if not 0 < len(envelope) <= MAX_ENVELOPE:
            raise ValueError("专业使用凭证格式或大小无效")
        installation = EngineInstallation(self.root)
        if installation.identity() is None:
            raise ValueError("请先生成本机安装身份")
        db = self._database(create=False)
        if db is None:
            raise ValueError("请先启用商业扩展")
        with closing(db):
            data, release = self._verified(db, digest)
            selection = self._selection(db, release.id)
            if selection[0] != digest or release.license_kind != "commercial":
                raise ValueError("请选择当前活动的商业扩展版本")
        summary = verify_local_lease(data, release, self.root, envelope)
        with closing(self._database(create=True)) as db, db:
            if self._selection(db, release.id) != selection:
                raise ValueError("凭证核验期间活动版本已变化，请刷新后重试")
            self._verified(db, digest)
            version = installation.save_verified(release.id, envelope, summary, revision)
        return {"extension_id": release.id, "summary": summary, "revision": version}
