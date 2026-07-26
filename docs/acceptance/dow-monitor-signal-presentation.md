# Dow Monitor Signal Presentation Acceptance

Status: pending implementation verification

Requirements:

- `REQ-DOW-MONITOR-STRICT-DOUBLE-BREAK-001`
- `REQ-DOW-MONITOR-CHINESE-MARKER-001`

Acceptance requires:

- executable mapping tests for direct, primary, buy retest, sell retest, and
  incomplete retest paths;
- executable assertions that pin labels are `B` and `S`, while hover titles
  remain `买点` and `卖点`;
- a successful production frontend build;
- production browser evidence for the NBIS 15-minute cross-session buy signal
  at 2026-07-21 09:45 America/New_York;
- confirmation that mini and detail charts show the same signal set.
