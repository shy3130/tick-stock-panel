from app.services.trade_journal.models import CashEvent, Fill, Roundtrip


def test_fill_is_frozen_and_normalized_fields():
    f = Fill("2024-02-05", "14:53:08", "601127.SH", "赛力斯", "buy", 200.0, 56.1, -11221.23, 1.23)
    assert f.side == "buy"
    assert f.amount < 0


def test_roundtrip_pnl_and_dividend_total():
    rt = Roundtrip("600418.SH", "江淮汽车", "2024-07-08", "2024-07-22", 2000.0, 41984.62, 42014.36, 30.26, 33.6, 11)
    assert abs(rt.pnl - 29.74) < 1e-9
    assert abs(rt.total_pnl - 63.34) < 1e-9
    assert abs(rt.buy_avg - 20.99231) < 1e-9


def test_cash_event_kinds():
    ev = CashEvent("2024-07-18", "600418.SH", "dividend", 42.0)
    assert ev.kind in {"dividend", "dividend_tax", "transfer_in", "transfer_out", "repo", "other"}
