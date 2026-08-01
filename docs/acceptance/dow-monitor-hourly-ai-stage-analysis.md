# Dow Monitor Hourly AI Stage Analysis Acceptance

Date: 2026-08-01

Requirements:

- `REQ-DOW-MONITOR-HOURLY-AI-CADENCE-001`
- `REQ-DOW-MONITOR-HOURLY-AI-STAGE-REPORT-001`
- `REQ-DOW-MONITOR-HOURLY-AI-MINUTE-PATH-001`
- `REQ-DOW-MONITOR-HOURLY-AI-VIEW-001`

Status: local semantic acceptance passed; production deployment was not part of
this acceptance.

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
