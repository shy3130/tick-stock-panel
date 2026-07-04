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
- Produces：`_require_list(args: dict, key: str, max_len: int) -> list`（新增共享 helper，Task 3/4/5 都会消费，校验非空+`isinstance(...,list)`+长度上限，否则 `raise ValueError`）；`run_backtest` 分支新校验规则——`symbols` 用 `_require_list(args,"symbols",20)`；`(end-start).days<=365`；不满足则 `raise ValueError`。

- [ ] **Step 1: 写失败的测试（`_EXCLUDED_TOOLS` 不再排除 run_backtest）**

先看 `backend/tests/services/test_agent_loop.py:87-114` 现状——`test_agent_loop_rejects_excluded_and_unknown_tools_then_done` 用同一个测试断言"`run_backtest` 被排除"和"未知工具 `nope` 被拒绝"两件事。`run_backtest` 重新开放后，这个测试对 `run_backtest` 部分的断言不再成立（它会真的尝试调用 `call_tool`，因为 `_FakeState` 没有 `repo` 属性会得到 `{"error": "tool requires app_state.repo"}`，恰好也满足 `"error" in r["result"]`，但测试名字和意图已经不对了）。

**⚠️ 修正（panel 3 评审 Medium-1，已verify属实）**：下面这版必须保留 `calls["n"]` 计数器——`fake_generate` 每次都返回同一个未知工具 JSON 的话，`run_agent_stream` 的 `for _ in range(MAX_TOOL_ROUNDS)` 循环只在 `generate()` **不再请求工具**（返回的不是合法 tool JSON）时才 `break`（`backend/app/services/agent_loop.py:49-54`），错误的工具调用本身不会提前终止循环——所以不加计数器、每次都返回工具请求的话会跑满 5 轮，产出 5 个 `tool_result`，`assert len(results) == 1` 必然失败。把 `backend/tests/services/test_agent_loop.py:87-114`（原 `test_agent_loop_rejects_excluded_and_unknown_tools_then_done` 函数）替换为：

```python
@pytest.mark.asyncio
async def test_agent_loop_rejects_unknown_tool_then_done():
    calls = {"n": 0}

    async def fake_generate(messages, **kw):
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
            generate=fake_generate,
            stream=fake_stream,
        )
    )
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(results) == 1
    assert "error" in results[0]["result"]
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_agent_loop_allows_run_backtest_after_reopen(monkeypatch):
    """验证 run_backtest 确实已经不在 _EXCLUDED_TOOLS 里、能穿透到 call_tool（而不是被 agent_loop 自己拦成 "tool not allowed"）。"""
    from app.services import agent_loop as agent_loop_mod

    calls = {"n": 0}

    async def fake_generate(messages, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return '{"tool":"run_backtest","args":{"strategy_id":"x","symbols":["000001.SZ"]}}'
        return "普通回答"

    async def fake_stream(messages, **kw):
        yield "好"

    monkeypatch.setattr(
        agent_loop_mod.agent_tools, "call_tool",
        lambda name, app_state, args: {"sentinel": True} if name == "run_backtest" else {"error": "unexpected"},
    )

    events = await _collect(
        run_agent_stream(
            [{"role": "user", "content": "跑个回测"}],
            _FakeState(),
            generate=fake_generate,
            stream=fake_stream,
        )
    )
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(results) == 1
    assert results[0]["result"] == {"sentinel": True}
```

（第二个测试通过 monkeypatch `agent_tools.call_tool` 返回一个 sentinel 值来验证：只要 `run_backtest` 请求真的穿透到了 `call_tool`（而不是被 `agent_loop.py` 自己的 `_ALLOWED_NAMES` 白名单挡在外面返回 `"tool not allowed: run_backtest"`），就说明 `_EXCLUDED_TOOLS` 确实已清空、`run_backtest` 确实进了白名单——这是 Medium-2 指出的覆盖缺口。）

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


def test_run_backtest_rejects_non_list_symbols():
    """symbols 传字符串而不是列表要被拒绝, 不能被 truthy 判断悄悄放行。"""
    state = SimpleNamespace(repo=object(), strategy_engine=object())
    with pytest.raises(ValueError, match="symbols"):
        call_tool("run_backtest", state, {"strategy_id": "x", "symbols": "000001.SZ"})


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
预期：新增的 3 个 `test_run_backtest_*` FAIL（因为闸门还没实现，`run_backtest` 目前仍会先在 `strategy_id` 检查后走到 `StrategyBacktestService(...).run(...)`，因为 `_require` 校验的 `repo`/`strategy_engine` 用的是 `object()` 占位，实际调用会在更深处报别的错而不是我们要的 `symbols`/`date range` 消息）；`test_agent_loop_rejects_unknown_tool_then_done` 应该 PASS（不依赖 run_backtest 排除逻辑）；`test_agent_loop_allows_run_backtest_after_reopen` 应该 FAIL（因为此时 `run_backtest` 仍在 `_EXCLUDED_TOOLS` 里，`tool_result` 会是 `{"error": "tool not allowed: run_backtest"}` 而不是 sentinel）。

- [ ] **Step 3: 实现闸门**

修改 `backend/app/services/agent_loop.py:12`：

```python
_EXCLUDED_TOOLS: set[str] = set()
```

**⚠️ 修正（panel 3 评审 Medium-4，已verify属实）**：`args.get("symbols") or []` 这类写法不校验类型——如果模型传入的是一个字符串（如 `"symbols": "000001.SZ"`）而不是列表，`"000001.SZ" or []` 会原样把字符串传下去（真值判断不看类型），`len("000001.SZ")` 也能算出一个数字（9），会悄悄通过"非空"和"长度"校验，后续把字符串当列表用（比如 `for symbol in symbols` 会逐字符遍历）产出无意义结果而不是清晰报错。这个问题在 Task 2/3/4/5 的 `symbols`/`pool`/`factor_ids` 校验里都存在。加一个共享 helper 统一处理，在 `backend/app/services/agent_tools.py` 里 `_require` 函数（`:142-146`）之后插入：

```python
def _require_list(args: dict, key: str, max_len: int) -> list:
    value = args.get(key)
    if not isinstance(value, list) or not value:
        raise ValueError(f"{key} required (non-empty list, max {max_len})")
    if len(value) > max_len:
        raise ValueError(f"{key} required (non-empty list, max {max_len})")
    return value
```

修改 `backend/app/services/agent_tools.py:110-127`（`run_backtest` 分支），用 `_require_list` 替换原来手写的 `symbols` 校验：

```python
    if name == "run_backtest":
        repo = _require(app_state, "repo")
        strategy_engine = _require(app_state, "strategy_engine")
        from app.backtest.engine import BacktestEngine
        from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService

        strategy_id = str(args.get("strategy_id") or "").strip()
        if not strategy_id:
            raise ValueError("strategy_id required")
        symbols = _require_list(args, "symbols", 20)
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
- Consumes：`app.backtest.optimizers.portfolio_weights(returns, method, scores) -> np.ndarray`（已有）、`app.backtest.portfolio.load_price_matrix(repo, symbols, start, end) -> (prices, kept)`（已有）、`returns_from_prices`/`momentum_from_prices`（已有，`backend/app/backtest/portfolio.py`）、Task 2 的 `_require_list(args, key, max_len) -> list`（新增共享 helper）。
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


def test_optimize_portfolio_tool_rejects_non_list_symbols():
    state = SimpleNamespace(repo=_FakePortfolioRepo(["000001.SZ"]))
    with pytest.raises(ValueError, match="symbols"):
        call_tool("optimize_portfolio", state, {"symbols": "000001.SZ"})


def test_optimize_portfolio_tool_weights_sum_to_one():
    symbols = ["000001.SZ", "000002.SZ", "600000.SH"]
    state = SimpleNamespace(repo=_FakePortfolioRepo(symbols))

    out = call_tool("optimize_portfolio", state, {"symbols": symbols, "method": "risk_parity", "lookback_days": 80})

    assert len(out["weights"]) == 3
    # 用 1e-5 而不是更紧的 1e-6: 权重四舍五入到 6 位小数后累加误差理论上可能超过 1e-6
    # (P3 阶段 score_weight 权重和断言就因为这个原因出现过偶发失败, panel 3 提醒同样适用于这里)。
    assert abs(sum(w["weight"] for w in out["weights"]) - 1.0) < 1e-5
    assert out["meta"]["kept"] == symbols
```

在文件顶部（若尚未导入）加 `import polars as pl`（`backend/tests/api/test_agent.py:1-6` 现状只有 `from types import SimpleNamespace` / `import pytest` / 两个 `from app...import`，需要新增 `import polars as pl`）。

- [ ] **Step 2: 跑测试确认失败**

运行：`cd backend && uv run --extra dev pytest tests/api/test_agent.py -v -k optimize_portfolio`
预期：4 个测试 FAIL，报 `ValueError: unknown agent tool: optimize_portfolio`

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

        symbols = _require_list(args, "symbols", 50)
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
预期：4 passed

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


def test_analyze_factor_tool_rejects_wide_date_range():
    state = SimpleNamespace(repo=_FakeFactorRepo())
    with pytest.raises(ValueError, match="date range"):
        call_tool("analyze_factor", state, {
            "factor_name": "momentum_20d", "symbols": ["A"],
            "start": "2020-01-01", "end": "2024-01-01",
        })


def test_compare_factors_tool_rejects_too_many_factor_ids():
    from app.backtest.factor_zoo import ALPHAS

    # 循环取 ALPHAS 里的合法 id 拼出 21 个 (允许重复), 只为触发长度上限, 不依赖 ALPHAS 具体数量。
    ids_pool = list(ALPHAS)
    factor_ids = (ids_pool * ((21 // len(ids_pool)) + 1))[:21]
    state = SimpleNamespace(repo=_FakeFactorRepo())
    with pytest.raises(ValueError, match="factor_ids"):
        call_tool("compare_factors", state, {"factor_ids": factor_ids, "symbols": ["A"]})


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
预期：7 个测试 FAIL，报 `ValueError: unknown agent tool: analyze_factor`（及 `compare_factors`）

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
**⚠️ 修正（panel 3 评审 Medium-3/Medium-4/Medium-5，已verify属实）**：
- `compare_factors` 原方案漏了 `factor_ids<=20` 上限（接口小节写了"复用现有 `max_length=20` 约束"，但伪代码没真的检查），已补。
- `symbols`/`factor_ids` 改用 Task 2 的 `_require_list`，堵住字符串被当列表放行的问题。
- `analyze_factor`/`compare_factors` 直接构造 `FactorBacktestService`/`FactorConfig`，绕开了 HTTP `/factor/run`、`/factors/compare` 端点自带的 `_guard_server_backtest_range()`（`backend/app/api/backtest.py:58-63`，服务器内存约 1.8GB，超过 `BACKTEST_MAX_SERVER_DAYS=186` 天会 OOM 风险）。agent tool 层加一个等效的天数上限校验（不依赖 `settings.backtest_range_guard` 开关，直接硬性限制，因为 agent 是模型自主触发、比人工操作更需要保守）。

```python
    if name == "analyze_factor":
        repo = _require(app_state, "repo")
        from app.backtest.engine import BacktestEngine
        from app.backtest.factor import FactorBacktestService, FactorConfig

        symbols = _require_list(args, "symbols", 50)
        factor_name = str(args.get("factor_name") or "").strip()
        if not factor_name:
            raise ValueError("factor_name required")
        end = date.fromisoformat(args["end"]) if args.get("end") else date.today()
        start = date.fromisoformat(args["start"]) if args.get("start") else end - timedelta(days=180)
        if (end - start).days > 186:
            raise ValueError("date range too wide (max 186 days)")

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

        symbols = _require_list(args, "symbols", 50)
        factor_ids = _require_list(args, "factor_ids", 20)
        unknown = [x for x in factor_ids if x not in ALPHAS]
        if unknown:
            raise ValueError(f"unknown factor: {unknown[0]}")

        end = date.fromisoformat(args["end"]) if args.get("end") else date.today()
        start = date.fromisoformat(args["start"]) if args.get("start") else end - timedelta(days=180)
        if (end - start).days > 186:
            raise ValueError("date range too wide (max 186 days)")
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
预期：7 passed

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


def test_compose_factor_score_rejects_non_list_pool():
    state = SimpleNamespace(repo=_FakeFactorRepo())
    with pytest.raises(ValueError, match="pool"):
        call_tool("compose_factor_score", state, {"factor_ids": ["momentum_20d"], "pool": "000001.SZ"})


def test_compose_factor_score_all_factors_excluded_returns_error(monkeypatch):
    """验证 High-1/2 修复后的路径: 所有因子都算不出 IC 时, 直接报错而不是返回空结果冒充成功。"""
    from app.backtest import engine as engine_mod

    monkeypatch.setattr(engine_mod.BacktestEngine, "load_panel", lambda self, *a, **kw: pl.DataFrame())

    state = SimpleNamespace(repo=_FakeFactorRepo())
    out = call_tool("compose_factor_score", state, {"factor_ids": ["momentum_20d"], "pool": ["A", "B"]})

    assert out["error"] == "所有因子均无法计算，无法合成"
    assert out["meta"]["excluded_factors"][0]["factor_id"] == "momentum_20d"


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


def test_compose_factor_score_excludes_symbols_with_partial_factor_coverage(monkeypatch):
    """验证 High-3 修复: 某只股票在打分日只覆盖部分存活因子时, 应整体从排名剔除、计入
    uncovered_symbols, 而不是只用它有的那部分因子打折扣计分。"""
    from app.backtest import engine as engine_mod

    symbols = ["A", "B", "C"]
    panel = _combined_factor_panel(symbols, 15, ["momentum_20d", "rsi_14"])
    last_date = panel["date"].max()
    # 把 B 在最后一天(即将成为 scored_date)的 rsi_14 置空。不影响 IC 计算——
    # _calc_ic() 先过滤 _next_return.is_not_null(), 而最后一天的 _next_return 恒为 null
    # (shift(-1) 没有下一行可移), 本来就不参与 IC 拟合, 所以这处置空只影响"当日取值"这一步。
    panel = panel.with_columns(
        pl.when((pl.col("symbol") == "B") & (pl.col("date") == last_date))
        .then(pl.lit(None, dtype=pl.Float64))
        .otherwise(pl.col("rsi_14"))
        .alias("rsi_14")
    )
    monkeypatch.setattr(engine_mod.BacktestEngine, "load_panel", lambda self, *a, **kw: panel)

    state = SimpleNamespace(repo=_FakeFactorRepo())
    out = call_tool("compose_factor_score", state, {
        "factor_ids": ["momentum_20d", "rsi_14"],
        "pool": symbols,
        "as_of": "2024-01-15",
        "lookback_days": 14,
    })

    assert "error" not in out or out.get("error") is None
    ranked_symbols = {r["symbol"] for r in out["ranked"]}
    assert ranked_symbols == {"A", "C"}
    assert "B" in out["meta"]["uncovered_symbols"]


def test_compose_factor_score_as_of_falls_back_to_latest_trading_day(monkeypatch):
    """as_of 本身不在面板日期范围内(非交易日/未来日期)时, 应回退到 <= as_of 的最新可用交易日。"""
    from app.backtest import engine as engine_mod

    symbols = ["A", "B", "C"]
    panel = _combined_factor_panel(symbols, 15, ["momentum_20d"])  # 覆盖 2024-01-01..2024-01-15
    monkeypatch.setattr(engine_mod.BacktestEngine, "load_panel", lambda self, *a, **kw: panel)

    state = SimpleNamespace(repo=_FakeFactorRepo())
    out = call_tool("compose_factor_score", state, {
        "factor_ids": ["momentum_20d"],
        "pool": symbols,
        "as_of": "2024-01-20",  # 面板里最后一天是 01-15, 请求日更晚, 应回退
        "lookback_days": 25,
    })

    assert "error" not in out or out.get("error") is None
    assert out["meta"]["scored_date"] == "2024-01-15"
    assert out["meta"]["as_of"] == "2024-01-20"


def test_compose_factor_score_top_n_clamped_to_pool_size(monkeypatch):
    """top_n 传超过 pool 大小的值不应报错, 应被钳到 pool 大小, 不是原样透传导致超量或报错。"""
    from app.backtest import engine as engine_mod

    symbols = ["A", "B", "C"]
    panel = _combined_factor_panel(symbols, 15, ["momentum_20d"])
    monkeypatch.setattr(engine_mod.BacktestEngine, "load_panel", lambda self, *a, **kw: panel)

    state = SimpleNamespace(repo=_FakeFactorRepo())
    out = call_tool("compose_factor_score", state, {
        "factor_ids": ["momentum_20d"],
        "pool": symbols,
        "as_of": "2024-01-15",
        "lookback_days": 14,
        "top_n": 99999,
    })

    assert "error" not in out or out.get("error") is None
    assert len(out["ranked"]) == len(symbols)
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
**⚠️ 修正（panel 3 评审 High-1/High-2/High-3，已verify全部属实，这是 Task 5 里最重要的一处返工）：**

- **High-1**：原方案 `day_panel_base = panel.filter(date == scored_date)` 先把面板切到单日，再对缺失因子调用 `_compute_missing_factor`——但滚动窗口类因子（`momentum_20d`/`rsi_14`/`macd_hist` 等）内部靠 `compute_indicators()` 在多日历史上做 `.over("symbol")` 滚动计算，单日切片没有历史，算不出这类因子。必须**先在完整多日 `panel` 上现算每个因子**，再筛到 `scored_date` 那一行。
- **High-2**：原方案在"哪些因子进 `used`"确定之后就立刻按 IC 归一化权重，但后续在当日面板阶段可能因为"当日无该因子值"再把某个因子剔除——此时权重分母已经算过了、没有重新归一化，导致 `composite_score` 的有效权重和小于 1。必须等**最终存活因子集合**确定之后，再归一化一次权重。
- **High-3**：原方案里某只股票哪怕只覆盖了"存活因子"里的一部分，也会被计入 `composite` 并参与排名（缺的那部分因子直接跳过、不贡献分数），这和 spec 里"pool 中部分股票在实际打分日缺数据 → 该股票直接从结果里剔除"的既定行为不符。必须只保留**对所有最终存活因子都有值**的股票，其余归入 `uncovered_symbols`。

修正后的实现（整段替换原方案）：

```python
    if name == "compose_factor_score":
        repo = _require(app_state, "repo")
        import numpy as np

        from app.backtest.engine import BacktestEngine
        from app.backtest.factor import FACTOR_COLUMNS, FactorBacktestService, FactorConfig, _rank_average
        from app.backtest.factor_zoo import ALPHAS

        pool = _require_list(args, "pool", 300)
        factor_ids = _require_list(args, "factor_ids", 20)
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
        candidates: list[dict] = []
        excluded: list[dict] = []
        for factor_id in factor_ids:
            ic = svc.compute_ic_only(FactorConfig(factor_name=factor_id, symbols=pool, start=start, end=as_of, rebalance="daily"))
            if ic["error"] is not None or ic["ic_mean"] is None or not ic["ic_std"]:
                excluded.append({"factor_id": factor_id, "reason": ic["error"] or "IC 不可用"})
                continue
            ir = ic["ic_mean"] / ic["ic_std"]
            candidates.append({"factor_id": factor_id, "ic_mean": ic["ic_mean"], "ir": ir, "sign": 1 if ic["ic_mean"] >= 0 else -1})

        if not candidates:
            return {"error": "所有因子均无法计算，无法合成", "meta": {"excluded_factors": excluded}}

        # 加载完整历史面板(不是单日切片!), 因为 _compute_missing_factor 内部的滚动窗口指标
        # (如 rsi_14/momentum_20d) 需要多日历史才能算。必须带全 OHLCV, 现算才有原始行情列可用。
        panel_columns = ["symbol", "date", "open", "high", "low", "close", "volume", "turnover_rate"]
        for c in candidates:
            if c["factor_id"] not in panel_columns:
                panel_columns.append(c["factor_id"])
        panel = BacktestEngine(repo).load_panel(pool, start, as_of, columns=panel_columns)
        if panel.is_empty():
            return {"error": "所选股票池在该日期范围内无可用行情数据"}

        available_dates = [d for d in panel["date"].unique().to_list() if d <= as_of]
        if not available_dates:
            return {"error": "所选股票池在该日期范围内无可用行情数据"}
        scored_date = max(available_dates)

        # 在完整历史面板上把每个候选因子现算出来(若还不是现成列), 再筛到 scored_date 那一行。
        # factor_day_values: factor_id -> {symbol: 打分日当天的因子值}
        factor_day_values: dict[str, dict[str, float]] = {}
        survivors: list[dict] = []
        for c in candidates:
            factor_id = c["factor_id"]
            source = panel if factor_id in panel.columns else FactorBacktestService._compute_missing_factor(panel, factor_id)
            if factor_id not in source.columns:
                excluded.append({"factor_id": factor_id, "reason": "因子列不可用"})
                continue
            day_slice = (
                source.filter(pl.col("date") == scored_date)
                .select(["symbol", factor_id])
                .filter(pl.col(factor_id).is_not_null())
            )
            if day_slice.is_empty():
                excluded.append({"factor_id": factor_id, "reason": "打分日无该因子有效值"})
                continue
            factor_day_values[factor_id] = dict(zip(day_slice["symbol"].to_list(), day_slice[factor_id].cast(pl.Float64).to_list()))
            survivors.append(c)

        if not survivors:
            return {"error": "所有因子均无法计算，无法合成", "meta": {"excluded_factors": excluded}}

        # 只保留对所有最终存活因子都有值的股票 —— 部分覆盖的股票整体剔除, 不参与排名。
        common_symbols = set(pool)
        for c in survivors:
            common_symbols &= set(factor_day_values[c["factor_id"]].keys())
        uncovered = [s for s in pool if s not in common_symbols]
        if not common_symbols:
            return {
                "error": "所选股票池在打分日没有任何标的同时覆盖所有可用因子",
                "meta": {"excluded_factors": excluded, "uncovered_symbols": uncovered},
            }

        # 确定最终存活因子集合之后才归一化权重 —— 避免被后续剔除的因子污染权重和。
        raw_weight_sum = sum(abs(c["ir"]) for c in survivors)
        note = None
        if raw_weight_sum == 0:
            for c in survivors:
                c["weight"] = 1.0 / len(survivors)
            note = "IC全为0，已退化为等权"
        else:
            for c in survivors:
                c["weight"] = abs(c["ir"]) / raw_weight_sum

        symbols_sorted = sorted(common_symbols)
        composite: dict[str, float] = {s: 0.0 for s in symbols_sorted}
        per_factor: dict[str, dict[str, dict]] = {s: {} for s in symbols_sorted}
        n = len(symbols_sorted)
        for c in survivors:
            factor_id = c["factor_id"]
            values_map = factor_day_values[factor_id]
            values = np.array([values_map[s] for s in symbols_sorted]) * c["sign"]
            if n == 1:
                normalized = np.array([0.5])
            else:
                ranks = _rank_average(values)
                normalized = (ranks - 1) / (n - 1)
            for sym, norm_v in zip(symbols_sorted, normalized):
                composite[sym] += c["weight"] * float(norm_v)
                per_factor[sym][factor_id] = {"weight": round(c["weight"], 4), "ic_mean": c["ic_mean"], "rank_normalized_value": round(float(norm_v), 4)}

        ranked = sorted(
            ({"symbol": sym, "composite_score": round(score, 6), "per_factor": per_factor[sym]} for sym, score in composite.items()),
            key=lambda r: r["composite_score"],
            reverse=True,
        )[:top_n]

        meta = {
            "used_factors": [c["factor_id"] for c in survivors],
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

**1. 规格覆盖度：** spec 的 4 个工具（`run_backtest` 闸门、`optimize_portfolio`、`analyze_factor`/`compare_factors`、`compose_factor_score`）分别对应 Task 2/3/4/5；`compute_ic_only` 新方法对应 Task 1；symbols/pool/factor_ids 上限、rank 公式修正、as_of 回退、未知 factor_id 报错、全部因子被剔除报错——均在 Task 5 的实现代码**和**测试里体现（`test_compose_factor_score_excludes_symbols_with_partial_factor_coverage`/`test_compose_factor_score_as_of_falls_back_to_latest_trading_day`/`test_compose_factor_score_all_factors_excluded_returns_error`/`test_compose_factor_score_top_n_clamped_to_pool_size` 等）。

**⚠️ 诚实说明（panel 3 评审 Medium-6，已verify属实并修正）**：本条早先写过"覆盖完整"，但当时给出的测试代码其实只有 4-5 个，远不到脑子里想的"八类边界"，这是一处过度自信的自检结论，已被指出。修正后新增了 4 个测试（`excludes_symbols_with_partial_factor_coverage`、`as_of_falls_back_to_latest_trading_day`、`all_factors_excluded_returns_error`、`rejects_non_list_pool`），并把 `top_n_clamped_to_pool_size` 从"名不副实"改成真的验证钳制行为。**仍未覆盖、如实说明**：`IC全为0退化等权`（`raw_weight_sum==0`）和`n==1 时 rank 固定 0.5` 这两个分支——因为通过 `call_tool` 黑盒测试构造"真实相关系数恰好算出 0"或"最终存活股票缩到只剩 1 只"需要相当刻意的 fixture（前者依赖 `_calc_ic` 内部真实 IC 计算结果为 0，后者需要在保证 IC 计算本身成立的前提下让打分日可用股票数缩到 1），权衡投入产出后没有强行造一个脆弱的测试，这两处的正确性目前依赖代码走查而非可执行测试覆盖，如果后续要补，应各自作为独立的小任务补上。

**2. 占位符扫描：** 无 TBD/TODO；所有测试均为可执行的完整代码。

**3. 类型一致性：** `compute_ic_only(config: FactorConfig) -> dict`（Task 1 定义）在 Task 5 里以 `svc.compute_ic_only(FactorConfig(...))` 的形式被消费，字段名 `ic_mean/ic_std/ir/ic_win_rate/error` 前后一致；`_rank_average` 在 Task 5 里从 `app.backtest.factor` 显式 import 而非复制实现，保证行为与 `factor.py` 内部完全一致。

**4. 已知限制（继承自 spec）：** `MAX_TOOL_ROUNDS=5` 意味着"筛选→因子→优化→回测"全链路几乎耗尽单次请求的工具调用配额；不做跨请求调用计数。这些不是本计划要修的问题。

**5. 自检时发现并修复的实现级 bug（不是 spec 层面的问题，是我写具体代码时才发现的）：** Task 5 最初版本在"当日面板"步骤里，循环对每个因子调用 `factor_zoo.compute_factor(day_panel, factor_id)` 并直接把结果赋回 `day_panel`；但真正该复用的是 `FactorBacktestService._compute_missing_factor`（它同时覆盖 `FACTOR_COLUMNS` 基础指标和 `ALPHAS`，`compute_factor` 只覆盖后者），而且它的返回值会把面板坍缩成只剩 `symbol/date/close/该因子列`（`factor.py:288`），如果直接覆盖 `day_panel` 循环处理下一个因子，会丢失 OHLCV，导致下一个需要现算的因子失败。已改为：保留一份不被覆盖的 `day_panel_base`（含 OHLCV），每个因子各自从这份稳定基准派生，不再链式覆盖。

**6. panel 3 对本计划的评审（第二轮，2026-07-04，结论"需改"）：3 条 High + 6 条 Medium + 2 条 Low，逐条 verify 后全部属实，无一误报，已全部修正：**
- High-1：Task 5 在单日切片上现算滚动窗口因子算不出来 → 改为在完整多日 `panel` 上现算，再筛到 `scored_date`。
- High-2：权重在"最终存活因子集合"确定前就归一化，被后续剔除的因子污染权重和 → 改为先确定 `survivors` 再归一化。
- High-3：部分因子覆盖的股票仍被打分排名，与 spec"缺数据整体剔除"矛盾 → 改为 `common_symbols` 交集，只保留对所有存活因子都有值的股票。
- Medium-1：Task 2 改写的 `test_agent_loop_rejects_unknown_tool_then_done`"精简版"丢了计数器，会跑满 5 轮而非 1 轮 → 恢复计数器（这是我自己"简化代码"时手滑引入的真实回归）。
- Medium-2：缺一个"`run_backtest` 确实进了 `agent_loop` 白名单"的覆盖 → 新增 `test_agent_loop_allows_run_backtest_after_reopen`（monkeypatch sentinel）。
- Medium-3：`compare_factors` 漏了 `factor_ids<=20` → 通过 `_require_list` 补上。
- Medium-4：`symbols`/`pool`/`factor_ids` 用 `args.get(...) or []` 不校验类型，字符串会被当列表悄悄放行 → 新增共享 helper `_require_list`，Task 2/3/4/5 统一改用。
- Medium-5：`analyze_factor`/`compare_factors` 绕开了 HTTP API 的 `_guard_server_backtest_range()` 内存保护 → 加等效的 `(end-start).days<=186` 硬性上限。
- Medium-6：本自检最初声称"覆盖完整"但测试代码对不上，过度自信 → 见上面第 1 条的诚实说明 + 新增 4 个测试。
- Low-1：`test_compose_factor_score_top_n_clamped_to_pool_size` 名不副实（实际测的是未知因子报错）→ 改成真的验证钳制行为。
- Low-2：组合优化权重和断言 `1e-6` 对四舍五入后的权重偏紧（这是 P3 阶段已经踩过一次的同类坑）→ 松到 `1e-5`。
