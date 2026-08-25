import functools
import re
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.api import kline


class FakeRepo:
    def __init__(self):
        self.daily_calls = 0
        self.batch_calls = 0
        self.last_execute_one_sql = None

    def get_daily(self, symbol, start, end):
        self.daily_calls += 1
        return pl.DataFrame()

    def get_daily_batch(self, symbols, start, end, columns=None):
        self.batch_calls += 1
        return pl.DataFrame()

    def execute_one(self, sql, params=None):
        self.last_execute_one_sql = sql
        return None


class FakeProvider:
    def __init__(self):
        self.daily_args = None
        self.daily_calls = []
        self.adj_args = None

    def get_daily(self, symbols, start_time, end_time, asset_type):
        self.daily_args = (symbols, start_time, end_time, asset_type)
        self.daily_calls.append((symbols, start_time, end_time, asset_type))
        return pl.DataFrame({
            "symbol": [symbols[0]],
            "date": [start_time.date()],
            "open": [1.0],
            "high": [1.0],
            "low": [1.0],
            "close": [1.0],
            "volume": [100.0],
            "amount": [100.0],
        })

    def get_adj_factors(self, symbols, start_time, end_time, asset_type):
        self.adj_args = (symbols, start_time, end_time, asset_type)
        return pl.DataFrame()

    def get_instruments(self, asset_type):
        rows = {
            "stock": ("600519.SH", "贵州茅台", "600519", "SH", 2_000.0, 1_000.0),
            "hk": ("02577.HK", "英诺赛科", "02577", "HK", 2_000.0, 1_000.0),
            "etf": ("513050.SH", "中概互联", "513050", "SH", 2_000.0, 1_000.0),
        }
        symbol, name, code, exchange, total_shares, float_shares = rows.get(
            asset_type,
            ("000001.INDEX", "指数", "000001", "INDEX", None, None),
        )
        return pl.DataFrame({
            "symbol": [symbol],
            "name": [name],
            "code": [code],
            "exchange": [exchange],
            "asset_type": [asset_type],
            "source": ["fake"],
            "total_shares": [total_shares],
            "float_shares": [float_shares],
        })


class CachedRepo(FakeRepo):
    def get_daily(self, symbol, start, end):
        self.daily_calls += 1
        return pl.DataFrame({
            "symbol": [symbol],
            "date": [start],
            "open": [9.0],
            "high": [9.0],
            "low": [9.0],
            "close": [9.0],
            "volume": [9.0],
        })

    def get_daily_batch(self, symbols, start, end, columns=None):
        self.batch_calls += 1
        return pl.DataFrame({
            "symbol": [symbols[0]],
            "date": [start],
            "open": [9.0],
            "high": [9.0],
            "low": [9.0],
            "close": [9.0],
            "volume": [9.0],
        })


def request(repo=None):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo or FakeRepo(), quote_service=None)))


class _OrderedExecuteRepo:
    """execute_one 模拟：按 SQL 中 ORDER BY 的列/方向对候选行排序后返回首行。

    把 _get_stock_info 的 deterministic ORDER BY 当作可观察契约验证——当排序
    列或方向写错、或 ORDER BY 缺失时，返回的行不同，测试失败。
    """

    def __init__(self, candidate_rows):
        # candidate_rows: list[tuple(name, total_shares, float_shares)]
        self._rows = candidate_rows
        self.last_execute_one_sql = None

    def execute_one(self, sql, params=None):
        self.last_execute_one_sql = sql
        ordered = _order_by_rows(sql, self._rows)
        return ordered[0] if ordered else None


def _order_by_rows(sql, rows):
    """根据 SQL 的 ``ORDER BY col [ASC|DESC] [NULLS LAST|FIRST] ...`` 对 rows 排序。"""
    m = re.search(r"ORDER BY (.+?)\s+LIMIT", sql, re.IGNORECASE | re.DOTALL)
    if not m:
        return list(rows)
    col_idx = {"name": 0, "total_shares": 1, "float_shares": 2}
    keys = []
    for part in m.group(1).split(","):
        toks = part.strip().split()
        if not toks or toks[0].lower() not in col_idx:
            continue
        idx = col_idx[toks[0].lower()]
        rest = " ".join(toks[1:]).upper()
        direction = "DESC" if "DESC" in rest else "ASC"
        if "NULLS FIRST" in rest:
            nulls_last = False
        elif "NULLS LAST" in rest:
            nulls_last = True
        else:
            nulls_last = direction == "ASC"  # DuckDB default
        keys.append((idx, direction, nulls_last))
    if not keys:
        return list(rows)

    def _cmp(a, b):
        for idx, direction, nulls_last in keys:
            av, bv = a[idx], b[idx]
            if av is None and bv is None:
                continue
            if av is None:
                return 1 if nulls_last else -1
            if bv is None:
                return -1 if nulls_last else 1
            if av < bv:
                return -1 if direction == "ASC" else 1
            if av > bv:
                return 1 if direction == "ASC" else -1
        return 0

    return sorted(rows, key=functools.cmp_to_key(_cmp))


def test_stock_info_uses_deterministic_order_for_limit():
    # 同一 symbol 多行，deterministic ORDER BY (name ASC NULLS LAST, ...) 必须选出
    # name 最小且非空的那行。若排序列/方向错误、或 ORDER BY 缺失，选出的行会不同。
    candidates = [
        ("C-Name", 1000, 800),
        ("A-Name", 2000, 1500),   # name ASC 最小，应被选中
        ("B-Name", 500, 300),
        (None, 999, 999),          # NULLS LAST，排末尾
    ]
    repo = _OrderedExecuteRepo(candidates)
    info = kline._get_stock_info(repo, "600519.SH")
    assert info["name"] == "A-Name"
    assert info["total_shares"] == 2000
    assert info["float_shares"] == 1500
    # 仍保留 SQL 断言，确保 ORDER BY 子句确实存在（删除/拼写错误会失败）。
    assert "ORDER BY" in repo.last_execute_one_sql
    assert "LIMIT 1" in repo.last_execute_one_sql


def test_daily_local_fallback_passes_datetime_to_provider(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily(
        request(),
        "600519.SH",
        days=120,
        start_date="2026-07-01",
        end_date="2026-07-02",
        ext_columns=None,
    )

    assert resp["source"] == "local_disk"
    assert isinstance(provider.daily_args[1], datetime)
    assert isinstance(provider.daily_args[2], datetime)
    assert provider.daily_args[3] == "stock"
    assert isinstance(provider.adj_args[1], datetime)
    assert isinstance(provider.adj_args[2], datetime)
    assert provider.adj_args[3] == "stock"
    assert resp["stock_info"]["float_shares"] == 1_000.0
    assert resp["rows"][0]["turnover_rate"] == 10.0


def test_daily_local_fallback_passes_hk_asset_type_and_skips_adj(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily(
        request(),
        "02577.HK",
        days=120,
        start_date="2026-07-01",
        end_date="2026-07-02",
        ext_columns=None,
    )

    assert resp["source"] == "local_disk"
    assert provider.daily_args[0] == ["02577.HK"]
    assert provider.daily_args[3] == "hk"
    assert provider.adj_args is None
    assert resp["stock_info"]["float_shares"] == 1_000.0
    assert resp["rows"][0]["turnover_rate"] == 10.0


def test_daily_batch_local_fallback_passes_datetime_to_provider(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily_batch(request(), {"symbols": ["600519.SH"], "days": 5})

    assert "600519.SH" in resp["data"]
    assert isinstance(provider.daily_args[1], datetime)
    assert isinstance(provider.daily_args[2], datetime)
    assert provider.daily_args[3] == "stock"


def test_daily_batch_local_fallback_splits_asset_types(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily_batch(request(), {"symbols": ["600519.SH", "513050.SH", "02577.HK"], "days": 5})

    assert set(resp["data"]) == {"600519.SH", "513050.SH", "02577.HK"}
    assert [(args[0], args[3]) for args in provider.daily_calls] == [
        (["600519.SH"], "stock"),
        (["513050.SH"], "etf"),
        (["02577.HK"], "hk"),
    ]


def test_daily_local_mode_ignores_cached_raw(monkeypatch):
    provider = FakeProvider()
    repo = CachedRepo()
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily(
        request(repo),
        "600519.SH",
        days=120,
        start_date="2026-07-01",
        end_date="2026-07-02",
        ext_columns=None,
    )

    assert repo.daily_calls == 0
    assert resp["source"] == "local_disk"
    assert resp["rows"][0]["close"] == 1.0


def test_live_candle_overwrites_derived_change_fields():
    class _QuoteService:
        def get_enriched_today(self):
            return (
                pl.DataFrame({
                    "symbol": ["600519.SH"],
                    "date": [date.today()],
                    "open": [11.0],
                    "high": [12.0],
                    "low": [10.5],
                    "close": [11.5],
                    "volume": [123.0],
                    "amount": [1400.0],
                    "change_pct": [0.15],
                    "change_amount": [1.5],
                    "amplitude": [0.15],
                    "turnover_rate": [2.3],
                }),
                date.today(),
            )

    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(quote_service=_QuoteService())))
    rows = [{
        "date": str(date.today()),
        "symbol": "600519.SH",
        "close": 10.0,
        "change_pct": 0.01,
        "change_amount": 0.1,
        "amplitude": 0.04,
        "turnover_rate": 1.0,
    }]

    out = kline._maybe_inject_live_candle(req, "600519.SH", rows)

    assert out[0]["close"] == 11.5
    assert out[0]["change_pct"] == 0.15
    assert out[0]["change_amount"] == 1.5
    assert out[0]["amplitude"] == 0.15
    assert out[0]["turnover_rate"] == 2.3


class FakeProviderWithMoneyflow(FakeProvider):
    def __init__(self, moneyflow_df):
        super().__init__()
        self._moneyflow_df = moneyflow_df
        self.moneyflow_args = None

    def get_moneyflow_range(self, symbol, start, end):
        self.moneyflow_args = (symbol, start, end)
        return self._moneyflow_df


def test_daily_local_mode_merges_main_net_inflow_for_stock(monkeypatch):
    moneyflow_df = pl.DataFrame({
        "date": ["2026-07-01"],
        "main_net_inflow": [300.0],
    })
    provider = FakeProviderWithMoneyflow(moneyflow_df)
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily(
        request(),
        "600519.SH",
        days=120,
        start_date="2026-07-01",
        end_date="2026-07-01",
        ext_columns=None,
    )

    assert provider.moneyflow_args is not None
    assert provider.moneyflow_args[0] == "600519.SH"
    assert resp["rows"][0]["main_net_inflow"] == 300.0


def test_daily_local_mode_skips_moneyflow_for_non_stock(monkeypatch):
    provider = FakeProviderWithMoneyflow(None)

    def _fail_if_called(symbol, start, end):
        raise AssertionError("get_moneyflow_range should not be called for non-stock asset types")

    provider.get_moneyflow_range = _fail_if_called

    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily(
        request(),
        "02577.HK",
        days=120,
        start_date="2026-07-01",
        end_date="2026-07-02",
        ext_columns=None,
    )

    assert resp["rows"][0]["main_net_inflow"] is None


def test_daily_local_mode_main_net_inflow_null_when_moneyflow_empty(monkeypatch):
    provider = FakeProviderWithMoneyflow(pl.DataFrame())
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily(
        request(),
        "600519.SH",
        days=120,
        start_date="2026-07-01",
        end_date="2026-07-01",
        ext_columns=None,
    )

    assert resp["rows"][0]["main_net_inflow"] is None


def test_daily_local_mode_moneyflow_exception_does_not_500(monkeypatch):
    provider = FakeProvider()

    def _raise(symbol, start, end):
        raise RuntimeError("disk read failed")

    provider.get_moneyflow_range = _raise

    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily(
        request(),
        "600519.SH",
        days=120,
        start_date="2026-07-01",
        end_date="2026-07-01",
        ext_columns=None,
    )

    assert resp["rows"][0]["main_net_inflow"] is None
    assert resp["rows"][0]["close"] == 1.0


def test_daily_batch_local_mode_ignores_cached_raw(monkeypatch):
    provider = FakeProvider()
    repo = CachedRepo()
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily_batch(request(repo), {"symbols": ["600519.SH"], "days": 5})

    assert repo.batch_calls == 0
    assert resp["data"]["600519.SH"][0]["close"] == 1.0


def test_parse_enriched_range_repair_accepts_bounded_range():
    start, end = kline._parse_enriched_range_repair(
        {"start_date": "2026-07-03", "end_date": "2026-08-02"},
        today=date(2026, 8, 2),
    )

    assert (start, end) == (date(2026, 7, 3), date(2026, 8, 2))


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"start_date": "2026-07-08", "end_date": "2026-07-03"}, "不能晚于"),
        ({"start_date": "2026-07-01", "end_date": "2026-08-02"}, "最多"),
        ({"start_date": "2026-07-03", "end_date": "2026-08-03"}, "未来"),
        ({"start_date": "20260703", "end_date": "2026-07-08"}, "YYYY-MM-DD"),
    ],
)
def test_parse_enriched_range_repair_rejects_invalid_ranges(body, detail):
    from fastapi import HTTPException

    with pytest.raises(HTTPException, match=detail) as exc:
        kline._parse_enriched_range_repair(body, today=date(2026, 8, 2))

    assert exc.value.status_code == 400
