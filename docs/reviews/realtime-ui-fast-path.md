# Realtime UI Fast Path Independent Review

Status: independent review complete; one runtime observation pending

Requirements:

- `REQ-REALTIME-UI-GATEWAY-001`
- `REQ-REALTIME-UI-FALLBACK-001`

## Requirements-to-evidence review

`REQ-REALTIME-UI-GATEWAY-001` is traced to
`backend/app/api/realtime.py`, `backend/app/services/realtime_market_data.py`,
and `backend/app/main.py`. Executable tests and candidate/production
observations establish dynamic subscription, Redis snapshot hydration,
symbol-scoped ordered updates, dataset/depth validation, bounded latest-state
delivery, heartbeat, disconnect cleanup, Origin enforcement, and the filtered
protocol schema. Production inspection confirms the configured same-service
`/ws/realtime` endpoint and no Longbridge credentials in payloads.

`REQ-REALTIME-UI-FALLBACK-001` is traced to the shared
`frontend/src/lib/realtimeMarketData.ts` client and its stock/Dow overlay
consumers. Executable tests establish stream/session ordering, jittered capped
reconnect, HTTP fallback timing, recovery hydration, market-session-aware
staleness, and preservation of SSE. Production logs independently show the
existing SSE route and the new WebSocket route active together.

An isolated bad-Redis candidate produced an explicit `fallback` protocol
message while HTTP remained healthy. A subsequent valid-Redis production
connection hydrated a `TSLA.US` snapshot at sequence 12469 and accepted a
same-stream update at sequence 12488. This is semantic failure/recovery
evidence, not a downstream metric or fixture.

The production image derives from the previously deployed shared Dow-list
image, and the shared-list component tests and production bundle checks pass.
This avoids regressing `REQ-DOW-TREND-STRATEGY-UI-001`.

## Review conclusion

The implementation and deployment evidence satisfy every static and exercised
runtime clause. Unconditional acceptance is intentionally withheld until a
continuous ten-minute observation during a regular market session confirms
the open-session freshness behavior. No downstream count, golden file, or
premarket observation is being used as a substitute.
