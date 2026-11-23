"""Device possession and signed leases for one local Tivelo installation."""

import base64
import hashlib
import json
import os
import sqlite3
import stat
import sys
from contextlib import closing
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from tivelo_contracts.signatures import decode_base64url


def _encoded(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def deployment_mode():
    return "desktop" if getattr(sys, "frozen", False) else "server"


_desktop_workspace = None


def bind_desktop_installation(workspace, installation_root):
    """Bind this desktop process's selected data copy to the same device and lease ledger."""
    global _desktop_workspace
    if deployment_mode() != "desktop":
        raise ValueError("仅桌面宿主可以绑定工作区的设备身份")
    pair = (Path(workspace).resolve(), Path(installation_root).resolve())
    if _desktop_workspace is not None and _desktop_workspace != pair:
        raise ValueError("更换设备账本需要重新启动桌面进程")
    _desktop_workspace = pair


def _credential_vault():
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring

        return Keyring()
    if sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring

        return WinVaultKeyring()
    if sys.platform == "linux":
        from keyring.backends.SecretService import Keyring

        return Keyring()
    raise ValueError("此桌面平台尚无受支持的系统设备密钥存储")


class EngineInstallation:
    def __init__(self, data_dir):
        self.root = Path(data_dir).resolve()
        if _desktop_workspace is not None and self.root == _desktop_workspace[0]:
            self.root = _desktop_workspace[1]

    def _db(self, *, write):
        directory = self.root
        for part in ("research", "engine", "installation"):
            directory /= part
            if directory.is_symlink():
                raise ValueError("安装凭证目录无效")
            if write:
                directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / "installation.sqlite3"
        if any(
            Path(str(path) + suffix).is_symlink() for suffix in ("", "-wal", "-shm", "-journal")
        ):
            raise ValueError("安装凭证文件路径无效")
        if not write and not path.exists():
            return None
        if write:
            fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(fd)
        if os.name == "posix" and (
            stat.S_IMODE(path.stat().st_mode) & 0o077
            or stat.S_IMODE(directory.stat().st_mode) & 0o077
        ):
            raise ValueError("安装目录和凭证文件必须只允许当前账户访问")
        db = sqlite3.connect(
            path.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=8
        )
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("安装凭证存储版本不受支持")
            if write:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS identity(singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                    "public BLOB NOT NULL, secret BLOB, storage TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS leases(extension TEXT PRIMARY KEY, envelope BLOB NOT NULL, "
                    "summary TEXT NOT NULL, revision INTEGER NOT NULL)"
                )
                db.execute("PRAGMA user_version=1")
            elif version == 0:
                db.close()
                return None
        except BaseException:
            db.close()
            raise
        return db

    def _vault_key(self, *, create):
        account = hashlib.sha256(os.fsencode(self.root)).hexdigest()
        try:
            vault = _credential_vault()
            secret = vault.get_password("Tivelo.Engine.Installation", account)
            if secret is None and create:
                key = Ed25519PrivateKey.generate()
                vault.set_password(
                    "Tivelo.Engine.Installation", account, _encoded(key.private_bytes_raw())
                )
                return key
        except Exception:  # Native vaults and D-Bus transport have different error types.
            raise ValueError("系统设备密钥存储不可用") from None
        if secret is None:
            raise ValueError("系统凭据库中缺少已登记的设备密钥")
        return Ed25519PrivateKey.from_private_bytes(decode_base64url(secret, size=32))

    def identity(self, *, create=False):
        db = self._db(write=create)
        if db is None:
            return None
        with closing(db), db:
            row = db.execute("SELECT public,storage FROM identity WHERE singleton=1").fetchone()
            if row is None:
                if not create:
                    return None
                vault = deployment_mode() == "desktop" or sys.platform == "win32"
                key = self._vault_key(create=True) if vault else Ed25519PrivateKey.generate()
                public = key.public_key().public_bytes_raw()
                storage = "system_vault" if vault else "private_file"
                db.execute(
                    "INSERT INTO identity VALUES(1,?,?,?)",
                    (public, None if vault else key.private_bytes_raw(), storage),
                )
            else:
                public, storage = row
            return dict(
                public_key=_encoded(public),
                sha256=hashlib.sha256(public).hexdigest(),
                storage=storage,
            )

    def device(self, purpose, binding):
        db = self._db(write=False)
        if db is None:
            raise ValueError("请先生成安装身份")
        with closing(db):
            row = db.execute(
                "SELECT public,secret,storage FROM identity WHERE singleton=1"
            ).fetchone()
        if row is None:
            raise ValueError("请先生成安装身份")
        public, secret, storage = row
        if storage == "system_vault":
            key = self._vault_key(create=False)
        elif (
            storage == "private_file" and deployment_mode() == "server" and sys.platform != "win32"
        ):
            key = Ed25519PrivateKey.from_private_bytes(secret)
        else:
            raise ValueError("设备凭据存储方式与当前运行环境不匹配")
        if key.public_key().public_bytes_raw() != public:
            raise ValueError("设备密钥与登记的公钥不匹配")
        domain, suffix = {
            "run": (b"tivelo-device-proof-v1\0", 16),
            "import": (b"tivelo-device-activation-v1\0", 0),
        }[purpose]

        def prove(challenge):
            offset = len(domain) + 32
            if (
                not isinstance(challenge, bytes)
                or len(challenge) != offset + 32 + suffix
                or not challenge.startswith(domain)
                or challenge[offset : offset + 32] != binding
            ):
                raise ValueError("设备挑战与当前操作不匹配")
            return key.sign(challenge)

        return public, prove

    def status(self):
        identity = self.identity()
        db = self._db(write=False)
        if db is None:
            return {"identity": identity, "leases": []}
        with closing(db):
            rows = db.execute("SELECT extension,summary,revision FROM leases ORDER BY extension")
            leases = [
                {"extension_id": name, "summary": json.loads(summary), "revision": revision}
                for name, summary, revision in rows
            ]
        return {"identity": identity, "leases": leases}

    def saved_lease(self, extension):
        db = self._db(write=False)
        if db is not None:
            with closing(db):
                row = db.execute(
                    "SELECT envelope,revision FROM leases WHERE extension=?", (extension,)
                ).fetchone()
                return row
        return None

    def lease_summary(self, extension):
        from .activation import LeaseSummary

        db = self._db(write=False)
        if db is not None:
            with closing(db):
                row = db.execute(
                    "SELECT summary FROM leases WHERE extension=?", (extension,)
                ).fetchone()
                if row:
                    return LeaseSummary.model_validate_json(row[0])
        return None

    def lease(self, extension):
        saved = self.saved_lease(extension)
        if saved:
            return saved[0]
        raise ValueError("此扩展缺少已登记的专业使用凭证")

    def save_verified(self, extension, envelope, summary, revision):
        with closing(self._db(write=True)) as db, db:
            row = db.execute(
                "SELECT revision,envelope,summary FROM leases WHERE extension=?", (extension,)
            ).fetchone()
            if row and row[1] == envelope:
                return row[0]
            current = row[0] if row else 0
            if current != revision:
                raise ValueError("专业使用凭证已更新，请刷新后重试")
            if row:
                previous = json.loads(row[2])
                if summary["scope_id"] == previous["scope_id"] and any(
                    summary[k] < previous[k] for k in ("generation", "revocation_epoch")
                ):
                    raise ValueError("此凭证早于已保存的权益或撤销版本")
            db.execute(
                "INSERT OR REPLACE INTO leases VALUES(?,?,?,?)",
                (extension, envelope, json.dumps(summary, separators=(",", ":")), current + 1),
            )
            return current + 1
