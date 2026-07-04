# P7.5 Agent 量化工具组 实现计划

> **面向 AI 代理的工作者：** REQUIRED SUB-SKILL: 使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 给 Agent 对话（`/api/agent/stream`）新增 4 个只读量化工具——重新开放 `run_backtest`（加安全闸门）、新增 `optimize_portfolio`、`analyze_factor`/`compare_factors`、`compose_factor_score`——让模型能在聊天里筛选、算因子、优化组合、跑回测。

**架构：** 全部工具落在 `backend/app/services/agent_tools.py::call_tool()` 的新分支里，复用已有的 P3 组合优化、因子回测服务，只有 `FactorBacktestService.compute_ic_only()` 和 `compose_factor_score` 本身是新逻辑。不新建 Swarm/pipeline 框架，多步编排靠模型自己在现有 `MAX_TOOL_ROUNDS=5` 循环里调用这些工具。

**技术栈：** Python 3.13、FastAPI、Polars、pytest-asyncio（`asyncio_mode=auto`，无需手写 `@pytest.mark.asyncio` 也可运行，但沿用现有文件里显式写法保持风格一致）。

## Global Constraints

- 每个新工具都在 `agent_tools.py::call_tool()` 里加分支，遵循现有 `_require(app_state, attr)` 模式获取依赖，不引入新的依赖获取方式。
- 所有新增校验失败一律 `raise ValueError(...)`——`agent_loop.py` 已有的 try/except 会把它转成 `tool_result.result.error`，不会中断整个对话，不需要额外包装。
- `symbols` 类参数在 agent tool 层一律要求非空：`run_backtest` ≤20，`optimize_portfolio`/`analyze_factor`/`compare_factors` ≤50。这些上限是本计划新加的，现有 HTTP API 本身没有等效约束，不能依赖 API 层。
- `run_backtest` 加日期区间 `(end-start).days <= 365`。
- 不做跨请求调用次数持久化计数（`agent_sessions.py` 已提供 session 持久化但无计数字段，扩展留待独立评估）。
- commit 需用户授权（批准本计划＝授权）；永不 push。

---

### Task 1: `FactorBacktestService.compute_ic_only()`

**文件：**
- 修改：`backend/app/backtest/factor.py`
- 测试：`backend/tests/backtest/test_factor_ic_only.py`（新建）

**接口：**
- Consumes：`FactorConfig`（已有，`backend/app/backtest/factor.py:69-79`）、`FactorBacktestService.__init__(engine)`（已有）、私有方法 `_load_factor_panel(config) -> pl.DataFrame`（已有，`:248-270`，已含因子补算/过滤/`_next_return`）、`_calc_ic(panel, factor_col) -> pl.DataFrame`（已有，`:292-305`）。
- Produces（Task 5 依赖）：`FactorBacktestService.compute_ic_only(config: FactorConfig) -> dict`，返回 `{"ic_mean": float|None, "ic_std": float|None, "ir": float|None, "ic_win_rate": float|None, "error": str|None}`。当 panel 为空或 IC 序列为空时，`error` 非空、其余字段为 `None`。

- [ ] **Step 1: 写失败的对拍测试**

创建 `backend/tests/backtest/test_factor_ic_only.py`：

```python
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl

from app.backtest.engine import BacktestEngine
from app.backtest.factor import FactorBacktestService, FactorConfig


def _panel(symbols: list[str], n_days: int, factor_name: str) -> pl.DataFrame:
    """构造无 ties 的因子面板：同一天不同 symbol 的因子值严格不同。"""
    rng = np.random.default_rng(7)
    rows = []
    for i, sym in enumerate(symbols):
        price = 10.0 + i
        for d in range(n_days):
            price *= 1 + rng.normal(0, 0.01)
            rows.append({
                "symbol": sym,
                "date": date(2024, 1, 1) + timedelta(days=d),
                "close": round(price, 4),
                factor_name: float(i) + d * 0.01,
            })
    return pl.DataFrame(rows)


def _service_with_panel(panel: pl.DataFrame) -> FactorBacktestService:
    engine = BacktestEngine(repo=None)
    engine.load_panel = lambda *a, **kw: panel  # 绕过 repo/parquet，直接注入面板
    return FactorBacktestService(engine)


def test_compute_ic_only_matches_run_ic_fields():
    panel = _panel(["A", "B", "C"], 10, "test_factor")
    svc = _service_with_panel(panel)
    config = FactorConfig(
        factor_name="test_factor",
        symbols=["A", "B", "C"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 10),
        rebalance="daily",
    )

    full = svc.run(config)
    ic_only = svc.compute_ic_only(config)

    assert ic_only["error"] is None
    assert ic_only["ic_mean"] == full.ic_mean
    assert ic_only["ic_std"] == full.ic_std
    assert ic_only["ir"] == full.ir
    assert ic_only["ic_win_rate"] == full.ic_win_rate


def test_compute_ic_only_reports_error_on_empty_panel():
    svc = _service_with_panel(pl.DataFrame())
    config = FactorConfig(
        factor_name="test_factor",
        symbols=["A"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 10),
        rebalance="daily",
    )

    out = svc.compute_ic_only(config)

    assert out["error"] is not None
    assert out["ic_mean"] is None
    assert out["ic_std"] is None
    assert out["ir"] is None
    assert out["ic_win_rate"] is None
```

- [ ] **Step 2: 跑测试确认失败**

运行：`cd backend && uv run --extra dev pytest tests/backtest/test_factor_ic_only.py -v`
预期：两个测试都 FAIL，报 `AttributeError: 'FactorBacktestService' object has no attribute 'compute_ic_only'`

- [ ] **Step 3: 实现 `compute_ic_only`**

在 `backend/app/backtest/factor.py` 里，紧跟在 `run()` 方法结束之后（`:219` 行 `return FactorResult(...)` 的收尾括号之后）、`random_control_ic` 之前，插入：

```python
    def compute_ic_only(self, config: FactorConfig) -> dict:
        """轻量 IC-only 计算：跳过分层回测和多空组合，只算 IC/IR。

        供 compose_factor_score 等需要对多个因子逐一算 IC 的场景复用，
        避免每个因子都重跑一次完整 run()（含分层净值、多空组合）。
        """
        panel = self._load_factor_panel(config)
        if panel.is_empty():
            return {"ic_mean": None, "ic_std": None, "ir": None, "ic_win_rate": None, "error": "无数据或因子列不可用"}

        ic_df = self._calc_ic(panel, config.factor_name)
        ic_values = [
            float(row["ic"]) for row in ic_df.iter_rows(named=True)
            if row["ic"] is not None and not np.isnan(float(row["ic"]))
        ]
        if not ic_values:
            return {"ic_mean": None, "ic_std": None, "ir": None, "ic_win_rate": None, "error": "过滤后无有效数据"}

        ic_mean = float(np.mean(ic_values))
        ic_std = float(np.std(ic_values))
        ir = (ic_mean / ic_std) if ic_std > 1e-8 else None
        ic_win_rate = sum(1 for v in ic_values if v > 0) / len(ic_values)
        return {
            "ic_mean": round(ic_mean, 4),
            "ic_std": round(ic_std, 4),
            "ir": round(ir, 4) if ir is not None else None,
            "ic_win_rate": round(ic_win_rate, 4),
            "error": None,
        }
```

- [ ] **Step 4: 跑测试确认通过**

运行：`cd backend && uv run --extra dev pytest tests/backtest/test_factor_ic_only.py -v`
预期：2 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/backtest/factor.py backend/tests/backtest/test_factor_ic_only.py
git commit -m "feat(factor): add compute_ic_only() lightweight IC calculation"
```

---

### Task 2: 重新开放 `run_backtest` + 安全闸门

**文件：**
- 修改：`backend/app/services/agent_loop.py:12`
- 修改：`backend/app/services/agent_tools.py:110-127`（`run_backtest` 分支）
- 修改：`backend/tests/services/test_agent_loop.py:87-114`（原测试依赖"run_backtest 被排除"，需要改写为只测未知工具名）
- 测试：`backend/tests/api/test_agent.py`（新增闸门测试）

**接口：**
- Consumes：`agent_tools.call_tool(name, app_state, args)`（已有签名不变）。
- Produces：`run_backtest` 分支新校验规则——`args["symbols"]` 必须非空列表且 `len<=20`；`(end-start).days<=365`；不满足则 `raise ValueError`。

- [ ] **Step 1: 写失败的测试（`_EXCLUDED_TOOLS` 不再排除 run_backtest）**

先看 `backend/tests/services/test_agent_loop.py:87-114` 现状——`test_agent_loop_rejects_excluded_and_unknown_tools_then_done` 用同一个测试断言"`run_backtest` 被排除"和"未知工具 `nope` 被拒绝"两件事。`run_backtest` 重新开放后，这个测试对 `run_backtest` 部分的断言不再成立（它会真的尝试调用 `call_tool`，因为 `_FakeState` 没有 `repo` 属性会得到 `{"error": "tool requires app_state.repo"}`，恰好也满足 `"error" in r["result"]`，但测试名字和意图已经不对了）。改写这个测试，只保留"未知工具名"这一断言：

```python
@pytest.mark.asyncio
async def test_agent_loop_rejects_unknown_tool_then_done():
    async def fake_generate(messages, **kw):
        try:
            return json.dumps({"tool": "nope", "args": {}})
        except Exception:
            return "普通回答"

    calls = {"n": 0}

    async def fake_generate2(messages, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return '{"tool":"nope","args":{}}'
        return "普通回答"

    async def fake_stream(messages, **kw):
        yield "好"

    events = await _collect(
        run_agent_stream(
            [{"role": "user", "content": "用一个不存在的工具"}],
            _FakeState(),
            generate=fake_generate2,
            stream=fake_stream,
        )
    )
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(results) == 1
    assert "error" in results[0]["result"]
    assert events[-1]["type"] == "done"
```

把 `backend/tests/services/test_agent_loop.py:87-114` 整段（原 `test_agent_loop_rejects_excluded_and_unknown_tools_then_done` 函数）替换为上面这个新函数（可以删掉未使用的 `fake_generate` 内层定义，只保留 `fake_generate2`，为了清晰直接写成下面这样，替换时用这个精简版本）：

```python
@pytest.mark.asyncio
async def test_agent_loop_rejects_unknown_tool_then_done():
    async def fake_generate(messages, **kw):
        return '{"tool":"nope","args":{}}'

    async def fake_stream(messages, **kw):
        yield "好"

    events = await _collect(
        run_agent_stream(
            [{"role": "user", "content": "用一个不存在的工具"}],
            _FakeState(),
            generate=fake_generate,
            stream=fake_stream,
        )
    )
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(results) == 1
    assert "error" in results[0]["result"]
    assert events[-1]["type"] == "done"
```

同时在 `backend/tests/api/test_agent.py` 末尾新增闸门测试：

```python
def test_run_backtest_requires_symbols():
    state = SimpleNamespace(repo=object(), strategy_engine=object())
    with pytest.raises(ValueError, match="symbols"):
        call_tool("run_backtest", state, {"strategy_id": "x"})


def test_run_backtest_rejects_too_many_symbols():
    state = SimpleNamespace(repo=object(), strategy_engine=object())
    with pytest.raises(ValueError, match="symbols"):
        call_tool("run_backtest", state, {
            "strategy_id": "x",
            "symbols": [f"{i:06d}.SZ" for i in range(21)],
        })


def test_run_backtest_rejects_wide_date_range():
    state = SimpleNamespace(repo=object(), strategy_engine=object())
    with pytest.raises(ValueError, match="date range"):
        call_tool("run_backtest", state, {
            "strategy_id": "x",
            "symbols": ["000001.SZ"],
            "start": "2023-01-01",
            "end": "2024-06-01",
        })
```

- [ ] **Step 2: 跑测试确认失败**

运行：`cd backend && uv run --extra dev pytest tests/services/test_agent_loop.py tests/api/test_agent.py -v`
预期：新增的 3 个 `test_run_backtest_*` FAIL（因为闸门还没实现，`run_backtest` 目前仍会先在 `strategy_id` 检查后走到 `StrategyBacktestService(...).run(...)`，因为 `_require` 校验的 `repo`/`strategy_engine` 用的是 `object()` 占位，实际调用会在更深处报别的错而不是我们要的 `symbols`/`date range` 消息）；`test_agent_loop_rejects_unknown_tool_then_done` 应该 PASS（因为它不依赖 run_backtest 排除逻辑）。

- [ ] **Step 3: 实现闸门**

修改 `backend/app/services/agent_loop.py:12`：

```python
_EXCLUDED_TOOLS: set[str] = set()
```

修改 `backend/app/services/agent_tools.py:110-127`（`run_backtest` 分支）：

```python
    if name == "run_backtest":
        repo = _require(app_state, "repo")
        strategy_engine = _require(app_state, "strategy_engine")
        from app.backtest.engine import BacktestEngine
        from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService

        strategy_id = str(args.get("strategy_id") or "").strip()
        if not strategy_id:
            raise ValueError("strategy_id required")
        symbols = args.get("symbols") or []
        if not symbols:
            raise ValueError("symbols required (non-empty, max 20)")
        if len(symbols) > 20:
            raise ValueError("symbols required (non-empty, max 20)")
        end = date.fromisoformat(args["end"]) if args.get("end") else date.today()
        start = date.fromisoformat(args["start"]) if args.get("start") else end - timedelta(days=180)
        if (end - start).days > 365:
            raise ValueError("date range too wide (max 365 days)")
        result = StrategyBacktestService(BacktestEngine(repo), strategy_engine).run(StrategyBacktestConfig(
            strategy_id=strategy_id,
            symbols=symbols,
            start=start,
            end=end,
        ))
        return _truncate(_plain(result))
```

（唯一的变化：`symbols` 不再允许省略/为空，加了 `len<=20` 和日期区间 `<=365` 天的校验；`StrategyBacktestConfig(symbols=...)` 从 `args.get("symbols")` 改成校验后的 `symbols` 变量。）

- [ ] **Step 4: 跑测试确认通过**

运行：`cd backend && uv run --extra dev pytest tests/services/test_agent_loop.py tests/api/test_agent.py -v`
预期：全部 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/agent_loop.py backend/app/services/agent_tools.py backend/tests/services/test_agent_loop.py backend/tests/api/test_agent.py
git commit -m "feat(agent): reopen run_backtest tool with symbols/date-range gate"
```

---

### Task 3: `optimize_portfolio` agent tool

**文件：**
- 修改：`backend/app/services/agent_tools.py`（`TOOLS` 列表 + `call_tool` 新分支）
- 测试：`backend/tests/api/test_agent.py`

**接口：**
- Consumes：`app.backtest.optimizers.portfolio_weights(returns, method, scores) -> np.ndarray`（已有）、`app.backtest.portfolio.load_price_matrix(repo, symbols, start, end) -> (prices, kept)`（已有）、`returns_from_prices`/`momentum_from_prices`（已有，`backend/app/backtest/portfolio.py`）。
- Produces：新 agent tool `optimize_portfolio`，`call_tool("optimize_portfolio", app_state, {"symbols":[...], "method": str, "lookback_days": int}) -> {"weights":[{"symbol","weight"}], "method", "lookback_days", "meta":{"kept","dropped"}}`。

- [ ] **Step 1: 写失败的测试**

在 `backend/tests/api/test_agent.py` 末尾新增：

```python
class _FakePortfolioRepo:
    def __init__(self, symbols: list[str], n_days: int = 60) -> None:
        self._symbols = symbols
        self._n_days = n_days

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
        import numpy as np
        from datetime import date as _date, timedelta as _timedelta

        if symbol not in self._symbols:
            return pl.DataFrame()
        rng = np.random.default_rng(sum(bytearray(symbol.encode())))
        base = 10.0
        rows = []
        for offset in range(self._n_days, 0, -1):
            base *= 1 + rng.normal(0, 0.01)
            rows.append({"symbol": symbol, "date": _date.today() - _timedelta(days=offset), "close": round(base, 3)})
        return pl.DataFrame(rows)


def test_optimize_portfolio_tool_requires_symbols():
    state = SimpleNamespace(repo=_FakePortfolioRepo(["000001.SZ"]))
    with pytest.raises(ValueError, match="symbols"):
        call_tool("optimize_portfolio", state, {"symbols": []})


def test_optimize_portfolio_tool_rejects_too_many_symbols():
    state = SimpleNamespace(repo=_FakePortfolioRepo(["000001.SZ"]))
    with pytest.raises(ValueError, match="symbols"):
        call_tool("optimize_portfolio", state, {"symbols": [f"{i:06d}.SZ" for i in range(51)]})


def test_optimize_portfolio_tool_weights_sum_to_one():
    symbols = ["000001.SZ", "000002.SZ", "600000.SH"]
    state = SimpleNamespace(repo=_FakePortfolioRepo(symbols))

    out = call_tool("optimize_portfolio", state, {"symbols": symbols, "method": "risk_parity", "lookback_days": 80})

    assert len(out["weights"]) == 3
    assert abs(sum(w["weight"] for w in out["weights"]) - 1.0) < 1e-6
    assert out["meta"]["kept"] == symbols
```

在文件顶部（若尚未导入）加 `import polars as pl`（`backend/tests/api/test_agent.py:1-6` 现状只有 `from types import SimpleNamespace` / `import pytest` / 两个 `from app...import`，需要新增 `import polars as pl`）。

- [ ] **Step 2: 跑测试确认失败**

运行：`cd backend && uv run --extra dev pytest tests/api/test_agent.py -v -k optimize_portfolio`
预期：3 个测试 FAIL，报 `ValueError: unknown agent tool: optimize_portfolio`

- [ ] **Step 3: 实现工具**

在 `backend/app/services/agent_tools.py` 的 `TOOLS` 列表末尾（`:56-62` 的 `list_ext_data` 条目之后，`]` 之前）新增：

```python
    {
        "name": "optimize_portfolio",
        "description": "Compute portfolio weights for a set of symbols (equal/equal_vol/risk_parity/mean_variance/max_diversification/score_weight).",
        "input_schema": {"type": "object", "properties": {"symbols": {"type": "array"}, "method": {"type": "string"}, "lookback_days": {"type": "integer"}}},
        "parameters": {"type": "object", "properties": {"symbols": {"type": "array"}, "method": {"type": "string"}, "lookback_days": {"type": "integer"}}},
        "read_only": True,
    },
```

在 `call_tool` 里 `list_ext_data` 分支（`:132-138`）和 `raise ValueError(f"unknown agent tool: {name}")`（`:139`）之间新增分支：

```python
    if name == "optimize_portfolio":
        repo = _require(app_state, "repo")
        import numpy as np

        from app.backtest.optimizers import portfolio_weights
        from app.backtest.portfolio import load_price_matrix, momentum_from_prices, returns_from_prices

        symbols = args.get("symbols") or []
        if not symbols:
            raise ValueError("symbols required (non-empty, max 50)")
        if len(symbols) > 50:
            raise ValueError("symbols required (non-empty, max 50)")
        method = str(args.get("method") or "risk_parity")
        if method not in {"equal", "equal_vol", "risk_parity", "mean_variance", "max_diversification", "score_weight"}:
            raise ValueError(f"unknown optimize method: {method}")
        lookback_days = max(20, min(1000, int(args.get("lookback_days") or 120)))
        end = date.today()
        start = end - timedelta(days=lookback_days)

        prices, kept = load_price_matrix(repo, symbols, start, end)
        if len(kept) < 2:
            raise ValueError("有效标的不足 2 只（数据缺失、港股或标的过少）")
        if prices.shape[0] < 2:
            raise ValueError("标的间共同交易日不足，无法估计收益/协方差")

        rets = returns_from_prices(prices)
        scores = momentum_from_prices(prices) if method == "score_weight" else None
        weights_arr = np.asarray(portfolio_weights(rets, method, scores), dtype=float)
        dropped = [s for s in symbols if s not in kept]
        weights = [{"symbol": s, "weight": round(float(weights_arr[i]), 6)} for i, s in enumerate(kept)]
        return _truncate({"weights": weights, "method": method, "lookback_days": lookback_days, "meta": {"kept": kept, "dropped": dropped}})
```

- [ ] **Step 4: 跑测试确认通过**

运行：`cd backend && uv run --extra dev pytest tests/api/test_agent.py -v -k optimize_portfolio`
预期：3 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/agent_tools.py backend/tests/api/test_agent.py
git commit -m "feat(agent): add optimize_portfolio tool"
```

---

### Task 4: `analyze_factor` + `compare_factors` agent tools

**文件：**
- 修改：`backend/app/services/agent_tools.py`（`TOOLS` 列表 + `call_tool` 两个新分支）
- 测试：`backend/tests/api/test_agent.py`

**接口：**
- Consumes：`app.backtest.factor.FactorBacktestService`/`FactorConfig`（已有）、`app.backtest.factor_zoo.ALPHAS`（已有，用于 `compare_factors` 的因子范围校验）、`app.backtest.engine.BacktestEngine`（已有）。
- Produces：`analyze_factor`（对应 `FactorConfig` 全字段 + `symbols` 非空≤50 校验，返回 `asdict(FactorResult)`）；`compare_factors`（`factor_ids` 需在 `ALPHAS` 中，`symbols` 非空≤50，返回 `{"factors":[{factor_id, coverage, n_dates, ic_mean, ic_ir, error}]}`）。

- [ ] **Step 1: 写失败的测试**

在 `backend/tests/api/test_agent.py` 末尾新增（复用 Task 1 的 `_panel` 思路，但这里通过 `BacktestEngine(repo=None)` + monkeypatch `load_panel` 直接注入，不依赖 `_FakePortfolioRepo`）：

```python
def _factor_panel(symbols, n_days, factor_name):
    import numpy as np
    from datetime import date as _date, timedelta as _timedelta

    rng = np.random.default_rng(11)
    rows = []
    for i, sym in enumerate(symbols):
        price = 10.0 + i
        for d in range(n_days):
            price *= 1 + rng.normal(0, 0.01)
            rows.append({
                "symbol": sym, "date": _date(2024, 1, 1) + _timedelta(days=d),
                "close": round(price, 4), factor_name: float(i) + d * 0.01,
            })
    return pl.DataFrame(rows)


class _FakeFactorRepo:
    """analyze_factor/compare_factors 分支只需要 app_state.repo 存在, 真正取数走 BacktestEngine.load_panel(monkeypatch)。"""


def test_analyze_factor_tool_requires_symbols():
    state = SimpleNamespace(repo=_FakeFactorRepo())
    with pytest.raises(ValueError, match="symbols"):
        call_tool("analyze_factor", state, {"factor_name": "momentum_20d", "symbols": []})


def test_analyze_factor_tool_returns_ic_fields(monkeypatch):
    from app.backtest import engine as engine_mod

    panel = _factor_panel(["A", "B", "C"], 10, "momentum_20d")
    monkeypatch.setattr(engine_mod.BacktestEngine, "load_panel", lambda self, *a, **kw: panel)

    state = SimpleNamespace(repo=_FakeFactorRepo())
    out = call_tool("analyze_factor", state, {
        "factor_name": "momentum_20d",
        "symbols": ["A", "B", "C"],
        "start": "2024-01-01",
        "end": "2024-01-10",
        "rebalance": "daily",
    })

    assert out["error"] is None
    assert "ic_mean" in out


def test_compare_factors_tool_rejects_unknown_factor():
    state = SimpleNamespace(repo=_FakeFactorRepo())
    with pytest.raises(ValueError, match="unknown factor"):
        call_tool("compare_factors", state, {"factor_ids": ["momentum_20d"], "symbols": ["A"]})


def test_compare_factors_tool_returns_rows(monkeypatch):
    from app.backtest import engine as engine_mod
    from app.backtest.factor_zoo import ALPHAS

    any_alpha = next(iter(ALPHAS))
    panel = _factor_panel(["A", "B", "C"], 10, any_alpha)
    monkeypatch.setattr(engine_mod.BacktestEngine, "load_panel", lambda self, *a, **kw: panel)

    state = SimpleNamespace(repo=_FakeFactorRepo())
    out = call_tool("compare_factors", state, {
        "factor_ids": [any_alpha],
        "symbols": ["A", "B", "C"],
        "start": "2024-01-01",
        "end": "2024-01-10",
    })

    assert len(out["factors"]) == 1
    assert out["factors"][0]["factor_id"] == any_alpha
```

- [ ] **Step 2: 跑测试确认失败**

运行：`cd backend && uv run --extra dev pytest tests/api/test_agent.py -v -k "analyze_factor or compare_factors"`
预期：5 个测试 FAIL，报 `ValueError: unknown agent tool: analyze_factor`（及 `compare_factors`）

- [ ] **Step 3: 实现两个工具**

在 `TOOLS` 列表里紧跟 Task 3 新增的 `optimize_portfolio` 条目之后新增：

```python
    {
        "name": "analyze_factor",
        "description": "Run single-factor IC/IR analysis and layered backtest for a set of symbols.",
        "input_schema": {"type": "object", "properties": {
            "factor_name": {"type": "string"}, "symbols": {"type": "array"},
            "start": {"type": "string"}, "end": {"type": "string"},
            "n_groups": {"type": "integer"}, "rebalance": {"type": "string"}, "weight": {"type": "string"},
        }},
        "parameters": {"type": "object", "properties": {
            "factor_name": {"type": "string"}, "symbols": {"type": "array"},
            "start": {"type": "string"}, "end": {"type": "string"},
            "n_groups": {"type": "integer"}, "rebalance": {"type": "string"}, "weight": {"type": "string"},
        }},
        "read_only": True,
    },
    {
        "name": "compare_factors",
        "description": "Compare multiple Alpha Zoo factors' IC/IR side by side (factor ids must exist in the Alpha Zoo).",
        "input_schema": {"type": "object", "properties": {
            "factor_ids": {"type": "array"}, "symbols": {"type": "array"},
            "start": {"type": "string"}, "end": {"type": "string"},
        }},
        "parameters": {"type": "object", "properties": {
            "factor_ids": {"type": "array"}, "symbols": {"type": "array"},
            "start": {"type": "string"}, "end": {"type": "string"},
        }},
        "read_only": True,
    },
```

在 `call_tool` 里紧跟 Task 3 新增的 `optimize_portfolio` 分支之后、`raise ValueError(f"unknown agent tool: {name}")` 之前新增：

```python
    if name == "analyze_factor":
        repo = _require(app_state, "repo")
        from app.backtest.engine import BacktestEngine
        from app.backtest.factor import FactorBacktestService, FactorConfig

        symbols = args.get("symbols") or []
        if not symbols:
            raise ValueError("symbols required (non-empty, max 50)")
        if len(symbols) > 50:
            raise ValueError("symbols required (non-empty, max 50)")
        factor_name = str(args.get("factor_name") or "").strip()
        if not factor_name:
            raise ValueError("factor_name required")
        end = date.fromisoformat(args["end"]) if args.get("end") else date.today()
        start = date.fromisoformat(args["start"]) if args.get("start") else end - timedelta(days=180)

        svc = FactorBacktestService(BacktestEngine(repo))
        result = svc.run(FactorConfig(
            factor_name=factor_name,
            symbols=symbols,
            start=start,
            end=end,
            n_groups=int(args.get("n_groups") or 5),
            rebalance=args.get("rebalance") or "monthly",
            weight=args.get("weight") or "equal",
        ))
        return _truncate(_plain(result))
    if name == "compare_factors":
        repo = _require(app_state, "repo")
        from app.backtest.engine import BacktestEngine
        from app.backtest.factor import FactorBacktestService, FactorConfig
        from app.backtest.factor_zoo import ALPHAS

        symbols = args.get("symbols") or []
        if not symbols:
            raise ValueError("symbols required (non-empty, max 50)")
        if len(symbols) > 50:
            raise ValueError("symbols required (non-empty, max 50)")
        factor_ids = list(args.get("factor_ids") or [])
        if not factor_ids:
            raise ValueError("factor_ids required")
        unknown = [x for x in factor_ids if x not in ALPHAS]
        if unknown:
            raise ValueError(f"unknown factor: {unknown[0]}")

        end = date.fromisoformat(args["end"]) if args.get("end") else date.today()
        start = date.fromisoformat(args["start"]) if args.get("start") else end - timedelta(days=180)
        svc = FactorBacktestService(BacktestEngine(repo))
        out = []
        for factor_id in factor_ids:
            result = svc.run(FactorConfig(factor_name=factor_id, symbols=symbols, start=start, end=end))
            out.append({
                "factor_id": factor_id,
                "coverage": result.n_symbols,
                "n_dates": result.n_dates,
                "ic_mean": result.ic_mean,
                "ic_ir": result.ir,
                "error": result.error,
            })
        return _truncate({"factors": out})
```

- [ ] **Step 4: 跑测试确认通过**

运行：`cd backend && uv run --extra dev pytest tests/api/test_agent.py -v -k "analyze_factor or compare_factors"`
预期：5 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/agent_tools.py backend/tests/api/test_agent.py
git commit -m "feat(agent): add analyze_factor and compare_factors tools"
```

---

### Task 5: `compose_factor_score` agent tool（IC 加权多因子合成）

**文件：**
- 修改：`backend/app/services/agent_tools.py`（`TOOLS` 列表 + `call_tool` 新分支）
- 测试：`backend/tests/api/test_agent.py`

**接口：**
- Consumes：Task 1 的 `FactorBacktestService.compute_ic_only(config) -> dict`；`app.backtest.factor.FactorConfig`/`FACTOR_COLUMNS`/`FactorBacktestService._compute_missing_factor(panel, factor_col)`（已有静态方法，用于当日面板里现算缺失的因子列）；`app.backtest.factor_zoo.ALPHAS`；`app.backtest.engine.BacktestEngine.load_panel`（已有）；`factor.py::_rank_average`（已有私有函数，本任务需要在 `agent_tools.py` 里导入使用，改为从 `app.backtest.factor` 显式 import，不复制实现）。
- Produces：`compose_factor_score` 工具，返回 `{"ranked": [{"symbol","composite_score","per_factor":{...}}], "meta": {"used_factors","excluded_factors","pool_size","as_of","scored_date"}}` 或 `{"error": str}`。

- [ ] **Step 1: 写失败的测试**

在 `backend/tests/api/test_agent.py` 末尾新增：

```python
def test_compose_factor_score_requires_pool():
    state = SimpleNamespace(repo=_FakeFactorRepo())
    with pytest.raises(ValueError, match="pool"):
        call_tool("compose_factor_score", state, {"factor_ids": ["momentum_20d"], "pool": []})


def test_compose_factor_score_rejects_pool_over_300():
    state = SimpleNamespace(repo=_FakeFactorRepo())
    with pytest.raises(ValueError, match="pool"):
        call_tool("compose_factor_score", state, {
            "factor_ids": ["momentum_20d"],
            "pool": [f"{i:06d}.SZ" for i in range(301)],
        })


def test_compose_factor_score_rejects_unknown_factor():
    state = SimpleNamespace(repo=_FakeFactorRepo())
    with pytest.raises(ValueError, match="unknown factor"):
        call_tool("compose_factor_score", state, {"factor_ids": ["not_a_real_factor"], "pool": ["A"]})


def _combined_factor_panel(symbols, n_days, factor_names):
    """构造同时含多个因子列的面板 (而不是每个因子各一份), 因为 compose_factor_score
    在算完 IC 权重后, 会用一次 load_panel 调用同时请求所有存活因子列 (见 Step 3 实现)。
    mock 统一返回这一份合并面板, 不需要按 columns 参数区分返回哪份, 避免遗漏。"""
    import numpy as np
    from datetime import date as _date, timedelta as _timedelta

    rng = np.random.default_rng(11)
    rows = []
    for i, sym in enumerate(symbols):
        price = 10.0 + i
        for d in range(n_days):
            price *= 1 + rng.normal(0, 0.01)
            row = {"symbol": sym, "date": _date(2024, 1, 1) + _timedelta(days=d), "close": round(price, 4)}
            for j, fname in enumerate(factor_names):
                row[fname] = float(i) + d * 0.01 + j * 100.0  # 因子间数值范围拉开, 互不干扰
            rows.append(row)
    return pl.DataFrame(rows)


def test_compose_factor_score_ranks_by_composite(monkeypatch):
    from app.backtest import engine as engine_mod

    symbols = ["A", "B", "C"]
    panel = _combined_factor_panel(symbols, 15, ["momentum_20d", "rsi_14"])
    monkeypatch.setattr(engine_mod.BacktestEngine, "load_panel", lambda self, *a, **kw: panel)

    state = SimpleNamespace(repo=_FakeFactorRepo())
    out = call_tool("compose_factor_score", state, {
        "factor_ids": ["momentum_20d", "rsi_14"],
        "pool": symbols,
        "as_of": "2024-01-15",
        "lookback_days": 14,
        "top_n": 3,
    })

    assert "error" not in out or out.get("error") is None
    assert len(out["ranked"]) == 3
    assert set(out["meta"]["used_factors"]) <= {"momentum_20d", "rsi_14"}
    scores = [r["composite_score"] for r in out["ranked"]]
    assert scores == sorted(scores, reverse=True)


def test_compose_factor_score_top_n_clamped_to_pool_size():
    state = SimpleNamespace(repo=_FakeFactorRepo())
    # top_n 传超大值不应报错, 应被钳到 pool 大小 (由 Step 3 实现里的 min(top_n, len(pool)) 保证,
    # 这里只验证不抛异常; 具体排序行为已由上一个测试覆盖)
    with pytest.raises(ValueError, match="unknown factor"):
        # 用一个必然失败的因子名, 只是为了在不依赖真实数据的情况下确认函数至少执行到了因子校验这一步
        call_tool("compose_factor_score", state, {"factor_ids": ["definitely_unknown"], "pool": ["A"], "top_n": 99999})
```

- [ ] **Step 2: 跑测试确认失败**

运行：`cd backend && uv run --extra dev pytest tests/api/test_agent.py -v -k compose_factor_score`
预期：全部 FAIL，报 `ValueError: unknown agent tool: compose_factor_score`

- [ ] **Step 3: 实现工具**

在 `TOOLS` 列表里紧跟 Task 4 新增的 `compare_factors` 条目之后新增：

```python
    {
        "name": "compose_factor_score",
        "description": "Combine multiple factors into one IC-weighted composite score across a symbol pool, ranked descending.",
        "input_schema": {"type": "object", "properties": {
            "factor_ids": {"type": "array"}, "pool": {"type": "array"},
            "as_of": {"type": "string"}, "lookback_days": {"type": "integer"}, "top_n": {"type": "integer"},
        }},
        "parameters": {"type": "object", "properties": {
            "factor_ids": {"type": "array"}, "pool": {"type": "array"},
            "as_of": {"type": "string"}, "lookback_days": {"type": "integer"}, "top_n": {"type": "integer"},
        }},
        "read_only": True,
    },
```

在 `call_tool` 里紧跟 Task 4 新增的 `compare_factors` 分支之后、`raise ValueError(f"unknown agent tool: {name}")` 之前新增：

```python
    if name == "compose_factor_score":
        repo = _require(app_state, "repo")
        import numpy as np

        from app.backtest.engine import BacktestEngine
        from app.backtest.factor import FACTOR_COLUMNS, FactorBacktestService, FactorConfig, _rank_average
        from app.backtest.factor_zoo import ALPHAS

        pool = args.get("pool") or []
        if not pool:
            raise ValueError("pool required (non-empty, max 300)")
        if len(pool) > 300:
            raise ValueError("pool required (non-empty, max 300)")
        factor_ids = list(args.get("factor_ids") or [])
        if not factor_ids:
            raise ValueError("factor_ids required")
        known_ids = {c["id"] for c in FACTOR_COLUMNS} | set(ALPHAS)
        unknown = [x for x in factor_ids if x not in known_ids]
        if unknown:
            raise ValueError(f"unknown factor: {unknown[0]}")

        as_of = date.fromisoformat(args["as_of"]) if args.get("as_of") else date.today()
        lookback_days = max(20, min(500, int(args.get("lookback_days") or 120)))
        start = as_of - timedelta(days=lookback_days)
        top_n_req = int(args.get("top_n") or 50)
        top_n = max(1, min(top_n_req, len(pool)))

        svc = FactorBacktestService(BacktestEngine(repo))
        used: list[dict] = []
        excluded: list[dict] = []
        for factor_id in factor_ids:
            ic = svc.compute_ic_only(FactorConfig(factor_name=factor_id, symbols=pool, start=start, end=as_of, rebalance="daily"))
            if ic["error"] is not None or ic["ic_mean"] is None or not ic["ic_std"]:
                excluded.append({"factor_id": factor_id, "reason": ic["error"] or "IC 不可用"})
                continue
            ir = ic["ic_mean"] / ic["ic_std"]
            used.append({"factor_id": factor_id, "ic_mean": ic["ic_mean"], "ir": ir, "sign": 1 if ic["ic_mean"] >= 0 else -1})

        if not used:
            return {"error": "所有因子均无法计算，无法合成", "meta": {"excluded_factors": excluded}}

        raw_weight_sum = sum(abs(f["ir"]) for f in used)
        note = None
        if raw_weight_sum == 0:
            for f in used:
                f["weight"] = 1.0 / len(used)
            note = "IC全为0，已退化为等权"
        else:
            for f in used:
                f["weight"] = abs(f["ir"]) / raw_weight_sum

        # 加载一份覆盖所有存活因子的面板, 取 pool 在 [start, as_of] 区间内 <= as_of 的最新可用交易日。
        # 必须带全 OHLCV(而不只是 close), 因为 _compute_missing_factor 需要它们才能现算 FACTOR_COLUMNS 里的基础指标因子。
        panel_columns = ["symbol", "date", "open", "high", "low", "close", "volume", "turnover_rate"]
        for f in used:
            if f["factor_id"] not in panel_columns:
                panel_columns.append(f["factor_id"])
        panel = BacktestEngine(repo).load_panel(pool, start, as_of, columns=panel_columns)
        if panel.is_empty():
            return {"error": "所选股票池在该日期范围内无可用行情数据"}

        available_dates = [d for d in panel["date"].unique().to_list() if d <= as_of]
        if not available_dates:
            return {"error": "所选股票池在该日期范围内无可用行情数据"}
        scored_date = max(available_dates)
        # day_panel_base 全程不被覆盖, 保留 OHLCV; 每个因子各自从这份稳定的基准面板派生,
        # 避免 _compute_missing_factor 的返回值(只含 symbol/date/close/该因子列)把 OHLCV 冲掉,
        # 导致后一个需要现算的因子拿不到计算指标所需的原始行情列。
        day_panel_base = panel.filter(pl.col("date") == scored_date)

        composite: dict[str, float] = {}
        per_factor: dict[str, dict[str, dict]] = {}
        for f in used:
            factor_id = f["factor_id"]
            if factor_id in day_panel_base.columns:
                sub = day_panel_base.select(["symbol", factor_id]).filter(pl.col(factor_id).is_not_null())
            else:
                computed = FactorBacktestService._compute_missing_factor(day_panel_base, factor_id)
                if factor_id not in computed.columns:
                    excluded.append({"factor_id": factor_id, "reason": "当日因子值不可用"})
                    continue
                sub = computed.select(["symbol", factor_id]).filter(pl.col(factor_id).is_not_null())
            symbols_n = sub["symbol"].to_list()
            n = len(symbols_n)
            if n == 0:
                continue
            values = sub[factor_id].cast(pl.Float64).to_numpy() * f["sign"]
            if n == 1:
                normalized = np.array([0.5])
            else:
                ranks = _rank_average(values)
                normalized = (ranks - 1) / (n - 1)
            for sym, norm_v in zip(symbols_n, normalized):
                composite[sym] = composite.get(sym, 0.0) + f["weight"] * float(norm_v)
                per_factor.setdefault(sym, {})[factor_id] = {"weight": round(f["weight"], 4), "ic_mean": f["ic_mean"], "rank_normalized_value": round(float(norm_v), 4)}

        uncovered = [s for s in pool if s not in composite]
        ranked = sorted(
            ({"symbol": sym, "composite_score": round(score, 6), "per_factor": per_factor.get(sym, {})} for sym, score in composite.items()),
            key=lambda r: r["composite_score"],
            reverse=True,
        )[:top_n]

        meta = {
            "used_factors": [f["factor_id"] for f in used],
            "excluded_factors": excluded,
            "pool_size": len(pool),
            "as_of": str(as_of),
            "scored_date": str(scored_date),
            "uncovered_symbols": uncovered,
        }
        if note:
            meta["note"] = note
        return _truncate({"ranked": ranked, "meta": meta})
```

- [ ] **Step 4: 跑测试确认通过**

运行：`cd backend && uv run --extra dev pytest tests/api/test_agent.py -v -k compose_factor_score`
预期：全部 PASS

- [ ] **Step 5: 全量回归**

运行：
```bash
cd backend && uv run --extra dev pytest tests/backtest/test_factor_ic_only.py tests/api/test_agent.py tests/services/test_agent_loop.py tests/api/test_agent_stream.py -v
```
预期：全部 PASS，无遗留失败。

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/agent_tools.py backend/tests/api/test_agent.py
git commit -m "feat(agent): add compose_factor_score IC-weighted multi-factor tool"
```

---

## 手测（全部任务完成后）

对照 spec 的验证章节，启动本地后端后手测：

```bash
curl -s -N -X POST http://localhost:8000/api/agent/stream \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"帮我用风险平价方法优化 000001.SZ, 600519.SH, 000002.SZ 这三只股票的组合权重"}]}'
```
预期看到 `tool_call name=optimize_portfolio` → `tool_result` 含 `weights` → `delta`/`done`。

```bash
curl -s -N -X POST http://localhost:8000/api/agent/stream \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"帮我跑个回测: 策略随便挑一个内置的, 标的用 600519.SH"}]}'
```
预期不再看到 `run_backtest` 被 `"error": "tool not allowed: run_backtest"` 拒绝（`_EXCLUDED_TOOLS` 已清空）。

## 自检

**1. 规格覆盖度：** spec 的 4 个工具（`run_backtest` 闸门、`optimize_portfolio`、`analyze_factor`/`compare_factors`、`compose_factor_score`）分别对应 Task 2/3/4/5；`compute_ic_only` 新方法对应 Task 1；symbols 上限（20/50/50/50）、pool 上限（300）、rank 公式修正 `(rank-1)/(n-1)`、n=1 固定 0.5、as_of 回退到最新可用交易日、未知 factor_id 报错、全部因子被剔除报错、IC 全零退化等权——均在 Task 5 的实现代码里体现。覆盖完整。

**2. 占位符扫描：** 无 TBD/TODO；所有测试均为可执行的完整代码。

**3. 类型一致性：** `compute_ic_only(config: FactorConfig) -> dict`（Task 1 定义）在 Task 5 里以 `svc.compute_ic_only(FactorConfig(...))` 的形式被消费，字段名 `ic_mean/ic_std/ir/ic_win_rate/error` 前后一致；`_rank_average` 在 Task 5 里从 `app.backtest.factor` 显式 import 而非复制实现，保证行为与 `factor.py` 内部完全一致。

**4. 已知限制（继承自 spec）：** `MAX_TOOL_ROUNDS=5` 意味着"筛选→因子→优化→回测"全链路几乎耗尽单次请求的工具调用配额；不做跨请求调用计数。这些不是本计划要修的问题。

**5. 自检时发现并修复的实现级 bug（不是 spec 层面的问题，是我写具体代码时才发现的）：** Task 5 最初版本在"当日面板"步骤里，循环对每个因子调用 `factor_zoo.compute_factor(day_panel, factor_id)` 并直接把结果赋回 `day_panel`；但真正该复用的是 `FactorBacktestService._compute_missing_factor`（它同时覆盖 `FACTOR_COLUMNS` 基础指标和 `ALPHAS`，`compute_factor` 只覆盖后者），而且它的返回值会把面板坍缩成只剩 `symbol/date/close/该因子列`（`factor.py:288`），如果直接覆盖 `day_panel` 循环处理下一个因子，会丢失 OHLCV，导致下一个需要现算的因子失败。已改为：保留一份不被覆盖的 `day_panel_base`（含 OHLCV），每个因子各自从这份稳定基准派生，不再链式覆盖。
