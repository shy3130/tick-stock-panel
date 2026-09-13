"""板块切换(盘中轮动) service 契约测试。

不依赖网络与真实 data/: 用 tmp_path 构造
分钟分区 + 前日日K + 概念扩展表 + 资金流扩展表, 断言:
聚合口径 (等权/前收基准)、切换信号方向 (rank_change)、轮动强度时间线、
资金流读取与降级 (flow 不可用退化为纯涨幅)、成分数去重、
非法参数 fail-closed、无分钟/无成分的明确 no_data。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import rps_rotation, sector_rotation
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, write_ext_parquet

DAY = "2026-09-11"
PREV = "2026-09-10"
SYMS_A = ["000001.SZ", "000002.SZ"]
SYMS_B = ["000003.SZ", "000004.SZ"]


def _reset_caches() -> None:
    # 成分映射 (600s) 与结果缓存 (30s) 都是进程级, 必须逐用例清理防串扰
    rps_rotation._map_cache.clear()
    rps_rotation._map_ts.clear()
    sector_rotation.invalidate_cache()


def _write_minute(data_dir: Path) -> None:
    """A 组先涨后平 (09:35 起涨到 10:00 后走平), B 组 10:00 后直线拉升 → 领涨从 A 切到 B。"""
    rows = []
    stamps = [datetime.fromisoformat(f"{DAY}T{h:02d}:{m:02d}:00") for h, m in [
        (9, 35), (9, 40), (9, 45), (9, 50), (9, 55),
        (10, 0), (10, 5), (10, 10), (10, 20), (10, 30), (10, 35),
    ]]
    for ts in stamps:
        late = ts.hour > 10 or (ts.hour == 10 and ts.minute >= 5)
        for sym in SYMS_A:
            price = 102.0 if not late else 101.5
            rows.append({"symbol": sym, "datetime": ts, "close": price})
        for sym in SYMS_B:
            price = 100.2 if not late else 106.0
            rows.append({"symbol": sym, "datetime": ts, "close": price})
    out = data_dir / "kline_minute" / f"date={DAY}"
    out.mkdir(parents=True)
    pl.DataFrame(rows).write_parquet(out / "part.parquet")


def _write_prev_daily(data_dir: Path) -> None:
    out = data_dir / "kline_daily" / f"date={PREV}"
    out.mkdir(parents=True)
    pl.DataFrame({
        "symbol": SYMS_A + SYMS_B,
        "close": [100.0] * 4,
    }).write_parquet(out / "part.parquet")


def _write_concept_ext(data_dir: Path) -> None:
    config = ExtConfig(
        id="ext_gn",
        label="测试概念",
        mode="snapshot",
        fields=[ExtField("symbol", "string", "代码"), ExtField("所属概念", "string", "所属概念")],
    )
    ExtConfigStore(data_dir).upsert(config)
    write_ext_parquet(
        pl.DataFrame({
            "symbol": SYMS_A + SYMS_B,
            "所属概念": ["A题材", "A题材", "B题材", "B题材"],
        }),
        config,
        data_dir,
    )


def _write_flow_ext(data_dir: Path) -> None:
    config = ExtConfig(
        id="ext_flow",
        label="测试资金流",
        mode="snapshot",
        fields=[ExtField("symbol", "string", "代码"), ExtField("净流入", "float", "净流入")],
    )
    ExtConfigStore(data_dir).upsert(config)
    write_ext_parquet(
        pl.DataFrame({
            "symbol": SYMS_A + SYMS_B,
            "净流入": [1000.0, 500.0, 100.0, 50.0],  # A 题材合计 1500, B 题材合计 150
        }),
        config,
        data_dir,
    )


@pytest.fixture()
def repo(tmp_path: Path):
    _reset_caches()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_minute(data_dir)
    _write_prev_daily(data_dir)
    _write_concept_ext(data_dir)
    _write_flow_ext(data_dir)
    yield SimpleNamespace(store=SimpleNamespace(data_dir=data_dir))
    _reset_caches()


def test_rotation_switch_direction_and_timeline(repo):
    """核心口径: 领涨从 A 切到 B → B rank_change>0 (切入), A <0 (退潮);
    末桶相对 1 小时前 top 换血 → rotation 高; 早期桶 rotation 低。"""
    result = sector_rotation.build_sector_rotation(repo, kind="concept", bucket_minutes=5)
    assert result["status"] == "ok"
    assert result["date"] == DAY
    assert result["basis"] == "prev_close"
    assert result["member_count"] == 2

    sectors = {item["name"]: item for item in result["sectors"]}
    assert set(sectors) == {"A题材", "B题材"}
    b, a = sectors["B题材"], sectors["A题材"]
    # 末桶 (10:35): B +6.0% > A +1.5%; 1 小时前 (09:35): A +2.0% > B +0.2%
    assert b["pct_now"] == pytest.approx(0.06, abs=1e-4)
    assert a["pct_now"] == pytest.approx(0.015, abs=1e-4)
    assert b["rank_now"] == 1 and b["rank_prev"] == 2
    assert a["rank_now"] == 2 and a["rank_prev"] == 1
    assert b["rank_change"] == 1  # 切入
    assert a["rank_change"] == -1  # 退潮
    assert a["n_members"] == 2 and b["n_members"] == 2
    assert a["n_members_with_bars"] == 2 and b["n_members_with_bars"] == 2

    timeline = result["timeline"]
    times = [point["time"] for point in timeline]
    assert times == sorted(times) and len(times) >= 9
    # 板块总数仅 2 个, top 集合恒为全集 → 集合口径的换血为 0;
    # 领涨易主由 leader 字段与 sectors 的 rank_change 体现
    assert timeline[-1]["rotation"] == 0.0
    assert timeline[-1]["leader"] == "B题材"
    assert timeline[-1]["leader_pct"] == pytest.approx(0.06, abs=1e-4)
    # 早期桶领涨是 A, 且未发生换血
    assert timeline[0]["rotation"] == 0.0
    assert timeline[0]["leader"] == "A题材"


def test_rotation_index_with_wide_universe(tmp_path):
    """宽板块域 (20 个) 的换血指标: 早段领涨梯队 S1-S10, 末段整体换为 S11-S20,
    两集合不相交 → 末桶 rotation=1; 首桶无对照 → 0。"""
    _reset_caches()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    rows = []
    stamps = [datetime.fromisoformat(f"{DAY}T{h:02d}:{m:02d}:00") for h, m in [
        (9, 35), (9, 40), (9, 45), (9, 50), (9, 55),
        (10, 0), (10, 5), (10, 10), (10, 20), (10, 30), (10, 35),
    ]]
    for index in range(20):
        sym = f"{index + 1:06d}.SZ"
        for ts in stamps:
            late = ts.hour > 10 or (ts.hour == 10 and ts.minute >= 5)
            # 早段 S01-S10 领涨 +2% / S11-S20 平淡 +0.2%; 末段整体对调 (回落 -1% / 拉升 +6%)
            price = (102.0 if not late else 99.0) if index < 10 else (100.2 if not late else 106.0)
            rows.append({"symbol": sym, "datetime": ts, "close": price})
    (data_dir / "kline_minute" / f"date={DAY}").mkdir(parents=True)
    pl.DataFrame(rows).write_parquet(data_dir / "kline_minute" / f"date={DAY}" / "part.parquet")
    (data_dir / "kline_daily" / f"date={PREV}").mkdir(parents=True)
    pl.DataFrame({"symbol": [f"{i + 1:06d}.SZ" for i in range(20)], "close": [100.0] * 20}).write_parquet(
        data_dir / "kline_daily" / f"date={PREV}" / "part.parquet"
    )
    config = ExtConfig(
        id="ext_gn",
        label="宽域概念",
        mode="snapshot",
        fields=[ExtField("symbol", "string", "代码"), ExtField("所属概念", "string", "所属概念")],
    )
    ExtConfigStore(data_dir).upsert(config)
    write_ext_parquet(
        pl.DataFrame({
            "symbol": [f"{i + 1:06d}.SZ" for i in range(20)],
            "所属概念": [f"S{i + 1:02d}" for i in range(20)],
        }),
        config,
        data_dir,
    )
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=data_dir))
    result = sector_rotation.build_sector_rotation(repo, kind="concept", bucket_minutes=5)
    assert result["status"] == "ok"
    assert result["member_count"] == 20
    timeline = result["timeline"]
    assert timeline[0]["rotation"] == 0.0
    # 早段 S01-S10 涨幅并列, 领涨归属由并列内排序决定 → 只断言梯队归属
    assert timeline[0]["leader"] in {f"S{i:02d}" for i in range(1, 11)}
    assert timeline[-1]["rotation"] == 1.0
    assert timeline[-1]["leader"] in {f"S{i:02d}" for i in range(11, 21)}
    # 末段切入者 rank_now=1, 1 小时前在 11 名开外 → rank_change > 0
    top = next(item for item in result["sectors"] if item["rank_now"] == 1)
    assert top["rank_change"] > 0
    fading = next(item for item in result["sectors"] if item["name"] == "S01")
    assert fading["rank_change"] < 0


def test_flow_from_selected_ext_and_score(repo):
    """资金流按用户选择的扩展列聚合到板块, 与涨幅各占 50%。"""
    result = sector_rotation.build_sector_rotation(
        repo, kind="concept", flow_field="ext_flow.净流入",
    )
    assert result["status"] == "ok"
    assert result["flow_available"] is True
    assert result["flow_field"] == "ext_flow.净流入"
    sectors = {item["name"]: item for item in result["sectors"]}
    assert sectors["A题材"]["flow"] == 1500.0
    assert sectors["B题材"]["flow"] == 150.0
    # 涨幅归一: B=100, A=0; 资金流归一: A=100, B=0 → 两者综合分均为 50
    assert sectors["A题材"]["score"] == pytest.approx(50.0)
    assert sectors["B题材"]["score"] == pytest.approx(50.0)


def test_flow_missing_degrades_to_pure_pct(repo):
    """资金流指向不存在的表/列 → 不阻断, flow_available=False, score 退化为纯涨幅归一。"""
    result = sector_rotation.build_sector_rotation(
        repo, kind="concept", flow_field="ext_nope.净流入",
    )
    assert result["status"] == "ok"
    assert result["flow_available"] is False
    sectors = {item["name"]: item for item in result["sectors"]}
    assert sectors["B题材"]["flow"] is None
    assert sectors["B题材"]["score"] == pytest.approx(100.0)
    assert sectors["A题材"]["score"] == pytest.approx(0.0)


def test_no_minute_partition_gives_no_data(repo, tmp_path):
    (repo.store.data_dir / "kline_minute" / f"date={DAY}" / "part.parquet").unlink()
    result = sector_rotation.build_sector_rotation(repo, kind="concept")
    assert result["status"] == "no_data"
    assert result["reason"] == "minute_missing"


def test_no_member_map_gives_no_data(repo):
    import shutil
    shutil.rmtree(repo.store.data_dir / "ext_data" / "ext_gn")
    result = sector_rotation.build_sector_rotation(repo, kind="concept")
    assert result["status"] == "no_data"
    assert result["reason"] == "members_missing"


def test_invalid_params_fail_closed(repo):
    with pytest.raises(ValueError):
        sector_rotation.build_sector_rotation(repo, kind="foo")
    with pytest.raises(ValueError):
        sector_rotation.build_sector_rotation(repo, kind="concept", bucket_minutes=3)


def test_result_cache_hit(repo):
    sector_rotation.build_sector_rotation(repo, kind="concept")
    # 第二次直接命中 30s 缓存 (无分钟数据也应返回缓存结果而非重算)
    (repo.store.data_dir / "kline_minute" / f"date={DAY}" / "part.parquet").unlink()
    again = sector_rotation.build_sector_rotation(repo, kind="concept")
    assert again["status"] == "ok"


def test_activity_ranking_and_custom_series(tmp_path):
    """活跃度 = 近 30 分钟成分股成交额合计: A 题材 6 桶x2 股x100 万 = 1200 万,
    B 题材 120 万 → universe 活跃降序 A 在前; 缺省展示行 = 活跃 Top10 (与
    score 排序的 sectors 区分); series_names 自定义行去重并剔除当日无行情板块。"""
    _reset_caches()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    rows = []
    stamps = [datetime.fromisoformat(f"{DAY}T{h:02d}:{m:02d}:00") for h, m in [
        (9, 35), (9, 40), (9, 45), (9, 50), (9, 55),
        (10, 0), (10, 5), (10, 10), (10, 20), (10, 30), (10, 35),
    ]]
    for ts in stamps:
        late = ts.hour > 10 or (ts.hour == 10 and ts.minute >= 5)
        for sym in SYMS_A:
            rows.append({"symbol": sym, "datetime": ts, "close": 102.0 if not late else 101.5, "amount": 1_000_000.0})
        for sym in SYMS_B:
            rows.append({"symbol": sym, "datetime": ts, "close": 100.2 if not late else 106.0, "amount": 100_000.0})
    (data_dir / "kline_minute" / f"date={DAY}").mkdir(parents=True)
    pl.DataFrame(rows).write_parquet(data_dir / "kline_minute" / f"date={DAY}" / "part.parquet")
    _write_prev_daily(data_dir)
    _write_concept_ext(data_dir)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=data_dir))

    result = sector_rotation.build_sector_rotation(repo, kind="concept", bucket_minutes=5)
    assert result["status"] == "ok"
    universe = result["universe"]
    assert universe[0]["name"] == "A题材"
    assert universe[0]["activity"] == pytest.approx(12_000_000.0)
    assert universe[1]["name"] == "B题材"
    assert universe[1]["activity"] == pytest.approx(1_200_000.0)
    # 缺省展示行 = 活跃 Top10 (A 在前), 与 score 排序的 sectors (B 在前) 区分
    assert result["series"]["sectors"] == ["A题材", "B题材"]
    assert next(s["name"] for s in result["sectors"]) == "B题材"

    # 自定义展示行: 去重 + 当日无行情板块剔除, matrix 与该板块桶涨幅一致
    custom = sector_rotation.build_sector_rotation(
        repo, kind="concept", bucket_minutes=5,
        series_names=["B题材", "不存在的板块", "B题材"],
    )
    assert custom["series"]["sectors"] == ["B题材"]
    assert custom["series"]["matrix"][0][-1] == pytest.approx(0.06, abs=1e-4)
    # 缓存键含 series_names: 自定义与缺省互不串数据
    assert result["series"]["sectors"] == ["A题材", "B题材"]


def test_industry_kind_uses_industry_map(repo):
    """kind=industry 无行业映射 → 明确 no_data (二选一互不串数据)。"""
    result = sector_rotation.build_sector_rotation(repo, kind="industry")
    assert result["status"] == "no_data"
    assert result["reason"] == "members_missing"


GYMS = ["600901.SH", "600902.SH"]  # 名称命中默认黑名单 (融资融券)
GIANT_ALL = [f"98{i:04d}.SZ" for i in range(400)]  # 巨盘概念: 400 成员, 超成员数上限
GIANT_BARS = GIANT_ALL[:2]


def _write_activity_data(tmp_path: Path):
    """带量额 + 属性类大桶的活跃度 fixture: 近 30 分钟窗口合计
    融资融券 60M > 巨盘概念 24M > A题材 12M > B题材 1.2M。"""
    _reset_caches()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    stamps = [datetime.fromisoformat(f"{DAY}T{h:02d}:{m:02d}:00") for h, m in [
        (9, 35), (9, 40), (9, 45), (9, 50), (9, 55),
        (10, 0), (10, 5), (10, 10), (10, 20), (10, 30), (10, 35),
    ]]
    rows = []
    for ts in stamps:
        late = ts.hour > 10 or (ts.hour == 10 and ts.minute >= 5)
        for syms, amount, early_close, late_close in (
            (GYMS, 5_000_000.0, 99.0, 101.0),        # 融资融券: -1% → +1% (动量 +2.0)
            (GIANT_BARS, 2_000_000.0, 101.0, 99.0),  # 巨盘概念: +1% → -1% (动量 -2.0)
            (SYMS_A, 1_000_000.0, 102.0, 101.5),     # A题材: +2% → +1.5% (动量 -0.5)
            (SYMS_B, 100_000.0, 100.2, 106.0),       # B题材: +0.2% → +6% (动量 +5.8)
        ):
            for sym in syms:
                rows.append({
                    "symbol": sym, "datetime": ts,
                    "close": late_close if late else early_close, "amount": amount,
                })
    (data_dir / "kline_minute" / f"date={DAY}").mkdir(parents=True)
    pl.DataFrame(rows).write_parquet(data_dir / "kline_minute" / f"date={DAY}" / "part.parquet")
    bar_syms = SYMS_A + SYMS_B + GYMS + GIANT_BARS
    (data_dir / "kline_daily" / f"date={PREV}").mkdir(parents=True)
    pl.DataFrame({"symbol": bar_syms, "close": [100.0] * len(bar_syms)}).write_parquet(
        data_dir / "kline_daily" / f"date={PREV}" / "part.parquet"
    )
    config = ExtConfig(
        id="ext_gn",
        label="活跃度测试概念",
        mode="snapshot",
        fields=[ExtField("symbol", "string", "代码"), ExtField("所属概念", "string", "所属概念")],
    )
    ExtConfigStore(data_dir).upsert(config)
    write_ext_parquet(
        pl.DataFrame({
            "symbol": SYMS_A + SYMS_B + GYMS + GIANT_ALL,
            "所属概念": (["A题材"] * len(SYMS_A) + ["B题材"] * len(SYMS_B)
                        + ["融资融券"] * len(GYMS) + ["巨盘概念"] * len(GIANT_ALL)),
        }),
        config,
        data_dir,
    )
    flow_config = ExtConfig(
        id="ext_flow",
        label="活跃度测试资金流",
        mode="snapshot",
        fields=[ExtField("symbol", "string", "代码"), ExtField("净流入", "float", "净流入")],
    )
    ExtConfigStore(data_dir).upsert(flow_config)
    write_ext_parquet(
        pl.DataFrame({
            "symbol": SYMS_A + SYMS_B + GYMS + GIANT_BARS,
            "净流入": [1000.0, 500.0, 100.0, 50.0, 10000.0, 10000.0, 10.0, 10.0],
        }),
        flow_config,
        data_dir,
    )
    return SimpleNamespace(store=SimpleNamespace(data_dir=data_dir))


def test_auto_exclude_default_and_universe_flag(tmp_path):
    """默认排除名单生效: 融资融券 (名称命中) 与巨盘概念 (成员数>上限) 不进自动活跃榜;
    universe 永不剔除但带 excluded 标记; 响应下发默认名单与上限。"""
    repo = _write_activity_data(tmp_path)
    result = sector_rotation.build_sector_rotation(repo, kind="concept", bucket_minutes=5, auto_rows=2)
    assert result["status"] == "ok"
    # 活跃榜 (60M/24M/12M/1.2M) 未过滤时应为 融资融券/巨盘概念 在前, 过滤后取 A/B
    assert result["series"]["sectors"] == ["A题材", "B题材"]
    assert [item["name"] for item in result["universe"]] == ["融资融券", "巨盘概念", "A题材", "B题材"]
    uni = {item["name"]: item for item in result["universe"]}
    assert uni["融资融券"]["excluded"] is True
    assert uni["巨盘概念"]["excluded"] is True
    assert uni["A题材"]["excluded"] is False
    assert "融资融券" in result["default_exclude_sectors"]
    assert result["max_auto_members"] == 300


def test_exclude_param_replaces_default_and_empty_clears(tmp_path):
    """exclude_sectors 整体替换默认名单 (替换后融资融券可入选);
    [] = 关闭名称过滤但成员数上限仍生效; 全排除时回退不过滤。"""
    repo = _write_activity_data(tmp_path)

    # 替换默认名单: 仅排除 A题材 → 活跃第一的融资融券得以入选
    replaced = sector_rotation.build_sector_rotation(
        repo, kind="concept", bucket_minutes=5, exclude_sectors=["A题材"], auto_rows=1,
    )
    assert replaced["series"]["sectors"] == ["融资融券"]

    # 空数组 = 清空名称过滤: 融资融券入选, 巨盘概念仍被成员数上限挡住
    cleared = sector_rotation.build_sector_rotation(
        repo, kind="concept", bucket_minutes=5, exclude_sectors=[], auto_rows=3,
    )
    assert cleared["series"]["sectors"] == ["融资融券", "A题材", "B题材"]

    # 全部排除 → 过滤后不足展示行数, 回退不过滤 (避免展示行开天窗)
    all_excluded = sector_rotation.build_sector_rotation(
        repo, kind="concept", bucket_minutes=5,
        exclude_sectors=["A题材", "B题材", "融资融券", "巨盘概念"], auto_rows=2,
    )
    assert all_excluded["series"]["sectors"] == ["融资融券", "巨盘概念"]


def test_custom_series_unaffected_by_exclude(tmp_path):
    """自定义监控清单不参与任何自动过滤: 黑名单命中的板块仍可手动展示。"""
    repo = _write_activity_data(tmp_path)
    result = sector_rotation.build_sector_rotation(
        repo, kind="concept", bucket_minutes=5, series_names=["融资融券", "不存在的板块", "融资融券"],
    )
    assert result["series"]["sectors"] == ["融资融券"]


def test_zero_cross_up_down_stats(tmp_path):
    """0 轴穿越统计: B 先跌后涨 → 上穿(转强) 1 次; C 先涨后跌 → 下穿(转弱) 1 次;
    A 恒在 0 轴上方不计; timeline 各桶带计数; cross_events 最新在前。"""
    _reset_caches()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    stamps = [datetime.fromisoformat(f"{DAY}T{h:02d}:{m:02d}:00") for h, m in [(9, 35), (9, 40), (9, 45)]]
    groups = {
        "A题材": (["000001.SZ", "000002.SZ"], [102.0, 102.0, 102.0]),
        "B题材": (["000003.SZ", "000004.SZ"], [99.0, 101.0, 101.0]),
        "C题材": (["000005.SZ", "000006.SZ"], [101.0, 99.0, 99.0]),
    }
    rows = []
    for index, ts in enumerate(stamps):
        for syms, closes in groups.values():
            for sym in syms:
                rows.append({"symbol": sym, "datetime": ts, "close": closes[index]})
    (data_dir / "kline_minute" / f"date={DAY}").mkdir(parents=True)
    pl.DataFrame(rows).write_parquet(data_dir / "kline_minute" / f"date={DAY}" / "part.parquet")
    (data_dir / "kline_daily" / f"date={PREV}").mkdir(parents=True)
    pl.DataFrame({
        "symbol": [sym for syms, _ in groups.values() for sym in syms],
        "close": [100.0] * 6,
    }).write_parquet(data_dir / "kline_daily" / f"date={PREV}" / "part.parquet")
    config = ExtConfig(
        id="ext_gn", label="穿越测试概念", mode="snapshot",
        fields=[ExtField("symbol", "string", "代码"), ExtField("所属概念", "string", "所属概念")],
    )
    ExtConfigStore(data_dir).upsert(config)
    write_ext_parquet(
        pl.DataFrame({
            "symbol": [sym for syms, _ in groups.values() for sym in syms],
            "所属概念": [name for name, (syms, _) in groups.items() for _ in syms],
        }),
        config,
        data_dir,
    )
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=data_dir))

    result = sector_rotation.build_sector_rotation(repo, kind="concept", bucket_minutes=5)
    assert result["status"] == "ok"
    assert sum(p.get("cross_up") or 0 for p in result["timeline"]) == 1
    assert sum(p.get("cross_down") or 0 for p in result["timeline"]) == 1
    events = result["cross_events"]
    by_name = {event["name"]: event for event in events}
    assert by_name["B题材"]["dir"] == "up" and by_name["B题材"]["time"] == "09:40"
    assert by_name["C题材"]["dir"] == "down" and by_name["C题材"]["time"] == "09:40"
    assert "A题材" not in by_name
    # 同桶内按涨幅降序先 B(转强) 后 C(转弱), 最新在前 → C 事件排最前
    assert events[0]["name"] == "C题材"


def test_sort_by_modes_select_different_dimensions(repo):
    """自动榜维度切换 (原 fixture 无量额, activity 退化为综合分):
    pct/score/rank_change/flow(不可用退化) 都是 B(+6%, 切入) 在前;
    非法维度 fail-closed。"""
    base = dict(kind="concept", bucket_minutes=5)
    for mode in ("pct", "score", "rank_change", "momentum", "flow"):
        result = sector_rotation.build_sector_rotation(repo, auto_rows=1, sort_by=mode, **base)
        assert result["series"]["sectors"] == ["B题材"], mode
    with pytest.raises(ValueError):
        sector_rotation.build_sector_rotation(repo, sort_by="nope", **base)


def test_momentum_mode_splits_strong_and_weak(tmp_path):
    """强弱切换维度对半取样: eligible 超过展示行数时, 前半取动量最高 (走强组),
    后半取动量最低 (走弱组), 中部板块跳过 — 热力图同时呈现对比两端。
    动量: B题材 +5.8 > 融资融券 +2.0 > A题材 -0.5 (巨盘概念 -2.0 被成员数上限剔除)。"""
    repo = _write_activity_data(tmp_path)
    # 清空名称过滤 → eligible = [B题材, 融资融券, A题材] 3 > 2 → 对半:
    # 走强第 1 (B题材) + 走弱第 1 (A题材), 动量第 2 的融资融券 (中部) 被跳过
    cleared = sector_rotation.build_sector_rotation(
        repo, kind="concept", bucket_minutes=5,
        exclude_sectors=[], sort_by="momentum", auto_rows=2,
    )
    assert cleared["series"]["sectors"] == ["B题材", "A题材"]
    assert "融资融券" not in cleared["series"]["sectors"]
    # 默认黑名单: eligible = [B题材, A题材] 2 ≤ 2 → 不触发对半, 普通降序
    default = sector_rotation.build_sector_rotation(
        repo, kind="concept", bucket_minutes=5, sort_by="momentum", auto_rows=2,
    )
    assert default["series"]["sectors"] == ["B题材", "A题材"]


def test_sort_by_flow_respects_exclude_and_member_cap(tmp_path):
    """flow 维度: 融资融券资金流 20000 最大但被默认黑名单拦截;
    清空名称过滤后入榜第一; 巨盘概念仍被成员数上限挡住; activity 维度不受影响。"""
    repo = _write_activity_data(tmp_path)
    flow = sector_rotation.build_sector_rotation(
        repo, kind="concept", bucket_minutes=5,
        flow_field="ext_flow.净流入", sort_by="flow", auto_rows=2,
    )
    assert flow["flow_available"] is True
    assert flow["series"]["sectors"] == ["A题材", "B题材"]

    cleared = sector_rotation.build_sector_rotation(
        repo, kind="concept", bucket_minutes=5,
        flow_field="ext_flow.净流入", sort_by="flow", exclude_sectors=[], auto_rows=2,
    )
    assert cleared["series"]["sectors"] == ["融资融券", "A题材"]

    activity = sector_rotation.build_sector_rotation(
        repo, kind="concept", bucket_minutes=5, auto_rows=2,
    )
    assert activity["series"]["sectors"] == ["A题材", "B题材"]


def test_exclude_cache_isolation_and_auto_rows(tmp_path):
    """缓存键包含排除名单与展示行数: 不同参数互不串数据, 同参数命中缓存。"""
    repo = _write_activity_data(tmp_path)
    default_first = sector_rotation.build_sector_rotation(repo, kind="concept", bucket_minutes=5, auto_rows=2)
    assert default_first["series"]["sectors"] == ["A题材", "B题材"]

    replaced = sector_rotation.build_sector_rotation(
        repo, kind="concept", bucket_minutes=5, exclude_sectors=["A题材"], auto_rows=1,
    )
    assert replaced["series"]["sectors"] == ["融资融券"]

    one_row = sector_rotation.build_sector_rotation(repo, kind="concept", bucket_minutes=5, auto_rows=1)
    assert one_row["series"]["sectors"] == ["A题材"]

    # 回到默认参数: 命中第一次的缓存, 未被后续调用串改
    (repo.store.data_dir / "kline_minute" / f"date={DAY}" / "part.parquet").unlink()
    default_again = sector_rotation.build_sector_rotation(repo, kind="concept", bucket_minutes=5, auto_rows=2)
    assert default_again["series"]["sectors"] == ["A题材", "B题材"]


def test_series_matrix_aligns_with_timeline_and_sectors(repo):
    """热力图矩阵: 行=热度降序板块 (与 sectors 同序同截断), 列=时间桶 (与 timeline
    同轴), 值=该桶板块涨幅 (列最大值=该桶领涨涨幅), 供前端按分钟轮动展示。"""
    result = sector_rotation.build_sector_rotation(repo, kind="concept", bucket_minutes=5)
    assert result["status"] == "ok"
    series = result["series"]
    timeline = result["timeline"]
    assert series["buckets"] == [point["time"] for point in timeline]
    assert series["sectors"] == [item["name"] for item in result["sectors"]]

    matrix = dict(zip(series["sectors"], series["matrix"], strict=True))
    # 末桶 (10:35): B +6.0% / A +1.5%, 与 sectors.pct_now 同口径
    last = len(series["buckets"]) - 1
    assert matrix["B题材"][last] == pytest.approx(0.06, abs=1e-4)
    assert matrix["A题材"][last] == pytest.approx(0.015, abs=1e-4)
    # 每列 (跳过无行情 None) 最大值 = 该桶领涨板块涨幅
    for col, point in enumerate(timeline):
        values = [matrix[name][col] for name in series["sectors"] if matrix[name][col] is not None]
        assert values
        assert max(values) == pytest.approx(point["leader_pct"], abs=1e-4)
