from app.services.trade_journal.diagnose import diagnose
from app.services.trade_journal.models import Fill, Roundtrip


def _rt(sym, open_d, close_d, pnl, hold):
    sell = 10000.0 + pnl
    return Roundtrip(sym, sym, open_d, close_d, 100.0, 10000.0, sell, 20.0, 0.0, hold)


def test_disposition_and_overtrading_metrics():
    trips = [
        _rt("600000.SH", "2024-01-02", "2024-01-03", 1000, 2),
        _rt("600001.SH", "2024-01-02", "2024-01-20", -1000, 15),
    ]
    result = diagnose(trips, [], {})
    assert result["disposition"]["loss_to_win_holding_ratio"] == 7.5
    assert result["disposition"]["flag"] is True
    assert result["overtrading"]["monthly_roundtrips"] == 2


def test_chasing_and_anchoring_metrics():
    fills = [
        Fill("2024-01-02", "", "600000.SH", "A", "buy", 100, 10.0, -1000.0, 1.0),
        Fill("2024-01-03", "", "600000.SH", "A", "buy", 100, 9.0, -900.0, 1.0),
        Fill("2024-01-04", "", "600001.SH", "B", "buy", 100, 20.0, -2000.0, 1.0),
    ]
    lookup = {
        ("600000.SH", "2024-01-02"): {"pos_20d": 0.95},
        ("600000.SH", "2024-01-03"): {"pos_20d": 0.1},
    }
    result = diagnose([], fills, lookup)
    assert result["chasing"]["covered_buys"] == 2
    assert result["chasing"]["chasing_buys"] == 1
    assert result["chasing"]["uncovered_buys"] == 1
    assert result["anchoring"]["add_buys"] == 1
    assert result["anchoring"]["loss_add_buys"] == 1
