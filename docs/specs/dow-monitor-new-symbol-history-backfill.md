# Dow Monitor New-Symbol History Backfill

## Authority

- Status: authoritative
- Approved by: explicit user approval on 2026-07-31
- Specification ID: `SPEC-DOW-MONITOR-NEW-SYMBOL-HISTORY-BACKFILL-001`
- Requirement: `REQ-DOW-MONITOR-NEW-SYMBOL-HISTORY-BACKFILL-001`
- Lower-layer dependency:
  `REQ-LONGBRIDGE-DOW-MONITOR-HISTORY-WARMUP-PROVIDER-001`

## REQ-DOW-MONITOR-NEW-SYMBOL-HISTORY-BACKFILL-001

Adding a monitored symbol MUST return without synchronously reading historical
market data. TickFlow MUST expose the separate collector warmup state for every
overview symbol without performing one status-file read per symbol.

The status reader MUST tolerate a missing, malformed, or stale shared status
file and MUST NOT make the overview endpoint fail. It MUST normalize equivalent
Hong Kong symbol forms when matching collector status.

The exposed state MUST distinguish pending, queued, running, rebuilding,
completed, partial, failed, and unknown states; include progress, missing
timeframes, last error, and update time; and MUST remain independent of formal
signals and real-time interpretation.

This requirement consumes but does not replace lower-layer acceptance that the
collector reuses one `QuoteContext`, performs history I/O off the WebSocket
callback/control path, and writes only missing ClickHouse rows.

## Semantic acceptance

Acceptance requires executable evidence for valid, missing, malformed, stale,
and Hong Kong alias status data; one batch read per overview; and an immediate
symbol-add response whose execution path cannot access the market gateway.
