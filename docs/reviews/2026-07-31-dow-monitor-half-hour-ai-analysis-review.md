# Independent Review: Dow Monitor Half-Hour AI Analysis

Status: local and production requirements-to-evidence review passed.

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
- Repository reads restore the UTC timezone that ClickHouse JSON rows omit.
  The shared frontend formatter then renders Beijing time explicitly and derives
  the history date in the symbol's exchange timezone, preventing both the
  observed eight-hour display error and US cross-midnight history misses.

Production review findings:

- The released image and `/health` identify Git revision `8a2c007`; both the
  3018 panel and dedicated worker are running without restarts, and the previous
  pair remains stopped under explicit rollback names.
- ClickHouse contains 29 visible rows for 29 logical keys. The worker remains
  unexposed and its production error scan is empty.
- Authenticated browser evidence proves that the former `03:30` UTC rendering
  is now `北京时间 11:30` in both the lightweight entry and lazy modal detail.
  Mobile width has no page overflow.
- RNG.US day/5m and half-hour analysis routes all returned 200. Production
  WebSocket connections and 19912 Dow-state responses remained healthy after
  the release.
- The collector-side new-symbol warmup status provider is still an acknowledged
  lower-layer gap and was not counted as accepted by this review.
