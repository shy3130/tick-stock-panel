"""SQLite-backed accounts, sessions, and invite registration.

The registry is intentionally small and stdlib-only.  SQLite WAL allows the
API workers to share it safely while user-owned application data stays in files
under ``data/users/{id}`` for easy inspection and migration.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import threading
import time
from pathlib import Path

from app.services.user_context import UserIdentity

SESSION_TTL = 30 * 24 * 3600
PBKDF2_ITER = 200_000
SALT_LEN = 16
TOKEN_BYTES = 32

_lock = threading.RLock()


def _db_path(data_dir: Path) -> Path:
    path = data_dir / "user_data" / "users.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect(data_dir: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(data_dir), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or os.urandom(SALT_LEN)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITER)
    return salt.hex(), digest.hex()


def _verify(password: str, salt_hex: str, digest_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITER)
    return secrets.compare_digest(actual, expected)


def init(data_dir: Path) -> None:
    with _lock, _connect(data_dir) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                disabled INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at REAL NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);
            CREATE TABLE IF NOT EXISTS invite_codes (
                digest TEXT PRIMARY KEY,
                label TEXT NOT NULL DEFAULT '',
                redeemed_by TEXT REFERENCES users(id) ON DELETE SET NULL,
                redeemed_at REAL,
                created_at REAL NOT NULL
            );
            """
        )


def _row_to_identity(row: sqlite3.Row) -> UserIdentity:
    return UserIdentity(id=row["id"], username=row["username"], role=row["role"])


def list_users(data_dir: Path) -> list[dict]:
    init(data_dir)
    with _connect(data_dir) as conn:
        rows = conn.execute(
            "SELECT id, username, role, disabled, created_at FROM users ORDER BY created_at"
        ).fetchall()
    return [dict(row) for row in rows]


def get_user(data_dir: Path, user_id: str) -> UserIdentity | None:
    init(data_dir)
    with _connect(data_dir) as conn:
        row = conn.execute(
            "SELECT id, username, role, disabled FROM users WHERE id=?", (user_id,)
        ).fetchone()
    if not row or row["disabled"]:
        return None
    return _row_to_identity(row)


def get_user_by_username(data_dir: Path, username: str) -> UserIdentity | None:
    init(data_dir)
    with _connect(data_dir) as conn:
        row = conn.execute(
            "SELECT id, username, role, disabled FROM users WHERE username=? COLLATE NOCASE",
            (username.strip(),),
        ).fetchone()
    if not row or row["disabled"]:
        return None
    return _row_to_identity(row)


def _new_id(username: str) -> str:
    # Stable, human-readable directory id, with random suffix to avoid reuse.
    base = "".join(ch.lower() if ch.isalnum() else "_" for ch in username).strip("_") or "user"
    return f"{base[:32]}_{secrets.token_hex(4)}"


def create_user(
    data_dir: Path,
    username: str,
    password: str,
    *,
    role: str = "user",
    user_id: str | None = None,
) -> UserIdentity:
    username = username.strip()
    if not 2 <= len(username) <= 64:
        raise ValueError("用户名长度需为 2-64 位")
    if len(password) < 6:
        raise ValueError("密码至少 6 位")
    if role not in {"admin", "user"}:
        raise ValueError("无效角色")
    salt, digest = _hash_password(password)
    identity = UserIdentity(id=user_id or _new_id(username), username=username, role=role)
    now = time.time()
    init(data_dir)
    with _lock, _connect(data_dir) as conn:
        try:
            conn.execute(
                "INSERT INTO users"
                "(id, username, password_salt, password_hash, role, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (identity.id, identity.username, salt, digest, role, now),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("用户名已存在") from exc
    return identity


def change_password(data_dir: Path, user_id: str, password: str) -> None:
    if len(password) < 6:
        raise ValueError("密码至少 6 位")
    salt, digest = _hash_password(password)
    with _lock, _connect(data_dir) as conn:
        cur = conn.execute(
            "UPDATE users SET password_salt=?, password_hash=? WHERE id=?",
            (salt, digest, user_id),
        )
        if cur.rowcount != 1:
            raise ValueError("用户不存在")
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def set_user_disabled(data_dir: Path, user_id: str, disabled: bool) -> None:
    with _lock, _connect(data_dir) as conn:
        row = conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise ValueError("用户不存在")
        if row["role"] == "admin" and disabled:
            active_admins = conn.execute(
                "SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0"
            ).fetchone()[0]
            if active_admins <= 1:
                raise ValueError("不能停用唯一的管理员")
        conn.execute("UPDATE users SET disabled=? WHERE id=?", (int(disabled), user_id))
        if disabled:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def import_legacy_admin(data_dir: Path) -> UserIdentity | None:
    """Create admin from the existing single-user auth.json exactly once."""
    init(data_dir)
    with _connect(data_dir) as conn:
        existing = conn.execute(
            "SELECT id, username, role, disabled FROM users WHERE username='admin'"
        ).fetchone()
        if existing:
            return _row_to_identity(existing) if not existing["disabled"] else None
    legacy_path = data_dir / "user_data" / "auth.json"
    if not legacy_path.exists():
        return None
    try:
        raw = json.loads(legacy_path.read_text(encoding="utf-8"))
        digest = str(raw.get("password_hash") or "")
        salt = str(raw.get("password_salt") or "")
    except (OSError, ValueError, TypeError):
        return None
    if not digest or not salt:
        return None
    identity = UserIdentity(id="admin", username="admin", role="admin")
    with _lock, _connect(data_dir) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users"
            "(id, username, password_salt, password_hash, role, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (identity.id, identity.username, salt, digest, identity.role, time.time()),
        )
    return identity


def ensure_bootstrap(data_dir: Path, auth_password: str = "") -> UserIdentity | None:
    identity = import_legacy_admin(data_dir)
    if identity:
        return identity
    init(data_dir)
    with _connect(data_dir) as conn:
        if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            return get_user_by_username(data_dir, "admin")
    if auth_password.strip():
        return create_user(data_dir, "admin", auth_password, role="admin", user_id="admin")
    return None


_LEGACY_PRIVATE_ITEMS = (
    "preferences.json",
    "watchlist.parquet",
    "strategy_overrides",
    "monitor_rules",
    "custom_signals",
    "strategy_cache.json",
    "alerts.jsonl",
    "ai_reports.json",
    "ai_stock_reports.json",
    "ai_market_recaps.json",
)


def migrate_legacy_admin_data(data_dir: Path) -> list[str]:
    """Copy legacy single-user files into the admin root without deleting originals."""
    source = data_dir / "user_data"
    target = data_dir / "users" / "admin"
    marker = target / ".legacy_migrated"
    if marker.exists():
        return []
    target.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in _LEGACY_PRIVATE_ITEMS:
        src = source / name
        dst = target / name
        if not src.exists() or dst.exists():
            continue
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        copied.append(name)
    # Custom strategy source lived outside user_data in the original layout.
    for kind in ("custom", "ai"):
        src = data_dir / "strategies" / kind
        dst = target / "strategies" / kind
        if src.exists() and not dst.exists():
            shutil.copytree(src, dst)
            copied.append(f"strategies/{kind}")
    marker.write_text(str(int(time.time())), encoding="utf-8")
    return copied


def authenticate(data_dir: Path, username: str, password: str) -> UserIdentity | None:
    init(data_dir)
    with _connect(data_dir) as conn:
        row = conn.execute(
            "SELECT id, username, password_salt, password_hash, role, disabled "
            "FROM users WHERE username=? COLLATE NOCASE",
            (username.strip(),),
        ).fetchone()
    if (
        not row
        or row["disabled"]
        or not _verify(password, row["password_salt"], row["password_hash"])
    ):
        return None
    return _row_to_identity(row)


def create_session(data_dir: Path, user: UserIdentity) -> str:
    token = secrets.token_urlsafe(TOKEN_BYTES)
    now = time.time()
    with _lock, _connect(data_dir) as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        conn.execute(
            "INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(?,?,?,?)",
            (hashlib.sha256(token.encode()).hexdigest(), user.id, now + SESSION_TTL, now),
        )
    return token


def user_for_session(data_dir: Path, token: str | None) -> UserIdentity | None:
    if not token:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    with _lock, _connect(data_dir) as conn:
        row = conn.execute(
            "SELECT u.id, u.username, u.role, u.disabled "
            "FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token_hash=? AND s.expires_at>?",
            (digest, time.time()),
        ).fetchone()
    if not row or row["disabled"]:
        return None
    return _row_to_identity(row)


def revoke_session(data_dir: Path, token: str | None) -> None:
    if not token:
        return
    with _lock, _connect(data_dir) as conn:
        conn.execute(
            "DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),)
        )


def add_invite(data_dir: Path, code: str, label: str = "") -> None:
    digest = hashlib.sha256(code.strip().casefold().encode()).hexdigest()
    init(data_dir)
    with _lock, _connect(data_dir) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO invite_codes(digest, label, created_at) VALUES(?,?,?)",
            (digest, label.strip(), time.time()),
        )


def sync_env_invites(data_dir: Path, raw_codes: str) -> None:
    for code in (part.strip() for part in raw_codes.split(",")):
        if code:
            add_invite(data_dir, code)


def consume_invite(data_dir: Path, code: str) -> str | None:
    digest = hashlib.sha256(code.strip().casefold().encode()).hexdigest()
    with _lock, _connect(data_dir) as conn:
        row = conn.execute(
            "SELECT digest FROM invite_codes WHERE digest=? AND redeemed_by IS NULL", (digest,)
        ).fetchone()
        if not row:
            return None
        return digest


def register_with_invite(
    data_dir: Path, code: str, username: str, password: str
) -> UserIdentity | None:
    """Atomically consume an invite and create its user account."""
    username = username.strip()
    if not 2 <= len(username) <= 64:
        raise ValueError("用户名长度需为 2-64 位")
    if len(password) < 6:
        raise ValueError("密码至少 6 位")
    invite_digest = hashlib.sha256(code.strip().casefold().encode()).hexdigest()
    salt, password_digest = _hash_password(password)
    identity = UserIdentity(id=_new_id(username), username=username, role="user")
    now = time.time()
    init(data_dir)
    with _lock, _connect(data_dir) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            invite = conn.execute(
                "SELECT digest FROM invite_codes WHERE digest=? AND redeemed_by IS NULL",
                (invite_digest,),
            ).fetchone()
            if not invite:
                conn.execute("ROLLBACK")
                return None
            conn.execute(
                "INSERT INTO users"
                "(id, username, password_salt, password_hash, role, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    identity.id,
                    identity.username,
                    salt,
                    password_digest,
                    identity.role,
                    now,
                ),
            )
            conn.execute(
                "UPDATE invite_codes SET redeemed_by=?, redeemed_at=? WHERE digest=?",
                (identity.id, now, invite_digest),
            )
            conn.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            conn.execute("ROLLBACK")
            raise ValueError("用户名已存在") from exc
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return identity


def redeem_invite_for_user(data_dir: Path, code: str, user: UserIdentity) -> bool:
    digest = hashlib.sha256(code.strip().casefold().encode()).hexdigest()
    with _lock, _connect(data_dir) as conn:
        cur = conn.execute(
            "UPDATE invite_codes SET redeemed_by=?, redeemed_at=? "
            "WHERE digest=? AND redeemed_by IS NULL",
            (user.id, time.time(), digest),
        )
        return cur.rowcount == 1


def list_invites(data_dir: Path) -> list[dict]:
    init(data_dir)
    with _connect(data_dir) as conn:
        rows = conn.execute(
            "SELECT digest, label, redeemed_by, redeemed_at, created_at "
            "FROM invite_codes ORDER BY created_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def has_invites(data_dir: Path) -> bool:
    init(data_dir)
    with _connect(data_dir) as conn:
        return conn.execute("SELECT 1 FROM invite_codes LIMIT 1").fetchone() is not None


def has_available_invites(data_dir: Path) -> bool:
    init(data_dir)
    with _connect(data_dir) as conn:
        return (
            conn.execute("SELECT 1 FROM invite_codes WHERE redeemed_by IS NULL LIMIT 1").fetchone()
            is not None
        )


def create_invite(data_dir: Path, label: str = "") -> str:
    code = f"SYCEE-{secrets.token_urlsafe(9).replace('_', '').replace('-', '').upper()}"
    add_invite(data_dir, code, label)
    return code


def is_configured(data_dir: Path) -> bool:
    init(data_dir)
    with _connect(data_dir) as conn:
        return conn.execute("SELECT 1 FROM users WHERE disabled=0 LIMIT 1").fetchone() is not None
