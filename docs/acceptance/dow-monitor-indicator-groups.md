# Dow monitor grouped-indicator semantic acceptance

Status: **PASS — production acceptance completed 2026-07-29.**

Applicable requirements: `REQ-DOW-MONITOR-INDICATOR-GROUPS-LAYOUT-001`, `REQ-DOW-MONITOR-LIVE-OBSERVATION-METRICS-001`, `REQ-DOW-MONITOR-STABLE-DECISION-METRICS-001`, and `REQ-DOW-MONITOR-INDICATOR-SIGNAL-BOUNDARY-001`.

## Executable evidence

| Command | Result |
| --- | --- |
| specified focused Vitest suites | 5 files, 39 passed |
| specified specification/realtime contract pytest suites | 3 passed |
| `backend/tests/test_realtime_websocket.py` | 5 passed; one existing `asyncio_mode` configuration warning |
| `pnpm --dir frontend build` | exit 0; `index-CTXmgw0J.js`, `DowMonitor-5MeBqeQ7.js`, `realtimeMarketData-CnBtfOxI.js` |
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
