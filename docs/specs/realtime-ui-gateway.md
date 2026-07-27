# Realtime UI Gateway

## REQ-REALTIME-UI-GATEWAY-001

The production `tickflow-stock-panel` FastAPI service on port 3018 MUST expose
a same-service WebSocket endpoint at `/ws/realtime`.

The gateway MUST:

- support dynamic subscribe and unsubscribe requests;
- accept only Quote, Depth, and Candlestick datasets;
- reject malformed symbols and subscription sets above 500 symbols;
- initialize a new subscription from the Redis latest-state keys;
- fan out Pub/Sub updates only to clients subscribed to the affected symbol;
- return one depth level by default and at most ten levels when requested;
- use a bounded latest-state outbound buffer for every client;
- replace obsolete pending messages for slow clients;
- send protocol heartbeats every 15 seconds;
- clean up subscriptions and pending state when a client disconnects;
- enforce the configured Origin allowlist;
- never expose Longbridge credentials, account data, environment variables,
  or unfiltered raw payloads.

## REQ-REALTIME-UI-FALLBACK-001

The production TickFlow single-stock detail/preview flow and Dow multi-stock
monitor MUST share one frontend WebSocket client implementation.

The client MUST:

- use protocol version `v1`;
- deduplicate and order updates by `streamId` and `sequence`;
- apply Quote, Depth, and current one-minute Candlestick updates without a
  manual page refresh;
- treat a changed `streamId` as a new collector session;
- reconnect with jittered exponential backoff capped at 15 seconds;
- enable the existing HTTP/ClickHouse path when the first connection has not
  succeeded within three seconds;
- enable the same fallback when no heartbeat or update is received for
  45 seconds;
- hydrate from the Redis-backed snapshot after reconnecting;
- stop fallback polling only after the realtime snapshot is accepted;
- normalize Hong Kong display aliases with leading zeros to the collector's
  canonical Redis/WebSocket symbol while preserving the original display key;
- mark Quote or Depth delayed after five seconds without an update during an
  open market session;
- mark the current one-minute Candlestick delayed after 90 seconds during an
  open market session;
- avoid treating normal closed-market inactivity as a connection failure.

The existing TickFlow SSE client MUST remain active for alerts, strategy
results, review progress, and existing invalidation events.
