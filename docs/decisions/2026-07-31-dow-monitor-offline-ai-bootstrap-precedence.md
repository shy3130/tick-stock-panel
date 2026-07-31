# Dow Monitor Offline AI Bootstrap Precedence

Decision ID: `DEC-20260731-DOW-MONITOR-OFFLINE-AI-BOOTSTRAP-001`

Approved by the user on 2026-07-31.

`SPEC-DOW-MONITOR-HALF-HOUR-AI-ANALYSIS-001` remains authoritative except for
the bounded offline-recovery scope resolved here.

Startup exception: exactly one latest completed checkpoint before `created_at` is eligible for bounded offline recovery.

Normal checkpoint rule: every later completed checkpoint on or after `created_at` may use bounded offline recovery when canonical minute results are missing.

Older checkpoints before the eligible startup checkpoint remain prohibited.

Boundary rule: startup requires `window_end < created_at`; normal scheduling uses `window_end >= created_at`.

Startup gate: a pre-created checkpoint is eligible only when `calendar.is_regular_session_time(market, created_at)` is true.
