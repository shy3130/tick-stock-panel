# TickFlow 全路径市场作用域实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 A股、港股、美股成为所有 TickFlow 业务路径共享的数据作用域，切换市场时路径不变且页面读取对应市场的真实数据。

**Architecture:** 前端用 `MarketScopeProvider` 将 URL `market` 参数、localStorage 与全局切换器统一；所有市场相关 API 和 React Query key 显式携带 market。后端用共享市场过滤契约隔离 symbol 和缓存，ClickHouse 三市场日线物化到现有日期分区 Parquet，再按市场读取 enriched；A 股专属页面对 HK/US 返回明确不适用状态。

**Tech Stack:** React 18、React Router、TanStack Query、TypeScript、FastAPI、Polars、ClickHouse、pytest、Vitest、Playwright、Docker Compose。

## Global Constraints

- 合法市场只有 `cn | hk | us`，默认 `cn`。
- 切换市场保持当前 pathname 和其他查询参数。
- 禁止跨市场缓存复用和 A 股静默兜底。
- 市场和 provider 是正交维度；provider 缺市场数据时显示不可用。
- 所有生产代码遵循测试先行，每个任务独立提交。
- 当前回滚镜像为 `tickflow-stock-panel-app:three-market-global-20260718-1727`。

---

### Task 1: 全局市场状态与 URL 契约

**Files:**
- Create: `frontend/src/lib/market-scope.tsx`
- Create: `frontend/src/lib/market-scope.test.ts`
- Modify: `frontend/src/router.tsx`
- Modify: `frontend/src/components/Layout.tsx`
- Modify: `frontend/src/lib/market-display.ts`

**Interfaces:**
- Produces: `useMarketScope(): { market: MarketCode; setMarket(market: MarketCode): void }`
- Produces: `withMarketParam(pathOrSearch: string, market: MarketCode): string`
- Consumes: existing `MarketFilterTabs` with `includeAll={false}`.

- [ ] **Step 1: Write failing URL/state tests**

```ts
expect(withMarketParam('/screener?as_of=2026-07-17', 'hk'))
  .toBe('/screener?as_of=2026-07-17&market=hk')
expect(normalizeMarketCode('invalid')).toBe('cn')
```

- [ ] **Step 2: Run test and confirm RED**

Run: `cd frontend && pnpm test --run src/lib/market-scope.test.ts`
Expected: FAIL because `withMarketParam` and provider do not exist.

- [ ] **Step 3: Implement provider and replace redirecting global entry**

`setMarket` must update the current URL with `navigate({ pathname, search }, { replace: true })`, preserve non-market params, and write `tickflow.market` to localStorage. `Layout` must call `setMarket` and stay on the current route.

- [ ] **Step 4: Verify GREEN and production build**

Run: `cd frontend && pnpm test --run src/lib/market-scope.test.ts src/lib/market-display.test.ts && pnpm build`
Expected: all tests PASS and Vite exits 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/market-scope.tsx frontend/src/lib/market-scope.test.ts frontend/src/router.tsx frontend/src/components/Layout.tsx frontend/src/lib/market-display.ts
git commit -m "feat: add global market scope"
```

### Task 2: 后端市场过滤与缓存契约

**Files:**
- Create: `backend/app/services/market_scope.py`
- Create: `backend/tests/test_market_scope.py`
- Modify: `backend/app/api/overview.py`
- Modify: `backend/app/api/screener.py`
- Modify: `backend/app/services/screener.py`
- Modify: `backend/app/services/market_overview_builder.py`

**Interfaces:**
- Produces: `normalize_market(value: str | None) -> Literal['cn','hk','us']`
- Produces: `market_for_symbol(symbol: str) -> str`
- Produces: `filter_frame_by_market(df: pl.DataFrame, market: str) -> pl.DataFrame`
- All overview/screener responses return top-level `market` and `currency`.

- [ ] **Step 1: Write failing frame-filter and cache-isolation tests**

```python
frame = pl.DataFrame({"symbol": ["000001.SZ", "1.HK", "A.US"]})
assert filter_frame_by_market(frame, "hk")["symbol"].to_list() == ["1.HK"]
assert overview_cache_key("hk", None) != overview_cache_key("us", None)
```

- [ ] **Step 2: Run test and confirm RED**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_market_scope.py -q`
Expected: import failure for the new market-scope functions.

- [ ] **Step 3: Implement shared helpers and add `market` to endpoint signatures**

`market_overview`, `market_snapshot`, screener cached/run/run_all must normalize market before reading data. Existing default remains `cn`; every cache key begins with market.

- [ ] **Step 4: Filter enriched before calculations**

`ScreenerService` accepts `market='cn'`; `_load_enriched_for_date` and cached result loads call `filter_frame_by_market` before joins and ranking.

- [ ] **Step 5: Verify tests**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_market_scope.py tests/test_instrument_market_ui.py tests/test_clickhouse_provider.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/market_scope.py backend/tests/test_market_scope.py backend/app/api/overview.py backend/app/api/screener.py backend/app/services/screener.py backend/app/services/market_overview_builder.py
git commit -m "feat: isolate backend data by market"
```

### Task 3: React Query 与 API 市场传播

**Files:**
- Modify: `frontend/src/lib/queryKeys.ts`
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/pages/Dashboard.tsx`
- Modify: `frontend/src/pages/Watchlist.tsx`
- Modify: `frontend/src/pages/Screener.tsx`
- Modify: `frontend/src/pages/Review.tsx`
- Test: `frontend/src/lib/market-scope.test.ts`

**Interfaces:**
- `QK.overviewMarket(market, asOf)`、`QK.marketSnapshot(market)`、`QK.screenerCached(market, ext)`.
- `api.overviewMarket(market, asOf)`、`api.marketSnapshot(market)`.

- [ ] **Step 1: Add failing query-key tests**

```ts
expect(QK.overviewMarket('cn')).not.toEqual(QK.overviewMarket('hk'))
expect(QK.marketSnapshot('hk')).toContain('hk')
```

- [ ] **Step 2: Confirm RED**

Run: `cd frontend && pnpm test --run src/lib/market-scope.test.ts`
Expected: old query-key signatures collide or fail type checking.

- [ ] **Step 3: Update keys, API calls and four core consumers**

Each page gets `market` from `useMarketScope`; no page owns a second market state. Invalidations use the market-specific key.

- [ ] **Step 4: Verify GREEN/build**

Run: `cd frontend && pnpm test --run src/lib/market-scope.test.ts src/lib/market-display.test.ts && pnpm build`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/queryKeys.ts frontend/src/lib/api.ts frontend/src/pages/Dashboard.tsx frontend/src/pages/Watchlist.tsx frontend/src/pages/Screener.tsx frontend/src/pages/Review.tsx frontend/src/lib/market-scope.test.ts
git commit -m "feat: propagate market through frontend queries"
```

### Task 4: ClickHouse 三市场日线物化

**Files:**
- Create: `backend/app/services/market_backfill.py`
- Create: `backend/tests/test_market_backfill.py`
- Modify: `backend/app/plugins/clickhouse/provider.py`
- Modify: `backend/app/services/kline_sync.py`
- Modify: `backend/app/api/data.py`

**Interfaces:**
- Produces: `backfill_market(repo, provider, market, start, end, on_progress=None) -> BackfillResult`
- Produces: `BackfillResult(market, symbols, rows, earliest_date, latest_date)`.

- [ ] **Step 1: Write failing market-batch test**

Use a fake provider returning CN/HK/US rows and assert `backfill_market(..., 'hk', ...)` writes only `.HK` symbols while preserving date partitions.

- [ ] **Step 2: Confirm RED**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_market_backfill.py -q`
Expected: module/function missing.

- [ ] **Step 3: Implement chunked ClickHouse reads and existing repository writes**

Chunk symbols by 500. Read `lb_daily_bars adjusted=1`, normalize with existing schema, persist via repository partition writer, and return exact counts.

- [ ] **Step 4: Extend data status with per-market coverage**

Response shape:

```json
{"markets":{"cn":{"symbols":5188},"hk":{"symbols":2817},"us":{"symbols":2030}}}
```

- [ ] **Step 5: Verify tests and live dry-run counts**

Run focused pytest, then on 10.28 run backfill first with a one-day date range and compare symbol suffixes to ClickHouse counts.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/market_backfill.py backend/tests/test_market_backfill.py backend/app/plugins/clickhouse/provider.py backend/app/services/kline_sync.py backend/app/api/data.py
git commit -m "feat: materialize three-market daily data"
```

### Task 5: 三市场 enriched 与看板聚合

**Files:**
- Modify: `backend/app/indicators/pipeline.py`
- Modify: `backend/app/services/market_overview_builder.py`
- Modify: `backend/app/market_rules.py`
- Create: `backend/tests/test_three_market_overview.py`
- Modify: `frontend/src/pages/Dashboard.tsx`

**Interfaces:**
- Enriched rows retain/infer `market` and never calculate CN price-limit fields for HK/US.
- `build_market_overview(..., market)` returns market-specific universal metrics and benchmark rows.

- [ ] **Step 1: Write failing HK/US overview tests**

Fixtures must contain one symbol per market. Assert HK overview excludes CN symbol, reports HKD, and contains no limit-ladder metric; US does the same with USD.

- [ ] **Step 2: Confirm RED**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_three_market_overview.py -q`
Expected: market argument unsupported or mixed-market rows.

- [ ] **Step 3: Implement per-market enriched and universal metrics**

Filter before all aggregates. For HK/US set limit-specific response fields to `null` and return `features.limit_rules=false`. Add benchmarks from MarketRule; missing benchmark data returns an empty list.

- [ ] **Step 4: Update Dashboard conditional UI**

CN renders existing limit/ladder cards. HK/US render 52-week high/low, amplitude and turnover cards and do not render CN-only labels.

- [ ] **Step 5: Verify tests/build**

Run backend focused tests and frontend production build.

- [ ] **Step 6: Commit**

```bash
git add backend/app/indicators/pipeline.py backend/app/services/market_overview_builder.py backend/app/market_rules.py backend/tests/test_three_market_overview.py frontend/src/pages/Dashboard.tsx
git commit -m "feat: add market-aware dashboard metrics"
```

### Task 6: 自选、策略和回测市场化

**Files:**
- Modify: `backend/app/api/watchlist.py`
- Modify: `backend/app/api/screener.py`
- Modify: `backend/app/api/backtest.py`
- Modify: `frontend/src/pages/Watchlist.tsx`
- Modify: `frontend/src/pages/Screener.tsx`
- Modify: `frontend/src/pages/backtest/StrategyBacktest.tsx`
- Create: `backend/tests/test_market_scoped_workflows.py`

**Interfaces:**
- Watchlist list may remain globally stored, but list/enriched/quotes responses are filtered by requested market.
- Screener and strategy cache identifiers include market.
- Backtest request defaults and result metadata include market/currency.

- [ ] **Step 1: Write failing workflow tests**

Assert watchlist HK returns only `.HK`; screener cache keys differ by market; backtest HK trade metadata uses HKD and the stored lot size.

- [ ] **Step 2: Confirm RED**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_market_scoped_workflows.py -q`
Expected: mixed symbols or missing market metadata.

- [ ] **Step 3: Implement backend and remove page-local market states**

All three frontend pages consume `useMarketScope`. Strategy/backtest payloads carry `market`; search is restricted to that market.

- [ ] **Step 4: Verify focused tests and frontend build**

Expected: PASS; no duplicate market switcher inside a page unless it is a multi-market result breakdown.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/watchlist.py backend/app/api/screener.py backend/app/api/backtest.py backend/tests/test_market_scoped_workflows.py frontend/src/pages/Watchlist.tsx frontend/src/pages/Screener.tsx frontend/src/pages/backtest/StrategyBacktest.tsx
git commit -m "feat: scope trading workflows by market"
```

### Task 7: 其余路径与 A 股专属能力边界

**Files:**
- Create: `frontend/src/components/MarketUnavailableState.tsx`
- Modify: `frontend/src/pages/StockAnalysis.tsx`
- Modify: `frontend/src/pages/Financials.tsx`
- Modify: `frontend/src/pages/Monitor.tsx`
- Modify: `frontend/src/pages/Review.tsx`
- Modify: `frontend/src/pages/Indices.tsx`
- Modify: `frontend/src/pages/ConceptAnalysis.tsx`
- Modify: `frontend/src/pages/IndustryAnalysis.tsx`
- Modify: `frontend/src/pages/LimitUpLadder.tsx`
- Modify: `backend/app/api/indices.py`
- Test: `frontend/src/lib/market-scope.test.ts`

**Interfaces:**
- `MarketUnavailableState` receives `market`, `feature`, `reason`.
- CN-only pages render no data table for HK/US.

- [ ] **Step 1: Add failing capability-matrix test**

Assert `limit-ladder` is supported only for CN and invalid market values normalize safely.

- [ ] **Step 2: Confirm RED**

Run frontend focused tests.

- [ ] **Step 3: Implement remaining consumers and explicit unavailable states**

Search pages restrict symbols by current market. Monitor/review/indices pass market. Concept/industry show only true current-market classification; limit ladder is explicitly CN-only.

- [ ] **Step 4: Verify build**

Run frontend tests and `pnpm build`.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/MarketUnavailableState.tsx frontend/src/pages backend/app/api/indices.py frontend/src/lib/market-scope.test.ts
git commit -m "feat: enforce market scope across remaining pages"
```

### Task 8: 全量回填、验证与 10.28 部署

**Files:**
- Modify: `docs/superpowers/specs/2026-07-18-global-market-scope-design.md` only if verification reveals a documented contract correction.

- [ ] **Step 1: Run complete local verification**

```powershell
cd backend
.venv\Scripts\python.exe -m pytest -q
cd ..\frontend
pnpm test --run
pnpm build
```

Expected: zero failures; only documented pre-existing warnings.

- [ ] **Step 2: Back up 10.28 data and current image**

Tag the current image as `rollback-before-market-scope-20260718` and create a timestamped copy of `kline_daily` and `kline_daily_enriched` metadata/partitions before replacement.

- [ ] **Step 3: Deploy code and run backfill per market**

Run CN, HK, US separately. Verify daily and enriched symbol counts by suffix and latest date before switching traffic.

- [ ] **Step 4: Run authenticated API smoke tests**

For overview, market snapshot, watchlist and screener, request cn/hk/us and assert every returned symbol belongs to the requested market.

- [ ] **Step 5: Run Playwright path-preservation tests**

From `/`, `/screener`, `/backtest`, `/indices` click A股/港股/美股. Assert pathname unchanged, `market` changes, visible data/currency changes, and CN-only labels never appear in HK/US.

- [ ] **Step 6: Inspect logs and record deployment**

Health must return `status=ok`; recent container logs must contain no `ERROR` or traceback. Commit any final verification documentation and report image/commit/rollback tag.
