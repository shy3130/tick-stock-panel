# Dow Monitor Half-Hour AI Analysis

## Authority

- Status: authoritative
- Approved by: explicit user approval on 2026-07-31
- Specification ID: `SPEC-DOW-MONITOR-HALF-HOUR-AI-ANALYSIS-001`
- Requirements:
  - `REQ-DOW-MONITOR-HALF-HOUR-AI-ANALYSIS-001`
  - `REQ-DOW-MONITOR-HALF-HOUR-AI-VIEW-001`

## REQ-DOW-MONITOR-HALF-HOUR-AI-ANALYSIS-001

A dedicated worker, separate from the 3018 panel process, MUST analyze only
enabled trend-monitor symbols at completed 30-minute checkpoints in each
symbol's regular exchange session. CN, HK, and US MUST use XSHG, XHKG, and XNYS
exchange calendars respectively; calendar holidays, DST, half-days, and lunch
breaks take precedence over fixed clock rules.

Each analysis MUST use cumulative same-trading-day observations from regular
session open through an explicit `data_cutoff` at or before the checkpoint.
Observations after that cutoff MUST NOT influence the result. A symbol added
intraday starts at the first completed checkpoint after `created_at`; historical
rows may enrich that checkpoint but MUST NOT trigger earlier model calls.

The logical key is `(market, symbol, trade_date, window_end)`. Results MUST be
stored permanently in ClickHouse without TTL. Retries MAY replace the same
logical key but MUST NOT expose duplicates.

The model output MUST be structured and validated against backend-supplied
evidence keys. Numeric evidence values MUST be rendered by backend code.
Invented keys, unstructured prose, and missing uncertainty/risk context MUST be
rejected. The AI path MUST NOT mutate or feed formal signals, real-time key
interpretation, WebSocket ingestion, or minute-result persistence.

Failures MUST be isolated per symbol/checkpoint. Default model-call concurrency
is one.

## REQ-DOW-MONITOR-HALF-HOUR-AI-VIEW-001

The trend-monitor overview MUST expose only a lightweight latest status,
checkpoint, title, and short summary. Full narrative, evidence, risks,
scenarios, and data-quality content MUST be fetched only when the user opens a
separate half-hour-analysis view.

Desktop MUST provide an independent compact entry. Mobile MUST place the entry
below the real-time key interpretation without merging their content. All saved
checkpoints for the current trading date MUST remain selectable in the view.
Visible checkpoints and cutoffs MUST interpret backend timestamps as UTC and
render them explicitly in `Asia/Shanghai` (Beijing time). The history query date
MUST remain the symbol exchange's trading date, including US sessions that cross
Beijing midnight.

AI/provider or ClickHouse unavailability MUST degrade this feature to an
explicit unavailable/failed state without changing existing real-time analysis
or formal-signal responses.
