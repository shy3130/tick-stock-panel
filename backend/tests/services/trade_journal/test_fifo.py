from app.services.trade_journal.fifo import pair_roundtrips
from app.services.trade_journal.models import CashEvent, Fill


def _fill(date, sym, side, qty, amount, fee=0.0, price=0.0):
    return Fill(date, "", sym, sym, side, qty, price, amount, fee)


def test_single_cycle_matches_ths_numbers():
    fills = [
        _fill("2024-02-05", "601127.SH", "buy", 200, -11221.23, fee=1.23),
        _fill("2024-02-06", "601127.SH", "sell", 200, 12334.48, fee=7.52),
    ]
    trips, open_pos, warnings = pair_roundtrips(fills, [], trading_days=None)
    assert len(trips) == 1 and open_pos == [] and warnings == []
    assert abs(trips[0].pnl - 1113.25) < 1e-9
    assert abs(trips[0].fees - 8.75) < 1e-9
    assert trips[0].holding_days == 2


def test_holding_days_uses_trading_calendar():
    days = ["2024-03-01", "2024-03-04", "2024-03-05", "2024-03-06", "2024-03-07", "2024-03-08", "2024-03-11", "2024-03-12", "2024-03-13", "2024-03-14", "2024-03-15", "2024-03-18", "2024-03-19", "2024-03-20", "2024-03-21", "2024-03-22", "2024-03-25"]
    trips, _, _ = pair_roundtrips(
        [_fill("2024-03-01", "600895.SH", "buy", 2200, -48163.30), _fill("2024-03-25", "600895.SH", "sell", 2200, 47051.28)],
        [],
        trading_days=days,
    )
    assert trips[0].holding_days == 17


def test_multi_buy_single_cycle_aggregates():
    trips, open_pos, _ = pair_roundtrips(
        [
            _fill("2024-02-08", "601127.SH", "buy", 900, -63276.96),
            _fill("2024-02-08", "601127.SH", "buy", 100, -7011.07),
            _fill("2024-03-26", "601127.SH", "sell", 1000, 91940.0),
        ],
        [],
        trading_days=None,
    )
    assert len(trips) == 1 and open_pos == []
    assert trips[0].qty == 1000
    assert abs(trips[0].buy_net - 70288.03) < 1e-9


def test_open_position_not_a_trip():
    trips, open_pos, _ = pair_roundtrips([_fill("2024-05-01", "000988.SZ", "buy", 900, -120000.0)], [], None)
    assert trips == []
    assert len(open_pos) == 1 and open_pos[0]["symbol"] == "000988.SZ"


def test_two_separate_cycles_same_symbol():
    trips, _, _ = pair_roundtrips(
        [
            _fill("2024-02-05", "601127.SH", "buy", 200, -11221.23),
            _fill("2024-02-06", "601127.SH", "sell", 200, 12334.48),
            _fill("2024-04-09", "601127.SH", "buy", 1000, -85920.0),
            _fill("2024-04-10", "601127.SH", "sell", 1000, 83050.0),
        ],
        [],
        None,
    )
    assert len(trips) == 2


def test_same_day_rebuy_does_not_close_cycle():
    fills = [
        _fill("2024-11-11", "300738.SZ", "buy", 1200, -16321.63),
        _fill("2024-11-15", "300738.SZ", "sell", 1200, 16418.15),
        _fill("2024-11-15", "300738.SZ", "buy", 1100, -14686.47),
        _fill("2024-11-15", "300738.SZ", "buy", 1100, -14620.46),
        _fill("2024-11-27", "300738.SZ", "sell", 1100, 13741.74),
        _fill("2024-12-03", "300738.SZ", "sell", 1100, 14137.52),
    ]
    trips, _, _ = pair_roundtrips(fills, [], None)
    assert len(trips) == 1
    assert trips[0].open_date == "2024-11-11" and trips[0].close_date == "2024-12-03"
    assert trips[0].qty == 3400


def test_dividend_attributed_to_containing_cycle():
    trips, _, _ = pair_roundtrips(
        [_fill("2024-07-01", "600418.SH", "buy", 600, -10000.0), _fill("2024-08-01", "600418.SH", "sell", 600, 10500.0)],
        [CashEvent("2024-07-18", "600418.SH", "dividend", 42.0), CashEvent("2024-07-23", "600418.SH", "dividend_tax", -8.4)],
        None,
    )
    assert abs(trips[0].dividend - 33.6) < 1e-9
    assert abs(trips[0].pnl - 500.0) < 1e-9


def test_dividend_tax_on_next_cycle_open_date_is_not_attributed():
    fills = [
        _fill("2024-07-22", "601138.SH", "buy", 100, -1000.0),
        _fill("2024-09-24", "601138.SH", "sell", 100, 900.0),
        _fill("2024-09-25", "601138.SH", "buy", 100, -1000.0),
        _fill("2024-10-25", "601138.SH", "sell", 100, 1100.0),
    ]
    events = [
        CashEvent("2024-08-14", "601138.SH", "dividend", 100.0),
        CashEvent("2024-09-25", "601138.SH", "dividend_tax", -10.0),
    ]
    trips, _, _ = pair_roundtrips(fills, events, None)
    assert trips[0].dividend == 100.0
    assert trips[1].dividend == 0.0


def test_oversell_warns_and_skips_excess():
    trips, _, warnings = pair_roundtrips(
        [_fill("2024-01-02", "600000.SH", "buy", 100, -800.0), _fill("2024-01-03", "600000.SH", "sell", 300, 2500.0)],
        [],
        None,
    )
    assert len(trips) == 1
    assert len(warnings) == 1 and "600000.SH" in warnings[0]
