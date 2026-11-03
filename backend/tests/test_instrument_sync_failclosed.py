"""instrument_sync 容灾测试: 单交易所失败不得覆盖旧维表(2026-10-12 SZ 瞬断教训)。"""

from __future__ import annotations

import polars as pl
import pytest

from app.services import instrument_sync as ims


class _FakeTickflow:
    """按交易所可控行为的假 client: ok_exchanges 之外的恒抛错。"""

    def __init__(self, ok_exchanges: set[str], counts: dict[str, int] | None = None):
        self.ok = ok_exchanges
        self.calls: dict[str, int] = {}
        self.counts = counts

    @property
    def exchanges(self):
        return self

    def get_instruments(self, ex, instrument_type="stock"):
        self.calls[ex] = self.calls.get(ex, 0) + 1
        if ex not in self.ok:
            raise RuntimeError(f"boom {ex}")
        return [
            {"symbol": f"{ex}-1.XSHG", "name": f"{ex}股", "code": "1", "exchange": ex,
             "region": "CN", "type": "stock", "ext": {}},
        ]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(ims.time, "sleep", lambda s: None)


def _existing(data_dir):
    out = data_dir / "instruments" / "instruments.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": ["600000.SH"], "name": ["旧"]}).write_parquet(out)
    return out


def test_partial_failure_keeps_existing_table(tmp_path, monkeypatch):
    """SZ 失败: 已有维表时不得覆盖写残表, 返回 0。"""
    out = _existing(tmp_path)
    before = pl.read_parquet(out)
    fake = _FakeTickflow(ok_exchanges={"SH", "BJ"})
    monkeypatch.setattr(ims, "get_client", lambda: fake)
    monkeypatch.setattr(ims, "_fetch_instruments_via_provider", lambda: None)

    assert ims.sync_instruments(tmp_path) == 0
    assert fake.calls.get("SZ") == ims._EXCHANGE_RETRIES + 1  # 带重试
    after = pl.read_parquet(out)
    assert after.equals(before)  # 旧表原样保留


def test_partial_failure_first_build_writes_subset(tmp_path, monkeypatch):
    """SZ 失败但无旧维表(首建): 降级写入部分数据, 不空手而归。"""
    fake = _FakeTickflow(ok_exchanges={"SH", "BJ"})
    monkeypatch.setattr(ims, "get_client", lambda: fake)
    monkeypatch.setattr(ims, "_fetch_instruments_via_provider", lambda: None)

    assert ims.sync_instruments(tmp_path) == 2  # SH+BJ 各 1
    df = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    assert df.height == 2


def test_all_exchanges_ok_overwrites(tmp_path, monkeypatch):
    """三交易所齐全: 正常覆盖写。"""
    out = _existing(tmp_path)
    fake = _FakeTickflow(ok_exchanges={"SH", "SZ", "BJ"})
    monkeypatch.setattr(ims, "get_client", lambda: fake)
    monkeypatch.setattr(ims, "_fetch_instruments_via_provider", lambda: None)

    assert ims.sync_instruments(tmp_path) == 3
    assert pl.read_parquet(out).height == 3
