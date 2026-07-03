from app.services.trade_journal import store


def test_store_roundtrip(tmp_path):
    payload = {"trips": [{"symbol": "600000.SH"}], "summary": {"total_trips": 1}}
    store.write_ledger(tmp_path, payload)
    assert store.read_ledger(tmp_path) == payload
    assert store.delete_ledger(tmp_path) is True
    assert store.read_ledger(tmp_path) is None
    assert store.delete_ledger(tmp_path) is False
