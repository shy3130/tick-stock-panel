# P3 组合优化器 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法跟踪进度。设计见 `docs/superpowers/specs/2026-07-04-vibe-frontend-porting-design.md`（子项目 1）。
>
> **本计划已过 codex 技术审查 + 真实数据源验证（2026-07-04），已并入其 Request Changes 全部结论。**

**目标：** 新增独立"组合优化器"工具页——选一组标的 + 优化方法 → 后端算权重与组合统计 → 前端表格 + 环形图展示。

**架构：** 后端按标的 `repo.get_daily_asset()` 拉收盘价（支持 stock + ETF），构造 returns matrix，套现成的 `portfolio_weights()`（`app/backtest/optimizers.py`），加 `POST /api/backtest/optimize` 薄端点。前端新建 `/optimizer` 页 + 菜单项，复用 `instrumentSearch` 选标的、`useECharts` 画环形图。

**技术栈：** 后端 FastAPI + Polars 1.40 + NumPy；前端 React + TS + Vite + ECharts + @tanstack/react-query。

## Global Constraints

- 后端测试：`cd backend && uv run --extra dev pytest <path> -q`。
- **数据加载用 `repo.get_daily_asset(asset_type, symbol, start, end, columns)`**（`repository.py:1009`）**逐标的拉**，`asset_type` 由 `app.api.kline._asset_type_for_symbol(symbol)` 判定（返回 `stock/etf/index/hk`，`kline.py:21`）。**不要用 `BacktestEngine.load_panel()`**——codex 实测它只读 `kline_daily_enriched`，**ETF 返回 0 行**（513050.SH 在 `kline_etf_enriched` 有 2211 行但 load_panel 读不到），改用 get_daily_asset 才能覆盖 ETF。
- **已知限制（须体现在 UI 的 dropped 提示 + 计划）**：**港股（`.HK`）本地无 enriched 数据**，`get_daily_asset("hk", ...)` 返回 0 行 → 港股一律进 `meta.dropped`。v1 优化器支持 **A股 + ETF**，不支持港股。
- Polars pivot 用 **`pivot(index="date", on="symbol", values="close")`**（`on=`，Polars 1.40 下 `columns=` 已 deprecation）。
- `portfolio_weights(returns, method, scores)` 签名固定（`optimizers.py:8`）：`returns` 是 `[T, N]` 日收益，`np.cov(r, rowvar=False)`，`method ∈ {equal, equal_vol, risk_parity, mean_variance, max_diversification, score_weight}`，`scores` 仅 `score_weight` 用（v1 用动量，见 Task 2）。
- 后端测试用 **最小 FastAPI + include_router** 构造 client（参照 `tests/api/test_ai_profiles.py:11-12`），不导入完整 `app.main`（避免 scheduler 副作用）。
- 样式复用现有组件（`PageHeader`、卡片、表格）与 Tailwind token；不引新 UI 风格。
- 前端 dev 走 `:3011`(Vite HMR)；LAN 生产 `:8000` 需 `pnpm build`（本计划不涉及）。
- commit 需用户授权——每个 Task 的 Commit 步骤先写好命令，实际提交等主线批准；永不 push。

---

## 文件结构

| 文件 | 职责 | 改动 |
|---|---|---|
| `backend/app/backtest/portfolio.py` | 逐标的拉价格构造 returns 矩阵 + 动量打分（IO 函数 + 纯函数） | 创建 |
| `backend/tests/backtest/test_portfolio_matrix.py` | portfolio.py 纯函数单测 | 创建 |
| `backend/app/api/backtest.py` | 加 `POST /api/backtest/optimize` | 修改 |
| `backend/tests/api/test_optimize_endpoint.py` | 端点集成测试（最小 FastAPI + FakeRepo） | 创建 |
| `frontend/src/lib/api.ts` | 加 `optimize()` + 类型 | 修改 |
| `frontend/src/pages/Optimizer.tsx` | 优化器页面 | 创建 |
| `frontend/src/router.tsx` | 加 `/optimizer` 路由 | 修改 |
| `frontend/src/components/Layout.tsx` | 加"组合优化"菜单项 | 修改 |

---

### Task 1：后端 returns-matrix + 动量打分 helper

**Files:**
- Create: `backend/app/backtest/portfolio.py`
- Test: `backend/tests/backtest/test_portfolio_matrix.py`

**Interfaces:**
- Produces:
  - `load_price_matrix(repo, symbols: list[str], start: date, end: date) -> tuple[np.ndarray, list[str]]` — 逐标的 `get_daily_asset` 拉收盘价，pivot 对齐，返回 `(prices[T,N], kept_symbols)`；kept 保持输入顺序、仅含有对齐数据的标的（港股/无数据被剔除）。
  - `returns_from_prices(prices: np.ndarray) -> np.ndarray` — 纯函数，`[T,N]` 价格 → `[T-1,N]` 日收益。
  - `momentum_from_prices(prices: np.ndarray) -> np.ndarray` — 纯函数，`[T,N]` 价格 → `[N]` 累计动量（last/first-1）。

- [ ] **Step 1: 写失败测试**（只测两个纯函数；`load_price_matrix` 的 IO 在 Task 2 集成测试覆盖）

```python
# backend/tests/backtest/test_portfolio_matrix.py
import numpy as np
from app.backtest.portfolio import returns_from_prices, momentum_from_prices


def test_returns_from_prices_basic():
    prices = np.array([[10.0, 100.0], [11.0, 90.0], [11.0, 99.0]])
    rets = returns_from_prices(prices)
    assert rets.shape == (2, 2)
    np.testing.assert_allclose(rets[0], [0.1, -0.1])
    np.testing.assert_allclose(rets[1], [0.0, 0.1])


def test_returns_from_prices_too_short():
    assert returns_from_prices(np.array([[10.0, 100.0]])).shape == (0, 2)


def test_momentum_from_prices():
    prices = np.array([[10.0, 100.0], [11.0, 90.0], [12.0, 99.0]])
    np.testing.assert_allclose(momentum_from_prices(prices), [0.2, -0.01])
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run --extra dev pytest tests/backtest/test_portfolio_matrix.py -q`
预期：FAIL（`ModuleNotFoundError: app.backtest.portfolio`）。

- [ ] **Step 3: 写实现**

```python
# backend/app/backtest/portfolio.py
from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl


def load_price_matrix(repo, symbols, start: date, end: date):
    """逐标的用 repo.get_daily_asset 拉收盘价，pivot 对齐成 [T,N] 价格矩阵。

    asset_type 由 _asset_type_for_symbol 判定，支持 stock + ETF；
    港股(.HK)/无数据标的 get_daily_asset 返回空，被自动剔除。
    返回 (prices, kept_symbols)；kept 保持输入顺序、仅含对齐后有数据的标的。
    """
    from app.api.kline import _asset_type_for_symbol

    frames = []
    for sym in symbols:
        asset_type = _asset_type_for_symbol(sym)
        try:
            df = repo.get_daily_asset(asset_type, sym, start, end, ["symbol", "date", "close"])
        except Exception:  # noqa: BLE001
            df = None
        if df is not None and not df.is_empty():
            frames.append(df.select(["symbol", "date", "close"]))

    if not frames:
        return np.empty((0, 0), dtype=float), []

    long = pl.concat(frames, how="vertical_relaxed")
    wide = long.pivot(index="date", on="symbol", values="close").sort("date")
    # 内对齐：丢掉任一标的缺失的交易日
    wide = wide.drop_nulls()
    kept = [s for s in symbols if s in wide.columns]
    if not kept:
        return np.empty((0, 0), dtype=float), []
    prices = wide.select(kept).to_numpy().astype(float)
    return prices, kept


def returns_from_prices(prices: np.ndarray) -> np.ndarray:
    """[T,N] 价格 → [T-1,N] 简单日收益。"""
    if prices.ndim != 2 or prices.shape[0] < 2:
        n = prices.shape[1] if prices.ndim == 2 else 0
        return np.empty((0, n), dtype=float)
    return prices[1:] / prices[:-1] - 1.0


def momentum_from_prices(prices: np.ndarray) -> np.ndarray:
    """[T,N] 价格 → [N] 累计动量 (last/first - 1)，作 score_weight 默认打分。"""
    if prices.ndim != 2 or prices.shape[0] < 1 or prices.shape[1] == 0:
        return np.empty((0,), dtype=float)
    return prices[-1] / prices[0] - 1.0
```

> 注：`_asset_type_for_symbol` 从 `app.api.kline` 引入是既有的对外可复用函数（内部只依赖 `is_etf_symbol`），无循环 import 风险；若实现时发现 import 环，改为把该判定逻辑复制到 portfolio.py（4 行）。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run --extra dev pytest tests/backtest/test_portfolio_matrix.py -q`
预期：PASS（3 passed）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/backtest/portfolio.py backend/tests/backtest/test_portfolio_matrix.py
git commit -m "feat(backtest): portfolio returns-matrix (stock+ETF) + momentum helper"
```

---

### Task 2：后端 `POST /api/backtest/optimize` 端点

**Files:**
- Modify: `backend/app/api/backtest.py`（在 `/run` 端点后加）
- Test: `backend/tests/api/test_optimize_endpoint.py`

**Interfaces:**
- Consumes: Task 1 的 `load_price_matrix`、`returns_from_prices`、`momentum_from_prices`；`optimizers.portfolio_weights`；`request.app.state.repo`。
- Produces: 端点返回
  `{ weights: [{symbol, name, weight}], stats: {annualized_vol, diversification_ratio, n}, method, lookback_days, meta: {kept, dropped} }`。

- [ ] **Step 1: 写失败测试**（最小 FastAPI + FakeRepo，参照 `tests/api/test_ai_profiles.py:11-12` 的 client 构造）

```python
# backend/tests/api/test_optimize_endpoint.py
from datetime import date, timedelta

import numpy as np
import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.backtest import router


class _FakeRepo:
    """按 symbol 返回不同的收盘价序列，并记录 get_daily_asset 收到的 asset_type。

    per_symbol[symbol] = (asset_type_returns_data, day_offsets)：
      - 若 symbol 不在 per_symbol → 返回空（模拟港股/缺数据 → dropped）。
      - day_offsets 控制交易日集合，用来构造"日期不重叠"场景。
    """
    def __init__(self, per_symbol):
        self._per = per_symbol
        self.asset_type_calls: dict[str, str] = {}

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
        self.asset_type_calls[symbol] = asset_type
        if symbol not in self._per:
            return pl.DataFrame()
        offsets = self._per[symbol]
        rng = np.random.default_rng(abs(hash(symbol)) % (2**32))
        base = 10.0
        rows = []
        for off in offsets:
            base *= 1 + rng.normal(0, 0.01)
            rows.append({"symbol": symbol, "date": date.today() - timedelta(days=off), "close": round(base, 3)})
        return pl.DataFrame(rows)

    def get_instruments(self):
        syms = list(self._per.keys())
        return pl.DataFrame({"symbol": syms, "name": [f"名{s[:3]}" for s in syms]})


def _client(repo):
    app = FastAPI()
    app.include_router(router)
    app.state.repo = repo
    return TestClient(app)


def _full(n=60):
    return list(range(n))[::-1]


def test_optimize_risk_parity_weights_sum_to_one():
    repo = _FakeRepo({"000001.SZ": _full(), "000002.SZ": _full(), "600000.SH": _full()})
    resp = _client(repo).post("/api/backtest/optimize",
                              json={"symbols": ["000001.SZ", "000002.SZ", "600000.SH"],
                                    "method": "risk_parity", "lookback_days": 80})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["weights"]) == 3
    assert abs(sum(w["weight"] for w in body["weights"]) - 1.0) < 1e-6
    assert all(w["weight"] >= 0 for w in body["weights"])
    assert body["stats"]["n"] == 3


def test_optimize_score_weight_momentum_sums_to_one():
    repo = _FakeRepo({"000001.SZ": _full(), "000002.SZ": _full(), "600000.SH": _full()})
    resp = _client(repo).post("/api/backtest/optimize",
                              json={"symbols": ["000001.SZ", "000002.SZ", "600000.SH"],
                                    "method": "score_weight", "lookback_days": 80})
    assert resp.status_code == 200
    assert abs(sum(w["weight"] for w in resp.json()["weights"]) - 1.0) < 1e-6


def test_optimize_rejects_single_symbol():
    repo = _FakeRepo({"000001.SZ": _full()})
    resp = _client(repo).post("/api/backtest/optimize",
                              json={"symbols": ["000001.SZ"], "method": "equal"})
    assert resp.status_code == 400


def test_optimize_etf_asset_type_and_hk_dropped():
    """ETF 用 asset_type=etf 拉；港股/缺数据进 dropped。"""
    repo = _FakeRepo({"600519.SH": _full(), "513050.SH": _full()})  # 00700.HK 不在 → 空
    resp = _client(repo).post("/api/backtest/optimize",
                              json={"symbols": ["600519.SH", "513050.SH", "00700.HK"],
                                    "method": "equal", "lookback_days": 80})
    assert resp.status_code == 200
    body = resp.json()
    assert repo.asset_type_calls["513050.SH"] == "etf"
    assert repo.asset_type_calls["00700.HK"] == "hk"
    assert "00700.HK" in body["meta"]["dropped"]
    assert set(body["meta"]["kept"]) == {"600519.SH", "513050.SH"}


def test_optimize_non_overlapping_dates_returns_400():
    """两标的交易日完全不重叠 → 共同交易日不足 → 400（不 500）。"""
    repo = _FakeRepo({"000001.SZ": list(range(60, 30, -1)),   # 第 30-60 天
                      "000002.SZ": list(range(30, 0, -1))})    # 第 0-30 天，无交集
    resp = _client(repo).post("/api/backtest/optimize",
                              json={"symbols": ["000001.SZ", "000002.SZ"],
                                    "method": "risk_parity", "lookback_days": 80})
    assert resp.status_code == 400
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run --extra dev pytest tests/api/test_optimize_endpoint.py -q`
预期：FAIL（404）。

- [ ] **Step 3: 写实现**（在 `backend/app/api/backtest.py` 的 `/run` 端点之后插入）

```python
# ================================================================
# 组合优化器 (P3)
# ================================================================

class OptimizeRequest(BaseModel):
    symbols: list[str] = Field(..., min_length=1)
    method: Literal[
        "equal", "equal_vol", "risk_parity",
        "mean_variance", "max_diversification", "score_weight",
    ] = "risk_parity"
    lookback_days: int = Field(120, ge=20, le=1000)


@router.post("/optimize")
def optimize(req: OptimizeRequest, request: Request):
    """组合优化器：给一组标的算配置权重（支持 A股 + ETF；港股无本地数据会被剔除）。"""
    import numpy as np
    from app.backtest.optimizers import portfolio_weights
    from app.backtest.portfolio import (
        load_price_matrix, returns_from_prices, momentum_from_prices,
    )

    repo = request.app.state.repo
    end = date.today()
    start = end - timedelta(days=req.lookback_days)

    prices, kept = load_price_matrix(repo, req.symbols, start, end)
    if len(kept) < 2:
        raise HTTPException(status_code=400, detail="有效标的不足 2 只（数据缺失、港股或标的过少）")
    # codex re-review High：共同交易日 < 2 时 returns 为空，portfolio_weights 返回 []，
    # 后面 w[i] 会 IndexError → 500。这里提前拦成 400。
    if prices.shape[0] < 2:
        raise HTTPException(status_code=400, detail="标的间共同交易日不足，无法估计收益/协方差")

    rets = returns_from_prices(prices)
    scores = momentum_from_prices(prices) if req.method == "score_weight" else None
    w = np.asarray(portfolio_weights(rets, req.method, scores), dtype=float)

    # 组合统计：年化波动 + 分散度比率（cov 与 portfolio_weights 一致用 rowvar=False）
    stats = {"n": len(kept), "annualized_vol": None, "diversification_ratio": None}
    clean = rets[np.isfinite(rets).all(axis=1)] if rets.size else rets
    if clean.shape[0] >= 2:
        cov = np.atleast_2d(np.cov(clean, rowvar=False))
        port_vol = float(np.sqrt(max(float(w @ cov @ w), 0.0)))
        vol = np.sqrt(np.maximum(np.diag(cov), 1e-12))
        stats["annualized_vol"] = round(port_vol * float(np.sqrt(252)), 6)
        if port_vol > 0:
            stats["diversification_ratio"] = round(float(w @ vol) / port_vol, 4)

    name_map: dict[str, str] = {}
    try:
        inst = repo.get_instruments()
        if inst is not None and not inst.is_empty() and {"symbol", "name"} <= set(inst.columns):
            name_map = dict(zip(inst["symbol"].to_list(), inst["name"].to_list()))
    except Exception:  # noqa: BLE001
        pass

    weights = [
        {"symbol": s, "name": name_map.get(s), "weight": round(float(w[i]), 6)}
        for i, s in enumerate(kept)
    ]
    dropped = [s for s in req.symbols if s not in kept]
    return {
        "weights": weights,
        "stats": stats,
        "method": req.method,
        "lookback_days": req.lookback_days,
        "meta": {"kept": kept, "dropped": dropped},
    }
```

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run --extra dev pytest tests/api/test_optimize_endpoint.py -q`
预期：PASS（5 passed）——含 ETF asset_type、港股 dropped、日期不重叠 400 三条边界覆盖。

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/backtest.py backend/tests/api/test_optimize_endpoint.py
git commit -m "feat(api): POST /api/backtest/optimize portfolio optimizer endpoint"
```

---

### Task 3：前端 api.optimize + 类型

**Files:**
- Modify: `frontend/src/lib/api.ts`

**Interfaces:**
- Produces: `api.optimize(body)` 返回 `OptimizeResult`；类型 `OptimizeMethod`、`OptimizeWeight`、`OptimizeResult`。

- [ ] **Step 1: 加类型与调用**（在 `api` 对象内，靠近其它 backtest 调用处）

```ts
export type OptimizeMethod =
  | 'equal' | 'equal_vol' | 'risk_parity'
  | 'mean_variance' | 'max_diversification' | 'score_weight'

export interface OptimizeWeight { symbol: string; name?: string | null; weight: number }
export interface OptimizeResult {
  weights: OptimizeWeight[]
  stats: { n: number; annualized_vol: number | null; diversification_ratio: number | null }
  method: OptimizeMethod
  lookback_days: number
  meta: { kept: string[]; dropped: string[] }
}

// api 对象内新增：
optimize: (body: { symbols: string[]; method: OptimizeMethod; lookback_days?: number }) =>
  request<OptimizeResult>('/api/backtest/optimize', {
    method: 'POST',
    body: JSON.stringify(body),
  }),
```

- [ ] **Step 2: tsc + Commit**

```bash
cd frontend && pnpm tsc --noEmit
git add src/lib/api.ts && git commit -m "feat(ui): optimize() api client + types"
```

预期：tsc EXIT 0。

---

### Task 4：前端 Optimizer 页面 + 路由 + 菜单

**Files:**
- Create: `frontend/src/pages/Optimizer.tsx`
- Modify: `frontend/src/router.tsx`、`frontend/src/components/Layout.tsx`

**Interfaces:**
- Consumes: Task 3 的 `api.optimize`、`OptimizeMethod`、`OptimizeResult`；`api.instrumentSearch`（`api.ts:1117`）、`instrumentSearchMeta`（`@/lib/instrumentSearch`）、`useECharts`（`@/pages/backtest/charts/useECharts`）、`PageHeader`。

- [ ] **Step 1: 写页面组件**

```tsx
// frontend/src/pages/Optimizer.tsx
import { useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { X, Loader2 } from 'lucide-react'
import { api, type OptimizeMethod, type OptimizeResult } from '@/lib/api'
import { instrumentSearchMeta } from '@/lib/instrumentSearch'
import { useECharts } from '@/pages/backtest/charts/useECharts'
import { PageHeader } from '@/components/PageHeader'

const METHODS: { key: OptimizeMethod; label: string; hint: string }[] = [
  { key: 'risk_parity', label: '风险平价', hint: '各标的风险贡献均等' },
  { key: 'equal', label: '等权', hint: '1/N 均分' },
  { key: 'equal_vol', label: '等波动', hint: '按波动倒数分配' },
  { key: 'mean_variance', label: '均值方差', hint: '收益/协方差最优（非负）' },
  { key: 'max_diversification', label: '最大分散', hint: '最大化分散度比率' },
  { key: 'score_weight', label: '动量加权', hint: '按近区间累计动量分配' },
]
const PIE = ['#3B82F6', '#22D3EE', '#F59E0B', '#A78BFA', '#2D9B65', '#C74040', '#E879F9', '#FACC15']

export function Optimizer() {
  const [symbols, setSymbols] = useState<{ symbol: string; name?: string }[]>([])
  const [query, setQuery] = useState('')
  const [method, setMethod] = useState<OptimizeMethod>('risk_parity')
  const [lookback, setLookback] = useState(120)
  const [result, setResult] = useState<OptimizeResult | null>(null)

  const search = useQuery({
    queryKey: ['optimizer-search', query],
    queryFn: () => api.instrumentSearch(query, 10),
    enabled: query.trim().length > 0,
  })

  const run = useMutation({
    mutationFn: () => api.optimize({ symbols: symbols.map(s => s.symbol), method, lookback_days: lookback }),
    onSuccess: setResult,
  })

  const addSymbol = (symbol: string, name?: string) => {
    if (!symbols.some(s => s.symbol === symbol)) setSymbols([...symbols, { symbol, name }])
    setQuery('')
  }

  const pieOption = useMemo(() => {
    if (!result) return null
    return {
      backgroundColor: 'transparent',
      tooltip: { trigger: 'item', formatter: (p: any) => `${p.name} ${(p.value * 100).toFixed(2)}%` },
      series: [{
        type: 'pie', radius: ['45%', '72%'], center: ['50%', '50%'],
        data: result.weights.map((w, i) => ({
          name: w.name ?? w.symbol,
          value: w.weight,
          itemStyle: { color: PIE[i % PIE.length] },
        })),
        label: { color: '#A1A1AA', fontSize: 11, formatter: (p: any) => `${p.name} ${p.percent.toFixed(1)}%` },
      }],
    }
  }, [result])
  const pieRef = useECharts(pieOption, [result])

  return (
    <div className="p-4 space-y-4">
      <PageHeader title="组合优化" subtitle="选一组标的 · 选优化方法 · 算配置权重" />

      <div className="rounded-card border border-border bg-surface p-3 space-y-2">
        <div className="flex flex-wrap gap-1.5">
          {symbols.map(s => (
            <span key={s.symbol} className="inline-flex items-center gap-1 rounded bg-elevated px-2 py-0.5 text-xs">
              {s.name ?? s.symbol}
              <button onClick={() => setSymbols(symbols.filter(x => x.symbol !== s.symbol))}><X className="h-3 w-3" /></button>
            </span>
          ))}
        </div>
        <div className="relative">
          <input
            value={query} onChange={e => setQuery(e.target.value)}
            placeholder="搜索代码/名称/拼音添加标的（支持 A股 / ETF）"
            className="w-full h-8 px-2.5 rounded-input border border-border bg-elevated text-xs"
          />
          {search.data && search.data.results.length > 0 && query && (
            <div className="absolute z-20 mt-1 w-full rounded-card border border-border bg-surface max-h-52 overflow-auto">
              {search.data.results.map(r => {
                const meta = instrumentSearchMeta(r)
                return (
                  <button key={r.symbol} onClick={() => addSymbol(r.symbol, r.name)}
                    className="flex w-full items-center justify-between px-2.5 py-1.5 text-xs hover:bg-elevated">
                    <span>{r.name} <span className="text-muted">{r.symbol}</span></span>
                    {meta ? <span className="text-muted">{meta}</span> : null}
                  </button>
                )
              })}
            </div>
          )}
        </div>
      </div>

      <div className="rounded-card border border-border bg-surface p-3 flex flex-wrap items-end gap-3">
        <label className="text-xs text-muted flex flex-col gap-1">
          优化方法
          <select value={method} onChange={e => setMethod(e.target.value as OptimizeMethod)}
            className="h-8 px-2 rounded-input border border-border bg-elevated text-xs text-foreground">
            {METHODS.map(m => <option key={m.key} value={m.key}>{m.label}</option>)}
          </select>
        </label>
        <label className="text-xs text-muted flex flex-col gap-1">
          回看天数
          <input type="number" min={20} max={1000} value={lookback}
            onChange={e => setLookback(Number(e.target.value))}
            className="h-8 w-24 px-2 rounded-input border border-border bg-elevated text-xs num" />
        </label>
        <span className="text-[11px] text-muted">{METHODS.find(m => m.key === method)?.hint}</span>
        <button
          disabled={symbols.length < 2 || run.isPending}
          onClick={() => run.mutate()}
          className="ml-auto h-8 px-4 rounded-btn bg-accent/90 text-base text-xs font-medium hover:bg-accent disabled:opacity-40 flex items-center gap-1.5">
          {run.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}计算权重
        </button>
      </div>

      {run.isError && <div className="text-xs text-danger">{(run.error as Error).message}</div>}

      {result && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <div className="rounded-card border border-border bg-surface p-3">
            <div className="flex gap-4 text-[11px] text-muted mb-2">
              <span>标的 {result.stats.n}</span>
              {result.stats.annualized_vol != null && <span>年化波动 {(result.stats.annualized_vol * 100).toFixed(1)}%</span>}
              {result.stats.diversification_ratio != null && <span>分散度 {result.stats.diversification_ratio}</span>}
            </div>
            <table className="w-full text-xs">
              <thead><tr className="text-muted"><th className="text-left py-1">标的</th><th className="text-right">权重</th></tr></thead>
              <tbody>
                {result.weights.slice().sort((a, b) => b.weight - a.weight).map(w => (
                  <tr key={w.symbol} className="border-t border-border/40">
                    <td className="py-1">{w.name ?? w.symbol} <span className="text-muted">{w.symbol}</span></td>
                    <td className="text-right num">{(w.weight * 100).toFixed(2)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {result.meta.dropped.length > 0 && (
              <div className="mt-2 text-[11px] text-muted">已剔除（无本地数据，如港股）：{result.meta.dropped.join(', ')}</div>
            )}
          </div>
          <div className="rounded-card border border-border bg-surface p-3">
            <div ref={pieRef} style={{ width: '100%', height: 300 }} />
          </div>
        </div>
      )}
    </div>
  )
}
```

- [ ] **Step 2: 注册路由**（`frontend/src/router.tsx`）

顶部 import 加 `import { Optimizer } from './pages/Optimizer'`；`children` 数组（`indices` 之后）加 `{ path: 'optimizer', element: <Optimizer /> },`。

- [ ] **Step 3: 加菜单项**（`frontend/src/components/Layout.tsx`）

nav 数组（`backtest` 之后）加 `{ to: '/optimizer', label: '组合优化', icon: PieChart },`；确保 `PieChart` 在 `lucide-react` import 列表里。

- [ ] **Step 4: tsc + 手测**

Run: `cd frontend && pnpm tsc --noEmit`（EXIT 0）。手测 `http://m4max.wf:3011/optimizer`：加 ≥2 A股/ETF → 选方法 → 计算 → 权重表和≈100% + 环形图渲染；单标的按钮禁用；加一个港股验证进"已剔除"。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/Optimizer.tsx frontend/src/router.tsx frontend/src/components/Layout.tsx
git commit -m "feat(ui): portfolio optimizer page + route + nav"
```

---

### Task 5：从策略池导入标的（纯前端）

> **codex review 修订**：原计划的"复用 `_apply_score` 做 strategy 打分"**砍掉**——`StrategyBacktestService(engine, strategy_engine)` 构造非平凡、`_apply_score(panel, StrategyDef, ...)` 复用成本高。score_weight 保持**动量**即可。本任务只做**前端"把某策略选出的标的一键导入优化器"**，用现有 `screenerStrategies` 数据源。

**Files:**
- Modify: `frontend/src/pages/Optimizer.tsx`

**Interfaces:**
- Consumes: `api.screenerStrategies()`（`api.ts:1227` → `{presets: ScreenerStrategy[]}`，字段 `id/name/description/source`）、`api.screenerRunPreset(id)`（`api.ts:1228` → `ScreenerResult{rows:any[]}`，每行含 `symbol`/`name`）。

- [ ] **Step 1: 前端加"从策略导入标的"**（`Optimizer.tsx` 标的选择卡片内，标签行下方加一行）

```tsx
// 组件顶部加：
const strategies = useQuery({ queryKey: ['opt-strategies'], queryFn: api.screenerStrategies })
const importFromStrategy = useMutation({
  mutationFn: (id: string) => api.screenerRunPreset(id),
  onSuccess: (res) => {
    const picked = (res.rows ?? [])
      .map((r: any) => ({ symbol: r.symbol as string, name: r.name as string | undefined }))
      .filter(p => p.symbol)
    setSymbols(prev => {
      const seen = new Set(prev.map(p => p.symbol))
      return [...prev, ...picked.filter(p => !seen.has(p.symbol))].slice(0, 50)
    })
  },
})

// 标的卡片内 JSX 加：
<div className="flex items-center gap-2">
  <select
    disabled={importFromStrategy.isPending}
    onChange={e => { if (e.target.value) importFromStrategy.mutate(e.target.value); e.target.value = '' }}
    className="h-7 px-2 rounded-input border border-border bg-elevated text-xs">
    <option value="">从策略导入标的…</option>
    {strategies.data?.presets.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
  </select>
  {importFromStrategy.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted" />}
</div>
```
> `ScreenerResult.rows` 是 `any[]`，`r.symbol`/`r.name` 按现有 screener 行结构取。（前端 tsconfig `noUnusedLocals=true`，勿留未用变量。）

- [ ] **Step 2: tsc + 手测 + Commit**

Run: `cd frontend && pnpm tsc --noEmit`（EXIT 0）；手测选一个策略 → 标的自动填入（去重、上限 50）→ 计算权重正常。
```bash
git add frontend/src/pages/Optimizer.tsx
git commit -m "feat(ui): import symbols from screener strategy into optimizer"
```

---

## 自检（规格覆盖）

- ✅ 独立优化器页 + 菜单入口（Task 4）
- ✅ 6 种方法（Task 2 Literal + Task 4 METHODS）
- ✅ 权重表 + 环形图 + 组合统计（Task 4）
- ✅ **支持 A股 + ETF**（Task 1 用 `get_daily_asset`，codex 实测 ETF 可读）；**港股不支持、明示剔除**（Global Constraints + Task 4 dropped 提示）
- ✅ score_weight = 动量（Task 2）；策略打分复用已按 codex 结论**砍掉**
- ✅ 从策略池导入标的（Task 5，用 `screenerStrategies`/`screenerRunPreset`）
- ✅ 边界：<2 标的 400、无数据/港股剔除并 meta 标注（Task 2）
- ✅ pivot 用 `on=`、测试用最小 FastAPI+router（Global Constraints，codex Medium/Low）
- 依赖顺序：Task 1 → 2 → 3 → 4 → 5，逐个可独立测试。
