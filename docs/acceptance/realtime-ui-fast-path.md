# Realtime UI Fast Path Acceptance

Status: deployed; conditional acceptance

Requirements:

- `REQ-REALTIME-UI-GATEWAY-001`
- `REQ-REALTIME-UI-FALLBACK-001`

## Build and protocol evidence

- Source commit: `804c4cd0847ae73cc6e04d1f8a4a7bee5cb1b072`.
- Protocol specification SHA-256:
  `8b388fdaf37ccc889e934bc11123b46be32be5056a536e6dc5a4d90582dbb803`.
- Gateway API SHA-256:
  `7cb00c4a2aedb7b87b9d621109791f75fc8ebf585b92423dc86bc64f36959c95`.
- Gateway service SHA-256:
  `55b4bf7566330a85a4faffc2157f806400c01a53e47069f3984443882ecdf2d0`.
- Shared frontend client SHA-256:
  `bc6808570d9365da71efcee94334833cb289b3c8e05f2b2f8661aadfcd0573c6`.
- The merged frontend build compiled 2,707 modules successfully.
- Realtime gateway/backend tests: 10 passed.
- Shared client, overlay, session, and Dow-monitor tests: 33 passed.
- Root specification contract gate: 8 passed.
- Earlier integrated component gate: 54 passed, plus 7
  `useDowMonitor` tests.

## Candidate and production observations

- Candidate image:
  `sha256:2cf64f5ae46dc502889bfba155ecfac784f688c33572ba07c2511323c7719424`.
- Production image:
  `sha256:1400058bee1b737f3482307456f1b6a35512da46bee4bef9a0cdefed472c2b0d`.
- Production base/rollback image:
  `sha256:4d92f7c7569ebe43257be9b985095477f694c5f302f7822dc1696781e279d466`.
- Production URL `http://192.168.10.28:3018` returned HTTP 200 for
  `/health`, `/`, `/dow-monitor`, and `/screener`.
- A production-origin WebSocket subscription for `TSLA.US` received protocol
  `v1` snapshot sequence `12469`, then an update in the same stream at
  sequence `12488`.
- The running collector reported Redis connected, zero Redis publish failures,
  480 latest symbols, and callback-to-publish p95 of 130.98 ms. The Redis
  latest-state key had the configured 86,400-second TTL.
- The existing SSE endpoint remained active in the production log while the
  WebSocket endpoint was in use.

## Failure and fallback observations

- An isolated candidate was started with an unreachable Redis endpoint.
  HTTP `/health` and `/` continued to return 200.
- Its WebSocket returned the explicit protocol message
  `{"type":"fallback","version":"v1","reason":"realtime_redis_unavailable"}`.
- After returning to the valid production Redis endpoint, snapshot hydration
  and ordered updates succeeded without changing the durable ClickHouse path.
- Unit tests cover the three-second initial fallback, 45-second liveness
  fallback, reconnect/session reset, sequence deduplication, and open-session
  Quote/Depth/Candlestick staleness thresholds.

## Outstanding observation

The deployment occurred outside a regular CN/HK/US session. A continuous
ten-minute regular-session observation is still required before changing this
record to unconditional acceptance. Premarket transport observations are not
treated as a substitute for that lower-layer semantic acceptance.

## 2026-07-27 Hong Kong alias recovery

- Frontend source commit: `6def4d9`.
- Production release commit: `4a18b7883211803ee9bb0907a396987f7aa9c3e9`.
- Production image: `tickflow-stock-panel-app:dow-monitor-4a18b7883211`.
- Production entrypoint: `assets/index-BF5RdvZR.js`.
- The regression test first failed because `01347.HK` and `0981.HK` were sent
  unchanged, while the collector keys were `1347.HK` and `981.HK`. After the
  implementation, the focused client suite passed 9 tests and the integrated
  frontend suite passed 75 tests.
- A production-origin WebSocket subscription received snapshots for
  `1347.HK`, `981.HK`, `2714.HK`, and `3759.HK`, four subsequent updates, and
  one 15-second heartbeat over an 18-second observation. It received no
  fallback message.
- The production health endpoint returned HTTP 200 and the running container
  exposed revision `4a18b7883211803ee9bb0907a396987f7aa9c3e9`.
- Browser asset verification loaded the new entrypoint without console
  warnings or errors. Card-level browser acceptance remains pending because
  the pre-existing browser session was invalidated by the production restart
  and the protected monitor APIs returned HTTP 401.
