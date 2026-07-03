from app.services.trade_journal import store


def test_store_roundtrip(tmp_path):
    payload = {"trips": [{"symbol": "600000.SH"}], "summary": {"total_trips": 1}}
    source = {"fills": [{"symbol": "600000.SH"}], "events": []}
    store.write_ledger(tmp_path, payload)
    store.write_source(tmp_path, source)
    assert store.read_ledger(tmp_path) == payload
    assert store.read_source(tmp_path) == source
    assert store.delete_ledger(tmp_path) is True
    assert store.read_ledger(tmp_path) is None
    assert store.read_source(tmp_path) is None
    assert store.delete_ledger(tmp_path) is False


def test_append_feedback(tmp_path):
    entry = {"rating": "helpful", "ledger_imported_at": "2026-07-03T00:00:00Z"}
    store.append_feedback(tmp_path, entry)
    assert store.read_feedback(tmp_path) == [entry]
