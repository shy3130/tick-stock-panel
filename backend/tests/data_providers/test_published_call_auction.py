import json
from datetime import date

import duckdb

from app.data_providers.fquant import generation
from app.data_providers.fquant.published_call_auction import PublishedCallAuctionReader


def test_pinned_open_final_preserves_tick_index_and_rejects_ambiguous(tmp_path, monkeypatch):
    root = tmp_path / "callauction"
    gen = root / "20260827T010101"
    gen.mkdir(parents=True)
    db = gen / "tdx-callauction-2026.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute("CREATE TABLE market_call_auction_results (code TEXT, trade_date DATE, session TEXT, tick_index INTEGER, event_time TEXT, price DOUBLE, volume BIGINT)")
    conn.executemany("INSERT INTO market_call_auction_results VALUES (?, ?, ?, ?, ?, ?, ?)", [
        ("sh600000", date(2026, 8, 27), "open", 1, "09:20:00", 10.0, 2),
        ("sh600000", date(2026, 8, 27), "open", 2, "09:25:00", 10.2, 3),
        ("sh600000", date(2026, 8, 27), "close", 3, "15:00:00", 10.2, 3),
    ])
    conn.close()
    manifest = {"generation": gen.name, "entries": [{"logical": "tdx_callauction_2026", "file": db.name}]}
    (gen / "manifest.json").write_text(json.dumps(manifest, separators=(",", ":")))
    (root / "current.json").write_text(json.dumps({"generation": gen.name}))
    monkeypatch.setattr(generation, "root_for", lambda logical: str(root))
    reader = PublishedCallAuctionReader(2026)
    final = reader.preopen_final("600000.SH", date(2026, 8, 27))
    assert final is not None
    assert final.tick_index == 2
    assert final.event_time == "09:25:00"
    assert reader.preopen_final("600000", date(2026, 8, 27)) is not None
    reader.close()


def test_bare_code_routes_by_market_and_non_0925_final_is_unavailable():
    class Result:
        def fetchall(self):
            return [(4, "09:26:00", 10.2, 3)]

    class Conn:
        def execute(self, *args):
            return Result()

    reader = object.__new__(PublishedCallAuctionReader)
    reader._closed = False
    reader._lock = __import__("threading").Lock()
    reader._conn = Conn()
    assert reader.preopen_final("600000", date(2026, 8, 27)) is None


def test_unknown_bare_code_fails_closed():
    reader = object.__new__(PublishedCallAuctionReader)
    reader._closed = False
    reader._lock = __import__("threading").Lock()
    try:
        reader.preopen_final("700000", date(2026, 8, 27))
    except Exception as exc:
        assert type(exc).__name__ == "CallAuctionIntegrityError"
    else:
        raise AssertionError("unknown market must fail closed")
