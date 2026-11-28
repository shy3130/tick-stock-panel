"""Issue #589: 9:25 全市场竞价扫描扩展测试。

覆盖：
- 扩展发现注册、默认关闭配置初始化、不覆盖用户配置、不伪造历史
- 筛选精确边界 (高开 == 5% 且量比 == 10 命中；低于任一阈值不命中；含浮点精度边界)
- 停牌/无量/缺失昨收/异常快照/非正股代码/无基准量容错
- 非交易日陈旧快照 fail-closed 跳过
- 缺少本地日K基准 fail-closed 报错
- 部分批次失败保留成功结果并记录 partial 状态；整批失败记 error，不伪装空成功
- 同日重复触发幂等 + 多日历史倒序查询 (最新日 & 指定日)
- 独立于 Fuyao 数据源（使用 TdxQuant 等通用 realtime 源正常工作）
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.ext_data import router as ext_router
from app.custom import auction_scan
from app.extensions.contracts import (
    BACKEND_EXTENSION_API_VERSION,
    ExtCollectorContext,
    ExtensionContext,
)
from app.extensions.loader import configure_backend_extensions, start_backend_extensions
from app.market_time import CN_TZ
from app.services import ext_pull
from app.services.ext_data import ExtConfigStore


def _ts_ms(d: date, hh: int = 9, mm: int = 25, ss: int = 1) -> int:
    return int(datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=CN_TZ).timestamp() * 1000)


def _seed_kline_daily(data_dir: Path, rows: list[dict]) -> None:
    out_dir = data_dir / "kline_daily"
    out_dir.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).with_columns(
        pl.col("date").cast(pl.Date),
        pl.col("volume").cast(pl.Float64),
    ).write_parquet(out_dir / "part.parquet")


# ── 1. 筛选边界与容错 (纯函数) ────────────────────────────────────────────

def test_scan_boundaries_exact_5pct_and_10x_and_fault_tolerance():
    target = date(2026, 4, 15)
    collected_at = "2026-04-15T09:25:05+08:00"
    # vol_ma5 = 24000 手/日 → 1分钟均量 = 100 手 → 竞价量 1000 手 => 量比恰好 10.0
    vol_ma5_map = {
        "000001.SZ": 24_000.0,  # 恰好 5% + 恰好 10x -> 命中
        "000002.SZ": 24_000.0,  # 浮点边界: 3.15 / 3.0 - 1 = 0.04999999999999997 (数学 5%) + 10x -> 命中
        "000003.SZ": 24_000.0,  # 4.99% + 15x -> 不命中
        "000004.SZ": 24_000.0,  # 6.00% + 9.99x -> 不命中
        "600519.SH": 12_000.0,  # 8.00% + 20x -> 命中且排第一
        "300750.SZ": 24_000.0,  # 停牌 volume=0 -> 跳过
        "688001.SH": 24_000.0,  # 缺失昨收 -> 跳过
        "000005.SZ": 24_000.0,  # NaN 价格 -> 跳过
    }
    name_map = {
        "000001.SZ": "平安银行",
        "000002.SZ": "万科A",
        "600519.SH": "贵州茅台",
    }
    snapshots = [
        # 恰好 5% & 恰好 10x
        {"symbol": "000001.SZ", "open": 10.50, "last_price": 10.50, "prev_close": 10.00, "volume": 1000},
        # 浮点精度边界: 3.15 / 3.0 - 1
        {"symbol": "000002.SZ", "open": 3.15, "last_price": 3.15, "prev_close": 3.00, "volume": 1000},
        # 低于高开阈值
        {"symbol": "000003.SZ", "open": 10.499, "last_price": 10.499, "prev_close": 10.00, "volume": 1500},
        # 低于量比阈值
        {"symbol": "000004.SZ", "open": 10.60, "last_price": 10.60, "prev_close": 10.00, "volume": 999},
        # 强命中 (应排在最前)
        {"symbol": "600519.SH", "open": 108.0, "last_price": 108.0, "prev_close": 100.0, "volume": 1000},
        # 停牌/无竞价成交
        {"symbol": "300750.SZ", "open": 220.0, "last_price": 220.0, "prev_close": 200.0, "volume": 0},
        # 缺失昨收
        {"symbol": "688001.SH", "open": 25.0, "last_price": 25.0, "prev_close": None, "volume": 2000},
        # 异常 NaN
        {"symbol": "000005.SZ", "open": float("nan"), "last_price": float("nan"), "prev_close": 10.0, "volume": 2000},
        # 缺少基准量
        {"symbol": "000999.SZ", "open": 11.0, "last_price": 11.0, "prev_close": 10.0, "volume": 5000},
        # 指数与 ETF (应被过滤)
        {"symbol": "000001.SH", "open": 3500.0, "last_price": 3500.0, "prev_close": 3000.0, "volume": 999999},
        {"symbol": "510300.SH", "open": 5.0, "last_price": 5.0, "prev_close": 4.0, "volume": 999999},
        # 畸形数据
        None,  # type: ignore[list-item]
        {"symbol": ""},
    ]

    matched, stats = auction_scan.scan_auction_snapshots(
        snapshots,
        vol_ma5_map,
        name_map,
        target_date=target,
        collected_at=collected_at,
    )

    assert [r["symbol"] for r in matched] == ["600519.SH", "000001.SZ", "000002.SZ"]
    assert stats.matched == 3
    assert stats.evaluated == 5
    assert stats.skipped_missing_baseline == 1
    assert stats.skipped_invalid >= 4

    top = matched[0]
    assert top["trade_date"] == "2026-04-15"
    assert top["name"] == "贵州茅台"
    assert top["open_gap_pct"] == pytest.approx(0.08)
    assert top["auction_vol_ratio"] == pytest.approx(20.0)
    assert top["collected_at"] == collected_at

    exact = matched[1]
    assert exact["open_gap_pct"] == pytest.approx(0.05)
    assert exact["auction_vol_ratio"] == pytest.approx(10.0)


# ── 2. 扩展加载、默认关闭配置、重启不覆盖 ─────────────────────────────────

def test_extension_loads_and_seeds_disabled_config_without_history(tmp_path):
    app = FastAPI()
    registry, errors = configure_backend_extensions(app)
    assert not any("auction_scan" in e.module for e in errors)
    assert auction_scan.EXTENSION_ID in registry.extension_ids()
    assert registry.get_ext_collector(auction_scan.COLLECTOR_ID) is not None

    ctx = ExtensionContext(
        api_version=BACKEND_EXTENSION_API_VERSION,
        data_dir=tmp_path,
        repository=SimpleNamespace(get_name_map=lambda _s=None: {}),
    )
    start_backend_extensions(ctx, registry)

    store = ExtConfigStore(tmp_path)
    cfg = store.get(auction_scan.EXT_CONFIG_ID)
    assert cfg is not None
    assert cfg.mode == "timeseries"
    assert cfg.pull is not None
    assert cfg.pull.enabled is False
    assert cfg.pull.collector == auction_scan.COLLECTOR_ID
    assert cfg.pull.schedule_time == "09:25"
    assert cfg.pull.time_window_end == "09:30"
    assert cfg.pull.schedule_trading_only is True
    # 不得预写/伪造任何历史分区
    assert not (tmp_path / "ext_data" / auction_scan.EXT_CONFIG_ID / "timeseries").exists()

    # 用户开启后，再次 startup 不得覆盖用户配置
    cfg.pull.enabled = True
    store.upsert(cfg)
    start_backend_extensions(ctx, registry)
    assert store.get(auction_scan.EXT_CONFIG_ID).pull.enabled is True


# ── 3. 非交易日陈旧快照 & 缺失日K基准 fail-closed ──────────────────────────

class _StubMarket:
    def __init__(self, snapshots, vol_ma5=None, names=None, excluded=None, partial_errors=()):
        self._snapshots = snapshots
        self._vol_ma5 = vol_ma5 or {}
        self._names = names or {}
        self._excluded = excluded or set()
        self._partial_errors = tuple(partial_errors)

    def get_realtime_stocks(self):
        return self._snapshots, self._partial_errors

    def get_vol_ma5_baseline(self, _target_date, *, lookback_days=5):
        del lookback_days
        return self._vol_ma5

    def get_name_map(self, _symbols=None):
        return self._names

    def get_excluded_non_stock_symbols(self):
        return self._excluded


def test_collector_skips_stale_non_trading_day_snapshots(tmp_path):
    stale_snaps = [
        {
            "symbol": "000001.SZ",
            "open": 11.0,
            "last_price": 11.0,
            "prev_close": 10.0,
            "volume": 2000,
            "timestamp": _ts_ms(date(2026, 4, 14), 15, 0, 0),  # 昨日收盘旧快照
        }
    ]
    collector = auction_scan.AuctionScanCollector()
    res = collector.collect(ExtCollectorContext(
        api_version=BACKEND_EXTENSION_API_VERSION,
        data_dir=tmp_path,
        target_date=date(2026, 4, 15),
        config_id=auction_scan.EXT_CONFIG_ID,
        market=_StubMarket(stale_snaps, vol_ma5={"000001.SZ": 24_000.0}),
    ))
    assert res.skipped_reason is not None
    assert res.rows == ()


def test_collector_fails_closed_when_local_kline_baseline_missing(tmp_path):
    snaps = [
        {
            "symbol": "000001.SZ",
            "open": 11.0,
            "last_price": 11.0,
            "prev_close": 10.0,
            "volume": 2000,
            "timestamp": _ts_ms(date(2026, 4, 15), 9, 25, 1),
        }
    ]
    collector = auction_scan.AuctionScanCollector()
    with pytest.raises(RuntimeError, match="基准"):
        collector.collect(ExtCollectorContext(
            api_version=BACKEND_EXTENSION_API_VERSION,
            data_dir=tmp_path,
            target_date=date(2026, 4, 15),
            config_id=auction_scan.EXT_CONFIG_ID,
            market=_StubMarket(snaps, vol_ma5={}),
        ))


def test_collector_supports_custom_threshold_params(tmp_path):
    """同一采集器可通过 collector_params 复用不同高开/量比阈值方案。"""
    target = date(2026, 4, 15)
    # 4% 高开 + 6x 量比：默认阈值 (5%, 10x) 不命中，放宽参数 (3%, 5x) 命中
    snaps = [
        {
            "symbol": "000001.SZ",
            "open": 10.40,
            "last_price": 10.40,
            "prev_close": 10.00,
            "volume": 600,
            "timestamp": _ts_ms(target, 9, 25, 2),
        }
    ]
    market = _StubMarket(snaps, vol_ma5={"000001.SZ": 24_000.0}, names={"000001.SZ": "平安银行"})
    collector = auction_scan.AuctionScanCollector()

    default_res = collector.collect(ExtCollectorContext(
        api_version=BACKEND_EXTENSION_API_VERSION,
        data_dir=tmp_path,
        target_date=target,
        config_id="ext_default",
        market=market,
    ))
    assert default_res.rows == ()

    relaxed_res = collector.collect(ExtCollectorContext(
        api_version=BACKEND_EXTENSION_API_VERSION,
        data_dir=tmp_path,
        target_date=target,
        config_id="ext_relaxed",
        params={"min_open_gap_pct": 0.03, "min_auction_vol_ratio": 5.0},
        market=market,
    ))
    assert len(relaxed_res.rows) == 1
    assert relaxed_res.rows[0]["symbol"] == "000001.SZ"
    assert relaxed_res.rows[0]["open_gap_pct"] == pytest.approx(0.04)
    assert relaxed_res.rows[0]["auction_vol_ratio"] == pytest.approx(6.0)


# ── 4. 端到端: 部分批次失败保留结果 + 重复触发幂等 + 多日倒序积累 ───────────

def test_auction_scan_e2e_partial_batch_idempotency_and_history(tmp_path, monkeypatch):
    _seed_kline_daily(tmp_path, [
        {"symbol": "000001.SZ", "date": date(2026, 4, 10), "volume": 24_000.0},
        {"symbol": "000001.SZ", "date": date(2026, 4, 13), "volume": 24_000.0},
        {"symbol": "600519.SH", "date": date(2026, 4, 10), "volume": 12_000.0},
        {"symbol": "600519.SH", "date": date(2026, 4, 13), "volume": 12_000.0},
    ])

    app = FastAPI()
    app.include_router(ext_router)
    registry, _ = configure_backend_extensions(app)
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        refresh_views=lambda: None,
        clear_cache=lambda: None,
        get_name_map=lambda _s=None: {"000001.SZ": "平安银行", "600519.SH": "贵州茅台"},
        get_index_symbol_set=lambda: {"000001.SH"},
        get_etf_instruments=lambda: pl.DataFrame(schema={"symbol": pl.Utf8}),
    )
    app.state.extension_registry = registry
    app.state.repo = repo
    start_backend_extensions(
        ExtensionContext(BACKEND_EXTENSION_API_VERSION, tmp_path, repo),
        registry,
    )

    # 模拟非 Fuyao 通用实时源 (如 TdxQuant): 不依赖 Fuyao 配置
    fake_provider = SimpleNamespace(
        last_realtime_meta=None,
        get_realtime=lambda: [],
    )

    from app.data_providers import custom as custom_sources
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "tdx")
    monkeypatch.setattr(preferences, "load", lambda: {"realtime_data_provider": "tdx"})
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, ds: name == "tdx" and ds == "realtime")
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: fake_provider)

    client = TestClient(app)

    # Day 1 (2026-04-14): 1 只命中，连续触发两次必须幂等不重复
    d1 = date(2026, 4, 14)
    monkeypatch.setattr(ext_pull, "cn_today", lambda: d1)
    fake_provider.get_realtime = lambda: [
        {"symbol": "000001.SZ", "open": 10.60, "last_price": 10.60, "prev_close": 10.00, "volume": 1200, "timestamp": _ts_ms(d1)},
        {"symbol": "600519.SH", "open": 101.0, "last_price": 101.0, "prev_close": 100.0, "volume": 200, "timestamp": _ts_ms(d1)},
    ]
    fake_provider.last_realtime_meta = {"requested": 2, "received": 2, "failed_batches": 0, "total_batches": 1, "errors": []}

    r1 = client.post(f"/api/ext-data/{auction_scan.EXT_CONFIG_ID}/pull/run")
    assert r1.status_code == 200 and r1.json()["rows"] == 1
    r1_again = client.post(f"/api/ext-data/{auction_scan.EXT_CONFIG_ID}/pull/run")
    assert r1_again.status_code == 200 and r1_again.json()["rows"] == 1

    day1_rows = client.get(f"/api/ext-data/{auction_scan.EXT_CONFIG_ID}/rows?date=2026-04-14").json()
    assert day1_rows["total"] == 1
    assert day1_rows["rows"][0]["symbol"] == "000001.SZ"

    # Day 2 (2026-04-15): 部分批次失败但有一批成功 -> 保留成功命中且状态为 partial
    d2 = date(2026, 4, 15)
    monkeypatch.setattr(ext_pull, "cn_today", lambda: d2)
    fake_provider.get_realtime = lambda: [
        {"symbol": "600519.SH", "open": 107.0, "last_price": 107.0, "prev_close": 100.0, "volume": 800, "timestamp": _ts_ms(d2)},
    ]
    fake_provider.last_realtime_meta = {
        "requested": 5500,
        "received": 2500,
        "failed_batches": 2,
        "total_batches": 6,
        "errors": ["第3批(1000只): 连接超时", "第4批(1000只): 连接超时"],
    }

    r2 = client.post(f"/api/ext-data/{auction_scan.EXT_CONFIG_ID}/pull/run")
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["status"] == "partial"
    assert body2["rows"] == 1
    assert "连接超时" in body2["message"]

    # 历史接口: 倒序日期 + 默认最新日 + 可切指定日
    latest = client.get(f"/api/ext-data/{auction_scan.EXT_CONFIG_ID}/rows").json()
    assert latest["date"] == "2026-04-15"
    assert latest["dates"] == ["2026-04-15", "2026-04-14"]
    assert latest["total"] == 1
    assert latest["rows"][0]["symbol"] == "600519.SH"

    # 整批完全失败 -> 必须返回 400 且记 error，不得展示为成功空数据
    d3 = date(2026, 4, 16)
    monkeypatch.setattr(ext_pull, "cn_today", lambda: d3)
    fake_provider.get_realtime = lambda: []
    fake_provider.last_realtime_meta = {
        "requested": 5500,
        "received": 0,
        "failed_batches": 6,
        "total_batches": 6,
        "errors": ["全批次超时"],
    }
    r3 = client.post(f"/api/ext-data/{auction_scan.EXT_CONFIG_ID}/pull/run")
    assert r3.status_code == 400
    cfg_after = ExtConfigStore(tmp_path).get(auction_scan.EXT_CONFIG_ID)
    assert cfg_after.pull.last_status == "error"
    assert not (tmp_path / "ext_data" / auction_scan.EXT_CONFIG_ID / "timeseries" / "date=2026-04-16").exists()
