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


def test_industry_kind_uses_industry_map(repo):
    """kind=industry 无行业映射 → 明确 no_data (二选一互不串数据)。"""
    result = sector_rotation.build_sector_rotation(repo, kind="industry")
    assert result["status"] == "no_data"
    assert result["reason"] == "members_missing"
