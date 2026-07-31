# Independent Review: Dow Monitor Daily Payload Safety

Requirement: `REQ-DOW-MONITOR-DAILY-PAYLOAD-SAFETY-001`

Status: local requirements-to-evidence review passed; production evidence
pending.

The final reviewer must trace the authoritative requirement independently
through the sanitizer, client boundary, per-timeframe error handling,
executable tests, and production evidence. A passing overview response or UI
snapshot is not sufficient evidence that the underlying bars are valid.

## Local independent review

- Requirement authority is indexed without a conflicting specification.
- `sanitize_engine_bars` rejects missing/non-finite values without inventing
  zeros and enforces literal OHLC/volume relationships.
- `LongbridgeDowClient.evaluate` invokes the sanitizer before HTTPX receives
  the JSON body.
- `DowMonitorService._evaluate_symbol` catches
  `InsufficientDowBars` before the generic engine-unavailable path and
  continues the remaining timeframes.
- The executable tests assert transport payload and persisted timeframe state,
  rather than a screenshot, golden, or mocked UI.
- `python scripts/check_spec_compliance.py` passed after the implementation
  mapping included the sanitizer.

The production RNG.US request and 192.168.10.28 logs still need review before
the final status can be marked complete.
