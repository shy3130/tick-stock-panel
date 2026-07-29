# Dow monitor grouped-indicator independent review

Review date: 2026-07-29. Result: **PASS**.

| Requirement | Implementation | Executable test | Semantic evidence | Result |
| --- | --- | --- | --- | --- |
| `REQ-DOW-MONITOR-INDICATOR-GROUPS-LAYOUT-001` | `monitorListPresentation.ts`, `DowMonitorList.tsx`, `DowMonitor.tsx` | 39 focused frontend tests; 3 contracts | Fresh production A/HK/US pages have nine headers; four grouped headers each have two lines; rows remain at or below 20/page | PASS |
| `REQ-DOW-MONITOR-LIVE-OBSERVATION-METRICS-001` | same | frontend, contracts, 5 WebSocket tests | Production `depthLevels: 5` subscription, raw quote/depth/candle formulas, `实时` fields and `--` missing value verified | PASS |
| `REQ-DOW-MONITOR-STABLE-DECISION-METRICS-001` | same | frontend and contracts | Fresh HK row distinguishes stable 5m/15m, ATR14 and fixed `0/2` confirmation from live observations | PASS |
| `REQ-DOW-MONITOR-INDICATOR-SIGNAL-BOUNDARY-001` | same | frontend and contracts | Formal signal and timestamp stayed identical in two five-second production snapshots while depth was rendered separately | PASS |

Independent review confirms the release serves the rebuilt static index and grouped DowMonitor chunk from `/app/static`; no container mount masks that path. The previously observed legacy layout was a browser tab that had not reloaded after deployment, not candidate image behavior.
