from __future__ import annotations

import json
from datetime import date

from app.data_providers.fquant.engine_data_duckdb_client import EngineDataDuckDBClient

SYMBOLS = ["600519.SH", "000001.SZ", "300059.SZ", "688981.SH", "513050.SH", "02577.HK"]


def main() -> None:
    client = EngineDataDuckDBClient()
    today = date.today().strftime("%Y%m%d")
    rows = []
    for symbol in SYMBOLS:
        code, _, suffix = symbol.partition(".")
        daily = client.get_wide(code, limit=1, asset_type="hk" if suffix == "HK" else None)
        latest = daily[0] if daily else {}
        volume = float(latest.get("volume") or 0)
        amount = float(latest.get("amount") or 0)
        rows.append({
            "symbol": symbol,
            "latest_date": latest.get("date"),
            "close": latest.get("close"),
            "volume": volume,
            "amount": amount,
            "amount_per_volume": amount / volume if volume else None,
            "minutes_rows_today": len(client.get_minutes(code, today)),
            "trans_rows_today": len(client.get_trans(code, today)),
        })
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
