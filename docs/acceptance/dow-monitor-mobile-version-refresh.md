# Dow Monitor Mobile and Version Refresh Acceptance

Requirements:

- `REQ-DOW-MONITOR-MOBILE-COMPACT-LIST-001`
- `REQ-DOW-MONITOR-FRONTEND-VERSION-REFRESH-001`

Status: local semantic acceptance passed; production acceptance pending.

Local semantic evidence:

- `frontend/src/components/dow-monitor/DowMonitorList.test.tsx` renders the real
  desktop table and mobile list from the same items and verifies mobile order,
  compact interpretation, controls, and detail selection.
- `frontend/src/pages/DowMonitor.test.tsx` verifies responsive header controls,
  20-row paging, live-state projection, market switching, and inline details.
- `frontend/src/components/AppVersionGuard.test.tsx` verifies the visible
  update prompt, hidden safe auto-reload, and failed-check isolation.
- `frontend/src/lib/appVersion.test.ts` verifies build comparison plus mutation
  and dialog deferral.
- `tests/backend/test_app_version.py` verifies uncached backend build metadata.
- `tests/frontend/test_dow_monitor_mobile_version.py` executes the real frontend
  component contracts from the repository test suite.
- `pnpm build` completed successfully on 2026-07-31.

Production evidence still required:

- 390x844 browser verification on `192.168.10.28:3018`, including no
  page-level horizontal overflow and retained detail access;
- build mismatch verification after the new image replaces the old image.
