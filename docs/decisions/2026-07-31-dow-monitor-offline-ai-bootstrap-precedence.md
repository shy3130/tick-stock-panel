# Dow Monitor Offline AI Bootstrap Precedence

Decision ID: `DEC-20260731-DOW-MONITOR-OFFLINE-AI-BOOTSTRAP-001`

Approved by the user on 2026-07-31.

`SPEC-DOW-MONITOR-HALF-HOUR-AI-ANALYSIS-001` remains authoritative for normal
half-hour checkpoints. `SPEC-DOW-MONITOR-OFFLINE-AI-BOOTSTRAP-001` supersedes
only the clause that prohibits a model call for the latest completed checkpoint
before a newly monitored symbol's `created_at`.

Exactly one such startup checkpoint is eligible. Older checkpoints remain
prohibited, and every later checkpoint continues under the existing normal
checkpoint rule.
