# Dow Monitor Hourly AI Manual Rerun Design

## Status and authority

- Status: approved design, pending written-spec review
- Approved by: explicit user selections on 2026-08-02
- Parent specification: `SPEC-DOW-MONITOR-HALF-HOUR-AI-ANALYSIS-001`
- Proposed requirement: `REQ-DOW-MONITOR-HOURLY-AI-MANUAL-RERUN-001`
- User decisions:
  - use an asynchronous durable request queue;
  - rerun the currently selected hourly report;
  - replace the report at the same logical checkpoint after success;
  - permit a manual rerun outside the symbol market's regular session.

The off-session permission is a narrow manual-retry exception to the existing
regular-session scheduling rule. Implementation MUST record this decision in
the authoritative parent specification and its index before production code is
changed.

## Problem

The intraday AI dialog can read report history and detail, but it cannot ask the
independent AI worker to regenerate a selected checkpoint. The 3018 panel
process intentionally has no model runner, while the worker has no control
plane for a user request. Calling the model directly from 3018 would couple a
slow and costly operation to the page service and could interfere with realtime
market-data handling.

## Scope

The feature adds one manual action for an already stored hourly AI analysis.
It does not add a new checkpoint, change the hourly calendar, alter a formal
signal, or modify realtime key interpretation. Historical 30-minute analyses
remain read-only.

## User experience

The hourly AI dialog header contains a `重跑AI分析` button for the currently
selected hourly record. The action is available for a stored hourly record and
is not shown for a legacy 30-minute record.

Before submission, the user sees a confirmation that:

- the selected checkpoint will be regenerated;
- the current report remains visible while work runs;
- a successful result replaces the report at that checkpoint.

The visible state sequence is:

```text
重跑AI分析 -> 提交中 -> 排队中 -> 重跑中 -> 已更新
```

While a request is being submitted, queued, or running, the action is disabled.
Only one active rerun request may exist for the same analysis. Closing the
dialog does not cancel server-side work. Reopening the same report recovers the
persisted request state.

After success, the selected detail, history tabs, and lightweight latest
summary are refreshed automatically. After failure, the existing report stays
visible, the sanitized failure reason is shown beside the action, and the user
may submit a new request.

## Chosen architecture

### API control plane

The 3018 API exposes two operations on the existing report identity:

```text
POST /api/dow-monitor/{symbol}/ai-analyses/{analysis_id}/rerun
GET  /api/dow-monitor/{symbol}/ai-analyses/{analysis_id}/rerun
```

`POST` validates that:

- the analysis exists and belongs to the route symbol and market;
- `report_frequency` is `hourly`;
- the symbol is still enabled in trend monitoring;
- the AI rerun repository is available.

It then creates a durable queued request or returns the already active request
for that analysis. Duplicate clicks MUST NOT create duplicate model calls.
The response contains `request_id`, `analysis_id`, `status`, timestamps, and a
boolean indicating whether an active request was reused.

`GET` returns the latest rerun request for the analysis. The frontend polls it
only while the status is `queued` or `running`.

The API MUST NOT build a snapshot or call the model. Missing analysis is 404;
legacy report, disabled symbol, or another semantic conflict is 409; unavailable
request storage is 503.

### Durable request repository

A dedicated ClickHouse table stores the request audit trail permanently. Each
row includes:

- request and analysis identities;
- market, symbol, trade date, original `window_end`, and original
  `data_cutoff`;
- status: `queued`, `running`, `completed`, or `failed`;
- requested, started, completed, and updated timestamps;
- sanitized error code and error message.

Replacing rows by request identity records state transitions without exposing
duplicate active work. The repository can find the oldest queued request and
the latest active request for an analysis. The report table remains the source
of truth for report content; the request table is control and audit state only.

### Worker execution

The existing independent AI worker consumes at most one manual request per
polling cycle and still gives the normal scheduled scan an opportunity in that
cycle. Manual and scheduled model calls remain serial, preserving the existing
default concurrency of one.

For a manual request the worker:

1. reloads the selected analysis and validates its identity;
2. marks the request `running`;
3. rebuilds the snapshot from canonical minute results using the selected
   report's original trade date and checkpoint;
4. fixes `data_cutoff` to the original checkpoint and excludes every later row;
5. uses the latest completed report strictly before that checkpoint as the
   preceding-stage context;
6. invokes the existing prompt and structured-output validator;
7. on success, saves the new report under the same deterministic analysis ID
   and logical key with `attempt` incremented, then marks the request completed;
8. on failure, marks only the request failed and leaves the prior report row
   unchanged.

The manual request MAY execute outside regular exchange hours. This permission
applies only because the target is an already stored hourly checkpoint. It MUST
NOT generate a new off-session checkpoint, extend the cutoff, read later bars,
or make an unmonitored symbol eligible for analysis.

If corrected canonical minute data now exists at or before the fixed cutoff,
the regenerated report may legitimately differ from the prior report. Missing
data may use the existing bounded offline materializer. Resource limits and
failure isolation remain unchanged.

## Replacement and compatibility semantics

The existing report remains readable until a fully validated replacement is
ready. `running` and `failed` manual states are never written over a completed
report. A successful result uses the same `(market, symbol, trade_date,
window_end)` and `analysis_id`, so history still shows one report for one
checkpoint. The report's `attempt` and `updated_at` expose that a newer version
was generated; every request remains in the audit table.

Existing scheduled retries, old hourly rows, legacy 30-minute rows, list
overview payloads, and detail response fields remain backward compatible.

## Frontend data flow

The dialog owns one mutation for `POST` and one lightweight request-status
query for the selected analysis. Polling is two seconds only while queued or
running. On completion it invalidates the selected detail, that symbol's trade-
date history, and the applicable trend-monitor overview. Selection changes
switch the status query key, so another report never inherits the first
report's loading state.

The button and feedback remain usable at narrow mobile widths. Long errors are
wrapped and do not widen the dialog.

## Failure and recovery rules

- Model or provider failure: keep the existing report; mark the request failed.
- Snapshot still insufficient after bounded materialization: keep the existing
  report; fail the request with an explicit data error and do not invent facts.
- Worker restart with a queued request: process it normally.
- Worker restart with a stale running request: return it to queued after a
  bounded stale threshold, then allow one retry.
- Duplicate submission: return the active request; do not add another call.
- ClickHouse request storage unavailable: reject submission and leave report
  reads, realtime interpretation, signals, and WebSocket behavior intact.
- Dialog close, page refresh, or client disconnect: do not cancel the request.

## Security, cost, and performance boundaries

The action uses the application's existing authenticated API boundary. The
server derives market, trade date, checkpoint, and cutoff from the stored
analysis; the client cannot supply or extend them. Confirmation plus active-
request deduplication guards accidental cost. Queue polling is lightweight and
does not enter the list bootstrap or WebSocket render path.

## Test and acceptance strategy

Production changes require observed failing behavior tests first.

Backend tests MUST prove:

1. submission targets the exact selected hourly analysis;
2. a legacy report, mismatched symbol, or disabled symbol is rejected;
3. duplicate submissions reuse the active request;
4. an off-session manual request is accepted and executed;
5. the worker uses the original checkpoint/cutoff and excludes later rows;
6. success replaces the same logical report and increments `attempt`;
7. model, validation, or data failure preserves the old report;
8. stale running recovery and per-request failure isolation are bounded;
9. scheduled analysis and formal signal paths remain unchanged.

Frontend tests MUST prove:

1. the action follows the selected hourly history tab;
2. legacy 30-minute reports do not expose the action;
3. confirmation precedes submission;
4. queued/running states disable duplicate submission;
5. success refreshes detail, history, and overview;
6. failure retains report content and exposes a retryable error;
7. reopening recovers persisted request status.

Semantic acceptance MUST additionally verify the full flow against a real
stored hourly report, including an off-session run, replacement under the same
checkpoint, no later-than-cutoff evidence, mobile button usability, one model
call, and no restart or degradation of 3018, 19912, realtime WebSocket, minute
persistence, or the formal-signal path.

## Out of scope

- batch rerun of multiple symbols or checkpoints;
- free-form prompt editing from this button;
- comparing or restoring old generated versions in the UI;
- rerunning legacy 30-minute reports;
- changing model choice or prompt content;
- cancelling a request after it starts;
- creating an HTTP control port on the AI worker.
