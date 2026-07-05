from app.services import agent_sessions


def test_create_session_has_null_attempt_fields(tmp_path):
    item = agent_sessions.create_session(tmp_path, "t")
    assert item["last_attempt_id"] is None
    assert item["last_attempt_status"] is None


def test_set_attempt_then_status(tmp_path):
    sid = agent_sessions.create_session(tmp_path, "t")["session_id"]

    agent_sessions.set_attempt(tmp_path, sid, "agent_attempt_abc", "running")
    running = agent_sessions.get_session(tmp_path, sid)
    assert running["last_attempt_id"] == "agent_attempt_abc"
    assert running["last_attempt_status"] == "running"

    agent_sessions.set_attempt_status(tmp_path, sid, "done")
    done = agent_sessions.get_session(tmp_path, sid)
    assert done["last_attempt_id"] == "agent_attempt_abc"
    assert done["last_attempt_status"] == "done"


def test_set_attempt_status_missing_session_is_noop(tmp_path):
    agent_sessions.set_attempt_status(tmp_path, "nope", "done")
    assert agent_sessions.get_session(tmp_path, "nope") is None
