"""AI 助手会话历史服务端存储 (跨设备同步) 的服务与 API 测试。

契约: GET 空结构、PUT 保存往返、结构裁剪 (无 id 会话剔除 / 消息截尾 200 /
会话上限 20)、损坏文件按空处理、API 挂载在 assistant 扩展路由下。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom.assistant import sessions as assistant_sessions
from app.custom.assistant.routes import build_router


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(build_router())
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


def _session(sid: str, n_msgs: int = 1) -> dict:
    return {
        "id": sid,
        "title": f"会话{sid}",
        "createdAt": 1700000000000,
        "messages": [
            {"id": f"m{i}", "role": "user", "content": f"hi {i}", "ts": i}
            for i in range(n_msgs)
        ],
    }


def test_api_roundtrip_and_empty_state(client: TestClient) -> None:
    assert client.get("/api/custom/assistant/sessions").json() == {
        "sessions": [],
        "activeId": "",
    }

    r = client.put(
        "/api/custom/assistant/sessions",
        json={"sessions": [_session("s1"), _session("s2")], "activeId": "s2"},
    )
    assert r.status_code == 200 and r.json() == {"ok": True, "sessions": 2}

    got = client.get("/api/custom/assistant/sessions").json()
    assert [s["id"] for s in got["sessions"]] == ["s1", "s2"]
    assert got["activeId"] == "s2"


def test_sanitize_trims_and_drops(client: TestClient, tmp_path: Path) -> None:
    # 无 id/非字符串 id 的会话剔除
    bad = [{"title": "no id"}, {"id": 123, "messages": []}]
    # 消息超 200 截尾
    long = _session("long", n_msgs=250)
    # 会话超 20 截断 (服务层: API 层 max_length=20 会先拒绝更大的载荷)
    many = [_session(f"s{i}") for i in range(25)]

    n = assistant_sessions.save_sessions(
        tmp_path, {"sessions": [*bad, long, *many], "activeId": "s0"}
    )
    assert n == assistant_sessions.MAX_SESSIONS

    got = assistant_sessions.load_sessions(tmp_path)
    ids = [s["id"] for s in got["sessions"]]
    assert len(ids) == 20
    assert "long" in ids  # 输入顺序靠前, 保留
    assert len(next(s for s in got["sessions"] if s["id"] == "long")["messages"]) == 200

    # API 层: 结构非法的会话静默剔除, 合法照常保存
    r = client.put(
        "/api/custom/assistant/sessions",
        json={"sessions": [*bad, _session("ok1")], "activeId": "ok1"},
    )
    assert r.status_code == 200 and r.json() == {"ok": True, "sessions": 1}
    ids2 = [s["id"] for s in client.get("/api/custom/assistant/sessions").json()["sessions"]]
    assert ids2 == ["ok1"]


def test_corrupted_file_treated_as_empty(tmp_path: Path) -> None:
    p = tmp_path / "user_data" / "assistant_sessions.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    assert assistant_sessions.load_sessions(tmp_path) == {"sessions": [], "activeId": ""}


def test_put_rejects_oversized_session_list(client: TestClient) -> None:
    r = client.put(
        "/api/custom/assistant/sessions",
        json={"sessions": [_session(f"s{i}") for i in range(21)]},
    )
    assert r.status_code == 422  # 超过 max_length=20, 防御异常客户端
