# Tick Stock Panel — Contributing, AI Development & Review Guide

This document applies to the entire repository and is shared by contributors, AI coding agents, and PR reviewers. Its purpose is to put changes in the right module, keep data semantics, pluginization, and compatibility consistent, and reduce rework through reproducible verification.

`CONTRIBUTING.md` is the project's contribution and review standard. It does not replace the user-facing documentation in `README.md` or the domain documentation under `docs/`. Every contributor and AI coding agent must read this document before modifying code, committing, or reviewing a PR.

When doing secondary development, also follow [`docs/secondary-development.md`](docs/secondary-development.md). On the frontend, prefer real, existing controlled slots or registration entry points; on the backend, prefer small-grained strategy interfaces and dependency injection. When existing extension points cannot express a core behavior change, modifying the source directly is allowed, but the change must stay focused and must come with compatibility verification.

## 1. Core Principles

### 1.1 Understand Before Modifying

- First locate the real call chain, data source, cache layer, and existing tests. Do not guess the implementation from file names or UI symptoms.
- Clarify the user problem, inputs, outputs, asset type, timeframe, and compatibility boundary before changing anything.
- When multiple interpretations exist, state your assumptions. Ambiguities that change business semantics must be confirmed first.
- Prefer reusing existing interfaces, services, types, and components. Do not build a second, parallel implementation of the same logic.

### 1.2 Keep It Simple

- Implement only the current requirement. Do not add unrequested configuration, compatibility layers, or general-purpose frameworks.
- Do not abstract single-use logic prematurely. Reuse only when real duplication or a clear module boundary appears.
- If a change fits within an existing module, do not introduce cross-layer dependencies or new directories.
- Comments explain "why", not what the code does.

### 1.3 Minimal Scope

- Every change must be traceable to the current problem.
- Do not refactor, rename, reformat, or delete unrelated code along the way.
- Preserve uncommitted changes from users and other contributors. Do not overwrite or revert them.
- Only clean up unused imports, variables, files, and debug output produced by this change.

### 1.4 Verification Results Define "Done"

- When fixing a bug, prefer writing a test that reproduces the problem first, then fix until the test passes.
- New features must cover at least the happy path, key boundaries, and failure paths.
- "The code is written", "the page opens", or "GitHub has no red X" do not mean the work is done.
- If the required verification cannot be run, the PR must state why and what risk remains.

## 2. Project Architecture

### 2.1 Tech Stack

- Backend: Python 3.11+, FastAPI, Pydantic v2, Polars, DuckDB, PyArrow.
- The backtest boundary may use pandas; business data computation should prefer Polars. `vectorbt` is an optional dependency and must never break main flows when it is not installed.
- Frontend: React 18, TypeScript, Vite, TanStack Query, Tailwind CSS, ECharts, lightweight-charts.
- Package management and verification commands: the backend uses `uv`, the frontend uses `pnpm`.

### 2.2 Main Data Flow

```text
Data source or data plugin
  -> services synchronization, validation, and normalization
  -> DataStore / KlineRepository / Parquet
  -> indicators pipeline producing enriched data
  -> strategy / monitoring / backtest / analysis services
  -> FastAPI API / SSE
  -> frontend API types + TanStack Query
  -> pages and shared components
```

Before changing anything, check the upstream and downstream along this chain. It is forbidden to bypass normalization, repositories, and business services by reading a data source or local file directly from the page or API layer.

### 2.3 Module Boundaries

| Directory | Responsibility | Placement rules |
| --- | --- | --- |
| `backend/app/api/` | HTTP, SSE, parameter validation, and response mapping | Keep it thin. No heavy computation, full scans, or data-source-specific business logic |
| `backend/app/services/` | Synchronization, realtime quotes, notifications, and other business orchestration | Compose domain capabilities. Do not duplicate repository or provider implementations |
| `backend/app/tickflow/` | Data storage, repositories, capability detection, and TickFlow integration | Expose capabilities only through existing abstractions; upper layers must not depend on storage details |
| `backend/app/data_providers/` | Data source interfaces, normalized models, and custom data sources | Define the dataset contract first for any new data capability, then implement it in a provider |
| `backend/app/plugins/` | Plugin discovery, loading, and plugin protocol | Plugin failure must not break main flows when the plugin is not enabled |
| `backend/app/indicators/` | Indicator computation and the enriched pipeline | Keep it vectorized; be explicit about raw price, adjusted price, and output units |
| `backend/app/strategy/` | Strategy registration, execution, monitoring, and AI strategies | Built-in and custom strategies should share the execution contract as much as possible; do not create divergent semantics |
| `backend/app/backtest/` | Backtesting, optimization, walk-forward optimization, and workers | Strictly isolate signal generation from fill simulation to prevent look-ahead bias |
| `frontend/src/lib/api.ts` | Frontend API requests and type contracts | Backend contract changes must be reflected in types and callers |
| `frontend/src/lib/queryKeys.ts` | TanStack Query keys | Maintain query keys centrally; do not build parallel keys inside components |
| `frontend/src/lib/useSharedQueries.ts` and similar | Cross-page shared queries | Prefer shared caching for identical data; avoid repeated requests per card |
| `frontend/src/pages/` | Page-level orchestration | Push complex domain logic down into hooks, components, or backend services |
| `frontend/src/components/` | Reusable interactive components | Keep prop contracts stable; do not put page-specific state into shared components |
| `data/` | User runtime data | Not committed to Git. Never write local samples, account information, or absolute paths into a PR |

## 3. Data Contracts That Must Not Be Mixed

Errors in financial data often do not raise exceptions, but they produce results that look plausible but are wrong. For the fields below, correctness of semantics must be proven in variable names, boundary conversions, or tests.

### 3.1 Ratios and Percentages

| Field/scenario | Current semantics | Example |
| --- | --- | --- |
| `change_pct` at the custom realtime data source entry | Decimal ratio | `0.0366` means `3.66%` |
| `change_pct` inside enriched and stock monitoring | Usually decimal ratio | Confirm the call chain before threshold comparisons; do not infer from the field name |
| Change percentage in the index realtime display cache | Percentage semantics exist | Must be converted explicitly before use; never reuse directly in stock monitoring |
| `turnover_rate` at the realtime data source entry | Decimal ratio | `0.05` means `5%` |
| `turnover_rate` in enriched | Percentage value | `5` means `5%` |

Any new cross-boundary mapping must add unit tests. Heuristic conversions such as "multiply by 100 if the value is less than 1" are forbidden, because they mask real data errors.

The `bid_volumes` / `ask_volumes` of the five-level order book follow the existing sealed-order-volume semantics, in units of "lots" (手); the frontend multiplies by `100` to convert to shares when computing sealed-order amounts. Providers must complete unit conversion at the data boundary.

### 3.2 Prices and Adjustment

- The `open/high/low/close` in enriched data are forward-adjusted prices.
- `raw_close/raw_high/raw_low` are unadjusted raw prices.
- `limit_up_price` / `limit_down_price` are runtime columns for **limit judgment**: the exchange-effective price with a ∓0.005 tolerance (`raw_close >= limit_up_price` means limit-up; `null` on days without price limits or when the base price is missing). They are an engine capability, not a signal — signals that reference them (limit-up/limit-down/touch-and-break/limit-up break) are ordinary custom signal definitions; deleting the definition does not affect the judgment columns, and referencing parties degrade gracefully.
- Limit-up, limit-down, one-line boards, limit-up breaks, and exchange price limits must be determined from raw prices and the corresponding trading-day rules.
- Which price an indicator or return series uses must be consistent with existing indicator definitions. Do not mix prices within a single formula.

### 3.3 Dates, Trading Days, and Time Zones

- Windows, N-day lookbacks, and batch recomputation all follow actual trading days. Natural days must never be substituted directly.
- A-share trading sessions are handled uniformly in Beijing time. The server time zone must not become an implicit input to business logic.
- The `datetime` of minute bars is uniformly Beijing wall-clock time (naive, e.g. `09:35:00`); data source entries (`_normalize_minute` / `_try_custom_minute` in `kline_sync`) must enforce normalization. UTC semantics must never be stored or emitted.
- Daily bars, minute bars, and realtime snapshots must have an explicit trading-date attribution, especially around the lunch break, after close, and across-day restarts.
- Minute bars for stocks, ETFs, and indices are stored and routed separately. Never guess the asset type from the code format alone.

### 3.4 Historical Share Capital and Turnover Rate

- The turnover rate should prefer historical share capital whose announcement date is not later than the target trading day.
- Only when usable historical share capital is missing may a fallback to the latest dimension-table share capital be allowed.
- The current-day dimension table, the financial announcement date, and the reporting period are different concepts. A reporting period must never leak data that has not been announced yet.
- When modifying share-capital completion or enriched recomputation, both paths must be covered: "historical share capital available" and "fallback without historical share capital".

## 4. Data Source Plugin Requirements

Data sources are already pluginized. Any general feature must access data through provider capabilities and normalized datasets. TickFlow SDK calls must never be hardcoded into strategies, monitoring, backtests, APIs, or frontend flows.

- Use the existing `get_provider()`, `provider_has_dataset()`, and preferences-based routing capabilities.
- Supported datasets include but are not limited to `daily`, `adj_factor`, `minute`, `full_minute` (intraday full-market minute persistence), `realtime`, `depth5`, and `financial`. New datasets must first define a clear input/output contract.
- Providers are responsible for converting vendor fields, units, dates, and symbol formats into the internal standard format.
- Upper-layer services depend on standard fields and capability declarations, not on vendor response structures.
- Only features explicitly marked as TickFlow-exclusive may depend on TickFlow directly, and they must not affect other providers.
- When a provider lacks a capability, the system must clearly indicate it, skip the feature, or fail closed. Silently switching to wrong data or wrong semantics is forbidden.
- Plugin load failures, missing fields, and empty data must have isolation tests. They must not cause application startup failure or make other data sources unavailable.
- New data source adapters must update the corresponding contract in `docs/custom-data-source.md` or `docs/plugin-development.md`.

### Capability Routing Matrix Contract

The capability matrix (the registry in `backend/app/data_providers/capabilities.py` plus `/api/settings/capability-matrix`) is the single source of truth for capability routing. The following invariants apply:

- The registry centrally declares each capability's display metadata, routing preference fields, and TickFlow tier requirements. The frontend must not hardcode the capability list. Add new capabilities following the existing pattern: registry + preferences getter + capability-matrix injection + matrix test.
- All page-level capability gating must rely on the matrix's `usable` value (whether the active source can genuinely provide the capability right now), not on the TickFlow plan perspective. Missing-capability prompts must guide users to data source configuration.
- Capability layer neutrality: general UI (sidebar badges, capability routing cards, per-page gating messages) must not contain TickFlow tier or subscription vocabulary. Tier information appears only in the TickFlow-exclusive detail card. Provider names may appear as routing facts.
- Each capability is routed independently. Follow-on/derived special values are forbidden (`same_as_daily` has been removed). Legacy invalid preference values self-heal via preference getter fallback instead of migration.
- Boundary notes: intraday monitoring is backed by the minute capability (`intraday_monitor_support`); there is no separate intraday capability. Both the `full_minute` (full minute data) and `depth5` datasets are open for plugin declaration. `depth5` is routed independently and does not fall back across sources on failure.
- Realtime indices are a product-level fixed contract and do not go through the routing matrix: the display layer (sidebar index strip, market overview) is fixed to the four core indices (`backend/app/services/index_const.py` is the single source of truth: SSE Composite / SZSE Component / ChiNext / STAR Composite). Backend consumers and the frontend Layout reference the same definition without creating copies. The index page is retained, but its symbols are fixed to the four core indices (no all-index search/browsing; `/api/index/list` and `/api/index/search` have been removed). Sidebar index multi-select configuration has been removed, along with the related preferences (`realtime_index_symbols`/`sidebar_index_symbols`/`indices_nav_pinned`/`realtime_pull_index`/`realtime_index_mode`). Monitoring rule index symbols are unrestricted — quote_service merges the four core indices with indices from enabled rules into an explicit pull list.
- Custom-source index supplement protocol: A-share snapshots generally do not include indices (fuyao's tested behavior has no indices; indices live on a separate endpoint). Providers may implement an optional method `get_realtime_indices(symbols) -> list[dict] | None` (record structure identical to `realtime`); quote_service duck-types this call in the custom-source branch to supplement. `None` means the request failed and the last valid index cache is kept; `[]` means the request succeeded but returned no data; if unimplemented, the source index cache stays empty and local daily K-lines take over as fallback. fuyao index snapshots have coupled-failure semantics — a request mixed with unknown codes fails entirely, so plugins must filter unsupported suffixes first (e.g. `.BJ`).

## 5. Domain-Specific Requirements

### 5.1 Strategy

- Built-in and custom strategies must follow a unified contract for base parameters, strategy parameters, filters, scoring, and result structure.
- After changing parameters, verify all of the following: persisted content, runtime instance, strategy result cache, monitoring instance, and frontend queries.
- The form state for creating a strategy must be initialized every time it opens. Edit-state data must not leak into create-state, and the previous strategy ID must not remain after closing or after a successful creation.
- Strategy IDs are stable identifiers. Renaming, copying, importing, and deleting must each handle ID conflicts separately. Conflicts must never be resolved by silently overwriting files.
- AI-generated strategy META must go through structured parsing and normalization. Preserve the ability to read legacy formats for compatibility, but new output must use the current specification.
- Scoring fields must have a deterministic data source or a temporary computation path. When inputs are missing, return an explicit "not computable" state. Never disguise a missing value as a zero score.
- Deleting a strategy touches user files. The target scope must be validated and fail closed. Path traversal and deletion of files outside the strategy directory must be impossible.

### 5.2 Monitoring and Notifications

- Monitoring computation should reuse normalized realtime quotes and unified rules. Do not compute separately for pages, voice, Feishu, and WeCom.
- Change percentage, limit-up/limit-down, and average-price crossovers must have explicit data semantics, trading-session rules, asset types, and threshold directions.
- Minute signals are enabled only when the provider declares minute data capability and the symbol is in the subscription pool. When permissions are missing, give an explicit status.
- The quote callback thread must not run long-running tasks such as full historical recomputation, synchronous webhooks, or slow disk scans.
- Notification text must use Chinese names that users can understand. Do not leak internal enum names, module names, or debug fields.
- Deduplication, cooldown, and recovery state for the same event must be testable, and post-restart behavior must be consistent with the persistence design.

### 5.3 Backtest, Optimization, and Walk-Forward Optimization

- Signal generation and fill simulation are two phases. The distinction between signal time, fillable time, and the price used must be explicit.
- Look-ahead bias is strictly forbidden: on a trading day, only data that was already public and available at that time may be used.
- Stock rules must preserve T+1, commissions, slippage, and non-fillable limit-up/limit-down constraints. New fill options must not bypass these rules.
- A daily backtest must not claim "filled at the moment the signal triggered". Minute-precision fills may only be enabled when minute data fully covers the target window.
- Exit options such as "same-day close", "next-day open", and "next-minute open" must each be tested for time alignment and missing boundary data.
- Optimization and walk-forward optimization share a matrix. The matrix must cover each fold's full window, and cache keys must include every input that changes matrix results.
- When modifying strategy, scoring, or sorting logic, verify simultaneously whether historical strategy results, backtest trade records, and UI display use the same candidate set and sort direction.

## 6. Caching, Concurrency, and Performance

The project has repository caches, enriched history caches, realtime aggregation, screening caches, strategy result caches, backtest matrix caches, mtime caches, SSE, and frontend TanStack Query. When modifying read/write paths, you must enumerate every affected cache layer.

### 6.1 Cache Consistency

- After writing data, check all of the following: persisted files, in-memory caches, generation/version, SSE events, and frontend query invalidation.
- It is not enough to guarantee "the file was written" while still returning stale in-memory objects.
- For multi-step refreshes, prefer building a new snapshot and replacing it atomically, so the UI never briefly shows empty lists or zero results during a refresh.
- Cache keys must cover asset type, timeframe, date, strategy parameters, and any other dimension that changes results.
- Invalidation scope should be precise. Never clear all history caches unconditionally just to fix a single data point.

### 6.2 Concurrency Safety

- When APIs, background workers, and realtime quote threads share dictionaries or caches, use locks, immutable snapshots, or atomic replacement.
- Do not perform network requests, full disk scans, or heavy recomputation while holding a lock.
- Cancellation, timeout, and application shutdown paths must release resources. Background threads must not prevent the process from exiting.

### 6.3 Performance Constraints

- List pages must avoid N+1 APIs, per-card independent requests, and repeated full strategy computations. Prefer batch APIs and shared queries.
- Polars computation should prefer expressions and batching. Avoid row-by-row Python loops.
- Realtime hot paths must not grow linearly with historical data volume. When a history window is needed, read only the necessary range.
- When there are no rules, no subscribers, or the provider lacks the capability, do not perform the corresponding realtime computation.
- A PR claiming a "performance improvement" must provide before/after data under the same data scale and the same environment, including at least latency or request counts.

## 7. Frontend Requirements

- Reuse the request wrappers and types in `frontend/src/lib/api.ts`. Components must not scatter request URLs directly.
- Use centrally defined TanStack Query keys. Parameters that affect results — asset type, date, timeframe, etc. — must be part of the query key.
- After switching stock, ETF, date, or strategy, cached results from the previous context must not be treated as current results.
- Realtime refresh must keep the last valid data until new data is ready, avoiding flicker where columns, counts, and lists disappear.
- Opening and closing dialogs must correctly reset temporary form state. Create state and edit state must be separated.
- Cross-page behaviors such as shared tabs, stock detail, and constituent stocks must use shared components. Do not duplicate interactions across multiple pages.
- New interactions must cover loading, empty data, error, disabled, and no-permission states.
- Changes should be checked at common desktop and narrow-screen sizes for text truncation, overlap, scroll regions, and dialog operability.
- Do not use frontend patches to mask backend unit or contract errors. Contract issues must be fixed at the data boundary.

## 8. Security, Compatibility, and User Data

- Never commit `data/`, secrets, tokens, webhooks, user strategies, logs, databases, Parquet samples, or machine absolute paths.
- Logs and exception responses must not contain authorization information, full configuration, or sensitive user data.
- File deletion, overwriting, migration, and batch recomputation are high-risk operations: restrict the target directory, validate paths, provide clear failure reasons, and stay fail-closed.
- Do not execute SQL, shell, or dynamic Python through unsafe string concatenation.
- Changes to configuration, strategy JSON, Parquet schemas, and API fields require backward compatibility by default. New fields should provide defaults, and readers should tolerate missing fields in historical data.
- When existing behavior must change, the PR must list the migration approach, degradation behavior, and scope of impact.
- Path, encoding, recycle-bin, and permission behavior differ across Windows, macOS, Linux, and Docker. For anything involving the filesystem, review the cross-platform failure paths at minimum.

## 9. Verification Matrix

Execute the minimum sufficient verification based on the scope of the change. The following are minimum requirements, not upper limits.

| Change type | Minimum verification |
| --- | --- |
| Backend pure functions, rules, or bug fixes | Targeted `pytest` for the affected code, including reproduction and boundary cases |
| API contracts | Service tests + API tests; check success, no-data, and error responses |
| Data sources or plugins | Provider contract tests, missing capability, empty data, field/unit normalization tests |
| Indicators, adjustment, turnover rate, limit-up/limit-down | Numeric assertions on fixed samples, covering historical boundaries and degradation paths |
| Strategies or monitoring | Parameter changes, cache invalidation, asset switching, restart/concurrency paths |
| Backtesting | Signal day vs. fill day, T+1, fees, slippage, non-fillable cases, data gaps |
| Caching or performance | Hit/invalidation tests, concurrent snapshot tests; before/after benchmarks when necessary |
| Frontend components or pages | `pnpm build`, plus manual checks of loading, empty, error, switching, and realtime refresh states |
| Frontend and backend contract changes together | Targeted backend tests + frontend build + corresponding page integration check |
| Files, Docker, or cross-platform logic | Normal path, permission/capability-missing, and target-unavailable failure paths |

Common commands:

```bash
cd backend
uv run pytest tests/path/to/test_x.py -q
uv run ruff check app/path.py tests/path.py

cd frontend
pnpm build

git diff --check
```

- For the backend, run at least the tests of the affected module, not just a single newly added test.
- New files and this change must not introduce Ruff warnings. Do not reformat the whole repository in a feature PR just to clean up historical warnings.
- Any frontend TypeScript, component, style, or API type change requires at least one `pnpm build`.
- Before committing, run `git diff --check` and confirm the final diff contains no debug code or unrelated files.

## 10. PR Submission Requirements

A reviewable PR should focus on one problem. Features, unrelated refactoring, bulk formatting, and dependency upgrades must not be mixed together.

The PR description must include:

1. **Problem and reproduction**: previous behavior, reproduction conditions, and affected users.
2. **Root cause**: specific to the call chain, data contract, or state lifecycle. "Fixed a bug" is not enough.
3. **Solution**: changed modules, key trade-offs, and why pluginization or existing semantics were not broken.
4. **Compatibility**: impact on historical configuration, strategies, caches, Parquet, APIs, data sources, and different asset types.
5. **Performance**: whether it enters realtime or list hot paths; include before/after data when claiming performance gains.
6. **Verification results**: list the commands actually executed and their results. Do not write tests that are only planned.
7. **UI evidence**: for visible changes, provide before/after screenshots or short videos, covering relevant light/dark or narrow-screen scenarios.
8. **Risks and rollback**: remaining risks, degradation behavior, and a feasible rollback approach.

Author self-check before submitting:

- [ ] The change only addresses the problem described in the PR, with no unrelated cleanup.
- [ ] The complete call chain and all affected caches have been checked.
- [ ] Units, adjustment, trading days, time zones, and asset types have been verified.
- [ ] The provider abstraction is not bypassed, and behavior is explicit when a capability is missing.
- [ ] Historical configuration and old data can still be read, or a migration plan is documented.
- [ ] There is no obvious performance regression in realtime threads, list requests, or batch processing.
- [ ] Error paths are fail-closed and never silently return wrong financial results.
- [ ] Tests fail on the unfixed code and pass after the fix.
- [ ] Applicable tests, builds, Ruff, and `git diff --check` have been executed.
- [ ] The final diff contains no sensitive data, local paths, debug output, or generated files.

## 11. PR Review Process

Both author self-review and maintainer review follow this order, to avoid looking only at page effects or newly added code:

1. **Confirm the problem**: reproduce the old behavior based on the description and tests, and confirm the PR solves the same problem.
2. **Check boundaries**: confirm the code sits in the correct module, with no reverse dependencies, parallel implementations, or heavy recomputation in the API layer.
3. **Verify data contracts**: check units, adjustment, dates, time zones, asset types, nulls, and sort direction.
4. **Verify pluginization**: check whether the change depends on provider capabilities and whether non-TickFlow data sources work or degrade explicitly.
5. **Trace state changes**: verify persistence, caches, generation, SSE, and frontend query invalidation from the write path onward.
6. **Check compatibility**: validate with historical configuration, old strategy JSON, old Parquet schemas, and data with missing fields.
7. **Check performance and concurrency**: determine whether it enters realtime, list, or full-scan hot paths, and check locks and atomic replacement.
8. **Check failure paths**: network failures, missing permissions, empty data, missing capabilities, and partial writes must not produce incorrect states.
9. **Evaluate test quality**: tests must assert business results and cover negative cases and boundaries. Asserting only that the API returns 200 is not enough.
10. **Check the final diff**: confirm necessity file by file, and exclude unrelated formatting, sensitive information, and temporary files.
11. **Give a conclusion**: explicitly state "ready to merge", "merge after changes", or "not recommended to merge", distinguishing blockers from suggestions.

Severity levels:

| Level | Definition | Merge requirement |
| --- | --- | --- |
| `P0` | Data corruption, security vulnerabilities, or potential for seriously incorrect trading results | Must not merge; fix and re-review fully |
| `P1` | Core flow unavailable, incorrect financial semantics, look-ahead bias, or broad regressions | Must not merge; fix and add regression tests |
| `P2` | Clear functional errors, compatibility issues, or obvious performance regressions | Merge after changes |
| `P3` | Maintainability, local UX, documentation, or test reinforcement suggestions | May not block, but the follow-up plan should be recorded |

Review comments should include the file and line number, trigger conditions, actual impact, and the suggested direction of the fix. Do not write only "this may be a problem" or give conclusions without evidence.

### 11.1 Review After Follow-Up Commits

- First verify item by item whether every blocker from the previous round is truly resolved in code and tests. Do not rely on the submitter's reply that "it's fixed".
- Reading new commits helps understand the intent, but the final pass must re-review the complete diff of the PR against the target branch, not only the last commit.
- Re-run the affected tests and check whether the fix introduced new issues in units, caching, compatibility, performance, or pluginization.
- Explicitly mark resolved issues as verified; for issues that remain, cite the current latest line numbers and explain what is missing.
- Only when all original blockers are resolved and no new blockers are introduced may the conclusion be changed to "ready to merge".

### 11.2 Recommended Review Output Format

```text
Conclusion: ready to merge / merge after changes / not recommended to merge

Blockers:
- [P1/P2] file:line - trigger conditions, actual impact, required changes

Non-blocking suggestions:
- [P3] file:line - suggestion and rationale

Verified:
- Tests or build commands executed and their results
- Item-by-item re-check results for the previous round's issues

Remaining risks:
- Platforms, data sources, data scales, or scenarios not covered
```

When no problems are found, explicitly write "no blockers found" and state what was not verified. Do not substitute a conclusion with "looks fine".

## 12. Must-Not-Merge Conditions

A PR must not be merged if any of the following applies:

- The root cause cannot be explained, and the change merely adds delays, retries, forced refreshes, or swallows exceptions to mask symptoms.
- Percentages, adjusted prices, natural days, and trading days are mixed, or financial computation semantics cannot be proven.
- A general feature is bound directly to a single data source without capability detection or explicit degradation.
- A known cache inconsistency remains after writes, requiring a user restart or manual data cleanup for correct display.
- The backtest introduces future data, bypasses fill constraints, or uses different strategy logic for historical results and trade records.
- The realtime thread gains blocking network requests, full historical computation, or unbounded tasks.
- Historical strategies, configuration, data files, or APIs are broken without a compatibility or migration plan.
- File operations could delete outside the intended scope, overwrite user data by default, or continue executing after failure.
- Tests do not reproduce the problem, cover only happy paths, or the necessary tests/frontend build fail.
- The PR contains secrets, user data, local data directories, machine absolute paths, or a large amount of unrelated changes.
- `P0`, `P1`, or `P2` issues raised in review have not been addressed or verified.

## 13. Additional Rules for AI Coding Agents

- Before starting, read this document, the target module, adjacent implementations, callers, and related tests.
- Write a brief plan with each step and how it will be verified before modifying. For simple changes, one sentence is enough.
- Do not present guesses as facts. Business semantics that cannot be confirmed from code, tests, or documentation must be explicitly asked about.
- Do not skip reproduction, root cause analysis, or regression verification just because the user asked to "just fix it".
- Do not automatically commit, push, merge, delete files, migrate databases, or bulk-overwrite data. Obtain explicit confirmation according to the current session's permission rules when needed.
- Do not overwrite existing changes in the working tree. When the same file is being modified concurrently, read the latest content first and preserve both sides' logic.
- Do not fabricate test results, performance data, GitHub status, or production behavior. Verification that was not run must be clearly marked as such.
- When reviewing a PR, report issues first, ordered by severity and citing file line numbers. Give the merge recommendation and a brief summary at the end.
- After completion, check `git diff` and `git status`, and accurately state what was changed, what was verified, and what risks remain.

## 14. Contribution Licensing and Provenance

This project uses a combination of **DCO (Developer Certificate of Origin) + contribution relicense grant**: at commit time you declare provenance and grant the maintainer the relicensing rights needed to change the license.

### 14.1 Every Commit Must Be DCO-Signed

All commits must carry a `Signed-off-by` trailer declaring that you created the contribution, or have the right to submit it under the project's terms, and that you agree to the contribution licensing terms in this section:

```bash
git commit -s -m "fix(scope): description"
```

`-s` automatically appends a line (the name and email must match your Git configuration):

```
Signed-off-by: Your Name <you@example.com>
```

The full DCO statement is at <https://developercertificate.org/>. PRs without the sign-off will not be merged; the CI DCO check will block them.

### 14.2 Contribution Relicense Grant

By submitting a contribution, you grant the project maintainer a **perpetual, irrevocable, worldwide, non-exclusive, royalty-free** copyright license permitting the maintainer to:

- distribute your contribution under the project's current license (see `LICENSE`);
- in the future, switch the project in whole or in part to another open-source license (including copyleft licenses such as AGPL-3.0);
- relicense your contribution for the purpose of commercial dual licensing.

You retain the copyright and attribution rights to your own contribution. This clause does not change your attribution rights and does not affect your independent use of your own contribution.

> Why this is needed: the project plans to adopt AGPL-3.0 in a future version and may offer a commercial license (AGPL + commercial dual licensing). Without an explicit relicense grant, the maintainer could only distribute third-party contributions under the existing license and could not complete the license switch or dual licensing. DCO alone is insufficient to support this right, so this clause applies together with DCO.

### 14.3 Boundary for Historical Contributions

This section's terms **apply only to commits made after the effective date**:

- Contributions already merged before that date remain licensed under the MIT license in effect at the time of their submission. **Historical versions remain MIT permanently** and will not be retroactively changed;
- The maintainer will separately contact major historical contributors to sign the relicense grant. Parts that are not re-signed continue to be governed by MIT and do not affect the project's continued distribution of existing code under MIT.

### 14.4 Cases Requiring a Formal CLA

The following cases require a written CLA (individual ICLA or corporate CCLA); a DCO trailer alone is not enough:

- The contribution is made within the scope of your employment duties (the company must sign a CCLA);
- The contribution involves patents held by you or your employer;
- A single contribution is large in scope or has a substantial impact on the project architecture.

When needed, the maintainer will raise this in the PR and provide the text. Signed records are archived separately outside the repository.

### 14.5 Pre-Commit Self-Check

- [ ] The commit is DCO-signed with `git commit -s`;
- [ ] The `Signed-off-by` name and email are real and match the Git commit identity;
- [ ] If the contribution is employed-work or involves patents, this is stated in the PR.

The measure of following this guide is not how much documentation is written, but whether: change scope is smaller, financial semantics have evidence, plugin boundaries are not bypassed, tests genuinely reproduce problems, review conclusions are actionable, and the same issue does not repeatedly cause rework.

---

# 中文版（原文）

# Tick Stock Panel 贡献、AI 开发与复审指南

本文档适用于整个仓库，供贡献者、AI 编码代理和 PR 审查者共同使用。目标是让改动落在正确的模块，保持数据口径、插件化和兼容性一致，并通过可复现的验证减少返工。

`CONTRIBUTING.md` 是项目贡献与审查规范，不替代 `README.md`、`docs/` 中的用户文档和领域文档。所有贡献者和 AI 编码代理在修改代码、提交或审查 PR 前都应阅读本文档。

涉及代码二次开发时，同时遵循 [`docs/secondary-development.md`](docs/secondary-development.md)。前端优先使用真实存在的受控插槽或注册入口，后端优先使用小粒度策略接口和依赖注入；现有扩展点无法表达核心行为变化时允许直接修改源码，但必须保持改动聚焦并补足兼容性验证。

## 1. 基本原则

### 1.1 修改前先理解

- 先定位真实调用链、数据来源、缓存层和现有测试，不根据文件名或界面现象猜测实现。
- 明确用户问题、输入、输出、资产类型、时间周期和兼容边界后再修改。
- 存在多种解释时必须说明假设；会改变业务口径的歧义必须先确认。
- 优先复用现有接口、服务、类型和组件，不平行实现第二套逻辑。

### 1.2 保持简单

- 只实现当前需求，不增加未被要求的配置、兼容层或通用框架。
- 单次使用的简单逻辑不提前抽象；出现真实重复或明确模块边界时再复用。
- 能在现有模块内完成的改动，不新增跨层依赖或新目录。
- 注释解释“为什么”，不复述代码行为。

### 1.3 最小范围修改

- 每一处变更都应能追溯到当前问题。
- 不顺手重构、重命名、格式化或删除无关代码。
- 保留用户和其他贡献者尚未提交的改动，不覆盖、不回滚。
- 只清理由本次修改产生的无用导入、变量、文件和调试输出。

### 1.4 以验证结果作为完成标准

- 修复 bug 时，优先编写能复现问题的测试，再修到测试通过。
- 新功能至少覆盖正常路径、关键边界和失败路径。
- “代码已写完”“页面能打开”或“GitHub 没有红灯”都不代表完成。
- 无法执行必要验证时，必须在 PR 中说明原因和剩余风险。

## 2. 项目架构

### 2.1 技术栈

- 后端：Python 3.11+、FastAPI、Pydantic v2、Polars、DuckDB、PyArrow。
- 回测边界允许使用 pandas；业务数据计算优先使用 Polars。`vectorbt` 是可选依赖，不得让未安装它的主流程失效。
- 前端：React 18、TypeScript、Vite、TanStack Query、Tailwind CSS、ECharts、lightweight-charts。
- 包管理和验证命令：后端使用 `uv`，前端使用 `pnpm`。

### 2.2 主数据流

```text
数据源或数据插件
  -> services 同步、校验与标准化
  -> DataStore / KlineRepository / Parquet
  -> indicators pipeline 生成 enriched 数据
  -> 策略 / 监控 / 回测 / 分析服务
  -> FastAPI API / SSE
  -> frontend API 类型 + TanStack Query
  -> 页面和共享组件
```

改动前应沿这条链路检查上下游。禁止通过页面或 API 层绕过标准化、仓库、业务服务而直接读取某个数据源或本地文件。

### 2.3 模块边界

| 目录 | 职责 | 放置要求 |
| --- | --- | --- |
| `backend/app/api/` | HTTP、SSE、参数校验和响应映射 | 保持薄层，不放重计算、全量扫描或数据源专属业务 |
| `backend/app/services/` | 同步、实时行情、通知等业务编排 | 组合领域能力，不复制仓库或 provider 的实现 |
| `backend/app/tickflow/` | 数据存储、仓库、能力检测和 TickFlow 接入 | 对外通过既有抽象暴露能力，避免上层依赖存储细节 |
| `backend/app/data_providers/` | 数据源接口、标准化模型和自定义数据源 | 新数据能力先定义数据集契约，再由 provider 实现 |
| `backend/app/plugins/` | 插件发现、加载和插件协议 | 插件失败不得破坏未启用插件的主流程 |
| `backend/app/indicators/` | 指标计算和 enriched 流水线 | 保持向量化，明确原始价、复权价和输出单位 |
| `backend/app/strategy/` | 策略注册、执行、监控和 AI 策略 | 内置与自定义策略尽量共享执行契约，不能各自形成不同口径 |
| `backend/app/backtest/` | 回测、优化、步进优化和 worker | 严格隔离信号生成与成交模拟，防止未来函数 |
| `frontend/src/lib/api.ts` | 前端 API 请求和类型契约 | 后端契约变化必须同步更新类型与调用方 |
| `frontend/src/lib/queryKeys.ts` | TanStack Query 查询键 | 查询键集中维护，不在组件内拼出平行键 |
| `frontend/src/lib/useSharedQueries.ts` 等 | 跨页面共享查询 | 相同数据优先共享缓存，避免每张卡片重复请求 |
| `frontend/src/pages/` | 页面级编排 | 复杂领域逻辑下沉到 hook、组件或后端服务 |
| `frontend/src/components/` | 可复用交互组件 | 保持 props 契约稳定，不把页面专属状态塞进公共组件 |
| `data/` | 用户运行时数据 | 不提交到 Git，不把本地样本、账号信息或绝对路径写入 PR |

## 3. 不可混用的数据契约

金融数据错误经常不会报异常，但会生成看似合理的错误结果。涉及下列字段时，必须在变量名、边界转换或测试中证明口径正确。

### 3.1 比例和百分比

| 字段/场景 | 当前口径 | 示例 |
| --- | --- | --- |
| 自定义实时数据源入口的 `change_pct` | 小数制 | `0.0366` 表示 `3.66%` |
| enriched 和股票监控内部的 `change_pct` | 通常为小数制 | 阈值比较前确认调用链，不凭字段名推断 |
| 指数实时展示缓存的涨跌幅 | 存在百分数口径 | 使用前必须显式转换，不得直接复用到股票监控 |
| 实时数据源入口的 `turnover_rate` | 小数制 | `0.05` 表示 `5%` |
| enriched 的 `turnover_rate` | 百分数值 | `5` 表示 `5%` |

新增跨边界映射时必须增加单位测试。禁止用“数值小于 1 就乘 100”一类启发式转换，这会掩盖真实数据错误。

五档盘口的 `bid_volumes` / `ask_volumes` 沿用现有封单量口径,单位为“手”;前端计算
封单额时再乘 `100` 换算为股。provider 必须在数据边界完成单位转换。

### 3.2 价格与复权

- enriched 的 `open/high/low/close` 为前复权价格。
- `raw_close/raw_high/raw_low` 为不复权原始价格。
- `limit_up_price` / `limit_down_price` 为涨跌停**判定价**运行时列:交易所生效价 ∓0.005 容差(`raw_close >= limit_up_price` 即涨停;无涨跌幅限制日或基准价缺失为 null)。它们是引擎能力而非信号——引用它们的信号(涨停/跌停/翘板/炸板)是普通自定义信号定义,删除定义后判定价列不受影响,引用方优雅降级。
- 涨停、跌停、一字板、炸板和交易所价格限制必须基于原始价及对应交易日规则判断。
- 技术指标和收益序列使用哪种价格，必须与现有指标定义一致，不得在同一公式中混用。

### 3.3 日期、交易日和时区

- 窗口、前 N 日和批次回算均按实际交易日，不得用自然日直接替代。
- A 股交易时段统一按北京时间处理；服务器时区不能成为业务逻辑的隐式输入。
- 分钟 K 的 `datetime` 统一为北京时间墙钟（naive，如 `09:35:00`）；数据源入口（`kline_sync` 的 `_normalize_minute` / `_try_custom_minute`）强制归一，禁止 UTC 口径入库或下发。
- 日线、分钟线和实时快照必须明确交易日期归属，尤其注意午休、收盘后和跨日重启。
- 分钟 K 的股票、ETF、指数分开存储和路由，不得仅凭代码格式猜测资产类型。

### 3.4 历史股本与换手率

- 历史换手率优先使用公告日不晚于目标交易日的历史股本。
- 缺少可用历史股本时，才允许降级到最新维表股本。
- 当日维表、财务公告日和报表期是不同概念，不能用报表期提前泄露尚未公告的数据。
- 修改股本补全或 enriched 重算时，必须覆盖“有历史股本”和“无历史股本降级”两条路径。

## 4. 数据源插件化要求

数据源已经插件化。任何通用功能都必须通过 provider 能力和标准化数据集访问数据，不能把 TickFlow SDK 调用硬编码到策略、监控、回测、API 或前端流程中。

- 使用现有的 `get_provider()`、`provider_has_dataset()` 和 preferences 路由能力。
- 支持的数据集包括但不限于 `daily`、`adj_factor`、`minute`、`full_minute`（盘中全市场分钟落盘）、`realtime`、`depth5`、`financial`；新增数据集应先定义清晰的输入输出契约。
- provider 负责把供应商字段、单位、日期和代码格式转换为内部标准格式。
- 上层服务依赖标准字段和能力声明，不依赖供应商响应结构。
- 只有明确标注为 TickFlow 专属的功能才可以直接依赖 TickFlow，并且不得影响其他 provider。
- provider 缺少某项能力时，应明确提示、跳过该功能或 fail-closed；禁止静默换用错误数据或错误口径。
- 插件加载失败、字段缺失和空数据必须有隔离测试，不能导致应用启动失败或其他数据源不可用。
- 新增数据源适配应同步更新 `docs/custom-data-source.md` 或 `docs/plugin-development.md` 中对应契约。

### 能力路由矩阵契约

能力矩阵（`backend/app/data_providers/capabilities.py` 注册表 + `/api/settings/capability-matrix`）是能力路由的单一权威，遵循以下不变量：

- 注册表集中声明每个能力的展示元数据、路由偏好字段与 TickFlow 档位要求；前端不硬编码能力清单。新增能力按既有模式扩展：注册表 + preferences getter + capability-matrix 注入 + 矩阵测试。
- 各页面能力门控统一以矩阵的 `usable` 为准（生效源当前能否真正提供该能力），不是 TickFlow 套餐视角；缺能力提示统一引导到数据源配置。
- 能力层中立：通用界面（侧栏徽章、能力路由卡、各页门控提示）不得出现 TickFlow 档位/订阅词汇；档位信息只在 TickFlow 专属详情卡展示。provider 名称作为路由事实可以出现。
- 每个能力独立路由，禁止跟随/派生特殊值（`same_as_daily` 已下线）；存量非法偏好值由 preferences getter 回退默认自愈，不做迁移。
- 边界注记：分时监控由分钟能力兜底（`intraday_monitor_support`），不单设分时能力；`full_minute`（全量分钟）和 `depth5` 数据集均已开放插件声明。`depth5` 独立路由且失败时不跨源回退。
- 实时指数为产品级固定契约，不走路由矩阵：展示层（侧栏指数条、市场总览）固定核心四只（`backend/app/services/index_const.py` 单一权威：上证/深成/创业板/科创综指），后端各消费方与前端 Layout 引用同一份定义不建副本；指数页保留但标的固定为核心四只（无全指数搜索/浏览，`/api/index/list`、`/api/index/search` 已下线）；侧栏指数多选配置已下线，相关偏好（`realtime_index_symbols`/`sidebar_index_symbols`/`indices_nav_pinned`/`realtime_pull_index`/`realtime_index_mode`）已删除。监控规则的指数标的不受限——quote_service 把核心四只 + 启用规则的指数并入显式拉取。
- 自定义源指数补充协议：A 股快照普遍不含指数（fuyao 实测无指数，指数在其独立端点）。provider 可实现可选方法 `get_realtime_indices(symbols) -> list[dict] | None`（record 结构与 realtime 一致），quote_service 在自定义源分支鸭子类型调用补拉；`None` 表示请求失败，保留上轮有效指数缓存，`[]` 表示成功但无数据；未实现的源指数缓存为空，由本地日K兜底接管。fuyao 指数快照有连坐语义——请求混入未知代码整批失败，插件侧必须先行过滤不支持的后缀（如 `.BJ`）。

## 5. 领域专项要求

### 5.1 策略

- 内置策略和自定义策略的基础参数、策略参数、过滤、评分和结果结构应走统一契约。
- 参数修改后要检查：持久化内容、运行时实例、策略结果缓存、监控实例和前端查询是否全部更新。
- 创建策略的表单状态必须在每次打开时初始化；编辑态数据不得泄漏到新建态，关闭或创建成功后不能残留上一个策略 ID。
- 策略 ID 是稳定标识。重命名、复制、导入和删除必须分别处理 ID 冲突，不能靠覆盖文件解决。
- AI 生成策略的 META 必须经过结构化解析和规范化；兼容历史格式时保留旧格式读取能力，但新输出使用当前规范。
- 评分字段必须有确定的数据来源或临时计算路径。缺少输入时应返回明确的不可计算状态，不能把空值伪装成零分。
- 删除策略涉及用户文件，必须校验目标范围并 fail-closed；不得允许路径穿越或删除策略目录之外的文件。

### 5.2 监控与通知

- 监控计算应复用标准化实时行情和统一规则，不为页面、语音、飞书、企业微信分别计算。
- 涨跌幅、涨跌停和均价穿越必须明确数据口径、交易时段、资产类型和阈值方向。
- 分钟信号只有在 provider 声明分钟数据能力且标的进入订阅池后才启用；无权限时给出明确状态。
- 行情回调线程中不得执行全量历史重算、同步 Webhook、慢磁盘扫描等长耗时任务。
- 通知文案使用用户可理解的中文名称，不泄漏内部枚举名、模块名或调试字段。
- 同一事件的去重、冷却和恢复状态必须可测试，重启后的行为要与持久化设计一致。

### 5.3 回测、优化与步进优化

- 信号生成和成交模拟是两个阶段。必须明确区分信号时间、可成交时间和使用的价格。
- 严禁未来函数：交易日只能使用当时已经公开且可获得的数据。
- 股票规则需保持 T+1、手续费、滑点、涨跌停不可成交等约束；新成交选项不得绕过这些规则。
- 日线回测不能声称“信号触发瞬间成交”。分钟精确成交只有在分钟数据完整覆盖目标窗口时才能启用。
- 卖出信号的“当日收盘”“次日开盘”“次分钟开盘”等选项必须分别测试时间对齐和边界数据缺失。
- 优化和步进优化共享矩阵时，矩阵必须覆盖每个 fold 的完整窗口；缓存键必须包含会改变矩阵结果的输入。
- 修改策略、评分或排序逻辑时，同时核对历史策略结果、回测交易记录和 UI 展示是否使用同一候选集和排序方向。

## 6. 缓存、并发与性能

项目同时存在仓库缓存、enriched 历史缓存、实时聚合、筛选缓存、策略结果缓存、回测矩阵缓存、mtime 缓存、SSE 和前端 TanStack Query。修改读写路径时必须列出受影响的缓存层。

### 6.1 缓存一致性

- 写数据后同步检查：持久化文件、内存缓存、generation/version、SSE 事件和前端 query invalidation。
- 不能只保证“文件已写入”，却继续返回旧内存对象。
- 多步刷新优先构建新快照后原子替换，避免 UI 在刷新期间短暂变成空列表或零结果。
- 缓存键必须覆盖资产类型、周期、日期、策略参数和其他会改变结果的维度。
- 失效范围应精确；不能为了修复单条数据而无条件清空所有历史缓存。

### 6.2 并发安全

- API、后台 worker 和实时行情线程共享字典或缓存时，使用锁、不可变快照或原子替换。
- 不在持锁期间执行网络请求、磁盘全量扫描或重计算。
- 取消、超时和应用关闭路径必须释放资源，后台线程不得阻止进程退出。

### 6.3 性能约束

- 列表页避免 N+1 API、每卡片独立请求和重复全量策略计算，优先批量接口及共享查询。
- Polars 计算优先表达式和批处理，避免逐行 Python 循环。
- 实时热路径不得随历史数据量线性增长；需要历史窗口时只读取必要范围。
- 无规则、无订阅者或 provider 无能力时，不执行对应实时计算。
- 声称“提升性能”的 PR 必须给出同一数据规模、同一环境下的修改前后数据，至少包括耗时或请求数。

## 7. 前端改动要求

- 复用 `frontend/src/lib/api.ts` 中的请求封装和类型，禁止组件直接散落请求地址。
- TanStack Query 查询键使用集中定义；资产类型、日期、周期等影响结果的参数必须进入查询键。
- 切换股票、ETF、日期或策略后，不得把上一上下文的缓存结果当作当前结果。
- 实时刷新保留上一份有效数据直到新数据就绪，避免列、计数和列表闪烁消失。
- 弹窗打开和关闭必须正确重置临时表单状态；新建态与编辑态分离。
- 公共标签、个股详情、成分股等跨页面行为使用共享组件，不在多个页面复制交互。
- 新增交互必须覆盖加载、空数据、错误、禁用和无权限状态。
- 改动应在常用桌面和窄屏尺寸下检查文字截断、遮挡、滚动区域和弹窗可操作性。
- 不用前端补丁掩盖后端单位或契约错误；契约问题应在数据边界修正。

## 8. 安全、兼容与用户数据

- 不提交 `data/`、密钥、Token、Webhook、用户策略、日志、数据库、Parquet 样本和机器绝对路径。
- 日志和异常响应不得包含授权信息、完整配置或用户敏感数据。
- 文件删除、覆盖、迁移和批量回算属于高风险操作：限制目标目录、验证路径、提供清晰失败原因，并保持 fail-closed。
- 不用不安全的字符串拼接执行 SQL、Shell 或动态 Python。
- 配置、策略 JSON、Parquet schema 和 API 字段变化默认需要向后兼容。新增字段优先提供默认值，读取历史数据时允许字段缺失。
- 必须改变既有行为时，在 PR 中列出迁移方式、降级行为和影响范围。
- Windows、macOS、Linux 和 Docker 的路径、编码、回收站及权限行为不同；涉及文件系统时至少审查跨平台失败路径。

## 9. 验证矩阵

根据改动范围执行最小但充分的验证。以下是最低要求，不是上限。

| 改动类型 | 最低验证 |
| --- | --- |
| 后端纯函数、规则或 bug 修复 | 对应定向 `pytest`，包含复现用例和边界用例 |
| API 契约 | service 测试 + API 测试；检查成功、无数据和错误响应 |
| 数据源或插件 | provider 契约测试、缺少能力、空数据、字段/单位标准化测试 |
| 指标、复权、换手率、涨跌停 | 固定样本数值断言，覆盖历史边界和降级路径 |
| 策略或监控 | 参数变更、缓存失效、资产切换、重启/并发相关路径 |
| 回测 | 信号日与成交日、T+1、费用、滑点、不可成交、数据缺口 |
| 缓存或性能 | 命中/失效测试、并发快照测试；必要时提供前后基准 |
| 前端组件或页面 | `pnpm build`，并手工检查加载、空、错、切换和实时刷新状态 |
| 前后端契约同时变化 | 后端定向测试 + 前端构建 + 对应页面联调 |
| 文件、Docker 或跨平台逻辑 | 正常路径、权限/能力缺失和目标不可用的失败路径 |

常用命令：

```bash
cd backend
uv run pytest tests/path/to/test_x.py -q
uv run ruff check app/path.py tests/path.py

cd frontend
pnpm build

git diff --check
```

- 后端至少运行受影响模块的测试，不应只运行新加的单个测试。
- 新增文件和本次改动不得引入 Ruff 告警。不要为清理历史告警而在功能 PR 中全仓格式化。
- 前端任何 TypeScript、组件、样式或 API 类型改动至少执行一次 `pnpm build`。
- 提交前必须运行 `git diff --check`，并检查最终 diff 不含调试代码和无关文件。

## 10. PR 提交要求

一个可审查的 PR 应聚焦一个问题。功能、无关重构、批量格式化和依赖升级不得混在一起。

PR 描述必须包含：

1. **问题与复现**：原行为、复现条件和影响用户。
2. **根因**：具体到调用链、数据契约或状态生命周期，不能只写“修复 bug”。
3. **解决方案**：改动模块、关键取舍，以及为什么没有破坏插件化或现有口径。
4. **兼容性**：对历史配置、策略、缓存、Parquet、API、数据源和不同资产类型的影响。
5. **性能**：是否进入实时或列表热路径；有性能主张时附前后数据。
6. **验证结果**：列出实际执行的命令和结果，不写计划执行的测试。
7. **界面证据**：有可见变化时提供修改前后截图或短视频，并覆盖浅色/深色或窄屏中相关场景。
8. **风险与回滚**：剩余风险、降级行为和可行的回滚方式。

提交前作者自检：

- [ ] 改动只解决 PR 描述的问题，没有夹带无关整理。
- [ ] 已检查完整调用链和所有受影响缓存。
- [ ] 已核对单位、复权、交易日、时区和资产类型。
- [ ] 未绕过 provider abstraction，缺少能力时行为明确。
- [ ] 历史配置和旧数据仍可读取，或已说明迁移方案。
- [ ] 实时线程、列表请求和批处理没有明显性能退化。
- [ ] 错误路径 fail-closed，不会静默返回错误金融结果。
- [ ] 测试能在未修复代码上失败，并在修复后通过。
- [ ] 已执行适用的测试、构建、Ruff 和 `git diff --check`。
- [ ] 最终 diff 不含敏感数据、本地路径、调试输出和生成文件。

## 11. PR 复审流程

作者自审和维护者复审均按以下顺序进行，避免只看页面效果或新增代码：

1. **确认问题**：根据描述和测试复现旧行为，确认 PR 解决的是同一个问题。
2. **检查边界**：确认代码位于正确模块，没有反向依赖、平行实现或 API 层重计算。
3. **核对数据契约**：检查单位、复权、日期、时区、资产类型、空值和排序方向。
4. **核对插件化**：检查是否依赖 provider 能力，非 TickFlow 数据源能否工作或明确降级。
5. **跟踪状态变化**：从写入一路检查持久化、缓存、generation、SSE 和前端查询失效。
6. **检查兼容性**：使用历史配置、旧策略 JSON、旧 Parquet schema 和缺字段数据验证。
7. **检查性能和并发**：定位是否进入实时、列表或全量扫描热路径，检查锁和原子替换。
8. **检查失败路径**：网络失败、无权限、空数据、能力缺失和部分写入时不得产生错误状态。
9. **评估测试质量**：测试必须断言业务结果，覆盖负例和边界，不能只断言接口返回 200。
10. **检查最终 diff**：按文件逐项确认必要性，排除无关格式化、敏感信息和临时文件。
11. **给出结论**：明确写出“可合并”“修改后合并”或“不建议合并”，区分阻断项和建议项。

问题严重级别：

| 级别 | 定义 | 合并要求 |
| --- | --- | --- |
| `P0` | 数据破坏、安全漏洞、可能导致严重错误交易结果 | 禁止合并，修复并完整复审 |
| `P1` | 核心流程不可用、错误金融口径、未来函数、广泛回归 | 禁止合并，修复并补回归测试 |
| `P2` | 明确功能错误、兼容性问题或明显性能退化 | 修改后合并 |
| `P3` | 维护性、局部体验、文档或测试补强建议 | 可不阻断，但应记录后续处理方式 |

复审意见应包含文件和行号、触发条件、实际影响及建议修改方向。不要只写“这里可能有问题”或直接给出没有证据的结论。

### 11.1 追加提交后的复审

- 先逐项核对上轮每个阻断问题是否在代码和测试中真正解决，不以提交者回复“已修改”为依据。
- 查看新增 commit 便于理解修复意图，但最终必须重新审查 PR 相对目标分支的完整 diff，不能只看最后一次提交。
- 重新执行受影响测试，并检查修复是否引入新的单位、缓存、兼容性、性能或插件化问题。
- 已解决的问题明确标记为已验证；仍存在的问题引用当前最新行号并说明缺失内容。
- 原阻断项全部解决且没有新增阻断项后，才能把结论改为“可合并”。

### 11.2 推荐的复审输出格式

```text
结论：可合并 / 修改后合并 / 不建议合并

阻断问题：
- [P1/P2] 文件:行号 - 触发条件、实际影响、修改要求

非阻断建议：
- [P3] 文件:行号 - 建议及理由

已验证：
- 执行的测试或构建命令及结果
- 上轮问题的逐项复核结果

剩余风险：
- 未覆盖的平台、数据源、数据规模或场景
```

没有发现问题时也应明确写“未发现阻断问题”，并说明尚未验证的范围，不能用“看起来没问题”代替结论。

## 12. 不得合并的情况

出现以下任一情况，PR 不应合并：

- 无法说明根因，修改只是增加延时、重试、强制刷新或吞掉异常来掩盖现象。
- 混用百分比、复权价、自然日和交易日，或无法证明金融计算口径。
- 通用功能直接绑定单一数据源，且没有能力检测或明确降级。
- 写入后存在已知缓存不一致，需用户重启或手动清数据才能正确显示。
- 回测引入未来数据、绕过成交约束，或历史结果和交易记录使用不同策略逻辑。
- 实时线程增加阻塞网络请求、全量历史计算或无界任务。
- 破坏历史策略、配置、数据文件或 API，且没有兼容或迁移方案。
- 文件操作可能越界删除、默认覆盖用户数据，或失败后继续执行。
- 测试没有复现问题、只覆盖正常路径，或必要测试/前端构建失败。
- PR 包含密钥、用户数据、本地数据目录、机器绝对路径或大量无关改动。
- 对评审提出的 `P0`、`P1`、`P2` 问题尚未处理或验证。

## 13. AI 编码代理额外规则

- 开始前先读取本文件、目标模块、相邻实现、调用方和相关测试。
- 修改前用简短计划写明每一步及验证方式；简单改动可只写一句。
- 不把猜测当成事实。无法从代码、测试或文档确认的业务口径，应明确询问。
- 不因用户要求“直接修复”就跳过复现、根因分析和回归验证。
- 不自动执行提交、推送、合并、删除文件、数据库迁移或批量数据覆盖；需要时按当前会话权限规则取得明确确认。
- 不覆盖工作区已有改动。遇到同文件并行修改时先读取最新内容并保留双方逻辑。
- 不虚构测试结果、性能数据、GitHub 状态或线上行为。未运行的验证必须明确标注。
- 审查 PR 时先报告问题，按严重级别排序并引用文件行号；最后再给合并建议和简短总结。
- 完成后检查 `git diff` 和 `git status`，准确说明改了什么、验证了什么、仍有什么风险。

## 14. 贡献许可与来源声明

本项目采用 **DCO（Developer Certificate of Origin）+ 贡献再许可授权**的组合：提交时声明来源，并授予维护者更换协议所需的再许可权。

### 14.1 每次提交必须签署 DCO

所有提交必须带 `Signed-off-by` 尾注，声明你创作了该贡献、或有权按本项目条款提交，并同意本节的贡献许可条款：

```bash
git commit -s -m "fix(scope): 说明"
```

`-s` 会自动追加一行（姓名与邮箱须与 Git 配置一致）：

```
Signed-off-by: 你的名字 <you@example.com>
```

DCO 完整声明见 <https://developercertificate.org/>。未签署的 PR 不予合并，CI 的 DCO 检查会拦下。

### 14.2 贡献再许可授权

提交贡献即表示你授予本项目维护者一项**永久、不可撤销、全球范围、非独占、免版税**的版权许可，允许维护者：

- 按项目当前协议（见 `LICENSE`）分发你的贡献；
- 在未来将项目整体或部分更换为其他开源协议（包括 copyleft 协议，如 AGPL-3.0）；
- 为商业双授权目的，对你的贡献进行再许可。

你保留自己贡献的版权与署名权，本条款不改变你的署名权，也不影响你对自己贡献的独立使用。

> 为什么需要这一条：项目计划在未来版本引入 AGPL-3.0，并可能提供商业许可（AGPL + 商业双授权）。若没有明确的再许可授权，维护者只能在现有协议下分发第三方贡献，无法完成协议切换与双授权。仅有 DCO 不足以支撑这一权利，因此本条与 DCO 同时适用。

### 14.3 历史贡献的边界

本节条款**仅对生效日之后的提交生效**：

- 此前已合入的贡献仍按其提交时生效的 MIT 协议授权，**历史版本永久保持 MIT**，不会被追溯变更；
- 维护者会另行联系主要历史贡献者补签再许可授权；未补签的部分继续按 MIT 生效，不影响项目继续以 MIT 分发既有代码。

### 14.4 需要签署正式 CLA 的情形

以下情形需要签署书面 CLA（个人 ICLA 或公司 CCLA），仅靠 DCO 尾注不够：

- 贡献属于你在雇主职责范围内完成的工作（须公司签署 CCLA）；
- 贡献涉及你或雇主持有的专利；
- 单次贡献规模较大，或对项目架构有实质影响。

需要时由维护者在 PR 中提出并提供文本，签署记录归档在仓库外单独保存。

### 14.5 提交前自检

- [ ] 提交已用 `git commit -s` 签署 DCO；
- [ ] `Signed-off-by` 的姓名邮箱真实且与 Git 提交身份一致；
- [ ] 若贡献受雇于公司或涉及专利，已在 PR 中说明。

遵循本指南的衡量标准不是文档写得多，而是：改动范围更小、金融口径有证据、插件边界不被绕过、测试能真实复现问题、复审结论可执行，且相同问题不再反复返工。
