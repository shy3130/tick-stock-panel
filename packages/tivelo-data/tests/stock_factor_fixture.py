"""Redistributable stock splits, rights issue and raw quotes on three trading days."""

import io

import pyarrow as pa
import pyarrow.parquet as pq

STOCK = "600000.SH"
EMPTY = "000001.SZ"
MISSING = "600001.SH"
START_MS = 1751299200000  # 2025-07-01, Asia/Shanghai
DAY_MS = 86400000
DUMP = "/api/dump/market-dumps/adjustment-factors/download-url"
OBJECT = "/releases/20250703/events.parquet"
RAW = "/api/a-share/prices/historical"
EVENTS = "/api/a-share/corporate-actions/adjustment-factors"


def raw_bars():
    return [
        dict(
            date_ms=START_MS + i * DAY_MS,
            open_price=c,
            high_price=c,
            low_price=c,
            close_price=c,
            volume=1000,
            turnover=c * 1000,
        )
        for i, c in enumerate((10.0, 5.5, 4.8))
    ]


def archive_rows():
    def row(symbol, i, bonus=0.0, rights=0.0, price=0.0):
        return dict(
            thscode=symbol,
            ticker=symbol[:6],
            currency="CNY",
            ex_date_ms=START_MS + i * DAY_MS,
            dividend_per_share=0.0,
            per_share_bonus=bonus,
            allotment_ratio=rights,
            allotment_price=price,
        )

    return [
        row(STOCK, 1, bonus=1.0),
        row(STOCK, 2, rights=0.5, price=2.2),
        row(EMPTY, -20, bonus=0.1),
    ]


def parquet_bytes(rows=None):
    file = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(archive_rows() if rows is None else rows), file)
    return file.getvalue()


def tdx_replies():
    bars = raw_bars()
    history = {"Date": [20250701, 20250702, 20250703]}
    history.update(
        {
            wire: [b[key] for b in bars]
            for wire, key in (
                ("Open", "open_price"),
                ("High", "high_price"),
                ("Close", "close_price"),
                ("Volume", "volume"),
                ("Amount", "turnover"),
            )
        }
    )
    return {
        "replies": {
            "events": {
                STOCK: {
                    "ErrorId": 0,
                    "Date": [20240701, 20250702, 20250703],
                    "Type": ["1"] * 3,
                    "Value": [[20, 0, 0, 0], [0, 0, 10, 0], [0, 2.2, 0, 5]],
                },
                EMPTY: {"ErrorId": 0, "Date": [], "Value": []},
                MISSING: {},
            },
            "history": {STOCK: {"ErrorId": 0, "Value": history}},
        }
    }
