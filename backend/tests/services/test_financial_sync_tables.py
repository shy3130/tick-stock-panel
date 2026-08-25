import polars as pl

from app.capabilities import Cap, CapabilityLimits, CapabilitySet
from app.services import financial_sync


def test_financial_sync_includes_quick_and_forecast_sources() -> None:
    assert "quick" in financial_sync.FINANCIAL_TABLES
    assert "forecast" in financial_sync.FINANCIAL_TABLES
    assert financial_sync._PROVIDER_TABLE_MAP["quick"] == "quick"
    assert financial_sync._PROVIDER_TABLE_MAP["forecast"] == "forecast"


def test_sync_quick_delegates_to_sync_table(tmp_path, monkeypatch) -> None:
    # sync_quick 仅经由 _sync_table 走 active provider 的本地财务表,
    # 不再读取既有 parquet / 合并外部源。
    seen: dict = {}

    def fake_sync_table(table, symbols, data_dir, capset, latest_only=True):
        seen["table"] = table
        seen["symbols"] = list(symbols)
        seen["latest_only"] = latest_only
        return 7

    monkeypatch.setattr(financial_sync, "_sync_table", fake_sync_table)
    capset = CapabilitySet({Cap.FINANCIAL: CapabilityLimits()})
    assert financial_sync.sync_quick(tmp_path, capset) == 7
    assert seen["table"] == "quick"
    assert seen["latest_only"] is True


def test_sync_forecast_delegates_to_sync_table(tmp_path, monkeypatch) -> None:
    seen: dict = {}

    def fake_sync_table(table, symbols, data_dir, capset, latest_only=True):
        seen["table"] = table
        return 4

    monkeypatch.setattr(financial_sync, "_sync_table", fake_sync_table)
    capset = CapabilitySet({Cap.FINANCIAL: CapabilityLimits()})
    assert financial_sync.sync_forecast(tmp_path, capset) == 4
    assert seen["table"] == "forecast"




def test_sync_all_uses_table_specific_sync_functions(tmp_path, monkeypatch) -> None:
    calls: list[str] = []

    def make_sync(table: str):
        def _sync(data_dir, capset):
            calls.append(table)
            return len(calls)
        return _sync

    monkeypatch.setattr(
        financial_sync,
        "_financial_sync_functions",
        lambda: {table: make_sync(table) for table in financial_sync.FINANCIAL_TABLES},
    )

    capset = CapabilitySet({Cap.FINANCIAL: CapabilityLimits()})
    result = financial_sync.sync_all(tmp_path, capset)
    assert calls == list(financial_sync.FINANCIAL_TABLES)
    assert result["quick"] == 5
    assert result["forecast"] == 6



# ================================================================
# 读取期 canonicalization（_canonicalize_financial_df）
# ================================================================


def test_canonicalize_metrics_maps_core_aliases() -> None:
    df = pl.DataFrame({
        "symbol": ["301308.SZ"],
        "t_date": ["2025-09-30"],
        "notice_date": ["2025-10-30"],
        "basic_eps": [1.71],
        "bps": [17.96],
        "weight_avg_roe": [10.19],
        "net_profit": [712633024.0],
        "total_income": [16734300160.0],
        "net_cash_flow": [2.2],
        "gross_margin": [15.29],
        "yo_y_income": [26.12],
        "yo_y_profit": [27.95],
    })
    out = financial_sync._canonicalize_financial_df(df, "metrics")
    row = out.row(0, named=True)
    # common
    assert row["period_end"] == "2025-09-30"
    assert row["announce_date"] == "2025-10-30"
    # metrics aliases
    assert row["eps_basic"] == 1.71
    assert row["ocfps"] == 2.2
    assert row["roe"] == 10.19
    assert row["revenue_yoy"] == 26.12
    assert row["net_income_yoy"] == 27.95
    # already-canonical passthrough
    assert row["bps"] == 17.96
    assert row["gross_margin"] == 15.29
    # derived net_margin = net_profit/total_income*100 (百分点口径)
    assert row["net_margin"] is not None
    assert abs(row["net_margin"] - (712633024.0 / 16734300160.0 * 100.0)) < 1e-6


def test_canonicalize_income_maps_aliases_without_faking_net_income() -> None:
    df = pl.DataFrame({
        "symbol": ["301308.SZ"],
        "t_date": ["2025-09-30"],
        "notice_date": ["2025-10-30"],
        "total_oper_income": [16_734_300_160.0],
        "operate_cost": [14_176_400_384.0],
        "operate_profit": [930_110_016.0],
        "sale_expense": [593_662_976.0],
        "manage_expense": [423_147_008.0],
        "finance_expense": [133_014_000.0],
        "parent_net_profit": [712_633_024.0],
        "total_profit": [900_000_000.0],
    })
    out = financial_sync._canonicalize_financial_df(df, "income")
    row = out.row(0, named=True)
    assert row["revenue"] == 16_734_300_160.0
    assert row["operating_cost"] == 14_176_400_384.0
    assert row["operating_profit"] == 930_110_016.0
    assert row["selling_expense"] == 593_662_976.0
    assert row["admin_expense"] == 423_147_008.0
    assert row["financial_expense"] == 133_014_000.0
    assert row["net_income_attributable"] == 712_633_024.0
    # 归母净利不得伪装成总净利：上游无净利润列 → net_income 保持缺失
    assert "net_income" not in out.columns


def test_canonicalize_balance_maps_monetary_and_short_loan() -> None:
    df = pl.DataFrame({
        "symbol": ["301308.SZ"],
        "t_date": ["2025-09-30"],
        "notice_date": ["2025-10-30"],
        "monetary_funds": [1_000_000_000.0],
        "short_loan": [200_000_000.0],
        "total_assets": [5_000_000_000.0],  # 同名 canonical，应原样保留
    })
    out = financial_sync._canonicalize_financial_df(df, "balance_sheet")
    row = out.row(0, named=True)
    assert row["cash_and_equivalents"] == 1_000_000_000.0
    assert row["short_term_borrowing"] == 200_000_000.0
    assert row["total_assets"] == 5_000_000_000.0


def test_canonicalize_cash_flow_maps_four_streams() -> None:
    df = pl.DataFrame({
        "symbol": ["301308.SZ"],
        "t_date": ["2025-09-30"],
        "notice_date": ["2025-10-30"],
        "net_cash_operate": [500_000_000.0],
        "net_cash_invest": [-300_000_000.0],
        "net_cash_finance": [100_000_000.0],
        "net_cash_flow": [300_000_000.0],
    })
    out = financial_sync._canonicalize_financial_df(df, "cash_flow")
    row = out.row(0, named=True)
    assert row["net_operating_cash_flow"] == 500_000_000.0
    assert row["net_investing_cash_flow"] == -300_000_000.0
    assert row["net_financing_cash_flow"] == 100_000_000.0
    assert row["net_cash_change"] == 300_000_000.0


def test_canonicalize_sentinel_announce_date_is_nulled() -> None:
    # 0001-01-01 哨兵 / 空串 / null 都视作无公告日；有效日期保留。
    df = pl.DataFrame({
        "t_date": ["2025-09-30", "2025-06-30", "2025-03-31", "2024-12-31"],
        "notice_date": ["0001-01-01", "", None, "2025-01-28"],
    })
    out = financial_sync._canonicalize_financial_df(df, "metrics")
    announce = out["announce_date"].to_list()
    assert announce == [None, None, None, "2025-01-28"]
    assert out["period_end"].to_list() == [
        "2025-09-30", "2025-06-30", "2025-03-31", "2024-12-31",
    ]


def test_canonical_existing_value_takes_priority_over_raw() -> None:
    # canonical 列已带值时不被 raw 覆盖；仅 canonical 为空时才从 raw 回填。
    df = pl.DataFrame({
        "t_date": ["2025-09-30"],
        "notice_date": ["2025-10-30"],
        "eps_basic": [9.99],   # canonical 已存在
        "basic_eps": [1.71],   # raw 不应覆盖 canonical
        "roe": [None],         # canonical 空 → 用 raw 回填
        "weight_avg_roe": [10.19],
    })
    out = financial_sync._canonicalize_financial_df(df, "metrics")
    row = out.row(0, named=True)
    assert row["eps_basic"] == 9.99
    assert row["roe"] == 10.19


def test_canonicalize_net_margin_zero_denominator_is_null() -> None:
    # 分母为 0 时不派生 net_margin（保持 null）；net_profit 缺失同样不派生。
    df = pl.DataFrame({
        "t_date": ["2025-09-30", "2025-06-30"],
        "net_profit": [100.0, None],
        "total_income": [0.0, 200.0],
    })
    out = financial_sync._canonicalize_financial_df(df, "metrics")
    assert out["net_margin"].to_list() == [None, None]


def test_canonicalize_metrics_mixed_dtype_yoy_stays_float() -> None:
    # fstore annual: yo_y_income=DOUBLE, yoy_income=VARCHAR。混合 dtype 的 coalesce
    # 若不做 Float64 cast 会把 revenue_yoy 污染成 String，前端 formatValue 只接受
    # number 会显示 —。这里断言结果列是 Python float 且 dtype 为 Float64。
    df = pl.DataFrame({
        "t_date": ["2025-09-30"],
        "notice_date": ["2025-10-30"],
        "yo_y_income": pl.Series([26.12], dtype=pl.Float64),
        "yoy_income": pl.Series([None], dtype=pl.Utf8),   # VARCHAR 列, 实测全 null
        "yo_y_profit": pl.Series([27.95], dtype=pl.Float64),
        "yoy_profit": pl.Series(["garbage"], dtype=pl.Utf8),  # 不可解析 → null
        "basic_eps": pl.Series([1.71], dtype=pl.Float64),
        "weight_avg_roe": pl.Series([10.19], dtype=pl.Float64),
        "net_cash_flow": pl.Series([2.2], dtype=pl.Float64),
    })
    out = financial_sync._canonicalize_financial_df(df, "metrics")
    # dtype 必须是 Float64, 不能被 VARCHAR 污染成 String
    assert out.schema["revenue_yoy"] == pl.Float64
    assert out.schema["net_income_yoy"] == pl.Float64
    row = out.row(0, named=True)
    # 值是 Python float（经 to_dicts() → JSON 后是 number 不是 string）
    assert isinstance(row["revenue_yoy"], float)
    assert isinstance(row["net_income_yoy"], float)
    assert row["revenue_yoy"] == 26.12
    assert row["net_income_yoy"] == 27.95
    assert row["eps_basic"] == 1.71
    assert row["roe"] == 10.19
    assert row["ocfps"] == 2.2


def test_canonical_announce_sentinel_does_not_hijack_notice_date() -> None:
    # canonical announce_date 已存在但值为哨兵 0001-01-01 / 空串时,
    # 不得抢占有效的 raw notice_date —— 哨兵应被 sanitize 成 null 再 coalesce。
    df = pl.DataFrame({
        "t_date": ["2025-09-30", "2025-06-30"],
        "announce_date": ["0001-01-01", ""],          # canonical 哨兵/空串
        "notice_date": ["2025-10-30", "2025-08-23"],  # 有效 raw
    })
    out = financial_sync._canonicalize_financial_df(df, "metrics")
    announce = out["announce_date"].to_list()
    assert announce == ["2025-10-30", "2025-08-23"]