"""扩展列 join 进过滤帧的回归用例（alpha-factory 信号桥的依赖）。

背景: 面板 screener 原先只把扩展列当"结果投影", WHERE/ORDER BY 引用扩展列会
Binder Error。alpha-factory 的信号表 af_stock_signal 要能被面板选股直接排序/过滤,
必须在 SQL 之前把这列 join 进帧 —— 这里锁定该行为。
"""
from __future__ import annotations

import polars as pl

from app.services.screener import join_ext_maps


def _frame() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["000001.SZ", "000002.SZ", "000003.SZ"],
        "close": [12.07, 3.06, 8.0],
    })


def test_join_adds_projection_name_and_factor_alias() -> None:
    df = join_ext_maps(_frame(), {"af_stock_signal__composite": {"000001.SZ": 0.94, "000003.SZ": 0.31}})
    assert "af_stock_signal__composite" in df.columns
    assert "ext_af_stock_signal_composite" in df.columns
    # 缺值保持 null, 不伪造 0 (0 会被排序当成最低分真实值)
    assert df.filter(pl.col("symbol") == "000002.SZ")["af_stock_signal__composite"].item() is None


def test_join_supports_sql_filter_and_order() -> None:
    import duckdb

    df = join_ext_maps(_frame(), {"af_stock_signal__composite": {"000001.SZ": 0.94, "000003.SZ": 0.31}})
    con = duckdb.connect(database=":memory:")
    con.register("enriched", df.to_arrow())
    out = con.execute(
        "SELECT symbol FROM enriched WHERE (ext_af_stock_signal_composite > 0.5) "
        "ORDER BY ext_af_stock_signal_composite DESC"
    ).pl()
    con.close()
    assert out["symbol"].to_list() == ["000001.SZ"]


def test_join_is_noop_without_payload() -> None:
    base = _frame()
    assert join_ext_maps(base, None).columns == base.columns
    assert join_ext_maps(base, {"af_stock_signal__composite": {}}).columns == base.columns


def test_empty_conditions_scans_instead_of_returning_empty(monkeypatch) -> None:
    """空 conditions 必须等价「无条件扫描」: 否则按信号 top-N 排序直接哑掉。

    回归点: run() 曾有 `if not conditions: return 空结果` 的早退, 用户只想按
    af_*_signal_composite 排序看全市场前列时, 接口静默返回 0 行且 elapsed 0ms。
    """
    from datetime import date

    from app.services.screener import ScreenerService

    svc = ScreenerService(None, "stock")  # repo=None: 跳过 integrity 断言
    frame = pl.DataFrame({
        "symbol": ["000002.SZ", "000001.SZ", "000003.SZ"],
        "close": [3.06, 12.07, 8.0],
    })
    monkeypatch.setattr(svc, "_load_enriched_for_date", lambda _d: frame)
    r = svc.run(as_of=date(2026, 11, 11), conditions=[], order_by="close DESC", limit=2)
    assert r.total == 2
    assert [x["symbol"] for x in r.rows] == ["000001.SZ", "000003.SZ"]
    assert r.elapsed_ms > 0
