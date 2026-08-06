"""验证 _get_fstore_realtime 在 DuckDB 模式下改用 daily_markets 且字段对齐。"""
from __future__ import annotations

import os

import pytest

from app.data_providers.fquant_provider import FQuantProvider

DUCKDB_PATH = "/Volumes/WD1/duckdb/fstore.duckdb"

pytestmark = pytest.mark.skipif(
    not os.path.exists(DUCKDB_PATH),
    reason=f"本机没有挂载 {DUCKDB_PATH}",
)


def test_fstore_realtime_duckdb_matches_postgres_shape():
    duckdb_provider = FQuantProvider()
    duckdb_rows = duckdb_provider._get_fstore_realtime(["600519.SH"])
    assert len(duckdb_rows) == 1
    row = duckdb_rows[0]
    # _get_fstore_realtime 最终经过 _fstore_quote_to_row -> _quote_row 归一，
    # 输出形状固定是 symbol/name/last_price/prev_close/open/high/low/volume/
    # amount/timestamp/source/ext（不是 SQL 里查出来的原始列名 code/price/
    # zdfd/cjl/cje —— 那些只是 _fstore_quote_to_row 的输入，不是这里的输出）。
    assert row["symbol"] == "600519.SH"
    assert row["last_price"] > 0
    assert row["source"] == "fquant:fstore:daily_markets"
    assert "change_pct" in row["ext"]
