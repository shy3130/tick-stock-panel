# Independent review: Dow monitor incremental evaluation and realtime priority

Status: passed for local semantic acceptance; production acceptance remains pending until the 10.28 deployment evidence is recorded.

Reviewed specification: `docs/superpowers/specs/2026-07-31-dow-monitor-incremental-evaluation-and-realtime-priority-design.md`.

## Requirement disposition

| Requirement | Independent conclusion | Evidence |
| --- | --- | --- |
| `REQ-DOW-MONITOR-INCREMENTAL-TIMEFRAME-EVALUATION-001` | Passed | Completed-boundary scheduling, partial cold start, cache reuse, retry and stable-state failure preservation have executable backend coverage. |
| `REQ-DOW-MONITOR-BOUNDED-SYMBOL-CONCURRENCY-001` | Passed | Cross-symbol concurrency is bounded at three, per-symbol timeframe order is preserved, and one-symbol failure isolation is covered. |
| `REQ-DOW-MONITOR-LIVE-INTERPRETATION-AVAILABILITY-001` | Passed | Frontend selection distinguishes stale scheduling from genuinely missing stable/capital evidence and preserves business interpretation when stable inputs remain available. |
| `REQ-DOW-MONITOR-MINUTE-RESULTS-REALTIME-APPEND-001` | Passed | Realtime contexts are symbol-local and causal; queueing is bounded, deduplicated, batched and fail-open. |
| `REQ-DOW-MONITOR-MINUTE-RESULTS-STAGGERED-BACKFILL-001` | Passed | Backfill is single-flight and market-aware, uses explicit completed market days, has row/time budgets, and propagates the remaining deadline through ClickHouse and Dow-engine HTTP calls. |

## Lower-layer semantic review

The review did not use frontend snapshots or downstream output as substitutes for scheduling, source selection, state preservation, storage-key or timeout semantics. It first reviewed the pure timeframe-due rules and backend service tests, then minute-result source/repository/history behavior, and only then the frontend interpretation behavior.

Four review rounds identified and verified fixes for:

- cross-symbol timeframe-state contamination;
- partial cold starts incorrectly evaluating every timeframe;
- temporary evaluation errors replacing an existing stable state with `ANALYSIS_PAUSED`;
- daily evaluation before the completed market close;
- nightly audits using the scheduler date instead of the completed market day;
- frontend fixtures and warmup precedence that hid stable business interpretation;
- bounded materialization deadlines stopping at cooperative loops instead of reaching ClickHouse and the 19912 HTTP request.

The final review found `P0=0`, `P1=0`, and `P2=0`. The deadline chain was verified as:

`materializer -> history build_contexts -> signature-compatible stable builder -> remaining budget -> LongbridgeDowClient.evaluate(timeout_s) -> httpx request timeout`.

Legacy stable builders without a `deadline` keyword remain compatible.

## Executable evidence

- Independent targeted backend regression: 99 passed.
- Independent frontend wrapper regression: 3 passed.
- Deadline propagation and legacy-builder compatibility probes: passed.
- Primary-agent full backend and frontend results are recorded in `docs/acceptance/dow-monitor-incremental-evaluation.md`.

## Residual boundary

Pure Python work and third-party networking cannot be forcibly preempted at the process level. The implementation provides cooperative loop checks, ClickHouse server/HTTP deadlines, and per-request 19912 HTTP deadlines. This residual is not a release blocker for the approved design.

## Final disposition

The production code satisfies the five active requirements and is acceptable for candidate deployment. Production status must not be marked passed until the 10.28 candidate and switched 3018 service complete the runbook acceptance checks without restarting 19912, the market WebSocket service, or the standalone AI worker.

## Supplemental review gate: HK display aliases

The first 10.28 candidate correctly failed the cycle-duration acceptance gate and was rolled back.
The follow-up change treats padded and unpadded HK symbols as one semantic stock when loading,
saving and removing timeframe state, selecting previous states and minute rows, and indexing
notifications. Two executable regressions cover canonical-state reuse and the absence of false
cold-start evaluation. Independent review disposition and final production evidence must be added
before this supplemental gate is marked passed.

Supplemental independent review result: `P0=0`, `P1=0`, `P2=0`; approved for
candidate deployment. The reviewer independently covered store removal,
notification isolation, non-HK behavior, and the non-collision of `00981.HK`
with `09981.HK`, in addition to the two production-root-cause regressions.
Production acceptance remains gated on the ten-cycle candidate and switched
3018 observations.

## Supplemental review gate: exact CN/HK close label

The `ab931342f081` candidate was not promoted after repeated 110-112 second
cycles revealed a completed-bucket mismatch for final rows labeled exactly at
CN/HK market close. A failing semantic regression was added before the marker
logic was changed. Independent review must confirm final-close, midday-break,
non-divisible-session and US isolation semantics before another candidate is
built.

Supplemental independent review result: `P0=0`, `P1=0`, `P2=0`; approved for
candidate deployment. Independent probes confirmed CN 15:00 and HK 16:00
equivalence with the last regular minute for every supported intraday period,
HK midday and non-divisible session behavior, US isolation, and final-close bar
aggregation values.

## Supplemental review gate: minute fetch is independent of daily state

The `2ec297c94449` candidate was not promoted after a zero-evaluation cycle
still took about 103 seconds. The minute fetch plan had used the oldest of all
states, including a prior-session daily state, even though daily input is loaded
through the separate daily loader. A failing regression now covers old and
missing-only daily state. Independent review must confirm new-symbol, partial
intraday cold start and daily-due behavior before another candidate is built.

Initial independent disposition for this gate was `P1=1`: ignoring day
unconditionally could build today's daily candle from only the last few minute
rows. The follow-up keeps the normal intraday start, but expands a single fetch
to local midnight when day is missing/failed or due after market close, without
marking the symbol as a long-history cold start. Final independent disposition
is pending.

Final independent disposition: `P0=0`, `P1=0`, `P2=0`; approved for candidate
deployment. Independent probes covered market-open old-day fast paths, missing
day full-session input, post-close old/forming day, return to the fast path
after FINAL save, new and partial intraday cold starts, midday, weekends and
market time zones. A complete 390-minute US session produced the correct daily
open, high, low, close, volume and FINAL completion.
