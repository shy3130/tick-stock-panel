"""minute_adjust 复权投影 / 基准标记 / 存量迁移 测试。

固定因子样本数值断言 (CONTRIBUTING §9): 除权日前后 ratio、VWAP 一致性、
标记门控 (未迁移行为逐字节不变)、迁移幂等与混合基准收敛。
"""

from datetime import date, datetime
from pathlib import Path

import polars as pl

from app.services import minute_adjust
from app.tickflow.repository import DataStore, KlineRepository

# 样本: AA 除权日 2026-07-01, ex_factor=1.25 (10送2.5量级);
#       BB 无除权事件。分钟窗口 06-30 (除权前) / 07-01 (除权日)。
AA, BB = "600001.SH", "600002.SH"
EX_DATE = date(2026, 7, 1)
EX_FACTOR = 1.25
RATIO_BEFORE = 1.0 / EX_FACTOR  # 前复权: 除权日前一日价格 * 1/1.25


def _minute_frame(symbol: str, day: date, price: float, rows: int = 3) -> pl.DataFrame:
    base = datetime(day.year, day.month, day.day, 9, 30)
    return pl.DataFrame({
        "symbol": [symbol] * rows,
        "datetime": [base.replace(minute=30 + i) for i in range(rows)],
        "open": [price] * rows,
        "high": [price * 1.01] * rows,
        "low": [price * 0.99] * rows,
        "close": [price] * rows,
        "volume": [100.0] * rows,
        "amount": [price * 100 * 100] * rows,  # volume(手)*100*price → VWAP=price
    })


def _write_factor_table(data_dir: Path, rows: list[tuple[str, date, float]]) -> None:
    d = data_dir / "adj_factor"
    d.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {"symbol": [r[0] for r in rows], "trade_date": [r[1] for r in rows], "ex_factor": [r[2] for r in rows]},
        schema={"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64},
    ).write_parquet(d / "all.parquet")


def _write_minute_partition(data_dir: Path, day: date, df: pl.DataFrame, etf: bool = False) -> None:
    root = data_dir / ("kline_etf_minute" if etf else "kline_minute")
    part = root / f"date={day.isoformat()}"
    part.mkdir(parents=True, exist_ok=True)
    df.write_parquet(part / "part.parquet")


def _write_daily_partition(data_dir: Path, day: date, closes: dict[str, float], etf: bool = False) -> None:
    root = data_dir / ("kline_etf_daily" if etf else "kline_daily")
    part = root / f"date={day.isoformat()}"
    part.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": list(closes.keys()),
        "close": list(closes.values()),
    }).write_parquet(part / "part.parquet")


# ================================================================
# 复权投影数学
# ================================================================

def test_apply_adjustment_factor_math(tmp_path):
    """除权日前的价格与 amount * ratio, 除权日起不动; volume 不变。"""
    _write_factor_table(tmp_path, [(AA, EX_DATE, EX_FACTOR)])
    df = pl.concat([_minute_frame(AA, date(2026, 6, 30), 10.0), _minute_frame(AA, EX_DATE, 8.0)])
    out = minute_adjust.apply_minute_adjustment(df, tmp_path, "stock")

    before = out.filter(pl.col("datetime").dt.date() == date(2026, 6, 30))
    after = out.filter(pl.col("datetime").dt.date() == EX_DATE)
    assert abs(before["close"][0] - 10.0 * RATIO_BEFORE) < 1e-9
    assert abs(before["high"][0] - 10.0 * 1.01 * RATIO_BEFORE) < 1e-9
    assert abs(before["amount"][0] - 10.0 * 100 * 100 * RATIO_BEFORE) < 1e-6
    assert before["volume"][0] == 100.0
    assert abs(after["close"][0] - 8.0) < 1e-12
    assert abs(after["amount"][0] - 8.0 * 100 * 100) < 1e-6


def test_apply_adjustment_vwap_consistency(tmp_path):
    """复权后 amount/(volume*100) 与复权 close 同口径 (均价线契约)。"""
    _write_factor_table(tmp_path, [(AA, EX_DATE, EX_FACTOR)])
    df = _minute_frame(AA, date(2026, 6, 30), 10.0)
    out = minute_adjust.apply_minute_adjustment(df, tmp_path, "stock")
    vwap = out["amount"][0] / (out["volume"][0] * 100)
    assert abs(vwap - out["close"][0]) < 1e-9


def test_apply_adjustment_identity_cases(tmp_path):
    """指数恒等 / 无因子表恒等 / 无事件恒等 (fail-open 语义)。"""
    df = _minute_frame(AA, date(2026, 6, 30), 10.0)

    out_index = minute_adjust.apply_minute_adjustment(df, tmp_path, "index")
    assert out_index.equals(df)

    out_no_factors = minute_adjust.apply_minute_adjustment(df, tmp_path, "stock")
    assert out_no_factors.equals(df)

    _write_factor_table(tmp_path, [(BB, EX_DATE, EX_FACTOR)])
    out_no_events = minute_adjust.apply_minute_adjustment(df, tmp_path, "stock")
    assert out_no_events.equals(df)


def test_apply_adjustment_etf_routing(tmp_path):
    """ETF 用 adj_factor_etf 表路由。"""
    etf = "159001.SZ"
    (tmp_path / "adj_factor_etf").mkdir(parents=True)
    pl.DataFrame({
        "symbol": [etf], "trade_date": [EX_DATE], "ex_factor": [EX_FACTOR],
    }, schema={"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64},
    ).write_parquet(tmp_path / "adj_factor_etf" / "all.parquet")

    df = _minute_frame(etf, date(2026, 6, 30), 10.0)
    out = minute_adjust.apply_minute_adjustment(df, tmp_path, "etf")
    assert abs(out["close"][0] - 10.0 * RATIO_BEFORE) < 1e-9


# ================================================================
# 基准标记与读取门控
# ================================================================

def test_fetch_adjust_param_gated_by_marker(tmp_path):
    assert minute_adjust.minute_fetch_adjust(tmp_path) == "forward"
    minute_adjust.mark_minute_basis_raw(tmp_path)
    assert minute_adjust.minute_fetch_adjust(tmp_path) == "none"
    assert minute_adjust.minute_basis_is_raw(tmp_path) is True


def test_repository_marker_gate(tmp_path):
    """标记未开启 → 读取原样 (旧行为逐字节不变); 开启 → 读取应用投影。"""
    _write_factor_table(tmp_path, [(AA, EX_DATE, EX_FACTOR)])
    raw_day = date(2026, 6, 30)
    _write_minute_partition(tmp_path, raw_day, _minute_frame(AA, raw_day, 10.0))
    repo = KlineRepository(DataStore(tmp_path))

    legacy = repo.get_minute(AA, raw_day)
    assert abs(legacy["close"][0] - 10.0) < 1e-12  # 未迁移: 原始值原样返回

    minute_adjust.mark_minute_basis_raw(tmp_path)
    adjusted = repo.get_minute(AA, raw_day)
    assert abs(adjusted["close"][0] - 10.0 * RATIO_BEFORE) < 1e-9

    # 当日 (因子事件之后) 恒等短路
    today_day = EX_DATE
    _write_minute_partition(tmp_path, today_day, _minute_frame(AA, today_day, 8.0))
    same = repo.get_minute(AA, today_day)
    assert abs(same["close"][0] - 8.0) < 1e-12


# ================================================================
# 存量迁移
# ================================================================

def test_migration_mixed_basis_partitions(tmp_path):
    """前复权存量 → 原始; 原始存量 no-op; 全部成功建标记; 幂等。"""
    _write_factor_table(tmp_path, [(AA, EX_DATE, EX_FACTOR)])
    day = date(2026, 6, 30)
    raw_price = 10.0
    # AA: 旧架构落盘形态 — 价格为"拉取时前复权"(*1/1.25), amount 保持原始 (复权只动价格)
    adjusted_stored = _minute_frame(AA, day, raw_price).with_columns(
        [pl.col(c) * RATIO_BEFORE for c in ("open", "high", "low", "close")]
    )
    raw_stored = _minute_frame(BB, day, raw_price)
    _write_minute_partition(tmp_path, day, pl.concat([adjusted_stored, raw_stored]))
    _write_daily_partition(tmp_path, day, {AA: raw_price, BB: raw_price})

    stats = minute_adjust.migrate_minute_to_raw(tmp_path)
    assert stats["failed"] == 0
    assert stats["marked"] is True
    assert stats["converted_symbols"] == 1  # 仅 AA
    assert minute_adjust.minute_basis_is_raw(tmp_path) is True

    stored = pl.read_parquet(tmp_path / "kline_minute" / f"date={day.isoformat()}" / "part.parquet")
    aa = stored.filter(pl.col("symbol") == AA)
    bb = stored.filter(pl.col("symbol") == BB)
    assert abs(aa["close"][0] - raw_price) < 1e-9          # 前复权被还原为原始
    assert abs(bb["close"][0] - raw_price) < 1e-12         # 原始 no-op
    assert abs(aa["amount"][0] - raw_price * 100 * 100) < 1e-6  # amount 本就原始, 不动

    # 幂等: 二次运行为 no-op
    stats2 = minute_adjust.migrate_minute_to_raw(tmp_path)
    assert stats2["marked"] is True
    assert stats2["converted_symbols"] == 0


def test_migration_missing_daily_anchor_skipped(tmp_path):
    """无日K锚点的分区跳过且不建标记 (未收敛), 日K补齐后可续跑。"""
    _write_factor_table(tmp_path, [(AA, EX_DATE, EX_FACTOR)])
    day = date(2026, 6, 30)
    _write_minute_partition(tmp_path, day, _minute_frame(AA, day, 10.0))  # 无日K分区

    stats = minute_adjust.migrate_minute_to_raw(tmp_path)
    assert stats["skipped_no_daily"] == 1
    assert stats["marked"] is False
    assert minute_adjust.minute_basis_is_raw(tmp_path) is False

    # 补齐日K锚点后续跑 → 收敛并标记
    _write_daily_partition(tmp_path, day, {AA: 10.0})
    stats2 = minute_adjust.migrate_minute_to_raw(tmp_path)
    assert stats2["marked"] is True


def test_migration_anchor_out_of_sane_range_rejected(tmp_path):
    """锚点偏离合理界 (数据异常) 不换算, 不产生破坏性写入。"""
    _write_factor_table(tmp_path, [(AA, EX_DATE, EX_FACTOR)])
    day = date(2026, 6, 30)
    # 日K锚点 0.001 vs 分钟 10 → k=10000 超界: 视为异常, 原样保留
    _write_minute_partition(tmp_path, day, _minute_frame(AA, day, 10.0))
    _write_daily_partition(tmp_path, day, {AA: 0.001})

    stats = minute_adjust.migrate_minute_to_raw(tmp_path)
    assert stats["marked"] is True  # 分区处理完成 (仅未换算), 仍可收敛
    stored = pl.read_parquet(tmp_path / "kline_minute" / f"date={day.isoformat()}" / "part.parquet")
    assert abs(stored["close"][0] - 10.0) < 1e-12
