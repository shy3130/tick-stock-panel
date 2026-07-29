# Dow monitor grouped-indicator semantic acceptance

Status: **PASS — production acceptance completed 2026-07-29.**

Applicable requirements: `REQ-DOW-MONITOR-INDICATOR-GROUPS-LAYOUT-001`, `REQ-DOW-MONITOR-LIVE-OBSERVATION-METRICS-001`, `REQ-DOW-MONITOR-STABLE-DECISION-METRICS-001`, and `REQ-DOW-MONITOR-INDICATOR-SIGNAL-BOUNDARY-001`.

## Executable evidence

| Command | Result |
| --- | --- |
| specified focused Vitest suites | 5 files, 40 passed |
| specified specification/realtime contract pytest suites | 3 passed |
| `backend/tests/test_realtime_websocket.py` | 5 passed; one existing `asyncio_mode` configuration warning |
| `pnpm --dir frontend build` | exit 0; final broad-review build produced `index-DKdW77Ki.js`, `DowMonitor-BTJPiTJw.js`, `realtimeMarketData-CbJf3qZq.js` |
| `python scripts/check_spec_compliance.py` | only baseline findings: expired `EXC-COLLECTION-MONITOR-PREACCEPTANCE-DEPLOY-001` and legacy detail-toggle test path outside `tests/`; no active grouped-requirement finding |

The local Vite preview returned API HTTP 500 and remained loading, so it was excluded from semantic proof. The authenticated production browser supplied the browser acceptance.

## Production release and serving proof

- candidate image: `tickflow-stock-panel-app:dow-monitor-indicator-groups-20260729-192023`;
- image ID: `sha256:b76900033fbd691ee34f3ab360477734576b731bf7e7295c2680d9ba6e3a8c89`;
- rollback image: `tickflow-stock-panel-app:dow-monitor-change-pct-f34edda-20260729-145154`;
- pre-deploy backup: `/home/alwin/backups/dow-monitor-indicator-groups-predeploy-20260729-192023`;
- served index SHA-256: `bca2680c70a6b59b307ff2fe03b0d591240a4102b7b22fc02fea139a20742270`, equal to candidate `/app/static/index.html` and local `frontend/dist/index.html`;
- served entry: `assets/index-CTXmgw0J.js`, SHA-256 `1efc43b03be17e51bb2b6654e5e66b1596db78de2afd1a6eb674fdff1b4d9c39`;
- grouped chunk: `assets/DowMonitor-5MeBqeQ7.js`, SHA-256 `a596b136a70e6c6c1e0d668b6500cb45021441cb29e0535744a1d587de5fe529`.

The container is `running`, restart count `0`; `/health` returned `{"status":"ok","version":"0.1.86","mode":"none"}`; deployment-window logs had no `ERROR`, `CRITICAL`, or `Traceback`. `dow_monitor_symbols.json` remained `1d5955494b4a74d8ae32bd550e4f744e09c4cab80c85f190acd01f3717bef59e` before and after deployment.

An initial stale pre-deploy Chrome tab displayed its retained legacy JavaScript. Filesystem inspection proved `/app/static` is the backend serving root and is not mounted over; a cache-busting authenticated reload then verified the served candidate bundle. This is the only release retry; no data mounts were changed.

## Raw production data and recomputation

`/ws/realtime` returned `hello/v1` and a full `1347.HK` snapshot after subscribing to `quote`, `depth`, `candlestick` with `depthLevels: 5`. Raw inputs: quote `lastDone=136.8`, `open=137.2`, `high=141.6`, `low=126.2`; minute candle `open=136.8`, `close=136.8`, `volume=339000`; available depth bid `7000`, ask `109000` (the exchange supplied one level per side).

- candle change: `(136.8 - 136.8) / 136.8 × 100 = 0.0000%`;
- five-level request / available-book pressure: `(7000 - 109000) / (7000 + 109000) × 100 = -87.9310%`, rendered `-87.93%`;
- day-high distance: `(141.6 - 136.8) / 136.8 × 100 = 3.5088%`, rendered `3.51%`;
- day-low distance: `(136.8 - 126.2) / 136.8 × 100 = 7.7485%`, rendered `7.75%`.

The fresh authenticated row rendered stable 5m `+0.37%`, 15m `+0.44%`, ATR14 `+1.54%`, and confirmation `0/2`; it rendered missing live volume speed as `--`, not zero. Its formal `买入确认 08:00` was unchanged in two snapshots five seconds apart while the real-time book value was rendered separately.

## Browser semantic acceptance

At the requested 1800×1080 viewport override, `document.documentElement.scrollWidth === clientWidth` (1636 CSS pixels after Chrome chrome), while the table retains its own horizontal scroll container. A cache-busting production reload verified:

- exactly nine headers: stock, price/change, intraday, four grouped columns, signal, and action;
- each grouped header has two lines: trend/position, momentum/speed, volume/funds, breakout/risk;
- one polyline per displayed mini chart (five rows, five polylines);
- A/HK/US each render the nine headers and two-line groups; current lists are 1, 5, and 7 rows respectively, all within the fixed maximum 20 rows/page;
- `查看详情 01347.HK` opens once and the second activation closes it; no modal is used;
- HK formal signals and timestamps were identical over a five-second real-time observation window.

## Independent raw-input recomputation (round 1)

Read-only authenticated production overview for `01347.HK`: completed minute `2026-07-29T15:59:00+08:00`, final 5m closes `136.4` (`15:50`) and `136.9` (`15:55`), final 15m closes `136.3` (`15:30`) and `136.9` (`15:45`). Thus 5m is `(136.9-136.4)/136.4*100=0.3665689%` → `+0.37%`, and 15m is `(136.9-136.3)/136.3*100=0.4402054%` → `+0.44%`.

Final 15m OHLC bars (open/high/low/close) used for ATR14: `11:15 130.3/131.1/128.1/129.1; 11:30 129.1/129.1/127.3/127.5; 11:45 127.4/127.8/127.2/127.3; 13:00 127.4/133.3/126.3/133.0; 13:15 133.0/133.9/130.2/133.5; 13:30 133.3/134.6/131.8/133.2; 13:45 133.2/134.2/131.6/132.5; 14:00 132.5/133.7/131.5/132.9; 14:15 132.9/134.0/132.4/133.9; 14:30 133.8/134.7/133.2/134.4; 14:45 134.3/136.4/134.3/135.8; 15:00 135.9/136.2/135.7/135.9; 15:15 136.0/136.8/136.0/136.5; 15:30 136.5/137.4/136.3/136.3; 15:45 136.2/136.9/135.7/136.9`.

The 14 TR inputs are `1.8,0.6,7.0,3.7,2.8,2.6,2.2,1.6,1.5,2.1,0.5,0.9,1.1,1.2`, sum `29.6`; `(29.6/14)/136.9*100=1.5444015%` → rendered `+1.54%`. The same response has `current_price=136.9`, `vwap_price=132.88026484713833`, `vwap_distance_pct=3.025080629908339`; `(136.9-132.88026484713833)/132.88026484713833*100=3.0250806%`. Its decision has no dominant or confirmation timeframes, therefore live `0/2`; `monitorListPresentation.test.ts > derives stable grouped decision metrics from completed bars and decisions` independently asserts the constructed `2/2` case.

## Deterministic boundary/missing/pagination proof

Adversarial inputs cannot be injected into production. `monitorListPresentation.test.ts > does not let realtime depth change a formal BUY signal` uses a bid-heavy five-level book `300/250` (`+9.0909%`) and ask-heavy book `10/100` (`-81.8182%`) plus a `100→101` 1m candle (`+1.00%`); both retain the same persisted `CONFIRMED BUY`. `> projects volume speed only within the valid 1m observation window` covers valid, too-early, insufficient-12-bar, and `candlestickDelayed` inputs; `DowMonitorList.test.tsx > keeps missing grouped values explicit instead of rendering zeroes` renders `ATR14 --`; `> requires complete active-funds data` maps delayed capital to unconfirmed/null. `DowMonitorList.test.tsx > renders grouped columns with real-time and stable labels` asserts per-field `实时` labels.

The final broad-review regression extends the volume-speed case with a candle
at `09:35:00` and `now=09:36:10`: although the elapsed time is only 70
seconds, the observation is rejected because the two timestamps are not in
the same absolute minute. The original 20-second lower bound and 75-second
staleness bound remain in the implementation. The stable-metric case now
proves both a `FORMING` 15m snapshot and a truthy `provisional` 15m snapshot
are rejected independently by control distance and relative volume; each
uses the valid 30m value, while a populated 5m-only fixture still returns
missing.

Pagination is proven by `DowMonitor.test.tsx > shows three exclusive markets, twenty rows, and subscribes only the current page`, which uses 45 same-market enabled symbols and page-one `1..20`, and `> changes the WebSocket subscription with pagination`, which verifies page-two `21..40`; production 1/5/7 counts are not pagination proof.

Round 2 rerun is `pnpm --dir frontend exec vitest run src/components/dow-monitor/monitorListPresentation.test.ts src/components/dow-monitor/DowMonitorList.test.tsx` = 20 (15 presentation, 5 list). It adds `degrades each delayed realtime feed independently`: delayed candle clears 1m momentum while other feeds remain valid; delayed depth clears only depth pressure; delayed quote clears both day-high and day-low distances. The list test verifies the visible cyan live badge immediately precedes 1m and that the breakout cell has two such live badges for high and low.

Historical 21/2 subset is superseded, has no retained exact command, and is not acceptance evidence.

```powershell
pnpm --dir frontend exec vitest run src/components/dow-monitor/monitorListPresentation.test.ts src/components/dow-monitor/DowMonitorList.test.tsx src/components/dow-monitor/DowMonitorDetailPanel.test.tsx src/pages/DowMonitor.test.tsx src/lib/realtimeMarketData.test.ts # 40
python -m pytest tests/spec_contracts/test_dow_monitor_list_websocket_contract.py tests/spec_contracts/test_realtime_frontend_contract.py -q # 3
Push-Location backend; python -m pytest tests/test_realtime_websocket.py -q; Pop-Location # 5
pnpm --dir frontend exec vitest run src/components/dow-monitor/monitorListPresentation.test.ts src/components/dow-monitor/DowMonitorList.test.tsx # 20 (15+5)
```
