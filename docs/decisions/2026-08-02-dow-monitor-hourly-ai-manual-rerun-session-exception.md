# Manual Hourly AI Rerun Session Exception

Decision ID: `DEC-20260802-DOW-MONITOR-HOURLY-AI-MANUAL-RERUN-SESSION-001`

## Decision

Scheduled intraday AI analysis remains limited to enabled trend-monitor
symbols during their regular exchange sessions. An authenticated, explicit
manual request MAY rerun an already stored hourly checkpoint outside regular
exchange hours.

This exception is limited to the selected report's existing market, symbol,
trade date, `window_end`, and `data_cutoff`. It does not create an off-session
checkpoint, extend the cutoff, admit an unmonitored symbol, or alter hourly
scheduling.

## Authority

The user explicitly selected the anytime-manual-rerun option on 2026-08-02
after the conflict with the session-only scheduled rule was presented. This
decision controls `REQ-DOW-MONITOR-HOURLY-AI-MANUAL-RERUN-001`; all other
session, data-boundary, worker-isolation, and signal-isolation rules remain in
force.
