# Task 6 release-verification report

## Round-1 evidence correction

Authenticated read-only overview capture for `01347.HK` supplied exact final-bars/OHLC/TR/VWAP inputs. Recomputed 5m `+0.3665689%`, 15m `+0.4402054%`, ATR14 `+1.5444015%`, VWAP distance `+3.0250806%`, and live confirmation `0/2`. The acceptance record now distinguishes this live data from deterministic adversarial depth proof (`+9.0909%` / `-81.8182%`, unchanged persisted CONFIRMED BUY), and cites the actual 45-symbol page-one/page-two pagination tests. Fresh evidence rerun: 21 frontend tests and 2 contracts passed.

The prior 21/2 subset has no retained exact command and is superseded; it is not relied upon. Exact reproducible commands are:
```powershell
pnpm --dir frontend exec vitest run src/components/dow-monitor/monitorListPresentation.test.ts src/components/dow-monitor/DowMonitorList.test.tsx src/components/dow-monitor/DowMonitorDetailPanel.test.tsx src/pages/DowMonitor.test.tsx src/lib/realtimeMarketData.test.ts
python -m pytest tests/spec_contracts/test_dow_monitor_list_websocket_contract.py tests/spec_contracts/test_realtime_frontend_contract.py -q
Push-Location backend; python -m pytest tests/test_realtime_websocket.py -q; Pop-Location
pnpm --dir frontend exec vitest run src/components/dow-monitor/monitorListPresentation.test.ts src/components/dow-monitor/DowMonitorList.test.tsx
```

Date: 2026-07-29. Result: **PASS**.

Focused tests: 39 Vitest + 3 contract pytest + 5 backend WebSocket pytest all passed. Build passed. The specification checker produced only the two documented baseline findings: expired collection-monitor exception and legacy detail-toggle test path.

Release: `tickflow-stock-panel-app:dow-monitor-indicator-groups-20260729-192023`, `sha256:b76900033fbd691ee34f3ab360477734576b731bf7e7295c2680d9ba6e3a8c89`; rollback image `tickflow-stock-panel-app:dow-monitor-change-pct-f34edda-20260729-145154`; backup `/home/alwin/backups/dow-monitor-indicator-groups-predeploy-20260729-192023`; symbols SHA-256 `1d5955494b4a74d8ae32bd550e4f744e09c4cab80c85f190acd01f3717bef59e` unchanged.

The image is layered only from the running image plus `frontend/dist` at `/app/static`. The production server served the candidate index SHA-256 `bca2680c70a6b59b307ff2fe03b0d591240a4102b7b22fc02fea139a20742270`, entry `index-CTXmgw0J.js` SHA-256 `1efc43b03be17e51bb2b6654e5e66b1596db78de2afd1a6eb674fdff1b4d9c39`, and grouped chunk `DowMonitor-5MeBqeQ7.js` SHA-256 `a596b136a70e6c6c1e0d668b6500cb45021441cb29e0535744a1d587de5fe529`.

An initial deployment was rolled back after a stale pre-deploy browser tab displayed old JavaScript. Diagnosis proved that `/app/static` is not masked by a mount and that the candidate index/chunk were correct. The same candidate was redeployed, then fresh cache-busting authenticated pages established the semantic evidence: nine headers, four two-line groups, A/HK/US layouts, maximum-20 paging, detail toggle, raw WebSocket formulas, and stable formal signals over a five-second check. Candidate is running, health OK, restart count 0, and logs have no deployment-window errors.
