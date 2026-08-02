# Dow Monitor Hourly AI Manual Rerun Acceptance

Status: pending implementation

Requirement: `REQ-DOW-MONITOR-HOURLY-AI-MANUAL-RERUN-001`

Acceptance requires executable and observed evidence that one confirmed action
targets the selected hourly checkpoint, persists one deduplicated asynchronous
request, and runs through the independent AI worker even outside regular
exchange hours. The regenerated input must retain the original trade date,
checkpoint, and cutoff and exclude every later observation.

A successful validated result must replace the same logical report and
increment its attempt while history continues to show one checkpoint. Queue,
running, and failure states must leave the prior report readable. Failure must
be retryable and must not overwrite the prior report.

The action must be absent for legacy 30-minute reports and usable on desktop
and narrow mobile layouts. Verification must independently confirm that 3018,
19912, realtime interpretation, formal signals, minute persistence, and the
market-data WebSocket are not coupled to model execution. Passing status will
be recorded only after these behaviors and the required test/build commands
have been observed.
