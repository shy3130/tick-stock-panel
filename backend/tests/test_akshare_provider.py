"""AkshareProvider 契约与字段口径测试。

不依赖真实网络与 akshare 包: 用假 client 返回东财批量报表样例行(实测列名,
2026-08 对拍 stock_lrb_em/stock_zcfz_em/stock_xjll_em/stock_yjbb_em/
stock_zh_a_gbjg_em), 验证 canonical 字段映射、归母口径 (净利润 →
net_income_attributable)、symbol 后缀归一、period_end 注入、announce_date
ms→ISO、latest_only 按标的取最新期与错误软失败。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.plugins.akshare import provider as ap
from app.plugins.akshare.provider import AkshareProvider

# 2026-08-15 北京零点 (= 2026-08-14 16:00 UTC) 的 epoch ms
_ANNOUNCE_MS = 1786752000000


class _FakeClient:
    """记录批量请求的报告期序列, 按 (method, period) 返回预置 polars 帧。"""

    def __init__(self, tables: dict | None = None, shares: dict | None = None):
        self.tables = tables or {}  # {(method, period): pl.DataFrame}
        self.shares = shares or {}  # {symbol: pl.DataFrame | Exception}
        self.calls: list[tuple[str, str]] = []

    def _bulk(self, method: str, period: str) -> pl.DataFrame:
        self.calls.append((method, period))
        out = self.tables.get((method, period))
        if isinstance(out, Exception):
            raise out
        return out if out is not None else pl.DataFrame()

    def income_market(self, period: str) -> pl.DataFrame:
        return self._bulk("income_market", period)

    def balance_sheet_market(self, period: str) -> pl.DataFrame:
        return self._bulk("balance_sheet_market", period)

    def cash_flow_market(self, period: str) -> pl.DataFrame:
        return self._bulk("cash_flow_market", period)

    def metrics_market(self, period: str) -> pl.DataFrame:
        return self._bulk("metrics_market", period)

    def share_structure(self, symbol: str) -> pl.DataFrame:
        out = self.shares.get(symbol)
        if isinstance(out, Exception):
            raise out
        return out if out is not None else pl.DataFrame()

    def close(self) -> None:
        pass


def _provider(client: _FakeClient) -> AkshareProvider:
    p = AkshareProvider()
    p._client = client
    return p


def _lrb(code: str, net_profit: float = 100.0, announce=_ANNOUNCE_MS) -> dict:
    """东财 stock_lrb_em 实测列名 (2026-08)。"""
    return {
        "序号": 1,
        "股票代码": code,
        "股票简称": "测试",
        "净利润": net_profit,
        "净利润同比": 5.0,
        "营业总收入": 1000.0,
        "营业总收入同比": 10.0,
        "营业总支出-营业支出": 600.0,
        "营业总支出-销售费用": 50.0,
        "营业总支出-管理费用": 80.0,
        "营业总支出-财务费用": 10.0,
        "营业总支出-营业总支出": 740.0,
        "营业利润": 260.0,
        "利润总额": 255.0,
        "公告日期": announce,
    }


def _periods(today: date, n: int) -> list[str]:
    ends = []
    for yy in range(today.year - 3, today.year + 1):
        for mm, dd in ((3, 31), (6, 30), (9, 30), (12, 31)):
            d = date(yy, mm, dd)
            if d <= today:
                ends.append(d)
    return [d.strftime("%Y%m%d") for d in sorted(ends, reverse=True)[:n]]


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(ap, "_recent_periods", lambda _t, n: _periods(date(2026, 8, 20), n))


# ---- 能力声明与可用性 ----


def test_datasets_declaration():
    """只声明 financial; 其余数据集 provider_has_dataset 为 False → 回退 tickflow。"""
    config = AkshareProvider().config
    assert "financial" in config.datasets
    assert "realtime" not in config.datasets
    assert "daily" not in config.datasets


def test_availability(monkeypatch):
    monkeypatch.setattr(ap.importlib.util, "find_spec", lambda name: object())
    assert ap.availability() == (True, "ok")
    monkeypatch.setattr(ap.importlib.util, "find_spec", lambda name: None)
    ok, reason = ap.availability()
    assert ok is False and "akshare" in reason


# ---- 字段映射与口径 ----


def test_income_maps_canonical_and_attributable_net_income():
    """利润表: canonical 列映射; 净利润实测为归母口径 → net_income_attributable。"""
    client = _FakeClient(
        {("income_market", "20260630"): pl.DataFrame([_lrb("600519", net_profit=454.0)])}
    )
    df = _provider(client).get_financials("income", ["600519.SH"], latest_only=True)
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["symbol"] == "600519.SH"
    assert row["period_end"] == "2026-06-30"
    assert row["announce_date"] == "2026-08-15"  # 上海零点 ms +8h
    assert row["revenue"] == 1000.0
    assert row["operating_cost"] == 600.0
    assert row["net_income_attributable"] == 454.0
    # 批量接口不提供总净利字段 → 不映射 net_income, 不凭字段名伪造口径
    assert "net_income" not in df.columns


def test_income_filters_to_requested_symbols_and_normalizes_suffix():
    """批量帧含全市场行, 只保留请求标的; 深/北标的按代码段归一后缀。"""
    frame = pl.DataFrame([_lrb("600519"), _lrb("000001"), _lrb("830799"), _lrb("601398")])
    client = _FakeClient({("income_market", "20260630"): frame})
    df = _provider(client).get_financials("income", ["000001.SZ", "830799.BJ"], latest_only=True)
    assert sorted(df["symbol"].to_list()) == ["000001.SZ", "830799.BJ"]


def test_balance_and_cashflow_maps():
    client = _FakeClient(
        {
            ("balance_sheet_market", "20260630"): pl.DataFrame(
                [
                    {
                        "股票代码": "600519",
                        "资产-货币资金": 500.0,
                        "资产-应收账款": 30.0,
                        "资产-总资产": 2000.0,
                        "负债-总负债": 400.0,
                        "股东权益合计": 1600.0,
                        "公告日期": _ANNOUNCE_MS,
                    }
                ]
            ),
            ("cash_flow_market", "20260630"): pl.DataFrame(
                [
                    {
                        "股票代码": "600519",
                        "经营性现金流-现金流量净额": 300.0,
                        "投资性现金流-现金流量净额": -100.0,
                        "融资性现金流-现金流量净额": -50.0,
                        "净现金流-净现金流": 150.0,
                        "公告日期": _ANNOUNCE_MS,
                    }
                ]
            ),
        }
    )
    p = _provider(client)
    b = p.get_financials("balance_sheet", ["600519.SH"], latest_only=True).row(0, named=True)
    assert b["total_assets"] == 2000.0 and b["total_equity"] == 1600.0
    c = p.get_financials("cash_flow", ["600519.SH"], latest_only=True).row(0, named=True)
    assert c["net_operating_cash_flow"] == 300.0 and c["net_cash_change"] == 150.0


def test_metrics_maps_percent_scale_as_is():
    """业绩报表: roe/yoy 为百分数数值, 与项目 metrics 因子口径一致直接透传。"""
    client = _FakeClient(
        {
            ("metrics_market", "20260630"): pl.DataFrame(
                [
                    {
                        "股票代码": "600519",
                        "每股收益": 36.18,
                        "营业总收入-营业总收入": 910.9,
                        "营业总收入-同比增长": 9.16,
                        "净利润-净利润": 454.0,
                        "净利润-同比增长": 8.89,
                        "每股净资产": 189.97,
                        "净资产收益率": 17.89,
                        "销售毛利率": 91.0,
                        "每股经营现金流量": 30.0,
                        "最新公告日期": _ANNOUNCE_MS,
                    }
                ]
            )
        }
    )
    row = (
        _provider(client)
        .get_financials("metrics", ["600519.SH"], latest_only=True)
        .row(0, named=True)
    )
    assert row["roe"] == 17.89  # 百分制, 不除 100
    assert row["revenue_yoy"] == 9.16
    assert row["bps"] == 189.97
    assert row["eps_basic"] == 36.18
    assert row["net_income_attributable"] == 454.0
    assert row["announce_date"] == "2026-08-15"


# ---- latest_only 语义 ----


def test_latest_only_takes_per_symbol_max_period():
    """披露季错峰: 拉最近 2 个候选期, 每股保留 period_end 最大的一行。"""
    client = _FakeClient(
        {
            ("income_market", "20260630"): pl.DataFrame([_lrb("600519", 454.0)]),
            ("income_market", "20260331"): pl.DataFrame([_lrb("000001", 100.0)]),
        }
    )
    df = _provider(client).get_financials("income", ["600519.SH", "000001.SZ"], latest_only=True)
    assert df.height == 2
    pe = {r["symbol"]: r["period_end"] for r in df.iter_rows(named=True)}
    assert pe == {"600519.SH": "2026-06-30", "000001.SZ": "2026-03-31"}


def test_history_pulls_multiple_periods():
    """latest_only=False: 逐期全量拉取并保留各期行。"""
    client = _FakeClient(
        {
            ("income_market", "20260630"): pl.DataFrame([_lrb("600519", 2.0)]),
            ("income_market", "20260331"): pl.DataFrame([_lrb("600519", 1.0)]),
        }
    )
    df = _provider(client).get_financials("income", ["600519.SH"], latest_only=False)
    assert sorted(df["period_end"].to_list()) == ["2026-03-31", "2026-06-30"]


# ---- shares ----


def test_shares_maps_float_shares_and_change_date():
    """股本: 变更日期(ms) → period_end; 已上市流通A股 → float_shares。"""
    client = _FakeClient(
        shares={
            "600519.SH": pl.DataFrame(
                [
                    {
                        "变更日期": _ANNOUNCE_MS,
                        "总股本": 1250.0,
                        "已上市流通A股": 1250.0,
                        "流通受限股份": 0.0,
                    },
                    {
                        "变更日期": 1437091200000,
                        "总股本": 1256.0,
                        "已上市流通A股": 1256.0,
                        "流通受限股份": 0.0,
                    },
                ]
            )
        }
    )
    df = _provider(client).get_financials("shares", ["600519.SH"], latest_only=True)
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["period_end"] == "2026-08-15"  # 最新一条变更记录
    assert row["float_shares"] == 1250.0
    assert row["total_shares"] == 1250.0

    df_all = _provider(client).get_financials("shares", ["600519.SH"], latest_only=False)
    assert df_all.height == 2


def test_shares_throttles_per_symbol(monkeypatch):
    sleeps = []
    monkeypatch.setattr(ap.time, "sleep", sleeps.append)
    client = _FakeClient(shares={s: pl.DataFrame() for s in ("600519.SH", "000001.SZ")})
    _provider(client).get_financials("shares", ["600519.SH", "000001.SZ"])
    assert sleeps == [ap._SHARE_INTERVAL_S]


# ---- 错误与边界 ----


def test_bulk_api_error_returns_empty():
    client = _FakeClient({("income_market", "20260630"): RuntimeError("限频")})
    df = _provider(client).get_financials("income", ["600519.SH"], latest_only=True)
    assert df.is_empty()


def test_unknown_table_returns_empty():
    df = _provider(_FakeClient()).get_financials("daily", ["600519.SH"])
    assert df.is_empty()


def test_empty_symbols_returns_empty():
    df = _provider(_FakeClient()).get_financials("income", [])
    assert df.is_empty()


def test_announce_date_handles_iso_and_none():
    """公告日期兼容已格式化 ISO 串与缺失值。"""
    row = _lrb("600519", announce="2026-08-13")
    client = _FakeClient({("income_market", "20260630"): pl.DataFrame([row])})
    out = _provider(client).get_financials("income", ["600519.SH"], latest_only=True)
    assert out.row(0, named=True)["announce_date"] == "2026-08-13"

    row2 = _lrb("600519", announce=None)
    client2 = _FakeClient({("income_market", "20260630"): pl.DataFrame([row2])})
    out2 = _provider(client2).get_financials("income", ["600519.SH"], latest_only=True)
    assert out2.row(0, named=True)["announce_date"] is None
