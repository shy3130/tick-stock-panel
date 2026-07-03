import json
from io import BytesIO
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import UploadFile

from app.api import trade_journal


class FakeRepo:
    def __init__(self):
        self.symbols = []

    def get_index_daily(self, symbol, start, end, columns=None):
        self.symbols.append(symbol)
        return pl.DataFrame(
            {
                "date": ["2024-02-05", "2024-02-06"],
                "close": [100.0, 101.0],
            }
        )


def request(repo=None):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo or FakeRepo())))


def upload_file(text: str) -> UploadFile:
    return UploadFile(filename="journal.csv", file=BytesIO(text.encode("utf-8")))


CSV = """成交日期,成交时间,代码,名称,交易类别,成交数量,成交价格,发生金额,费用
2024-02-05,14:53:08,601127,赛力斯,买入,200,56.1,-11221.23,1.23
2024-02-06,14:54:53,601127,赛力斯,卖出,200,61.71,12334.48,7.52
"""


@pytest.mark.asyncio
async def test_upload_preview_does_not_write(tmp_path, monkeypatch):
    monkeypatch.setattr(trade_journal.settings, "data_dir", tmp_path)
    resp = await trade_journal.upload_journal(request(), file=upload_file(CSV), commit=False)
    assert resp["guessed_mapping"]["成交日期"] == "date"
    assert resp["preview_rows"][0]["代码"] == 601127 or resp["preview_rows"][0]["代码"] == "601127"
    assert trade_journal.store.read_ledger(tmp_path) is None


@pytest.mark.asyncio
async def test_upload_commit_writes_normalized_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(trade_journal.settings, "data_dir", tmp_path)
    resp = await trade_journal.upload_journal(
        request(),
        file=upload_file(CSV),
        commit=True,
        mapping=json.dumps(trade_journal.THS_PRESET["mapping"], ensure_ascii=False),
    )
    assert resp["summary"]["total_trips"] == 1
    assert abs(resp["trips"][0]["total_pnl"] - 1113.25) < 1e-9
    assert resp["warnings"] == ["追涨诊断: 1 只标的无本地日K或历史不足20日未覆盖"]
    assert trade_journal.get_ledger()["summary"]["total_trips"] == 1
    assert trade_journal.delete_ledger() == {"deleted": True}


@pytest.mark.asyncio
async def test_upload_commit_falls_back_unknown_benchmark(tmp_path, monkeypatch):
    repo = FakeRepo()
    monkeypatch.setattr(trade_journal.settings, "data_dir", tmp_path)
    resp = await trade_journal.upload_journal(
        request(repo),
        file=upload_file(CSV),
        commit=True,
        benchmark="BAD.INDEX",
        mapping=json.dumps(trade_journal.THS_PRESET["mapping"], ensure_ascii=False),
    )
    assert resp["benchmark"]["code"] == "000300.SH"
    assert repo.symbols == ["000300.SH"]
