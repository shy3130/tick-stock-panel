# Dow Monitor Intraday AI Stage Analysis

## Authority

- Status: authoritative
- Approved by: explicit user approvals on 2026-07-31 and 2026-08-01
- Specification ID: `SPEC-DOW-MONITOR-HALF-HOUR-AI-ANALYSIS-001`
- Requirements:
  - `REQ-DOW-MONITOR-HALF-HOUR-AI-ANALYSIS-001`
  - `REQ-DOW-MONITOR-HALF-HOUR-AI-VIEW-001`
  - `REQ-DOW-MONITOR-HOURLY-AI-CADENCE-001`
  - `REQ-DOW-MONITOR-HOURLY-AI-STAGE-REPORT-001`
  - `REQ-DOW-MONITOR-HOURLY-AI-MINUTE-PATH-001`
  - `REQ-DOW-MONITOR-HOURLY-AI-VIEW-001`

The internal half-hour class, table and type names MAY remain for backward
compatibility. New user-facing behavior is an hourly intraday stage analysis.

## REQ-DOW-MONITOR-HALF-HOUR-AI-ANALYSIS-001

A dedicated worker, separate from the 3018 panel process, MUST analyze only
enabled trend-monitor symbols during each symbol's regular exchange session.
CN, HK, and US MUST use XSHG, XHKG, and XNYS exchange calendars respectively;
calendar holidays, DST, half-days, and lunch breaks take precedence over fixed
clock rules.

The logical key remains `(market, symbol, trade_date, window_end)`. Results MUST
be stored permanently in ClickHouse without TTL. Retries MAY replace the same
logical key but MUST NOT expose duplicates. Historical 30-minute results MUST
remain readable and MUST NOT be rewritten as hourly results.

The AI path MUST NOT mutate or feed formal signals, real-time key
interpretation, WebSocket ingestion, or minute-result persistence. Failures
MUST be isolated per symbol/checkpoint. Default model-call concurrency is one.

## REQ-DOW-MONITOR-HALF-HOUR-AI-VIEW-001

Existing 30-minute results MUST remain selectable and render their original
conclusion, evidence, risks, scenarios and data-quality content. Desktop and
mobile MUST keep the AI analysis entry separate from real-time key
interpretation. AI/provider or ClickHouse unavailability MUST degrade only
this feature and MUST NOT change formal-signal or real-time-analysis responses.

## REQ-DOW-MONITOR-HOURLY-AI-CADENCE-001

For every continuous regular-session segment, the worker MUST schedule the
first whole exchange-local clock hour strictly after segment open, each later
whole hour inside the segment, and the segment close when it is not already a
scheduled whole hour. Therefore a first or close stage MAY contain fewer than
60 trading minutes. It MUST NOT continue recurring 30-minute model calls.

On restart the worker MUST execute at most the newest eligible uncompleted
checkpoint. It MUST NOT batch every missed checkpoint or fall back to an older
checkpoint when the newest eligible checkpoint is terminal. The bounded
offline bootstrap exception remains available under its separately indexed
authority, but its eligible checkpoints come from this hourly/segment-close
calendar.

## REQ-DOW-MONITOR-HOURLY-AI-STAGE-REPORT-001

Every new report MUST behave as a senior intraday securities analyst's stage
summary. It MUST explain what changed during the stage and why it matters,
rather than merely listing indicator values. It MUST include:

中文语义边界：报告必须解释分钟级路径、相邻阶段变化和当日累计结构，分别面向
持仓者与未参与者给出条件化建议；不得仅复述指标。

- a concise stage headline and opportunity/risk direction;
- the stage's minute-level path and changes hidden by its endpoint;
- comparison with the immediately preceding completed report;
- the cumulative intraday structure from regular-session open to cutoff;
- channel direction and maturity of relevant patterns;
- business interpretation of volume, price and available capital-flow facts;
- separate conditional guidance for existing holders and users not yet involved;
- strengthening, risk and invalidation conditions for the next stage;
- confidence and explicit data-quality limitations.

The report MUST NOT contain position percentages, order instructions or direct
formal BUY/SELL mutations. Advice MUST remain conditional on backend-supplied
facts and thresholds.

## REQ-DOW-MONITOR-HOURLY-AI-MINUTE-PATH-001

Each report MUST use two separately identified scopes:

1. the stage slice since the previous report checkpoint (or session open for
   the first report); and
2. cumulative same-trading-day observations from regular-session open through
   an explicit `data_cutoff` at or before the checkpoint.

Observations after `data_cutoff`, incomplete future bars and duplicated cutoff
bars MUST NOT influence the result. Lunch breaks MUST NOT count as trading
minutes. Available offline minute data MAY satisfy the input immediately; the
worker MUST NOT wait for WebSocket accumulation when canonical stored data is
already sufficient.

Deterministic backend code MUST derive numeric facts, five-minute subpaths,
high/low times, volume distribution, channel candidates, pattern candidates
and comparisons. The model MAY interpret only supplied facts. Numeric evidence
MUST reference a validated backend metric key; invented keys, invented prices,
unstructured output and missing uncertainty context MUST be rejected.

## REQ-DOW-MONITOR-HOURLY-AI-VIEW-001

The trend-monitor overview MUST expose only a lightweight latest status,
checkpoint, stage interval, frequency, title, short summary and opportunity
change. The complete structured report MUST be fetched only when the user opens
the separate intraday-AI view.

The new detail view MUST present, in order: headline, current-stage path,
hidden minute changes, previous-stage comparison, day-to-now overview,
channel/pattern assessment, volume/capital interpretation, holder guidance,
watcher guidance, next-stage confirmation/risk/invalidation, and data quality.

Visible checkpoints and cutoffs MUST interpret backend timestamps as UTC and
render them explicitly in `Asia/Shanghai` (Beijing time). The history query date
MUST remain the symbol exchange's trading date, including US sessions crossing
Beijing midnight. Mobile MUST keep the existing compact first-four-column list
and open the long report in a separate dialog.
