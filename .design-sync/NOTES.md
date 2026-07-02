# design-sync notes

## Repo shape
This repo is a full application (stock-panel dashboard: Python backend + React
frontend), **not** a component library. `frontend/package.json` has
`"private": true` and no `main`/`module`/`exports` field — there is nothing
published for external consumption.

Of the ~68 files under `frontend/src/components/` (plus subfolders
`stock-analysis/`, `monitor/`, `screener/`, `financials/`, `signals/`,
`ext-data/`, `stock-table/`, `data/`), almost all are business components
wired to this app's data hooks (`@/lib/use*`), API client (`@/lib/api`), or
Zustand-style stores (`@/store`) — not standalone, reusable UI.

**Scope was manually curated down to 7 components** that have no such
imports and render meaningfully from props alone: `DatePicker`, `EmptyState`,
`Logo`, `PageHeader`, `WarmupBadge`, `EChartsCandlestick`, `ToastContainer`
(from `Toast.tsx`).

Explicitly excluded, and why:
- **ColumnCustomizer** — thin wrapper around **ListColumnCustomizer**, which
  itself calls `useQuery`/`api` internally (live data fetch) and can't render
  standalone without a `QueryClientProvider` + mocked API response.
- **CandlestickChart.tsx** — dead code (no references anywhere in the app;
  superseded by `EChartsCandlestick`, which uses echarts instead of
  lightweight-charts).
- Everything else — data/store coupled (confirmed via grep for
  `@/lib/use|@/lib/api|@/store` imports).

If new presentational components are added to `src/components/` later,
re-run that same triage (grep for those three import patterns) before adding
them to `componentSrcMap` + the synth entry.

## No package build — synth entry + hand-built cssEntry
There is no `dist/` and no library entry point, so:

- **Bundle entry**: `frontend/.design-sync-entry.mjs` (gitignored, NOT
  committed) — hand-written, re-exports only the 7 scoped components, e.g.
  `export { DatePicker } from './src/components/DatePicker.tsx';`. It also
  re-exports the module-level `toast()` helper from `Toast.tsx` — needed by
  the `ToastContainer` preview to populate the notification stack; the first
  attempt only exported `ToastContainer` and the preview crashed with
  `toast is not a function`.
- **cssEntry**: `frontend/.design-sync-styles.css` (gitignored) — NOT the
  vite build output (that has a content hash and isn't stable across
  rebuilds). Regenerate via:
  ```sh
  cd frontend && npx tailwindcss -i ./src/index.css -o ./.design-sync-styles.css --config tailwind.config.ts
  ```
  Then **manually prepend** these two lines (tailwindcss CLI output has no
  `@import`, and rebuilding this file wipes any hand-edit):
  ```css
  @import url("https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap");
  @import url("https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&display=swap");
  ```
  Why: the real app loads Inter via `rsms.me/inter/inter.css` and JetBrains
  Mono via a Google Fonts `<link>` in `index.html` — neither is a `@import`
  inside any stylesheet, so the converter's font scraper can't find them.
  Without these two lines, validate prints `[FONT_MISSING]`; with them, it's
  the informational `[FONT_REMOTE]` (Inter is re-sourced from Google Fonts
  instead of rsms.me — same typeface, different CDN, visually identical).
- **Known render warns accepted**: `[FONT_REMOTE]` for "HarmonyOS Sans SC"
  and "IBM Plex Mono" — these are Tailwind fallback-stack font names never
  actually shipped anywhere, even in the real production app (CJK/mono
  system-font fallback by design). Not a design-sync-only compromise.
- **cfg.overrides**: `EChartsCandlestick` uses `cardMode: column` (its
  stories render wider than a grid cell); `ToastContainer` uses
  `cardMode: single` + `viewport: 360x200` (it's a `fixed`-positioned
  overlay, escapes a default-sized card).
- No `buildCmd` is set in config — there's nothing to build/run before the
  converter besides the two steps above.

## Re-sync risks
- The `frontend/.design-sync-entry.mjs` and `frontend/.design-sync-styles.css`
  files are gitignored (never committed) — a fresh clone must recreate both
  before re-running the converter. Consider committing a small script if this
  repo gets synced often.
- If `index.html`'s font `<link>` tags change (different CDN, different
  families), the two hand-prepended `@import` lines above go stale silently
  — `[FONT_MISSING]` would resurface on the next validate run as the signal
  to re-check.
- All 7 components are fully authored and graded `good` — no floor cards
  remain in this scope. A future re-sync should carry every grade forward
  (`carried forward`, 0 `grade cleared`) unless a preview or its component
  source actually changed.
