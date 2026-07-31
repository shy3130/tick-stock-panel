# Independent Review: Dow Monitor Half-Hour AI Analysis

Status: local requirements-to-evidence review passed; production review
pending.

Independent findings:

- Scheduling authority is the exchange calendar wrapper, not a hard-coded
  market-hours table. Lunch is split into distinct segments and completed
  checkpoints are timezone-aware Beijing timestamps.
- Snapshot construction enforces both session-open and data-cutoff boundaries;
  a future price cannot influence derived high, latest price, or evidence.
- The table has a stable logical key, `ReplacingMergeTree(updated_at)`, and no
  TTL. Queries select latest logical rows rather than depending on background
  merges.
- Model claims can reference only keys present in the bounded backend snapshot.
  Backend code supplies labels, numeric values, and units.
- The worker module is started only by its dedicated Compose service; the 3018
  lifespan constructs only a read repository and never imports the worker.
- Monitor symbols are read-only inputs to the worker. No formal-signal store,
  WebSocket publisher, quote context, or minute-result writer is injected.
- Overview is lightweight and failure-tolerant; long detail is fetched lazily.
  Mobile and desktop entries remain separate from real-time interpretation.

The production review must still inspect the actual ClickHouse table, container
process/port state, a completed model call, and before/after signal/WebSocket
evidence.
