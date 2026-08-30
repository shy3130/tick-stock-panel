from datetime import date
import json

import pytest

from app.data_providers.fquant.daily_market_research import TurnoverFact
from app.data_providers.fquant.escape_risk_intraday import (
    CatalogPinnedEscapeRiskIntradayReader,
    EscapeRiskIntradayIntegrityError,
    _minute_times,
    _minute_timestamp,
)


class Markets:
    def __init__(self):
        self.closed = 0

    def generation(self):
        return "markets-g1"

    def manifest_sha256(self):
        return "a" * 64

    def pin_identity_verified(self):
        return True

    def close(self):
        self.closed += 1


def _route_file(tmp_path, generation, file_name):
    directory = tmp_path / generation
    directory.mkdir()
    db = directory / file_name
    db.write_bytes(b"")
    manifest = {
        "generation": generation,
        "entries": [{"logical": file_name.removesuffix(".duckdb"), "file": file_name}],
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return str(db)


def test_routes_are_resolved_once_and_manifest_is_stable(tmp_path, monkeypatch):
    minutes = _route_file(tmp_path, "minutes-g1", "minutes.duckdb")
    trans = _route_file(tmp_path, "trans-g1", "trans.duckdb")
    calls = []

    def resolve(route_key, market, trade_date):
        calls.append((route_key, market, trade_date))
        return minutes if route_key == "tdx_minutes" else trans

    monkeypatch.setattr(
        "app.data_providers.fquant.escape_risk_intraday.catalog_resolver.resolve_route",
        resolve,
    )
    markets = Markets()
    days = (date(2025, 8, 28), date(2025, 8, 29))
    reader = CatalogPinnedEscapeRiskIntradayReader(days, markets)
    try:
        first = reader.run_manifest()
        second = reader.run_manifest()
        assert first == second
        assert len(calls) == 4
        assert first["coverage"] == {
            "requested_days": 2,
            "resolved_days": 2,
            "unavailable_days": 0,
            "first_day": "2025-08-28",
            "last_day": "2025-08-29",
        }
    finally:
        reader.close()
        reader.close()
    assert markets.closed == 1


def _complete_rows():
    minute_rows = []
    trans_rows = []
    for index in range(240):
        price = 10.0 + index / 10_000
        minute_rows.append((index, price, 1))
        clock = _minute_times(index)[-1]
        trans_rows.append((clock, price - 0.01, price + 0.01, 100, price * 100))
    return minute_rows, trans_rows


def test_minute_timestamps_use_sealed_bar_close_not_transaction_bucket():
    day = date(2025, 8, 28)
    assert _minute_times(0) == ("09:25", "09:30")
    assert _minute_timestamp(day, 0).strftime("%H:%M") == "09:31"
    assert _minute_timestamp(day, 59).strftime("%H:%M") == "10:30"
    assert _minute_timestamp(day, 119).strftime("%H:%M") == "11:30"
    assert _minute_timestamp(day, 120).strftime("%H:%M") == "13:01"
    assert _minute_timestamp(day, 239).strftime("%H:%M") == "15:00"


def test_build_day_reconciles_hands_to_shares_and_uses_trans_amount():
    minute_rows, trans_rows = _complete_rows()
    turnover = TurnoverFact(1_000_000, 2.4, None)
    built = CatalogPinnedEscapeRiskIntradayReader._build_day(
        "600519.SH",
        date(2025, 8, 28),
        minute_rows,
        trans_rows,
        10.0,
        9.5,
        11.0,
        8.55,
        turnover,
    )
    assert len(built.minutes) == 240
    assert built.minutes[0].volume_shares == 100
    assert built.minutes[-1].cumulative_vwap == pytest.approx(
        sum(row[4] for row in trans_rows) / 24_000
    )
    assert built.turnover is turnover


def test_build_day_rejects_volume_mismatch():
    minute_rows, trans_rows = _complete_rows()
    trans_rows[10] = (*trans_rows[10][:3], 99, trans_rows[10][4])
    with pytest.raises(EscapeRiskIntradayIntegrityError, match="volume_mismatch"):
        CatalogPinnedEscapeRiskIntradayReader._build_day(
            "600519.SH",
            date(2025, 8, 28),
            minute_rows,
            trans_rows,
            10.0,
            9.5,
            11.0,
            8.55,
            None,
        )
