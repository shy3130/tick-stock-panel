status: pending

# Dow Monitor Offline AI Bootstrap Acceptance

Applies to:

- `REQ-DOW-MONITOR-HALF-HOUR-AI-OFFLINE-BOOTSTRAP-001`
- `REQ-DOW-MONITOR-HALF-HOUR-AI-BOOTSTRAP-ISOLATION-001`

Semantic acceptance will close only after an independently observed run proves
that a symbol added at 10:17 selects 10:00 as its one eligible startup
checkpoint, materializes bounded offline minute results, and performs one
analysis using data no later than that checkpoint. The evidence must also show
that 09:30 and all older checkpoints remain uncalled; a duplicate logical key
does not call the model again; later normal checkpoints follow the existing
`created_at` rule; and insufficient offline data persists `insufficient_data`
without an invented result or model call.

The same evidence must prove that later normal checkpoints on or after
`created_at` may use the same bounded offline recovery only when their canonical
minute results are missing; this does not expand eligibility to older
checkpoints before the single startup checkpoint.

Isolation evidence must show that the bounded work uses the existing minute
result materializer and ClickHouse offline inputs, stays per-symbol and
per-checkpoint, preserves model concurrency one, enforces the 500-row and
15-second limits, and cannot execute in the 3018 WebSocket or realtime-render
paths. Formal buy/sell signals, realtime key interpretation, WebSocket
ingestion, and minute realtime append results must remain unchanged.
