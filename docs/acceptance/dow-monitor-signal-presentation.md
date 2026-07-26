# Dow Monitor Signal Presentation Acceptance

Status: accepted

Requirements:

- `REQ-DOW-MONITOR-STRICT-DOUBLE-BREAK-001`
- `REQ-DOW-MONITOR-CHINESE-MARKER-001`
- `REQ-DOW-MONITOR-LINE-TOGGLE-001`

Acceptance requires:

- executable mapping tests for direct, primary, buy retest, sell retest, and
  incomplete retest paths;
- executable assertions that pin labels are `B` and `S`, while hover titles
  remain `买点` and `卖点`;
- a successful production frontend build;
- production browser evidence for the NBIS 15-minute cross-session buy signal
  at 2026-07-21 09:45 America/New_York;
- confirmation that mini and detail charts show the same signal set.
- confirmation that the expanded chart exposes one line-visibility switch
  shared by intraday and daily timeframes without hiding buy/sell markers.

## Evidence

- The focused frontend gate passed 50 tests across
  `DowMonitorDetailDialog.test.tsx` and `DowMonitor.test.tsx`.
- The production frontend build compiled 2,706 modules successfully.
- The generated Dow monitor chunk contains
  `FIRST_ACCEPTANCE_HIGH_BROKEN`, stable `B`/`S` pin labels, and Chinese hover
  titles.
- Production serves `assets/index-B9TXovkQ.js` from immutable image
  `tickflow-stock-panel-app:dow-monitor-716674ba8b78`.
- The NBIS 15-minute payload contains a strict cross-session buy confirmation
  at `2026-07-21T09:45:00-04:00` with trigger path `TWO_BAR_RETEST` and reason
  code `FIRST_ACCEPTANCE_HIGH_BROKEN`.
- Chrome verification showed the restored red `B` marker in both the mini and
  detail chart signal sets. No question-mark pin or application error was
  present.
- After restarting the production container, the image and frontend entry
  remained unchanged and the NBIS detail chart retained the signal set.
- The focused frontend gate passed 51 tests after adding the line-toggle
  regression case, and the production frontend build again compiled 2,706
  modules successfully.
- Production serves `assets/index-4ZGFFqc6.js` from immutable image
  `tickflow-stock-panel-app:dow-monitor-6ac84fa83c36`.
- Chrome verification on the NBIS detail chart found exactly one
  `显示趋势线和压力线` switch, checked by default. Disabling it preserved the
  visible sell signal, switching to daily K kept the disabled state, and
  re-enabling it restored line visibility.
