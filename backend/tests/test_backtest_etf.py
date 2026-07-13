import time
import types
from datetime import date

import polars as pl

from app.services.backtest import BacktestConfig
from app.backtest.engine import BacktestEngine, PanelCache
from app.backtest.factor import FactorConfig
from app.backtest.strategy import StrategyBacktestConfig


def test_configs_default_to_stock():
    assert BacktestConfig(symbols=[], start=date(2026, 1, 1), end=date(2026, 1, 2)).asset_type == "stock"
    assert FactorConfig(factor_name="x", symbols=None, start=date(2026, 1, 1), end=date(2026, 1, 2)).asset_type == "stock"
    assert StrategyBacktestConfig(strategy_id="x", symbols=None, start=date(2026, 1, 1), end=date(2026, 1, 2)).asset_type == "stock"


def test_panel_cache_key_isolates_asset_type():
    args = (["510300"], date(2026, 1, 1), date(2026, 1, 2), None)
    k_stock = PanelCache._make_key(*args, "stock")
    k_etf = PanelCache._make_key(*args, "etf")
    assert k_stock != k_etf
    assert k_etf.startswith("etf:")
    assert k_stock.startswith("stock:")


def test_engine_loads_from_etf_dir(monkeypatch, tmp_path):
    """asset_type='etf' 时, load_panel 应扫 ETF enriched 目录, 不走 stock 缓存。"""
    captured = {}

    def fake_scan(path, *a, **k):
        captured["path"] = str(path)
        return pl.LazyFrame({
            "symbol": pl.Series("symbol", [], dtype=pl.Utf8),
            "date": pl.Series("date", [], dtype=pl.Date),
            "open": pl.Series("open", [], dtype=pl.Float64),
            "high": pl.Series("high", [], dtype=pl.Float64),
            "low": pl.Series("low", [], dtype=pl.Float64),
            "close": pl.Series("close", [], dtype=pl.Float64),
            "volume": pl.Series("volume", [], dtype=pl.Float64),
        })

    monkeypatch.setattr("app.backtest.engine.pl.scan_parquet", fake_scan)

    # get_enriched_range 返回 None: 即便被调也不命中缓存; etf 分支本就不该调它
    repo = types.SimpleNamespace(
        store=types.SimpleNamespace(data_dir=tmp_path),
        get_enriched_range=lambda *a, **k: None,
    )
    eng = BacktestEngine(repo)
    eng._load_panel_inner(["510300"], date(2026, 1, 1), date(2026, 1, 2), None, "etf")
    assert "kline_etf_enriched" in captured["path"]


def test_engine_stock_uses_daily_enriched_dir(monkeypatch, tmp_path):
    captured = {}

    def fake_scan(path, *a, **k):
        captured["path"] = str(path)
        return pl.LazyFrame({
            "symbol": pl.Series("symbol", [], dtype=pl.Utf8),
            "date": pl.Series("date", [], dtype=pl.Date),
            "open": pl.Series("open", [], dtype=pl.Float64),
            "high": pl.Series("high", [], dtype=pl.Float64),
            "low": pl.Series("low", [], dtype=pl.Float64),
            "close": pl.Series("close", [], dtype=pl.Float64),
            "volume": pl.Series("volume", [], dtype=pl.Float64),
        })

    monkeypatch.setattr("app.backtest.engine.pl.scan_parquet", fake_scan)
    repo = types.SimpleNamespace(
        store=types.SimpleNamespace(data_dir=tmp_path),
        get_enriched_range=lambda *a, **k: None,
    )
    eng = BacktestEngine(repo)
    eng._load_panel_inner(["600519"], date(2026, 1, 1), date(2026, 1, 2), None, "stock")
    assert "kline_daily_enriched" in captured["path"]


def test_panel_cache_single_flight_computes_once():
    """N 个线程并发同 key 冷启动: compute_fn 只应被调用一次, 其余复用结果 (无缓存踩踏)。"""
    import threading

    cache = PanelCache()
    calls = []
    barrier = threading.Barrier(8)
    df = pl.DataFrame({"symbol": ["510300"]})

    def slow_compute(symbols, start, end, columns, asset_type):
        calls.append(1)
        time.sleep(0.05)  # 拉长窗口, 逼出并发 miss
        return df

    args = (["510300"], date(2026, 1, 1), date(2026, 1, 2), None)
    results = []
    rlock = threading.Lock()

    def worker():
        barrier.wait()  # 所有线程同时起跑, 制造冷启动踩踏
        r = cache.get_or_compute(*args, slow_compute, "stock")
        with rlock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(calls) == 1, f"面板被重复加载 {sum(calls)} 次, single-flight 失效"
    assert len(results) == 8 and all(r is df for r in results)


def test_panel_cache_single_flight_error_propagates_and_retries():
    """leader compute 抛错: 不缓存失败, 异常透传给所有等待者, 后续调用可重试成功。"""
    import threading

    cache = PanelCache()
    barrier = threading.Barrier(4)
    boom = RuntimeError("scan failed")

    def failing_compute(symbols, start, end, columns, asset_type):
        time.sleep(0.03)
        raise boom

    args = (["510300"], date(2026, 1, 1), date(2026, 1, 2), None)
    errors = []
    elock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            cache.get_or_compute(*args, failing_compute, "stock")
        except RuntimeError as e:
            with elock:
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 4 and all(e is boom for e in errors), "失败未透传给全部跟随者"

    # 失败未被缓存 —— 重试应重新 compute 并成功
    df = pl.DataFrame({"symbol": ["510300"]})
    got = cache.get_or_compute(*args, lambda *a: df, "stock")
    assert got is df


def test_panel_cache_stats_counts_scans_hits_reuses():
    """遥测计数: 首次 miss 计扫盘, 二次同 key 计命中, 并发同 key 跟随者计复用。"""
    import threading

    cache = PanelCache()
    df = pl.DataFrame({"symbol": ["510300"]})
    args = (["510300"], date(2026, 1, 1), date(2026, 1, 2), None)

    # 1) 首次: 冷 miss -> 扫盘 1 次
    cache.get_or_compute(*args, lambda *a: df, "stock")
    s = cache.stats()
    assert s["compute_count"] == 1 and s["hit_count"] == 0

    # 2) 二次同 key: 命中缓存, 不扫盘
    cache.get_or_compute(*args, lambda *a: df, "stock")
    s = cache.stats()
    assert s["compute_count"] == 1 and s["hit_count"] == 1

    # 3) 新 key 并发踩踏: 1 次扫盘 + N-1 次 single-flight 复用
    barrier = threading.Barrier(5)
    args2 = (["600000"], date(2026, 2, 1), date(2026, 2, 2), None)

    def slow(*a):
        time.sleep(0.04)
        return df

    def worker():
        barrier.wait()
        cache.get_or_compute(*args2, slow, "stock")

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    s = cache.stats()
    # 核心不变量: 5 线程并发同 key 只扫盘 1 次 (args1 首次 + args2 一次 = 2)。
    assert s["compute_count"] == 2, "并发同 key 应只扫盘 1 次"
    # 其余 4 线程要么 single-flight 复用, 要么(慢调度下 leader 已写缓存)命中 ——
    # 二者之和恒为 4。不锁定 reuse/hit 具体分配, 避免时序 flaky。
    assert s["reuse_count"] + (s["hit_count"] - 1) == 4, "4 个非 leader 线程应复用或命中"


def test_job_key_includes_asset_type_and_is_consistent():
    """stream 与 cancel 必须用同一 job_key: asset_type 进 key 且相同入参产出相同 key。"""
    from app.api.backtest import _make_job_key

    args = ("s1", None, None, None, "open_t+1", None, None,
            0.0002, 5.0, 10, 1.0, 1_000_000.0, "equal", None, None,
            "position", 5, None, None)
    k_stock = _make_job_key(*args, asset_type="stock")
    k_etf = _make_job_key(*args, asset_type="etf")
    assert k_stock != k_etf
    # 相同参数(含 asset_type)必须产出相同 key —— stream 端与 cancel 端对齐的前提
    assert _make_job_key(*args, asset_type="etf") == k_etf
