"""回归测试:repository.py 里手动重新拼接 compute_indicators/compute_signals
(/compute_limit_signals) 的"现算/缓存重建"路径,必须复用 pipeline.py::
compute_all() 里那段 NaN/Inf 清理逻辑 —— 之前这些路径各自手动拼接,
全部绕过了清理,极端低流动性标的会让 NaN/Inf 混进 API 响应和内存缓存。
"""
from datetime import date, timedelta

import polars as pl

from app.storage.repository import DataStore, KlineRepository


def repo(tmp_path):
    return KlineRepository(DataStore(tmp_path))


def _flat_illiquid_daily(symbol: str, n: int, start: date) -> pl.DataFrame:
    """连续 n 天完全无波动、零成交量的日K,含 append_enriched 需要的 raw_* 列。

    9 日最高价=最低价 → KDJ RSV 分母真实为 0(不是 null,fill_null 拦不住);
    5 日均量为 0 → vol_ratio_5d 真实 0/0。用来触发 compute_indicators 里
    未受保护的除零场景。enriched parquet(14 列窄表)是 get_daily/_refresh_enriched
    的读取源,所以直接构造该 schema,而不是走 kline_daily 原始表。
    """
    dates = [start + timedelta(days=i) for i in range(n)]
    return pl.DataFrame({
        "symbol": [symbol] * n,
        "date": dates,
        "open": [10.0] * n,
        "high": [10.0] * n,
        "low": [10.0] * n,
        "close": [10.0] * n,
        "raw_close": [10.0] * n,
        "raw_high": [10.0] * n,
        "raw_low": [10.0] * n,
        "volume": [0.0] * n,
        "amount": [0.0] * n,
    })


def _assert_no_nan_or_inf(df: pl.DataFrame) -> None:
    assert not df.is_empty()
    float_cols = [c for c in df.columns if df[c].dtype.is_float()]
    assert float_cols, "没有浮点列可检查,测试数据/断言需要复核"
    for c in float_cols:
        col = df[c]
        assert col.is_nan().sum() == 0, f"{c} 仍含 NaN"
        assert col.is_infinite().sum() == 0, f"{c} 仍含 Inf"


def _register_kline_enriched_view(r: KlineRepository) -> None:
    """A 股 kline_enriched 视图只在 _register_views (构造期) 建过一次,当时目录还是
    空的会被跳过注册;repo.refresh_index_views() 只覆盖 index/etf/hk,不含 A 股。
    生产环境靠 daily_pipeline.py::_refresh_single_view 兜底,测试里手动补一次。
    """
    d = r.store.data_dir.as_posix()
    r.db.execute(
        f"""CREATE OR REPLACE VIEW kline_enriched AS
            SELECT * FROM read_parquet('{d}/kline_daily_enriched/**/*.parquet', union_by_name=true)"""
    )


def test_get_daily_hot_path_has_no_nan_or_inf_for_illiquid_symbol(tmp_path, monkeypatch):
    """/api/kline/daily 的现算热路径 (get_daily -> _compute_enriched_range)。"""
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: False)
    r = repo(tmp_path)

    symbol = "000001.SZ"
    df = _flat_illiquid_daily(symbol, 12, date(2026, 5, 1))
    r.append_enriched(df)

    # _compute_enriched_range 会 join instruments 算涨跌停信号,给一份最小维表,
    # 避免 join 因为 instruments 完全空 schema 而抛异常、掩盖本测试要验证的行为。
    inst_dir = tmp_path / "instruments"
    inst_dir.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": [symbol], "name": ["测试股"]}).write_parquet(inst_dir / "instruments.parquet")
    r._refresh_instruments()

    out = r.get_daily(symbol, date(2026, 5, 1), date(2026, 5, 12))

    _assert_no_nan_or_inf(out)


def test_refresh_enriched_latest_cache_has_no_nan_or_inf(tmp_path, monkeypatch):
    """服务启动/跨日刷新时的 _refresh_enriched -> _enriched_cache + _live_agg_cache。

    完整历史不再常驻 (_enriched_history_cache 已删除); 最新日缓存与盘中聚合表是
    启动时即时计算的两个产物, 都必须复用 compute_all 的 NaN/Inf 清理。
    """
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: False)
    r = repo(tmp_path)

    symbol = "000001.SZ"
    df = _flat_illiquid_daily(symbol, 12, date(2026, 5, 1))
    r.append_enriched(df)
    _register_kline_enriched_view(r)

    r._refresh_enriched()

    assert r._enriched_cache is not None
    _assert_no_nan_or_inf(r._enriched_cache)
    # 完整历史缓存已删除, 不应再存在该属性
    assert not hasattr(r, "_enriched_history_cache")
    if r._live_agg_cache is not None and not r._live_agg_cache.is_empty():
        _assert_no_nan_or_inf(r._live_agg_cache)


def test_build_live_agg_has_no_inf_when_raw_close_is_zero(tmp_path, monkeypatch):
    """回归测试:_build_live_agg 里 `_adj_factor = close / raw_close` 没有零值
    保护(只在 join 产生 null 时兜底,真实的 0 拦不住)。数据源异常导致某天
    raw_close 恰好是 0 时,之前会让 _adj_factor 变成 Inf,污染盘中递推状态,
    进而在 compute_enriched_today 里让当天所有增量指标都被 Inf 污染。

    _build_live_agg 旧实现有一条 `hist_all["date"].min() <= latest - 300 天` 的闸门,
    历史不够会提前返回空表; 现已删除该闸门 (300 日历天很少恰好落在交易日), 这里仍
    构造 340 天数据, 确保进入真正的计算分支。
    """
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: False)
    r = repo(tmp_path)

    symbol = "000001.SZ"
    start = date(2026, 1, 1)
    df = _flat_illiquid_daily(symbol, 340, start)
    latest_idx = 339
    latest = start + timedelta(days=latest_idx)
    # 让最新一天不再是"完全无波动"(避免和 KDJ/vol_ratio 的 0/0 场景混在一起),
    # 但 raw_close 显式设为 0 —— 只触发 _adj_factor 这一处除零。
    df = df.with_columns(
        pl.when(pl.col("date") == latest).then(0.0).otherwise(pl.col("raw_close")).alias("raw_close"),
        pl.when(pl.col("date") == latest).then(1_000.0).otherwise(pl.col("volume")).alias("volume"),
    )
    r.append_enriched(df)
    _register_kline_enriched_view(r)

    r._refresh_enriched()

    assert r._live_agg_cache is not None and not r._live_agg_cache.is_empty(), (
        "live_agg 应该已经建好(历史跨度已覆盖 90 天阈值),否则本测试没有真正走到"
        "有除零风险的计算分支"
    )
    _assert_no_nan_or_inf(r._live_agg_cache)
    assert r._live_agg_cache["_adj_factor"].to_list() == [1.0]
