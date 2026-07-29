# Dow monitor control-period fallback authority decision

Decision ID: `DEC-20260729-DOW-MONITOR-CONTROL-FALLBACK-001`

Status: approved by explicit user ruling on 2026-07-29.

Scope:

- `REQ-DOW-MONITOR-LIST-INDICATORS-001`
- `REQ-DOW-MONITOR-STABLE-DECISION-METRICS-001`

The approved grouped-indicator design is authoritative for the control-period
fallback boundary: control-line distance and relative volume independently
try a stable 15-minute snapshot, then a stable 30-minute snapshot, and
otherwise return missing. A 5-minute snapshot is never eligible for either
fallback chain.

This decision resolves the older wording in
`docs/specs/dow-monitor-list-websocket.md` that allowed control-line distance
to continue from 30 minutes to 5 minutes. The authoritative specification is
amended in the same change. No requirement ID or direct executable-test path
changes: the affected requirements remain directly traced only to
`tests/spec_contracts/test_dow_monitor_list_websocket_contract.py` under the
previously approved contract-only test-authority ruling.
