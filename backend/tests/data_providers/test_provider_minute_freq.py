from datetime import datetime

from app.data_providers.fquant_provider import FQuantProvider


class FakeMinuteEngine:
    def __init__(self):
        self.calls = []

    def get_minutes(self, code, date_yyyymmdd):  # noqa: ARG002
        self.calls.append(code)
        return [
            {"price": 10.0, "volume": 1},
            {"price": 12.0, "volume": 2},
            {"price": 9.0, "volume": 3},
            {"price": 11.0, "volume": 4},
            {"price": 13.0, "volume": 5},
            {"price": 14.0, "volume": 6},
        ]


def test_get_minute_aggregates_requested_freq():
    engine = FakeMinuteEngine()
    provider = object.__new__(FQuantProvider)
    provider._engine = engine
    provider._engine_mode = "disk"
    provider.name = "fquant_local"

    df = provider.get_minute(
        ["600519.SH"],
        datetime(2026, 7, 1),
        datetime(2026, 7, 1),
        "stock",
        freq="5m",
    )

    rows = df.to_dicts()
    assert engine.calls == ["600519.SH"]
    assert len(rows) == 2
    assert rows[0]["datetime"] == "2026-07-01 09:35:00"
    assert rows[0]["open"] == 10.0
    assert rows[0]["high"] == 13.0
    assert rows[0]["low"] == 9.0
    assert rows[0]["close"] == 13.0
    assert rows[0]["volume"] == 15.0
    assert rows[0]["amount"] == 170.0
    assert rows[0]["freq"] == "5m"
    assert rows[1]["datetime"] == "2026-07-01 09:36:00"
    assert rows[1]["close"] == 14.0
