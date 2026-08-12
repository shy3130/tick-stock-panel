from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import market_overview_builder as builder


class FakeProvider:
    def __init__(self):
        self.calls = []

    def get_latest_market_supplements(self, symbols):
        self.calls.append(list(symbols))
        return pl.DataFrame([
            {"symbol": "000001.SZ", "date": "2026-07-03", "turnover_rate": 0.44, "change_pct": 0.0268},
            {"symbol": "603986.SH", "date": "2026-07-03", "turnover_rate": 9.33, "change_pct": -0.0298},
        ])


def test_fill_turnover_from_provider_when_enriched_missing(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    rows = [
        {"symbol": "000001.SZ", "turnover_rate": None},
        {"symbol": "603986.SH", "turnover_rate": None},
    ]

    out = builder._fill_market_supplements_from_provider(rows, date(2026, 7, 3))

    assert provider.calls == [["000001.SZ", "603986.SH"]]
    assert [r["turnover_rate"] for r in out] == [0.44, 9.33]


def test_fill_market_supplements_replaces_existing_change_pct(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    rows = [{"symbol": "000001.SZ", "turnover_rate": 1.23, "change_pct": -0.09}]

    out = builder._fill_market_supplements_from_provider(rows, date(2026, 7, 3))

    assert provider.calls == [["000001.SZ"]]
    assert out[0]["turnover_rate"] == 0.44
    assert out[0]["change_pct"] == 0.0268


def test_fill_market_supplements_skips_stale_snapshot(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    rows = [{"symbol": "000001.SZ", "turnover_rate": 1.23, "change_pct": -0.09}]

    out = builder._fill_market_supplements_from_provider(rows, date(2026, 7, 4))

    assert out == rows
    assert provider.calls == [["000001.SZ"]]


def test_fill_market_supplements_replaces_implausible_change_pct(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    rows = [{"symbol": "000001.SZ", "turnover_rate": 1.23, "change_pct": -0.96}]

    out = builder._fill_market_supplements_from_provider(rows, date(2026, 7, 3))

    assert out[0]["turnover_rate"] == 0.44
    assert out[0]["change_pct"] == 0.0268


def test_fill_market_supplements_drops_implausible_supplement_change_pct(monkeypatch):
    class BadProvider:
        def get_latest_market_supplements(self, _symbols):
            return pl.DataFrame([
                {"symbol": "920189.BJ", "date": "2026-07-03", "turnover_rate": 39.94, "change_pct": 4.0061},
            ])

    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: BadProvider())

    rows = [{"symbol": "920189.BJ", "turnover_rate": None, "change_pct": 0.6051}]

    out = builder._fill_market_supplements_from_provider(rows, date(2026, 7, 3))

    assert out[0]["turnover_rate"] == 39.94
    assert out[0]["change_pct"] is None



# ================================================================
# 行为等价: _market_aggregates (null/NaN/0/涨跌停/多板块/多 tier)
# ================================================================



def _agg_rows():
    """构造覆盖 null/NaN/0/inf/涨跌停/多板块/多 tier 的行集。"""
    return [
        # 沪主板,上涨,放量,ma5 上
        {"symbol": "600001.SH", "name": "甲", "change_pct": 0.05, "amount": 1.0e9,
         "turnover_rate": 6.0, "vol_ratio_5d": 2.0, "close": 10.0, "ma5": 9.0,
         "ma20": 9.5, "ma60": 8.0, "high_60d": 9.8, "low_60d": 5.0,
         "consecutive_limit_ups": 2, "signal_limit_up": False, "signal_limit_down": False,
         "signal_broken_limit_up": False, "signal_n_day_high": False, "signal_n_day_low": False},
        # 深主板,下跌,turnover None,vol_ratio None
        {"symbol": "000001.SZ", "name": "乙", "change_pct": -0.04, "amount": None,
         "turnover_rate": None, "vol_ratio_5d": None, "close": None, "ma5": 10.0,
         "ma20": None, "ma60": float("nan"), "high_60d": 11.0, "low_60d": 9.0,
         "consecutive_limit_ups": None, "signal_limit_up": None, "signal_limit_down": None,
         "signal_broken_limit_up": None, "signal_n_day_high": None, "signal_n_day_low": None},
        # 北交所,涨停(信号),amount inf(应按 0 计入),turnover NaN
        {"symbol": "920019.BJ", "name": "丙", "change_pct": 0.30, "amount": float("inf"),
         "turnover_rate": float("nan"), "vol_ratio_5d": 0.5, "close": 5.0, "ma5": 4.0,
         "ma20": 6.0, "ma60": 5.0, "high_60d": None, "low_60d": None,
         "consecutive_limit_ups": 0, "signal_limit_up": True, "signal_limit_down": False,
         "signal_broken_limit_up": False, "signal_n_day_high": False, "signal_n_day_low": False},
        # 创业板,change_pct 0(flat),封板被破
        {"symbol": "300001.SZ", "name": "丁", "change_pct": 0.0, "amount": 5.0e7,
         "turnover_rate": 1.0, "vol_ratio_5d": 1.6, "close": 20.0, "ma5": 21.0,
         "ma20": 20.0, "ma60": 19.0, "high_60d": 22.0, "low_60d": 18.0,
         "consecutive_limit_ups": 0, "signal_limit_up": False, "signal_limit_down": False,
         "signal_broken_limit_up": True, "signal_n_day_high": True, "signal_n_day_low": False},
        # 科创板,change_pct None,null signals
        {"symbol": "688001.SH", "name": "戊", "change_pct": None, "amount": 3.0e8,
         "turnover_rate": 0.5, "vol_ratio_5d": 0.9, "close": 50.0, "ma5": 49.0,
         "ma20": 50.0, "ma60": 51.0, "high_60d": 55.0, "low_60d": 45.0,
         "consecutive_limit_ups": 1, "signal_limit_up": False, "signal_limit_down": True,
         "signal_broken_limit_up": False, "signal_n_day_high": False, "signal_n_day_low": True},
    ]


def test_market_aggregates_matches_explicit_expected():
    """用独立显式期望锁住核心字段口径(null/NaN/inf/0/涨跌停/多 board/tier)。"""
    agg = builder._market_aggregates(_agg_rows())
    # breadth: 5 行, change_pct>0: 甲丙(2), <0: 乙(1), flat: 丁(0.0)+戊(None→0)=2
    assert agg["total"] == 5
    assert agg["up"] == 2
    assert agg["down"] == 1
    assert agg["flat"] == 2
    # amount: 甲1e9 + 乙(None→0) + 丙(inf→0) + 丁5e7 + 戊3e8 = 1.35e9
    assert agg["total_amount"] == 1.35e9
    assert agg["avg_amount"] == 1.35e9 / 5
    # pct_values: 0.05,-0.04,0.30,0.0 (戊 None 被剔除) → sorted=[-0.04,0,0.05,0.30], median idx2=0.05
    assert sorted(agg["pct_values"]) == [-0.04, 0.0, 0.05, 0.30]
    assert agg["avg_pct"] == pytest.approx((0.05 - 0.04 + 0.30 + 0.0) / 4)
    assert agg["median_pct"] == 0.05
    # strong_up(>=0.03): 甲0.05,丙0.30 =2; strong_down(<=-0.03): 乙-0.04 =1
    assert agg["strong_up"] == 2
    assert agg["strong_down"] == 1
    # limit_up: 丙(signal) + 戊(consec=1) =2; broken: 丁 =1; limit_down: 戊 =1
    # limit_up: 甲(consec2>0)+丙(signal)+戊(consec1>0)=3; broken: 丁=1; limit_down: 戊=1
    assert agg["limit_up"] == 3
    assert agg["limit_down"] == 1
    # max_boards: max consec = 2(甲)
    assert agg["max_boards"] == 2
    # above_ma5: close&ma5 均有限且 close>=ma5: 甲(10>=9),丙(5>=4),戊(50>=49)=3; 乙 close None; 丁 20<21
    assert agg["above_ma5"] == 3
    # above_ma20: 甲(10>=9.5? no 10>=9.5 yes)=1; 乙 ma20 None; 丙 5<6; 丁 20>=20 yes=2; 戊 50>=50 yes=3
    assert agg["above_ma20"] == 3
    # above_ma60: 乙 ma60 nan; 甲 10>=8 yes; 丙 5>=5 yes; 丁 20>=19 yes; 戊 50<51 → 3
    assert agg["above_ma60"] == 3
    # new_high: 甲 close10>=high9.8 yes; 丁 signal_n_day_high True; 戊 signal_n_day_low True(new_low)
    assert agg["new_high"] == 2  # 甲 + 丁(signal)
    assert agg["new_low"] == 1  # 戊(signal_n_day_low)
    # turnover: 6.0,1.0,0.5 (乙None 丙NaN 剔除) → avg=(6+1+0.5)/3=2.5; high(>=5): 6.0 →1
    assert agg["avg_turnover"] == pytest.approx((6.0 + 1.0 + 0.5) / 3)
    assert agg["high_turnover"] == 1
    # vol_ratio: 2.0,0.5,1.6,0.9 (乙 None 剔除) → avg=(2+0.5+1.6+0.9)/4=1.25; high(>=1.5): 2.0,1.6 →2
    assert agg["avg_vol_ratio"] == pytest.approx((2.0 + 0.5 + 1.6 + 0.9) / 4)
    assert agg["high_vol_ratio"] == 2


def test_market_aggregates_empty_rows():
    agg = builder._market_aggregates([])
    assert agg["total"] == 0
    assert agg["up"] == agg["down"] == agg["flat"] == 0
    assert agg["avg_vol_ratio"] == 1
    assert agg["pct_values"] == []


def test_market_aggregates_missing_columns_safe():
    """cols 被裁剪时(缺列)不报错,全 null → 计 0。"""
    agg = builder._market_aggregates([{"symbol": "600001.SH"}])
    assert agg["total"] == 1
    assert agg["up"] == 0
    assert agg["limit_up"] == 0
    assert agg["avg_vol_ratio"] == 1


# ================================================================
# supplements 有界缓存: 复用 / 代际失效 / as_of 失效 / 盘中 TTL / repo 隔离
# ================================================================


def _fake_repo(data_dir, generation=0):
    return SimpleNamespace(
        store=SimpleNamespace(data_dir=data_dir),
        cache_generation=generation,
    )


def _patch_provider(monkeypatch, provider):
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)


def test_supplements_cache_reuses_same_key(monkeypatch, tmp_path):
    """同 key(data_dir/gen/as_of/symbols)重复 build,provider 只调一次。"""
    builder._clear_supplements_cache()
    provider = FakeProvider()
    _patch_provider(monkeypatch, provider)
    repo = _fake_repo(tmp_path)
    rows = [{"symbol": "000001.SZ", "turnover_rate": None}, {"symbol": "603986.SH", "turnover_rate": None}]

    builder._fill_market_supplements_from_provider([dict(r) for r in rows], date(2026, 7, 3), repo=repo)
    builder._fill_market_supplements_from_provider([dict(r) for r in rows], date(2026, 7, 3), repo=repo)

    assert len(provider.calls) == 1


def test_supplements_cache_invalidation_on_generation(monkeypatch, tmp_path):
    """cache_generation 改变 → 重查 provider。"""
    builder._clear_supplements_cache()
    provider = FakeProvider()
    _patch_provider(monkeypatch, provider)
    rows = [{"symbol": "000001.SZ", "turnover_rate": None}]

    builder._fill_market_supplements_from_provider(list(rows), date(2026, 7, 3), repo=_fake_repo(tmp_path, 0))
    builder._fill_market_supplements_from_provider(list(rows), date(2026, 7, 3), repo=_fake_repo(tmp_path, 1))

    assert len(provider.calls) == 2


def test_supplements_cache_invalidation_on_as_of(monkeypatch, tmp_path):
    """as_of 改变 → 重查 provider。"""
    builder._clear_supplements_cache()
    provider = FakeProvider()
    _patch_provider(monkeypatch, provider)
    repo = _fake_repo(tmp_path)
    rows = [{"symbol": "000001.SZ", "turnover_rate": None}]

    builder._fill_market_supplements_from_provider(list(rows), date(2026, 7, 3), repo=repo)
    builder._fill_market_supplements_from_provider(list(rows), date(2026, 7, 4), repo=repo)

    assert len(provider.calls) == 2


def test_supplements_cache_running_ttl_expiry(monkeypatch, tmp_path):
    """盘中(quote_running)短 TTL 到期 → 重查;不依赖 sleep,monkeypatch 时钟。"""
    builder._clear_supplements_cache()
    provider = FakeProvider()
    _patch_provider(monkeypatch, provider)
    repo = _fake_repo(tmp_path)
    rows = [{"symbol": "000001.SZ", "turnover_rate": None}]

    clock = [0.0]
    monkeypatch.setattr(builder, "_now", lambda: clock[0])

    builder._fill_market_supplements_from_provider(list(rows), date(2026, 7, 3), repo=repo, quote_running=True)
    assert len(provider.calls) == 1
    # 未到期(4s < 5s TTL)→ 命中缓存
    clock[0] = 4.0
    builder._fill_market_supplements_from_provider(list(rows), date(2026, 7, 3), repo=repo, quote_running=True)
    assert len(provider.calls) == 1
    # 到期(6s >= 5s TTL)→ 重查
    clock[0] = 6.0
    builder._fill_market_supplements_from_provider(list(rows), date(2026, 7, 3), repo=repo, quote_running=True)
    assert len(provider.calls) == 2


def test_supplements_cache_idle_ttl_longer_than_running(monkeypatch, tmp_path):
    """盘后(idle)用 60s TTL,盘中 5s 到期时盘后仍命中。"""
    builder._clear_supplements_cache()
    provider = FakeProvider()
    _patch_provider(monkeypatch, provider)
    repo = _fake_repo(tmp_path)
    rows = [{"symbol": "000001.SZ", "turnover_rate": None}]

    clock = [0.0]
    monkeypatch.setattr(builder, "_now", lambda: clock[0])

    builder._fill_market_supplements_from_provider(list(rows), date(2026, 7, 3), repo=repo, quote_running=False)
    assert len(provider.calls) == 1
    clock[0] = 6.0  # 超过 5s 但 < 60s → idle 仍命中
    builder._fill_market_supplements_from_provider(list(rows), date(2026, 7, 3), repo=repo, quote_running=False)
    assert len(provider.calls) == 1


def test_supplements_cache_isolated_between_repos(monkeypatch, tmp_path):
    """两个 repo(不同 data_dir)不串缓存,各自独立查 provider。"""
    builder._clear_supplements_cache()
    provider = FakeProvider()
    _patch_provider(monkeypatch, provider)
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    rows = [{"symbol": "000001.SZ", "turnover_rate": None}]

    builder._fill_market_supplements_from_provider(list(rows), date(2026, 7, 3), repo=_fake_repo(dir_a))
    builder._fill_market_supplements_from_provider(list(rows), date(2026, 7, 3), repo=_fake_repo(dir_b))
    builder._fill_market_supplements_from_provider(list(rows), date(2026, 7, 3), repo=_fake_repo(dir_a))

    assert len(provider.calls) == 2  # a, b 各一次; a 第二次命中


def test_supplements_cache_not_polluted_by_caller_mutation(monkeypatch, tmp_path):
    """调用方改 rows 后续值不应污染缓存:清空 rows 的 turnover 再查应仍得缓存原值。"""
    builder._clear_supplements_cache()
    provider = FakeProvider()
    _patch_provider(monkeypatch, provider)
    repo = _fake_repo(tmp_path)

    out1 = builder._fill_market_supplements_from_provider(
        [{"symbol": "000001.SZ", "turnover_rate": None}], date(2026, 7, 3), repo=repo)
    assert out1[0]["turnover_rate"] == 0.44
    # 调用方后续把同一对象改坏
    out1[0]["turnover_rate"] = 999.0
    # 再查:命中缓存,应得到 provider 原值 0.44,而非被污染的 999
    out2 = builder._fill_market_supplements_from_provider(
        [{"symbol": "000001.SZ", "turnover_rate": None}], date(2026, 7, 3), repo=repo)
    assert out2[0]["turnover_rate"] == 0.44
    assert len(provider.calls) == 1


def test_supplements_cache_bounded(monkeypatch, tmp_path):
    """缓存条目数受 _SUPPLEMENTS_CACHE_MAX 限制,不无界增长。"""
    builder._clear_supplements_cache()
    provider = FakeProvider()
    _patch_provider(monkeypatch, provider)
    repo = _fake_repo(tmp_path)

    # 用不同 as_of 插入超过上限的条目
    for i in range(builder._SUPPLEMENTS_CACHE_MAX + 3):
        rows = [{"symbol": f"{i:06d}.SZ", "turnover_rate": None}]
        builder._fill_market_supplements_from_provider(list(rows), date(2026, 1, 1 + i), repo=repo)

    assert len(builder._supplements_cache) <= builder._SUPPLEMENTS_CACHE_MAX


# ================================================================
# ext 维度行代际缓存
# ================================================================


def test_ext_cache_bounded_and_generational(monkeypatch, tmp_path):
    """ext cache key=(data_dir, generation);条目数有界;generation 改变不复用。"""
    builder._clear_ext_cache()
    # 用空 ext 目录:load_all 返回空,_read_ext_rows 返回空,但仍写缓存条目
    repo = _fake_repo(tmp_path, 0)
    builder._load_dimension_sources(repo, "concept")
    builder._load_dimension_sources(repo, "concept")
    assert len(builder._ext_cache) == 1
    # 不同 generation → 新 key(不命中),但旧条目保留 → 条目增长受 _EXT_CACHE_MAX 限制
    for g in range(builder._EXT_CACHE_MAX + 3):
        builder._load_dimension_sources(_fake_repo(tmp_path, g), "concept")
    assert len(builder._ext_cache) <= builder._EXT_CACHE_MAX
    # 不同 data_dir → 独立 key
    n_before = len(builder._ext_cache)
    other = tmp_path / "other"
    other.mkdir()
    builder._load_dimension_sources(_fake_repo(other, 0), "concept")
    assert len(builder._ext_cache) >= n_before  # 新 data_dir 写入新条目


def test_build_market_overview_keeps_sealed_limit_contract(monkeypatch, tmp_path):
    class FakeScreener:
        def latest_date(self):
            return date(2026, 7, 3)

        def _load_enriched_for_date(self, _as_of, columns=None):
            frame = pl.DataFrame(_agg_rows())
            return frame.select(
                [name for name in columns or [] if name in frame.columns],
            )

    class FakeDepth:
        def get_sealed_map(self, _as_of, *, is_down):
            if is_down:
                return {"688001.SH": {"sealed": False}}
            return {
                "600001.SH": {"sealed": True},
                "920019.BJ": {"sealed": False},
            }

        def is_sealed_ready(self, _as_of):
            return True

    monkeypatch.setattr(builder, "ScreenerService", lambda _repo: FakeScreener())
    monkeypatch.setattr(builder, "_index_quotes", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        builder,
        "_fill_market_supplements_from_provider",
        lambda rows, *_args, **_kwargs: rows,
    )
    monkeypatch.setattr(
        builder,
        "_dimension_rank",
        lambda *_args, **_kwargs: {"leading": [], "lagging": []},
    )

    result = builder.build_market_overview(
        _fake_repo(tmp_path),
        depth_service=FakeDepth(),
    )

    assert result["limit"]["sealed_ready"] is True
    assert result["limit"]["limit_up"] == 2
    assert result["limit"]["fake_up"] == 1
    assert result["limit"]["limit_down"] == 0
    assert result["limit"]["fake_down"] == 1
    assert result["limit"]["broken"] == 1
    assert result["limit"]["seal_rate"] == pytest.approx(200 / 3)