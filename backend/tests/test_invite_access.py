from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import invites as invite_api
from app.services import invites


def test_redeeming_code_rotates_only_that_codes_browser_session(tmp_path):
    store = invites.InviteAccessStore(tmp_path, ["alpha", "beta"])

    alpha_first = store.redeem("alpha")
    beta = store.redeem("beta")
    assert alpha_first is not None
    assert beta is not None
    assert store.is_valid_session(alpha_first.token)
    assert store.is_valid_session(beta.token)

    alpha_second = store.redeem(" ALPHA ")
    assert alpha_second is not None
    assert not store.is_valid_session(alpha_first.token)
    assert store.is_valid_session(alpha_second.token)
    assert store.is_valid_session(beta.token)


def test_invite_sessions_survive_restart_without_storing_plaintext_codes(tmp_path):
    store = invites.InviteAccessStore(tmp_path, ["alpha", "beta"])
    session = store.redeem("alpha")
    assert session is not None

    state_text = (tmp_path / "user_data" / "invites.json").read_text(encoding="utf-8")
    assert "alpha" not in state_text
    assert "beta" not in state_text

    restored = invites.InviteAccessStore(tmp_path, ["alpha", "beta"])
    assert restored.is_valid_session(session.token)
    assert restored.redeem("unknown") is None


def test_invite_api_sets_cookie_and_reports_authorized(monkeypatch, tmp_path):
    store = invites.InviteAccessStore(tmp_path, ["alpha"])
    monkeypatch.setattr(invites, "get_store", lambda: store)
    invite_api._fail_counter.clear()

    app = FastAPI()
    app.include_router(invite_api.router)
    client = TestClient(app)

    assert client.get("/api/invite/status").json() == {
        "enabled": True,
        "authorized": False,
        "capacity": 1,
    }
    invalid = client.post("/api/invite/redeem", json={"code": "wrong"})
    assert invalid.status_code == 401

    accepted = client.post("/api/invite/redeem", json={"code": "alpha"})
    assert accepted.status_code == 200
    assert accepted.json() == {"ok": True, "authorized": True}
    assert "HttpOnly" in accepted.headers["set-cookie"]
    assert "SameSite=lax" in accepted.headers["set-cookie"]
    assert client.get("/api/invite/status").json()["authorized"] is True
