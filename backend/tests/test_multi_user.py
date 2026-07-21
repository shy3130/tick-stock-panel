from __future__ import annotations

import json
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import invites as invite_api
from app.config import settings
from app.services import alert_store, preferences, users, watchlist
from app.services.user_context import as_user
from app.strategy import config as strategy_config


def test_accounts_sessions_and_invites_are_bound_to_users(tmp_path):
    users.init(tmp_path)
    admin = users.create_user(tmp_path, "admin", "admin-pass", role="admin")
    users.add_invite(tmp_path, "one-time")

    member = users.create_user(tmp_path, "alice", "member-pass")
    assert users.redeem_invite_for_user(tmp_path, "one-time", member)
    assert not users.redeem_invite_for_user(tmp_path, "one-time", admin)

    token = users.create_session(tmp_path, member)
    assert users.user_for_session(tmp_path, token) == member
    users.revoke_session(tmp_path, token)
    assert users.user_for_session(tmp_path, token) is None


def test_private_files_are_isolated_while_global_preferences_are_shared(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    admin = users.create_user(tmp_path, "admin", "admin-pass", role="admin")
    alice = users.create_user(tmp_path, "alice", "member-pass")
    now = int(time.time() * 1000)

    with as_user(admin):
        watchlist.add("600519.SH")
        preferences.save({"nav_hidden": ["/review"], "realtime_quote_interval": 8})
        strategy_config.save_override(tmp_path, "trend", {"params": {"days": 20}})
        alert_store.append_many(tmp_path, [{"ts": now, "source": "strategy"}])
    with as_user(alice):
        watchlist.add("000001.SZ")
        preferences.save({"nav_hidden": ["/financials"]})
        strategy_config.save_override(tmp_path, "trend", {"params": {"days": 60}})
        alert_store.append_many(tmp_path, [{"ts": now + 1, "source": "price"}])

    with as_user(admin):
        assert [row["symbol"] for row in watchlist.list_symbols()] == ["600519.SH"]
        assert preferences.load()["nav_hidden"] == ["/review"]
        assert strategy_config.load_override(tmp_path, "trend")["params"]["days"] == 20
        assert [event["ts"] for event in alert_store.list_recent(tmp_path)] == [now]
    with as_user(alice):
        assert [row["symbol"] for row in watchlist.list_symbols()] == ["000001.SZ"]
        assert preferences.load()["nav_hidden"] == ["/financials"]
        assert preferences.get_realtime_quote_interval() == 8
        assert strategy_config.load_override(tmp_path, "trend")["params"]["days"] == 60
        assert [event["ts"] for event in alert_store.list_recent(tmp_path)] == [now + 1]


def test_legacy_admin_migration_copies_without_deleting(tmp_path):
    legacy = tmp_path / "user_data"
    legacy.mkdir(parents=True)
    (legacy / "preferences.json").write_text(json.dumps({"nav_hidden": ["/review"]}))
    (legacy / "alerts.jsonl").write_text('{"ts": 1}\n')

    copied = users.migrate_legacy_admin_data(tmp_path)

    assert "preferences.json" in copied
    assert (legacy / "preferences.json").exists()
    assert (tmp_path / "users" / "admin" / "preferences.json").exists()
    assert users.migrate_legacy_admin_data(tmp_path) == []


def test_registration_consumes_invite_and_sets_account_session(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "invite_codes", "alpha")
    invite_api._fail_counter.clear()
    app = FastAPI()
    app.include_router(invite_api.router)
    client = TestClient(app)

    response = client.post(
        "/api/invite/redeem",
        json={"code": "alpha", "username": "alice", "password": "member-pass"},
    )
    assert response.status_code == 200
    assert response.json()["user"]["username"] == "alice"
    assert "tf_session" in response.headers["set-cookie"]

    second = client.post(
        "/api/invite/redeem",
        json={"code": "alpha", "username": "bob", "password": "member-pass"},
    )
    assert second.status_code == 401
