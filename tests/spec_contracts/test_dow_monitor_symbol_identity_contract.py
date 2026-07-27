from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services import dow_monitor_service as service_module  # noqa: E402
from app.services.dow_monitor_service import DowMonitorService  # noqa: E402
from app.services.dow_monitor_store import DowMonitorStore  # noqa: E402


def test_capital_queries_and_result_keys_use_canonical_hk_symbols(
    tmp_path,
    monkeypatch,
) -> None:
    """A leading-zero display alias must not miss canonical ClickHouse capital."""

    now = datetime(2026, 7, 27, 15, 5, tzinfo=ZoneInfo("Asia/Hong_Kong"))
    service = DowMonitorService(
        DowMonitorStore(tmp_path),
        object(),
        object(),
        lambda *_args: None,
        now_fn=lambda: now,
    )
    seen_symbols: list[list[str]] = []

    def fake_fetch(symbols, *, now, max_quote_age_minutes):
        del now, max_quote_age_minutes
        seen_symbols.append(list(symbols))
        return {
            "1347.HK": {
                "capital_minute": "2026-07-27 15:05:00",
                "total_net": 0,
                "large_net": 0,
                "flow_15m": 0,
                "flow_30m": 0,
                "flow_points": 17,
            },
            "981.HK": {
                "capital_minute": "2026-07-27 15:05:00",
                "total_net": 88.0,
                "large_net": 21.0,
                "flow_15m": 8.0,
                "flow_30m": 13.0,
                "flow_points": 17,
            },
        }

    monkeypatch.setattr(service_module, "_fetch_realtime_signal_rows", fake_fetch)
    monkeypatch.setattr(
        service,
        "_intraday_capital_windows_by_symbol",
        lambda symbols: (
            seen_symbols.append(list(symbols))
            or {
                "1347.HK": [{"end_time": "2026-07-27 15:05:00"}],
                "981.HK": [{"end_time": "2026-07-27 15:05:00"}],
            }
        ),
    )

    result = service._intraday_capital_by_symbol(
        ["01347.HK", "0981.HK", "002714.SZ"]
    )

    assert seen_symbols == [
        ["002714.SZ", "1347.HK", "981.HK"],
        ["002714.SZ", "1347.HK", "981.HK"],
    ]
    assert sorted(result) == ["1347.HK", "981.HK"]
    assert result["1347.HK"]["total_net"] == 0
    assert result["1347.HK"]["large_net"] == 0
