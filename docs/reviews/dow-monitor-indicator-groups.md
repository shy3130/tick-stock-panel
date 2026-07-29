# Dow monitor grouped-indicator independent review

Review date: 2026-07-29. Result: **PASS**.

| Requirement | Implementation | Executable test | Semantic evidence | Result |
| --- | --- | --- | --- | --- |
| `REQ-DOW-MONITOR-INDICATOR-GROUPS-LAYOUT-001` | `DowMonitorList.tsx` headers; `DowMonitor.tsx` market/page state | `DowMonitorList.test.tsx > renders grouped columns with real-time and stable labels`; `DowMonitor.test.tsx > shows three exclusive markets, twenty rows, and subscribes only the current page`; `> changes the WebSocket subscription with pagination` | Fresh A/HK/US pages have nine headers/four two-line groups; 45-symbol fixture proves page-one and page-two 20-row behavior. | PASS |
| `REQ-DOW-MONITOR-LIVE-OBSERVATION-METRICS-001` | `monitorListPresentation.ts`: `realtimeMomentum1m`, `depthPressurePct`, `dayRangeDistances`, `volumeSpeed` | `monitorListPresentation.test.ts > derives 1m momentum, day-range distance, and five-level depth pressure independently`; `> projects volume speed only within the valid 1m observation window` | Production raw WS formulas plus explicit delayed/missing fixture inputs and per-field `实时` render checks; next-minute-at-70s fixture proves minute identity is required. | PASS |
| `REQ-DOW-MONITOR-STABLE-DECISION-METRICS-001` | `monitorListPresentation.ts`: `stableState`, `control`, `relativeVolume`, `completedBars`, `momentum`, `atr14Pct`, `confirmedTimeframes` | `monitorListPresentation.test.ts > rejects forming or provisional stable metrics and never falls back to 5m`; `> uses only completed 15m/30m bars for channel and momentum`; `> derives stable grouped decision metrics from completed bars and decisions` | Literal fixtures prove both forming and provisional 15m values fall back to valid 30m values, and populated 5m-only values remain ineligible. | PASS |
| `REQ-DOW-MONITOR-INDICATOR-SIGNAL-BOUNDARY-001` | `monitorListPresentation.ts`: `formalSignal`, `warningSignal`, `deriveMonitorRow`; `DowMonitor.tsx` wiring | `monitorListPresentation.test.ts > does not let realtime depth change a formal BUY signal`; `> keeps the newest persisted formal signal and timestamp even when data is stale`; `DowMonitor.test.tsx > updates real-time price without changing the persisted signal` | Opposite five-level books have opposite pressure signs but identical persisted formal signal; live snapshot window is observational. | PASS |

Independent review confirms the release serves the rebuilt static index and grouped DowMonitor chunk from `/app/static`; no container mount masks that path. The previously observed legacy layout was a browser tab that had not reloaded after deployment, not candidate image behavior.

Round 2 verification: the exact 20-test presentation/list command passed; `python -m pytest tests/spec_contracts/test_dow_monitor_list_websocket_contract.py tests/spec_contracts/test_realtime_frontend_contract.py -q` passed 3; `pnpm --dir frontend build` passed. `python scripts/check_spec_compliance.py` remains limited to its two documented baseline findings.

Historical 21/2 subset is superseded, has no retained exact command, and is not acceptance evidence.

Final broad-review requirements-to-evidence review: authority decision
`DEC-20260729-DOW-MONITOR-CONTROL-FALLBACK-001` aligns the older
authoritative specification with the approved design (15m → 30m only).
Traceability remains contract-only as explicitly ruled: every affected
requirement directly names only
`tests/spec_contracts/test_dow_monitor_list_websocket_contract.py`. The
focused five-file suite passed 40 tests; three contracts and five backend
WebSocket tests passed; the build passed; compliance output remained limited
to the two recorded baseline findings.

```powershell
pnpm --dir frontend exec vitest run src/components/dow-monitor/monitorListPresentation.test.ts src/components/dow-monitor/DowMonitorList.test.tsx src/components/dow-monitor/DowMonitorDetailPanel.test.tsx src/pages/DowMonitor.test.tsx src/lib/realtimeMarketData.test.ts # 40
python -m pytest tests/spec_contracts/test_dow_monitor_list_websocket_contract.py tests/spec_contracts/test_realtime_frontend_contract.py -q # 3
Push-Location backend; python -m pytest tests/test_realtime_websocket.py -q; Pop-Location # 5
pnpm --dir frontend exec vitest run src/components/dow-monitor/monitorListPresentation.test.ts src/components/dow-monitor/DowMonitorList.test.tsx # 20 (15+5)
```
