# Dow Monitor Hourly AI Stage Analysis Acceptance

Date: 2026-08-01

Requirements:

- `REQ-DOW-MONITOR-HOURLY-AI-CADENCE-001`
- `REQ-DOW-MONITOR-HOURLY-AI-STAGE-REPORT-001`
- `REQ-DOW-MONITOR-HOURLY-AI-MINUTE-PATH-001`
- `REQ-DOW-MONITOR-HOURLY-AI-VIEW-001`

Status: local semantic acceptance and the 2026-08-02 production release passed.
The first newly generated hourly report remains a next-market-session check
because the release occurred outside trading hours.

## Lower-layer semantic evidence

- The exchange-calendar tests prove hourly and continuous-segment-close
  checkpoints for XSHG, XHKG and XNYS, including lunch segmentation, restart
  catch-up and the rule that no earlier checkpoint is selected when the newest
  eligible checkpoint is already terminal.
- `tests/backend/test_dow_monitor_hourly_ai_structure.py` proves the deterministic
  minute layer before any model output is considered: cutoff-bounded minute
  normalization, duplicate replacement, stage/cumulative separation, five-minute
  path slices, channel classification, V/inverted-V and breakout/breakdown
  patterns, volume distribution and opportunity change.
- Snapshot tests prove that the decision minute at the exact cutoff is included
  once, rows after the cutoff are excluded, lunch contributes no synthetic
  trading minutes and a partial latest row lowers data quality without erasing
  available minute structure.
- Repository tests prove permanent storage in the existing no-TTL ClickHouse
  table, idempotent schema extension, structured-report round trips, latest prior
  report lookup and continued readability of legacy 30-minute rows.
- Prompt/parser tests prove that new calls require the senior-analyst stage
  report, reject unknown evidence keys and reject indicator-only narration. The
  worker tests prove read-only monitor inputs, previous-stage comparison,
  per-checkpoint isolation, one-model-call default concurrency and no formal
  signal mutation.

## API and UI evidence

- Overview responses retain only lightweight stage metadata and do not include
  the long report. Detail is still fetched lazily through the existing history
  and detail routes.
- New hourly reports render in the approved order: headline, minute path, hidden
  changes, previous-stage comparison, cumulative day, channel/pattern, volume
  and capital interpretation, holder guidance, watcher guidance, next-stage
  conditions and data quality.
- Legacy 30-minute records keep their former dialog presentation. The action is
  labelled `盘中AI分析`, while an old record is explicitly identified as a
  historical 30-minute analysis.

## Real-data replay evidence

The deterministic layer was replayed against 390 regular-session one-minute
Longbridge bars for `NBIS.US` on 2026-07-31. For the final hourly stage it found:

- open 190.66, high 191.94 at 19:55 UTC, low 186.85 at 19:42 UTC and close 190.41;
- stage change -0.1311%, VWAP 189.9901 and a `TRANSITION` channel;
- second-half volume about 3.23 times first-half volume and final-five-minute
  volume share 39.50%;
- no mature V reversal or breakout pattern, `EXPANSION_DOWN` volume direction,
  opportunity score -0.1101 and `WEAKENING` versus the prior completed hour;
- cumulative regular-session change -5.2380% from open to cutoff.

This demonstrates that the lower layer exposes the business-relevant sequence:
a late low, a sharp repair above hourly VWAP and heavy late volume, while still
distinguishing that repair from a confirmed reversal.

## Verification evidence

- Backend: `212 passed` (`pytest tests/backend -q`).
- Frontend: `210 passed, 2 skipped` across 47 files
  (`pnpm --dir frontend test --run`).
- Production frontend compilation: `pnpm --dir frontend build` passed.
- Targeted Python static analysis: Ruff passed.
- Specification compliance: `Specification compliance passed`.
- `git diff --check` passed after the evidence and runbook updates.

`pnpm --dir frontend lint` is not accepted as evidence because this repository's
frontend package currently has no executable ESLint dependency; the command
fails before inspecting source. The successful TypeScript production build and
the full frontend test suite are the executable frontend acceptance evidence.

## 2026-08-02 production release evidence

- Release commit: `1c0cc309d932c6fea176d6f9f590c0fd78d5e144`.
- Source archive SHA-256:
  `c7c9602bcb4744e756f4cf61d295dd1e8f5fe70c50bcff6980c3fd68d2fec0da`.
- Production tag: `tickflow-stock-panel-app:dow-hourly-ai-1c0cc309d932`;
  image ID:
  `sha256:176e2e17502a4fddb31cca3ae17e0f3d3964d0e2bf703aeb45bc8860fbda8c53`.
- Both `TickFlow_Stock_Panel` and `TickFlow_Dow_AI_Worker` run that exact image
  with `RestartCount=0`. The 3018 `/health` endpoint reports the exact release
  commit, and the release-window logs contain no
  `ERROR`, `CRITICAL`, `Traceback` or `Exception` matches.
- The 19912 process stayed PID `3511290`, with its original
  `2026-07-27 13:51:40` start time; `/api/health` remained healthy. It was not
  restarted during this release.
- Production WebSocket verification completed
  `hello v1 -> NBIS.US snapshot -> unsubscribed` on port 3018.
- The production HTML references `index-B8_3aR8a.js` and
  `index-Dfiy7upg.css`; the resolved lazy chunk is
  `DowMonitor-_pnq4Vok.js`. That chunk contains the new hourly report fields and
  the approved `盘中AI分析`, `小时阶段分析`, holder-advice and watcher-advice
  presentation contracts.
- The ClickHouse migration was additive and idempotent. AI analysis counts
  stayed physical/logical `156/156`; minute-result counts stayed
  `16438/14356`. The new five columns are present, and the existing table keeps
  its no-TTL `ReplacingMergeTree(updated_at)` contract.
- A legacy `NBIS.US` row remains readable as `report_frequency=half_hour` with
  null stage metadata and an empty structured report. The deployed API retains
  all 13 existing monitored symbols.
- The worker remains enabled with poll interval 15 seconds and model concurrency
  1. Because 2026-08-02 is outside a regular trading session, no fake report or
  model call was inserted merely to exercise the UI. The first real hourly row,
  model call and A/H/US cross-market display check are deferred to their next
  regular market sessions.

Rollback is image-only and data-preserving: restore the previous app image
`tickflow-stock-panel-app:dow-monitor-incremental-7c7af48a11ab` and previous
worker image `tickflow-stock-panel-app:dow-offline-bootstrap-6530979992a0`
separately with `--no-deps`. Do not restart 19912 and do not remove the additive
ClickHouse columns or stored rows.
