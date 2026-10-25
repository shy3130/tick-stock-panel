"""矩阵回测的扩展数据字段注入 — 对齐 / 缺失补空 / 模式拦截 / 叠加条件联动。

背景: enriched parquet 只落 14 个存储列 (扩展列按设计不落盘), 扩展时序数值
字段由引擎在矩阵加载后按 (日期, 标的) 骨架现场 join (attach_matrix_ext_fields),
与财务因子 attach_matrix_fundamental_fields 同构。设计对齐 docs/features.md
「第三方数据接入」与 strategy.py 叠加条件矩阵列的注入约定。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from app.backtest.engine import BacktestEngine
from app.factors.ext_factors import (
    numeric_timeseries_ext_columns,
)
from app.tickflow.repository import DataStore, KlineRepository

SYMS = ["600001.SH", "600002.SH"]
DATES = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
EXT_COL = "ext_ext_demo_rank"      # ext_column_name("ext_demo", "rank")
SNAP_COL = "ext_ext_snapdemo_heat"  # 快照模式数值列 — 必须被拒
STR_COL = "ext_ext_demo_sname"      # 字符串列 — 必须被拒


def _write_enriched(data_dir: Path) -> None:
    for offset, d in enumerate(DATES):
        rows = []
        for i, sym in enumerate(SYMS):
            close = 10.0 + i + offset
            rows.append({
                "symbol": sym, "date": d,
                "open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
                "volume": 1000.0, "amount": close * 100000.0,
                "raw_close": close, "raw_high": close * 1.01, "raw_low": close * 0.99,
                "turnover_rate": 1.0 + i,
                "consecutive_limit_ups": 0, "consecutive_limit_downs": 0,
            })
        part = data_dir / "kline_daily_enriched" / f"date={d.isoformat()}"
        part.mkdir(parents=True)
        pl.DataFrame(rows).write_parquet(part / "part.parquet")
    inst = data_dir / "instruments"
    inst.mkdir(parents=True)
    pl.DataFrame({
        "symbol": SYMS, "name": ["甲", "乙"],
        "total_shares": [1e9, 1e9], "float_shares": [1e9, 1e9],
    }).write_parquet(inst / "part.parquet")


def _write_ext_config(data_dir: Path, config_id: str, mode: str, fields: list[dict]) -> None:
    cfg_dir = data_dir / "ext_data" / config_id
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(json.dumps({
        "id": config_id, "label": config_id, "mode": mode, "fields": fields,
        "symbol_map": {"type": "mapped", "col": "symbol"},
    }, ensure_ascii=False), encoding="utf-8")


def _write_ext_timeseries(data_dir: Path, rows: list[dict]) -> None:
    """rows: [{symbol, date, rank, sname}] — 缺行即缺失组合 (期望 NaN)。"""
    by_date: dict[str, list[dict]] = {}
    for r in rows:
        by_date.setdefault(r["date"], []).append(r)
    ts = data_dir / "ext_data" / "ext_demo" / "timeseries"
    ts.mkdir(parents=True)
    for d, day_rows in by_date.items():
        part = ts / f"date={d}"
        part.mkdir(parents=True)
        pl.DataFrame(day_rows).write_parquet(part / "part.parquet")


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    _write_enriched(tmp_path)
    _write_ext_config(tmp_path, "ext_demo", "timeseries", [
        {"name": "rank", "dtype": "int", "label": "排名"},
        {"name": "sname", "dtype": "string", "label": "名称"},
    ])
    _write_ext_config(tmp_path, "ext_snapdemo", "snapshot", [
        {"name": "heat", "dtype": "float", "label": "热度"},
    ])
    # 数据布局: 600001 全 4 天有排名; 600002 只有前 2 天 (后 2 天缺失 → NaN);
    # 1/5 顺带带字符串列验证只注入数值。
    rows = []
    for k, d in enumerate(DATES):
        # 600001: 首日 15 (区间外), 其余 50 (20<50<=100 命中叠加区间)
        rows.append({"symbol": SYMS[0], "date": d.isoformat(), "rank": 15 if k == 0 else 50, "sname": "甲"})
    for d in DATES[:2]:
        rows.append({"symbol": SYMS[1], "date": d.isoformat(), "rank": 150, "sname": "乙"})
    _write_ext_timeseries(tmp_path, rows)
    return tmp_path


def _engine(data_dir: Path) -> BacktestEngine:
    return BacktestEngine(KlineRepository(DataStore(data_dir)))


def _plan(matrix_extra: frozenset[str]) -> SimpleNamespace:
    return SimpleNamespace(
        execution_backend="matrix_native",
        base_columns=frozenset({"turnover_rate"}),
        instrument_columns=frozenset(),
        matrix_columns=matrix_extra,
        fundamental_columns=frozenset(),
    )


def test_numeric_timeseries_columns_filter(data_dir: Path):
    cols = numeric_timeseries_ext_columns(data_dir)
    assert EXT_COL in cols
    assert SNAP_COL not in cols   # 快照模式排除 (回看历史 = 未来函数)
    assert STR_COL not in cols    # 字符串列不进矩阵


def test_matrix_load_attaches_ext_fields_aligned(data_dir: Path):
    engine = _engine(data_dir)
    market = engine.load_market_data_matrix_for_backtest(
        None, DATES[0], DATES[-1], _plan(frozenset({EXT_COL})),
    )
    assert EXT_COL in market.fields
    arr = np.asarray(market.fields[EXT_COL])
    assert arr.shape == (len(DATES), len(SYMS))
    labels = list(market.timestamp_labels)
    syms = list(market.symbols)
    i0, i1 = labels.index(DATES[0].isoformat()), syms.index(SYMS[0])
    j1 = syms.index(SYMS[1])
    assert arr[i0, i0 - i0] == pytest.approx(15.0)     # 600001 首日排名 15
    assert arr[labels.index(DATES[-1].isoformat()), j1] != arr[labels.index(DATES[-1].isoformat()), j1]  # 600002 末日缺失 → NaN
    assert arr[labels.index(DATES[1].isoformat()), j1] == pytest.approx(150.0)
    # 基础列照常来自 parquet
    assert "turnover_rate" in market.fields
    # 数组只读 (frozen 副本约定, 同财务因子)
    with pytest.raises(ValueError):
        arr[0, 0] = 1.0


def test_matrix_load_rejects_snapshot_or_unknown_ext(data_dir: Path):
    engine = _engine(data_dir)
    with pytest.raises(ValueError, match=r"不可用于矩阵回测"):
        engine.load_market_data_matrix_for_backtest(
            None, DATES[0], DATES[-1], _plan(frozenset({SNAP_COL})),
        )
    with pytest.raises(ValueError, match=r"不可用于矩阵回测"):
        engine.load_market_data_matrix_for_backtest(
            None, DATES[0], DATES[-1], _plan(frozenset({"ext_ext_demo_ranks"})),  # 拼错的名
        )


def test_overlay_mask_on_ext_field(data_dir: Path):
    """端到端语义: 叠加条件在注入后的 ext 字段上评估, NaN 不命中。"""
    from app.backtest.strategy import StrategyBacktestService

    engine = _engine(data_dir)
    market = engine.load_market_data_matrix_for_backtest(
        None, DATES[0], DATES[-1], _plan(frozenset({EXT_COL})),
    )
    mask, err = StrategyBacktestService._overlay_matrix_mask(market, [
        {"left": EXT_COL, "op": ">", "right": "20", "leftDays": 0, "rightDays": 0},
        {"left": EXT_COL, "op": "<=", "right": "100", "leftDays": 0, "rightDays": 0},
    ])
    assert err is None and mask is not None
    arr = np.asarray(market.fields[EXT_COL])
    # 掩码与数组逐元素一致: 15 命中 (20<15? 不 — 15>20 为假!)
    np.testing.assert_array_equal(mask, (arr > 20) & (arr <= 100))
    # 正命中样本: 600001 第 2~4 天 (rank=50) 命中, 首日 15 与 600002 全部不命中
    assert mask[1, 0] and mask[2, 0] and mask[3, 0]
    assert not mask[0, 0] and not mask.all(axis=0)[1]


def test_attach_requires_existing_data(data_dir: Path):
    """配置存在但数据全空 → 明确报错而非静默全 NaN (fail-closed)。"""
    empty_dir = data_dir / "ext_data" / "ext_empty"
    empty_dir.mkdir(parents=True)
    (empty_dir / "config.json").write_text(json.dumps({
        "id": "ext_empty", "label": "e", "mode": "timeseries",
        "fields": [{"name": "v", "dtype": "float"}],
        "symbol_map": {"type": "mapped", "col": "symbol"},
    }), encoding="utf-8")
    engine = _engine(data_dir)
    with pytest.raises(ValueError, match=r"扩展字段数据不可用"):
        engine.load_market_data_matrix_for_backtest(
            None, DATES[0], DATES[-1], _plan(frozenset({"ext_ext_empty_v"})),
        )


# ---------------------------------------------------------------
# 端到端: 矩阵策略 + 扩展叠加条件真实回测 (回归: SignalMatrix 只读修复)
# ---------------------------------------------------------------

_ALWAYS_ENTRY_STRATEGY = '''import numpy as np
from app.backtest.matrix import make_signal_matrix

META = {
    "id": "ext_overlay_probe",
    "name": "ext叠加探针",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [],
    "scoring": {},
    "required_features": ["open", "high", "low", "close", "volume"],
}
EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = []
EXIT_SIGNALS = []
STOP_LOSS = None
MAX_HOLD_DAYS = 1

class AlwaysEntry:
    def required_fields(self):
        return frozenset({"open", "high", "low", "close", "volume"})
    def required_warmup_bars(self, params):
        return 1
    def compute_signals(self, market, params):
        entry = np.ones(market.shape, dtype=np.uint8)
        return make_signal_matrix(market.shape, entry=entry)

MATRIX_STRATEGY = AlwaysEntry()
'''


def _write_strategy(data_dir: Path) -> None:
    sd = data_dir / "strategies" / "custom"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "ext_overlay_probe.py").write_text(_ALWAYS_ENTRY_STRATEGY, encoding="utf-8")


def _service_for(data_dir: Path):
    from app.backtest.strategy import StrategyBacktestService, StrategyEngine

    _write_strategy(data_dir)
    engine = StrategyEngine(strategy_dirs=[data_dir / "strategies" / "custom"])
    return StrategyBacktestService(BacktestEngine(KlineRepository(DataStore(data_dir))), engine)


def _ext_overlay_conditions() -> list[dict]:
    return [{"left": EXT_COL, "op": "<=", "right": "100", "leftDays": 0, "rightDays": 0}]


def test_matrix_backtest_runs_with_ext_overlay(data_dir: Path):
    """扩展叠加条件端到端: 全命中版本正常出信号 (回归 SignalMatrix 只读修复)。"""
    from app.backtest.strategy import StrategyBacktestConfig

    service = _service_for(data_dir)
    res = service.run(StrategyBacktestConfig(
        strategy_id="ext_overlay_probe", symbols=None,
        start=DATES[0], end=DATES[-1],
        overrides={
            "basic_filter": {"enabled": False},   # 合成小盘股过不了默认市值过滤
            "overlay_filter": _ext_overlay_conditions(),
        },
        matching="close_t", fees_pct=0, slippage_bps=0, max_positions=2,
    ))
    assert res.error is None, res.error
    assert (res.stats.get("n_trades") or 0) > 0


def test_matrix_backtest_ext_overlay_can_block_all(data_data_dir=None):
    """反证过滤真实生效: 永不满足的叠加条件 → 无买入信号。"""
    import tempfile

    from app.backtest.strategy import StrategyBacktestConfig
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        _write_enriched(data_dir)
        _write_ext_config(data_dir, "ext_demo", "timeseries", [
            {"name": "rank", "dtype": "int", "label": "排名"},
            {"name": "sname", "dtype": "string", "label": "名称"},
        ])
        rows = [
            {"symbol": SYMS[0], "date": d.isoformat(), "rank": 15, "sname": "甲"}
            for d in DATES
        ] + [
            {"symbol": SYMS[1], "date": d.isoformat(), "rank": 150, "sname": "乙"}
            for d in DATES[:2]
        ]
        _write_ext_timeseries(data_dir, rows)
        service = _service_for(data_dir)
        res = service.run(StrategyBacktestConfig(
            strategy_id="ext_overlay_probe", symbols=None,
            start=DATES[0], end=DATES[-1],
            overrides={
                "basic_filter": {"enabled": False},
                "overlay_filter": [
                    {"left": EXT_COL, "op": ">", "right": "1000", "leftDays": 0, "rightDays": 0},
                ],
            },
            matching="close_t", fees_pct=0, slippage_bps=0, max_positions=2,
        ))
        assert res.error is not None and "未产生买入信号" in res.error
