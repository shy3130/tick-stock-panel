"""fuyao 财务适配测试 (不依赖真实网络)。

覆盖: 三大报表字段映射 (canonical 列名 + 扩展列透传 + ISO 日期口径)、
latest_only 分档 (limit 1 vs 8)、metrics 组装 (eps_basic 顺带 / bps 估值反推 /
指标 index_id 映射与未知 id 透传 / 单股指标失败不弃行)、shares 恒空、
报告期合并写入的逐列填空语义 (并集共存, 新行缺列不覆盖旧值)。
"""

from __future__ import annotations

import polars as pl
import pytest

from app.plugins.fuyao import client as fc
from app.plugins.fuyao import provider as fp
from app.plugins.fuyao.provider import FuyaoProvider
from app.services.financial_sync import _merge_report_history


class _FakeFinClient:
    """财务端点假客户端: 记录调用入参, 按表返回预置行。"""

    def __init__(
        self,
        statements: dict[str, list[dict]] | None = None,
        indicators: dict[str, list[dict]] | None = None,
        indicator_error: Exception | None = None,
        valuations: list[dict] | None = None,
        prices: list[dict] | None = None,
    ):
        self.statements = statements or {}
        self.indicators = indicators or {}
        self.indicator_error = indicator_error
        self.valuations = valuations or []
        self.prices = prices or []
        self.stmt_calls: list[tuple] = []
        self.ind_calls: list[str] = []

    def financial_statements(self, stmt, thscode, limit=1):
        self.stmt_calls.append((stmt, thscode, limit))
        return [dict(r, thscode=thscode) for r in self.statements.get(stmt, [])]

    def financial_indicators(self, thscode, report):
        self.ind_calls.append(f"{thscode}@{report}")
        if self.indicator_error:
            raise self.indicator_error
        return self.indicators.get(report, [])

    def valuations_snapshot(self, thscodes):
        return [r for r in self.valuations if r.get("thscode") in thscodes]

    def price_snapshot_batch(self, thscodes):
        return [r for r in self.prices if r.get("thscode") in thscodes]


def _provider_with(monkeypatch, fake):
    monkeypatch.setattr(
        fp, "fuyao_client", type("M", (), {"FuyaoClient": lambda **kw: fake})
    )
    monkeypatch.setattr(fp, "get_api_key", lambda: "test-key")
    monkeypatch.setattr(fp, "_HIST_INTERVAL_S", 0)
    return FuyaoProvider()


# period_end_ms: 2026-06-30 上海零点; report_date_ms: 2026-08-15 上海零点
_INCOME_ROW = {
    "period": "quarterly",
    "fiscal_year": 2026,
    "fiscal_period": "Q2",
    "report_date_ms": 1786723200000,
    "period_end_ms": 1782748800000,
    "currency": "CNY",
    "operating_income": 90703260964.48,
    "net_profit": 46033330566.78,
    "parent_holder_net_profit": 44516880421.86,
    "basic_eps": 35.57,
    "operating_expenses": 50000000000.0,  # 扶摇独有 → 扩展列
}


def test_income_mapping_canonical_columns(monkeypatch):
    fake = _FakeFinClient(statements={"income": [_INCOME_ROW]})
    provider = _provider_with(monkeypatch, fake)
    df = provider.get_financials("income", ["600519.SH"], latest_only=True)
    row = df.to_dicts()[0]
    assert row["symbol"] == "600519.SH"
    assert row["period_end"] == "2026-06-30"
    assert row["announce_date"] == "2026-08-15"
    assert row["revenue"] == pytest.approx(_INCOME_ROW["operating_income"])
    assert row["net_income"] == pytest.approx(_INCOME_ROW["net_profit"])
    assert row["net_income_attributable"] == pytest.approx(
        _INCOME_ROW["parent_holder_net_profit"]
    )
    assert row["basic_eps"] == 35.57
    # 扩展列: 扶摇独有数字字段原名透传; 字符串元数据不透传
    assert row["operating_expenses"] == 50000000000.0
    assert "thscode" not in df.columns and "ticker" not in df.columns
    # 原始名不残留 (已映射字段)
    assert "operating_income" not in df.columns and "net_profit" not in df.columns


def test_statements_limit_latest_vs_history(monkeypatch):
    fake = _FakeFinClient(statements={"income": [_INCOME_ROW]})
    provider = _provider_with(monkeypatch, fake)
    provider.get_financials("income", ["600519.SH"], latest_only=True)
    assert fake.stmt_calls == [("income", "600519.SH", 1)]
    provider.get_financials("income", ["600519.SH"], latest_only=False)
    assert fake.stmt_calls[-1] == ("income", "600519.SH", fp._FINANCIAL_HISTORY_PERIODS)


def test_balance_and_cashflow_mapping(monkeypatch):
    balance = {
        "period_end_ms": 1782748800000,
        "report_date_ms": 1786723200000,
        "assets_total": 309050784569.31,
        "total_debt": 46954432394.95,
        "holder_equity_total": 262096352174.36,
    }
    cashflow = {
        "period_end_ms": 1782748800000,
        "report_date_ms": 1786723200000,
        "act_cash_flow_net": 92000000000.0,
        "invest_cash_flow_net": -3000000000.0,
        "pay_dividends_profits_interest_cash": 64000000000.0,  # 扶摇独有 → 扩展列
    }
    fake = _FakeFinClient(
        statements={"balance_sheet": [balance], "cash_flow": [cashflow]}
    )
    provider = _provider_with(monkeypatch, fake)
    bal = provider.get_financials("balance_sheet", ["600519.SH"]).to_dicts()[0]
    assert bal["total_assets"] == pytest.approx(balance["assets_total"])
    assert bal["total_liabilities"] == pytest.approx(balance["total_debt"])
    assert bal["total_equity"] == pytest.approx(balance["holder_equity_total"])
    cf = provider.get_financials("cash_flow", ["600519.SH"]).to_dicts()[0]
    assert cf["net_operating_cash_flow"] == pytest.approx(92000000000.0)
    assert cf["net_investing_cash_flow"] == pytest.approx(-3000000000.0)
    assert cf["pay_dividends_profits_interest_cash"] == pytest.approx(64000000000.0)


class _PerSymbolIncomeClient(_FakeFinClient):
    """按标的返回不同利润表行, 模拟全市场同步的逐股请求。"""

    def __init__(self, rows_by_symbol: dict[str, dict]):
        super().__init__()
        self.rows_by_symbol = rows_by_symbol

    def financial_statements(self, stmt, thscode, limit=1):
        self.stmt_calls.append((stmt, thscode, limit))
        return [dict(self.rows_by_symbol[thscode], thscode=thscode)]


def test_income_sparse_column_after_100_null_rows(monkeypatch):
    """前 100 只标的某映射列全空、之后才出现数值时, 整表仍须构建成功。

    真实场景: interest_expenses→financial_expense 仅银行等少数标的有值,
    全市场按代码顺序同步时前 100 行全为 None, Polars 默认按前 100 行推断出
    Null 列, 遇到中信银行的 6.1199e10 即 append 失败, income 整表从未写入。
    """
    rows_by_symbol = {
        f"{600000 + i:06d}.SH": dict(_INCOME_ROW, interest_expenses=None)
        for i in range(100)
    }
    rows_by_symbol["601998.SH"] = dict(_INCOME_ROW, interest_expenses=6.1199e10)
    provider = _provider_with(monkeypatch, _PerSymbolIncomeClient(rows_by_symbol))

    df = provider.get_financials("income", list(rows_by_symbol), latest_only=True)

    assert df.height == 101
    assert df.schema["financial_expense"] == pl.Float64
    bank = df.filter(pl.col("symbol") == "601998.SH").row(0, named=True)
    assert bank["financial_expense"] == pytest.approx(6.1199e10)
    assert df.filter(pl.col("symbol") != "601998.SH")["financial_expense"].null_count() == 100


_METRICS_ABILITIES = [
    {
        "ability": "profitability",
        "indicators": [
            {"index_id": "index_weighted_avg_roe", "value": "16.7500"},
            {"index_id": "sale_gross_margin", "value": "89.5552"},
        ],
    },
    {
        "ability": "growth",
        # 实测 index_id 与文档有出入; 未映射 id 原名透传
        "indicators": [{"index_id": "fixed_asset_invest_expansion_ratio", "value": "2.12587300"}],
    },
    {
        "ability": "solvency",
        "indicators": [{"index_id": "earned_interest_multiple", "value": None}],  # null → 不写列
    },
]


def test_metrics_assembly(monkeypatch):
    fake = _FakeFinClient(
        statements={"income": [_INCOME_ROW]},
        indicators={"2026-2": _METRICS_ABILITIES},
        valuations=[{"thscode": "600519.SH", "pb_mrq": 6.455055}],
        prices=[{"thscode": "600519.SH", "last_price": 1297.4}],
    )
    provider = _provider_with(monkeypatch, fake)
    df = provider.get_financials("metrics", ["600519.SH"], latest_only=True)
    row = df.to_dicts()[0]
    assert row["period_end"] == "2026-06-30"
    assert row["announce_date"] == "2026-08-15"
    assert row["eps_basic"] == 35.57  # 顺带取自利润表
    assert row["bps"] == pytest.approx(1297.4 / 6.455055)  # 估值反推
    assert row["roe"] == pytest.approx(16.75)  # 字符串 → float
    assert row["gross_margin"] == pytest.approx(89.5552)
    assert row["fixed_asset_invest_expansion_ratio"] == pytest.approx(2.125873)
    assert "earned_interest_multiple" not in df.columns  # 全空指标不成列
    assert fake.ind_calls == ["600519.SH@2026-2"]  # report 由利润表最新期反推


def test_metrics_indicator_failure_keeps_row(monkeypatch):
    """指标端点单股失败 (如未披露期 code=5003) → 行仍写入 (eps/bps 保留)。"""
    fake = _FakeFinClient(
        statements={"income": [_INCOME_ROW]},
        indicator_error=fc.FuyaoError("code=5003"),
        valuations=[{"thscode": "600519.SH", "pb_mrq": 6.455055}],
        prices=[{"thscode": "600519.SH", "last_price": 1297.4}],
    )
    provider = _provider_with(monkeypatch, fake)
    df = provider.get_financials("metrics", ["600519.SH"], latest_only=True)
    row = df.to_dicts()[0]
    assert row["symbol"] == "600519.SH"
    assert row["eps_basic"] == 35.57
    assert "roe" not in df.columns


def test_metrics_skips_symbol_without_income(monkeypatch):
    fake = _FakeFinClient(statements={"income": []})
    provider = _provider_with(monkeypatch, fake)
    assert provider.get_financials("metrics", ["600519.SH"]).is_empty()


def test_shares_returns_empty(monkeypatch):
    provider = _provider_with(monkeypatch, _FakeFinClient())
    assert provider.get_financials("shares", ["600519.SH"]).is_empty()


def test_merge_fills_missing_cells_from_old_rows():
    """逐列填空: 同报告期新行缺的列由旧行补齐, 有值则覆盖 (并集共存语义)。"""
    old = pl.DataFrame({
        "symbol": ["600519.SH", "600519.SH"],
        "period_end": ["2026-03-31", "2026-06-30"],
        "announce_date": ["2026-04-20", "2026-08-10"],
        "diluted_eps": [68.1, 70.2],       # tickflow 提供, fuyao 没有
        "net_income": [280.0, 460.0],
    })
    new = pl.DataFrame({
        "symbol": ["600519.SH"],
        "period_end": ["2026-06-30"],
        "announce_date": ["2026-08-15"],   # 更晚公告 → 该期以新行为基准
        "net_income": [461.5],             # 修正值覆盖
        # diluted_eps 缺失 → 由旧行 70.2 补齐
        "bps": [200.99],                   # fuyao 扩展列, 旧行没有
    })
    merged = _merge_report_history(old, new).to_dicts()
    assert len(merged) == 2
    q2 = next(r for r in merged if r["period_end"] == "2026-06-30")
    assert q2["net_income"] == pytest.approx(461.5)   # 新值覆盖
    assert q2["diluted_eps"] == pytest.approx(70.2)   # 旧行补齐
    assert q2["bps"] == pytest.approx(200.99)         # 扩展列并入
    q1 = next(r for r in merged if r["period_end"] == "2026-03-31")
    assert q1["diluted_eps"] == pytest.approx(68.1)   # 未触碰期原样保留
    # 旧公告覆盖新公告的倒序场景: announce 早的行不覆盖晚的
    reversed_new = pl.DataFrame({
        "symbol": ["600519.SH"],
        "period_end": ["2026-06-30"],
        "announce_date": ["2026-08-01"],
        "net_income": [999.0],
    })
    q2b = _merge_report_history(old, reversed_new).to_dicts()[1]
    # 公告更晚的 old 行 (08-10) 胜出, 早公告的新行不覆盖 → 业绩修正以最新公告为准
    assert q2b["net_income"] == pytest.approx(460.0)
