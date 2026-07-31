# Dow Monitor Mobile Layout and Version Refresh

## Authority

- Status: authoritative
- Approved by: explicit user approval of mobile Scheme B on 2026-07-31
- Specification ID: `SPEC-DOW-MONITOR-MOBILE-VERSION-REFRESH-001`
- Requirements:
  - `REQ-DOW-MONITOR-MOBILE-COMPACT-LIST-001`
  - `REQ-DOW-MONITOR-FRONTEND-VERSION-REFRESH-001`

## REQ-DOW-MONITOR-MOBILE-COMPACT-LIST-001

Below 768 CSS pixels, every monitor list item MUST present, in scan order:

1. stock name/code;
2. current price/change;
3. the background-free mini daily trend;
4. the complete deterministic key interpretation.

The remaining raw indicator columns MUST be hidden from the mobile list but
MUST remain reachable through the existing detail view. The page MUST NOT
produce viewport-level horizontal overflow at 390 CSS pixels. Row selection,
pause/resume, removal, pagination, and second-click detail collapse MUST remain
available.

At 768 CSS pixels and above, the existing complete desktop table and all its
indicator columns remain authoritative.

## REQ-DOW-MONITOR-FRONTEND-VERSION-REFRESH-001

The frontend bundle and backend runtime MUST expose the same immutable build
identifier. An open page MUST check the uncached backend build identifier on
startup and every 60 seconds.

When the page is visible and the build differs, it MUST present a non-blocking
refresh action. When the page is hidden, it MAY refresh automatically only
when no mutation is active and no dialog is open. A failed version check MUST
NOT affect authentication, market data, WebSocket state, or current content.
The same remote build MUST NOT create repeated prompts.
