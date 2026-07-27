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

## 2026-07-27 requirements-to-evidence addendum

The Hong Kong alias defect was traced from the authoritative requirement to
the shared client, its failing behavioral test, the rebuilt production asset,
and a production protocol observation. The client now canonicalizes only
one-to-five-digit `.HK` aliases, preserves six-digit `.SH`/`.SZ` symbols, and
maps canonical stream state back to the original display key. Removing any of
those mappings makes the regression test fail.

Lower-layer acceptance is independent of the Dow card rendering: the
production WebSocket itself returned all four subscribed HK snapshots,
updates, and a heartbeat with no fallback. The integrated Dow component suite
and production asset hash establish the higher-layer implementation path, but
card-level browser acceptance is explicitly not claimed until a fresh
authenticated session is available.

The previously recorded freshness-threshold discrepancy between the indexed
5/90-second wording and the current 120/180-second implementation remains
outside this alias repair. It is not used as evidence for this change and
continues to prevent unconditional acceptance of the complete requirement.
