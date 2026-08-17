import math
import threading

import pytest

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


def test_write_journal_commits_source_and_ledger_in_single_state_file(tmp_path):
    legacy_source = {"fills": [{"symbol": "000001.SZ"}], "events": []}
    legacy_ledger = {"summary": {"total_trips": 1}}
    source = {"fills": [{"symbol": "600000.SH"}], "events": []}
    ledger = {"summary": {"total_trips": 2}}
    store.write_source(tmp_path, legacy_source)
    store.write_ledger(tmp_path, legacy_ledger)

    store.write_journal(tmp_path, source, ledger)

    assert store.journal_state_path(tmp_path).exists()
    assert store.read_source(tmp_path) == source
    assert store.read_ledger(tmp_path) == ledger
    assert store.delete_ledger(tmp_path) is True
    assert store.journal_state_path(tmp_path).exists() is False


def test_append_feedback(tmp_path):
    entry = {"rating": "helpful", "ledger_imported_at": "2026-07-03T00:00:00Z"}
    store.append_feedback(tmp_path, entry)
    assert store.read_feedback(tmp_path) == [entry]


def test_store_rejects_nonfinite_payload_without_creating_file(tmp_path):
    with pytest.raises(ValueError):
        store.write_ledger(tmp_path, {"summary": {"total_pnl": math.inf}})

    assert store.read_ledger(tmp_path) is None


def test_journal_write_lock_serializes_threads(tmp_path):
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def first_writer():
        with store.journal_write_lock(tmp_path):
            first_entered.set()
            release_first.wait(timeout=1)

    def second_writer():
        first_entered.wait(timeout=1)
        with store.journal_write_lock(tmp_path):
            second_entered.set()

    first = threading.Thread(target=first_writer)
    second = threading.Thread(target=second_writer)
    first.start()
    assert first_entered.wait(timeout=1)
    second.start()
    try:
        assert not second_entered.wait(timeout=0.1)
    finally:
        release_first.set()
        first.join(timeout=1)
        second.join(timeout=1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert second_entered.is_set()
