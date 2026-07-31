# Dow Monitor Daily Payload Safety

## Authority

- Status: authoritative
- Approved by: explicit user approval on 2026-07-31
- Specification ID: `SPEC-DOW-MONITOR-DAILY-PAYLOAD-SAFETY-001`
- Requirement: `REQ-DOW-MONITOR-DAILY-PAYLOAD-SAFETY-001`

## REQ-DOW-MONITOR-DAILY-PAYLOAD-SAFETY-001

Before any Dow-engine evaluation request is serialized, TickFlow MUST normalize
the supplied bars and MUST exclude rows with a missing or non-finite OHLCV
value, a non-positive OHLC value, a negative volume, an invalid OHLC
relationship, an empty timestamp, or a duplicate timestamp superseded by the
latest valid row.

Missing values MUST remain missing and MUST NOT be replaced by zero. The
serialized request body MUST contain only standards-compliant finite JSON
numbers.

When fewer than two valid bars remain for one timeframe, TickFlow MUST classify
that timeframe as `ANALYSIS_PAUSED` with a machine-readable
`HISTORY_INCOMPLETE` reason. The insufficient timeframe MUST NOT make other
timeframes fail or suppress their valid results.

This requirement changes only the evaluation input boundary. It MUST NOT change
formal signal semantics, real-time WebSocket fields, or persisted completed
signals.

## Semantic acceptance

Acceptance requires executable evidence that:

1. the RNG.US-style non-finite daily row never reaches the HTTP transport;
2. valid rows preserve their original finite OHLCV values;
3. duplicate and invalid rows are handled deterministically;
4. an insufficient daily timeframe does not prevent another valid timeframe
   from being evaluated and stored.
