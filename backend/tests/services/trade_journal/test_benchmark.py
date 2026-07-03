from app.services.trade_journal.benchmark import account_excess, per_trip_excess
from app.services.trade_journal.models import Roundtrip


def _rt(sym, open_d, close_d, buy=10000.0, sell=11000.0):
    return Roundtrip(sym, sym, open_d, close_d, 100.0, buy, sell, 10.0, 0.0, 2)


def test_account_excess_uses_total_pnl_and_index_closes():
    result = account_excess([_rt("600000.SH", "2024-01-02", "2024-01-05")], {"2024-01-02": 100.0, "2024-01-05": 110.0})
    assert result["pnl"] == 1000.0
    assert abs(result["account_return"] - 0.1) < 1e-9
    assert abs(result["benchmark_return"] - 0.1) < 1e-9
    assert abs(result["excess"]) < 1e-9


def test_per_trip_excess_skips_hk():
    rows = per_trip_excess([_rt("02577.HK", "2024-01-02", "2024-01-05")], {"2024-01-02": 100.0, "2024-01-05": 110.0})
    assert rows[0]["benchmark_pct"] is None
    assert rows[0]["excess"] is None
