"""rps_rotation 公开入口回归测试。

用一个临时 enriched 数据集 + 临时概念快照, 证明:
  - _latest_enriched_date 走公开 getter repo.enriched_latest_date() (不再读私有历史缓存);
  - build_rps_rotation 经 repo.get_enriched_range 派生的 change_pct 产出非空轮动矩阵。

不依赖真实 ../data; 全部在 tmp_path 内构造。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from app.services import rps_rotation
from app.services.ext_data import ExtConfig, ExtField
from app.storage.repository import DataStore, KlineRepository


def _enriched_rows(symbols: list[str], dates: list[date]) -> pl.DataFrame:
    """构造 ENRICHED_STORAGE_COLS 窗口: 每日收盘递增 → change_pct 非零。"""
    rows = []
    base = 10.0
    for i, d in enumerate(dates):
        for j, sym in enumerate(symbols):
            px = base + i + j * 0.5
            rows.append({
                "symbol": sym,
                "date": d,
                "open": px,
                "high": px + 0.3,
                "low": px - 0.3,
                "close": px,
                "raw_close": px,
                "raw_high": px + 0.3,
                "raw_low": px - 0.3,
                "volume": 1000.0 + i * 10,
                "amount": 10000.0 + i * 100,
                "turnover_rate": 1.0,
            })
    return pl.DataFrame(rows)


def _write_concept_snapshot(data_dir, symbols: list[str], concept: str) -> None:
    """写一份 snapshot 模式的概念扩展表, 供 _load_concept_map_df 读取。"""
    cfg = ExtConfig(
        id="ext_gn_ths",
        label="同花顺概念",
        mode="snapshot",
        fields=[ExtField(name="概念", label="概念"), ExtField(name="symbol", label="symbol")],
    )
    base = data_dir / "ext_data" / cfg.id
    base.mkdir(parents=True, exist_ok=True)
    df = pl.DataFrame({
        "symbol": symbols,
        "概念": [concept] * len(symbols),
    })
    df.write_parquet(base / "data.parquet")
    import json
    (base / "config.json").write_text(
        json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _make_repo(tmp_path) -> KlineRepository:
    return KlineRepository(DataStore(tmp_path))


def test_latest_enriched_date_uses_public_getter(tmp_path):
    """_latest_enriched_date 应走 enriched_latest_date(), 且历史私有字段已删除。"""
    repo = _make_repo(tmp_path)
    # 未加载时返回 None, 不抛 AttributeError
    assert rps_rotation._latest_enriched_date(repo) is None
    # 仓库不再保留全历史私有字段 (HistoryCore 切片已删除)
    assert not hasattr(repo, "_enriched_history_cache")


def test_build_rps_rotation_non_empty_matrix(tmp_path):
    """端到端: enriched + 概念快照 → 非空轮动矩阵, change_pct 经 get_enriched_range 派生。"""
    repo = _make_repo(tmp_path)

    symbols = ["000001.SZ", "000002.SZ", "600000.SH"]
    # 足够长的窗口以满足 RPS warmup (start = latest - days*2-10 日历天)
    dates = [date(2026, 5, 1) + timedelta(days=i) for i in range(15)]
    df = _enriched_rows(symbols, dates)
    repo.append_enriched(df)
    repo.store._register_views()

    # instruments: compute_all 需要 (涨跌停信号), 给一份最小维表
    inst_dir = tmp_path / "instruments"
    inst_dir.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": symbols, "name": ["甲", "乙", "丙"]}).write_parquet(
        inst_dir / "instruments.parquet"
    )
    repo._refresh_instruments()

    # 写概念快照
    _write_concept_snapshot(tmp_path, symbols, "测试概念")

    # 刷新缓存 → enriched_latest_date 可用; 结果缓存也清掉, 避免跨测试污染
    repo.refresh_cache()
    rps_rotation.invalidate_cache()
    rps_rotation._concept_map_cache = None

    out = rps_rotation.build_rps_rotation(repo, days=7)
    assert out["dates"], "轮动矩阵 dates 非空"
    assert out["columns"], "轮动矩阵 columns 非空"
    assert out["concept_count"] >= 1
    # 每个日期列非空, 且 (概念, 涨幅) 对齐全
    for d in out["dates"]:
        col = out["columns"][d]
        assert col, f"日期 {d} 的概念列为空"
        assert all(pair[0] == "测试概念" for pair in col)
    # change_pct 非零 (收盘每日递增) —— 派生列经 get_enriched_range 正确产出
    flat_pcts = [pair[1] for col in out["columns"].values() for pair in col]
    assert any(abs(p) > 0 for p in flat_pcts), "change_pct 全为零, 派生路径异常"
    # latest 在最前
    assert out["dates"][0] == str(dates[-1])


def test_build_rps_rotation_empty_without_enriched(tmp_path):
    """无 enriched 数据时返回空矩阵, 不抛异常。"""
    repo = _make_repo(tmp_path)
    rps_rotation.invalidate_cache()
    rps_rotation._concept_map_cache = None
    out = rps_rotation.build_rps_rotation(repo, days=7)
    assert out == {"dates": [], "columns": {}, "concept_count": 0}
