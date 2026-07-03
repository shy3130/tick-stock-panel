# C5：回测稳健性验证（walk-forward / Bootstrap / MC permutation）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 在既有策略回测（已有 IC/IR/Calmar/per_symbol）之上加稳健性检验层：① walk-forward 分窗一致性（默认开）；② Bootstrap Sharpe 置信区间（默认开）；③ Monte-Carlo permutation 显著性 p 值（手动开，算力大）；④ 按退出原因（exit_reason）分组统计。结果并入 run_card（依赖 C2）。

**架构：** 纯后处理，零改动回测引擎。新模块 `app/backtest/robustness.py`：统计函数吃 `equity_curve`（日频净值序列）与 `trades`（含 `exit_reason`，`strategy.py:626` 已有）；walk-forward 复用 `StrategyBacktestService.run` 逐窗重跑。新端点 `POST /api/backtest/strategy/robustness`。

**技术栈：** Python 3.12 / numpy / polars。测试 `cd backend && uv run --extra dev pytest`。

**前置依赖：** C2 已落地（run_card 存在）。未落地时任务 3 的 run_card 并入步骤跳过，其余不受影响。

**现状证据：**
- 当前回测已有 IC/IR/Calmar/per_symbol 等结果，但缺少分窗一致性、统计置信区间和随机对照，难以判断策略是否只是样本内偶然有效。
- `strategy.py` 的交易结果已有 `exit_reason` 字段；退出原因分组可纯后处理实现，不需要改撮合/回测引擎。
- C2 run_card 是持久化承载面；稳健性结果应写入 run_card，避免散落成一次性 API 响应。
- Monte-Carlo permutation 成本较高，默认必须关闭；否则会把普通回测路径拖慢。

---

## 文件结构

| 文件 | 职责 | 改动 |
|---|---|---|
| `backend/app/backtest/robustness.py` | 统计函数（bootstrap/permutation/exit 分组/walk-forward 汇总） | 创建 |
| `backend/app/api/backtest.py` | robustness 端点 | 修改（追加路由） |
| `backend/tests/backtest/test_robustness.py` | 合成序列单测 | 创建 |

---

### 任务 1：统计核心（bootstrap / permutation / exit 分组）

**文件：**
- 创建：`backend/app/backtest/robustness.py`
- 测试：`backend/tests/backtest/test_robustness.py`

- [ ] **步骤 1：编写失败的测试**

```python
# backend/tests/backtest/test_robustness.py
import numpy as np

from app.backtest import robustness as rb


def _returns_from_curve(curve):
    vals = np.array([p["value"] for p in curve], dtype=float)
    return vals[1:] / vals[:-1] - 1.0


def test_bootstrap_ci_contains_point_sharpe():
    rng = np.random.default_rng(7)
    rets = rng.normal(0.001, 0.01, 500)          # 正漂移日收益
    out = rb.bootstrap_sharpe_ci(rets, n_boot=500, seed=7)
    assert out["ci_low"] < out["sharpe"] < out["ci_high"]
    assert out["n_boot"] == 500


def test_bootstrap_deterministic_with_seed():
    rets = np.random.default_rng(1).normal(0.0005, 0.012, 300)
    a = rb.bootstrap_sharpe_ci(rets, n_boot=200, seed=42)
    b = rb.bootstrap_sharpe_ci(rets, n_boot=200, seed=42)
    assert a == b


def test_permutation_pvalue_low_for_strong_signal():
    rng = np.random.default_rng(3)
    rets = rng.normal(0.002, 0.005, 400)          # 强正收益 → p 应显著
    out = rb.mc_permutation_pvalue(rets, n_perm=500, seed=3)
    assert out["p_value"] < 0.05


def test_permutation_pvalue_high_for_noise():
    rng = np.random.default_rng(5)
    rets = rng.normal(0.0, 0.01, 400)             # 纯噪声 → p 不显著
    out = rb.mc_permutation_pvalue(rets, n_perm=500, seed=5)
    assert out["p_value"] > 0.05


def test_exit_reason_breakdown():
    trades = [
        {"exit_reason": "stop_loss", "pnl_pct": -5.0},
        {"exit_reason": "stop_loss", "pnl_pct": -4.0},
        {"exit_reason": "signal", "pnl_pct": 8.0},
        {"exit_reason": None, "pnl_pct": 1.0},
    ]
    rows = rb.exit_reason_breakdown(trades)
    by = {r["exit_reason"]: r for r in rows}
    assert by["stop_loss"]["n"] == 2
    assert by["stop_loss"]["win_rate"] == 0.0
    assert by["signal"]["avg_pnl_pct"] == 8.0
    assert by["(none)"]["n"] == 1


def test_walk_forward_summary_dispersion():
    folds = [{"stats": {"sharpe": 1.0}}, {"stats": {"sharpe": 1.2}},
             {"stats": {"sharpe": -0.3}}]
    s = rb.walk_forward_summary(folds, metric="sharpe")
    assert s["n_folds"] == 3
    assert s["positive_folds"] == 2
    assert abs(s["mean"] - 0.6333) < 1e-3
    assert s["worst"] == -0.3
```

- [ ] **步骤 2：运行验证失败**

运行：`cd backend && uv run --extra dev pytest tests/backtest/test_robustness.py -v`
预期：FAIL（模块不存在）

- [ ] **步骤 3：实现**

```python
# backend/app/backtest/robustness.py
"""回测稳健性检验（C5）：纯后处理，不触碰回测引擎。

输入统一为日收益序列（由 equity_curve 差分而来）或 trades 列表。
"""
from __future__ import annotations

import numpy as np

_ANNUAL = 252.0


def _sharpe(rets: np.ndarray) -> float:
    sd = rets.std(ddof=1)
    if sd == 0 or len(rets) < 2:
        return 0.0
    return float(rets.mean() / sd * np.sqrt(_ANNUAL))


def bootstrap_sharpe_ci(rets: np.ndarray, n_boot: int = 1000,
                        ci: float = 0.95, seed: int | None = None) -> dict:
    """iid bootstrap 重抽样日收益,给出年化 Sharpe 的置信区间。"""
    rets = np.asarray(rets, dtype=float)
    rng = np.random.default_rng(seed)
    n = len(rets)
    samples = np.empty(n_boot)
    for i in range(n_boot):
        samples[i] = _sharpe(rets[rng.integers(0, n, n)])
    lo, hi = np.quantile(samples, [(1 - ci) / 2, 1 - (1 - ci) / 2])
    return {"sharpe": round(_sharpe(rets), 4), "ci_low": round(float(lo), 4),
            "ci_high": round(float(hi), 4), "ci": ci, "n_boot": n_boot}


def mc_permutation_pvalue(rets: np.ndarray, n_perm: int = 1000,
                          seed: int | None = None) -> dict:
    """符号置换检验:H0=收益无漂移。置换收益符号,统计 |Sharpe| 超越原值的比例。"""
    rets = np.asarray(rets, dtype=float)
    rng = np.random.default_rng(seed)
    observed = abs(_sharpe(rets))
    count = 0
    for _ in range(n_perm):
        signs = rng.choice([-1.0, 1.0], size=len(rets))
        if abs(_sharpe(rets * signs)) >= observed:
            count += 1
    return {"p_value": round((count + 1) / (n_perm + 1), 4), "n_perm": n_perm,
            "observed_sharpe": round(_sharpe(rets), 4)}


def exit_reason_breakdown(trades: list[dict]) -> list[dict]:
    groups: dict[str, list[float]] = {}
    for t in trades:
        key = t.get("exit_reason") or "(none)"
        groups.setdefault(str(key), []).append(float(t.get("pnl_pct") or 0.0))
    out = []
    for reason, pnls in sorted(groups.items()):
        arr = np.asarray(pnls)
        out.append({
            "exit_reason": reason,
            "n": len(arr),
            "win_rate": round(float((arr > 0).mean()), 4),
            "avg_pnl_pct": round(float(arr.mean()), 4),
            "total_pnl_pct": round(float(arr.sum()), 4),
        })
    return out


def walk_forward_summary(folds: list[dict], metric: str = "sharpe") -> dict:
    vals = np.asarray([float(f["stats"].get(metric, 0.0)) for f in folds])
    return {
        "metric": metric,
        "n_folds": len(vals),
        "mean": round(float(vals.mean()), 4) if len(vals) else 0.0,
        "std": round(float(vals.std(ddof=1)), 4) if len(vals) > 1 else 0.0,
        "worst": round(float(vals.min()), 4) if len(vals) else 0.0,
        "positive_folds": int((vals > 0).sum()),
    }


def returns_from_equity_curve(curve: list[dict]) -> np.ndarray:
    vals = np.asarray([float(p["value"]) for p in curve], dtype=float)
    if len(vals) < 2:
        return np.empty(0)
    return vals[1:] / vals[:-1] - 1.0
```

- [ ] **步骤 4：运行测试验证通过 + Commit**

```bash
cd backend && uv run --extra dev pytest tests/backtest/test_robustness.py -v
git add -A && git commit -m "feat(backtest): robustness stats core — bootstrap CI, permutation p, exit breakdown (C5)"
```

---

### 任务 2：robustness 端点（walk-forward 逐窗重跑）

**文件：**
- 修改：`backend/app/api/backtest.py`（`strategy_run` 之后追加端点）
- 测试：`backend/tests/backtest/test_robustness_windows.py`

- [ ] **步骤 1：编写失败的测试（分窗切割逻辑）**

```python
# backend/tests/backtest/test_robustness_windows.py
from datetime import date

from app.api.backtest import _walk_forward_windows


def test_windows_cover_range_without_overlap():
    ws = _walk_forward_windows(date(2024, 1, 1), date(2024, 12, 31), n_folds=4)
    assert len(ws) == 4
    assert ws[0][0] == date(2024, 1, 1)
    assert ws[-1][1] == date(2024, 12, 31)
    for (s1, e1), (s2, _e2) in zip(ws, ws[1:]):
        assert e1 < s2  # 相邻窗不重叠


def test_windows_min_fold_length_guard():
    import pytest
    with pytest.raises(ValueError, match="窗口过短"):
        _walk_forward_windows(date(2024, 1, 1), date(2024, 2, 1), n_folds=4)  # 每窗<30天
```

- [ ] **步骤 2：实现窗口切割 + 端点**

`backend/app/api/backtest.py` 追加：

```python
def _walk_forward_windows(start: date, end: date, n_folds: int) -> list[tuple[date, date]]:
    total = (end - start).days + 1
    fold_len = total // n_folds
    if fold_len < 30:
        raise ValueError(f"窗口过短: {n_folds} 窗每窗仅 {fold_len} 天(<30)")
    windows: list[tuple[date, date]] = []
    cur = start
    for i in range(n_folds):
        fold_end = end if i == n_folds - 1 else cur + timedelta(days=fold_len - 1)
        windows.append((cur, fold_end))
        cur = fold_end + timedelta(days=1)
    return windows


class RobustnessRequest(StrategyBacktestRequest):
    n_folds: int = 4
    bootstrap: bool = True
    mc_permutation: bool = False   # 手动开(算力大)
    n_boot: int = 1000
    n_perm: int = 1000


@router.post("/strategy/robustness")
def strategy_robustness(req: RobustnessRequest, request: Request):
    """稳健性检验:全区间跑一次拿基线,再 walk-forward 分窗重跑。"""
    from app.backtest import robustness as rb
    from app.backtest.strategy import StrategyBacktestService, StrategyBacktestConfig

    engine = _get_engine(request)
    svc = StrategyBacktestService(engine, request.app.state.strategy_engine)
    end = req.end or date.today()
    start = _resolve_start(req, end, FACTOR_DEFAULT_DAYS)
    _guard_server_backtest_range(start, end)

    def _run(s: date, e: date):
        cfg = StrategyBacktestConfig(
            strategy_id=req.strategy_id, symbols=req.symbols or None, start=s, end=e,
            params=req.params, overrides=req.overrides, matching=req.matching,
            entry_fill=req.entry_fill, exit_fill=req.exit_fill, fees_pct=req.fees_pct,
            slippage_bps=req.slippage_bps, max_positions=req.max_positions,
            max_exposure_pct=req.max_exposure_pct, initial_capital=req.initial_capital,
            position_sizing=req.position_sizing, mode=req.mode,
            holding_days=req.holding_days,
        )
        return svc.run(cfg)

    full = _run(start, end)
    if full.error:
        raise HTTPException(status_code=400, detail=full.error)
    rets = rb.returns_from_equity_curve(full.equity_curve)

    folds = []
    for ws, we in _walk_forward_windows(start, end, req.n_folds):
        r = _run(ws, we)
        folds.append({"start": str(ws), "end": str(we),
                      "stats": r.stats, "error": r.error})

    out = {
        "run_id": full.run_id,
        "full_stats": full.stats,
        "walk_forward": {
            "folds": folds,
            "summary": rb.walk_forward_summary([f for f in folds if not f["error"]]),
        },
        "exit_breakdown": rb.exit_reason_breakdown(full.trades),
    }
    if req.bootstrap and len(rets) >= 60:
        out["bootstrap"] = rb.bootstrap_sharpe_ci(rets, n_boot=req.n_boot)
    if req.mc_permutation and len(rets) >= 60:
        out["mc_permutation"] = rb.mc_permutation_pvalue(rets, n_perm=req.n_perm)
    return out
```

（`timedelta` 已在文件 date import 附近补充：`from datetime import date, timedelta`——先查现有 import 行避免重复。）

- [ ] **步骤 3：运行两条测试 + 全量回归**

```bash
cd backend && uv run --extra dev pytest tests/backtest/test_robustness_windows.py tests/backtest/test_robustness.py -v && uv run --extra dev pytest -q
```

- [ ] **步骤 4：手动全链路（真实策略）**

```bash
curl -s -X POST localhost:8000/api/backtest/strategy/robustness \
  -H 'content-type: application/json' \
  -d '{"strategy_id":"macd_golden","start":"2025-01-01","end":"2025-12-31","n_folds":4}' | head -c 800
```
预期：full_stats + 4 折 walk_forward + bootstrap CI + exit_breakdown

- [ ] **步骤 5：Commit** `git commit -am "feat(backtest): /strategy/robustness endpoint with walk-forward (C5)"`

---

### 任务 3：结果并入 run_card（依赖 C2）

- [ ] **步骤 1：** `strategy_robustness` 末尾（return 前）调用 C2 的 `_save_strategy_run_card(run_id=full.run_id, req_dict=req.model_dump(mode="json"), strategy_def=None, stats={**full.stats, "robustness": {k: out[k] for k in ("walk_forward", "bootstrap", "mc_permutation") if k in out}})`。
- [ ] **步骤 2：** 手动跑一次，确认 `data/research/run_cards/{run_id}.json` 里有 `stats.robustness.walk_forward.summary`。
- [ ] **步骤 3：Commit** `git commit -am "feat(research): robustness results merged into run_card"`

---

**前端说明：** 本计划不含 UI。回测页加"稳健性"标签页（展示折线/CI/表格）留给前端迭代，接口契约即上述响应结构。

## 非目标

- 不改 `StrategyBacktestService` 的撮合、信号、资金曲线生成逻辑；稳健性检验只消费既有回测结果。
- 不默认开启 MC permutation；它只能由请求参数显式打开。
- 不在本计划做前端图表页，只稳定后端响应契约和 run_card 持久化。
- 不把 walk-forward 结果解释成“未来收益保证”；只输出分窗一致性和显著性辅助指标。
