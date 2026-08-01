# Independent Review: Dow Monitor Hourly AI Stage Analysis

Date: 2026-08-01

Status: local requirements-to-evidence review and production-release evidence
review passed. The first live post-release hourly report remains a
next-market-session observation.

## Authority and precedence

- `docs/specs/dow-monitor-half-hour-ai-analysis.md` is the authoritative updated
  specification. The legacy name is retained only for compatibility.
- `docs/decisions/2026-08-01-dow-monitor-hourly-ai-cadence-precedence.md`
  resolves the only applicable conflict: hourly/segment-close scheduling
  controls checkpoint eligibility, while the offline-bootstrap specification
  continues to control bounded recovery and isolation.
- Each new requirement has implementation, executable tests, semantic
  acceptance and this independent review in `docs/traceability.yaml`.

## Requirements-to-evidence findings

- Cadence: the calendar implementation derives checkpoints from continuous
  exchange sessions, not a fixed Beijing clock. The worker selects at most the
  newest due checkpoint after restart. This satisfies
  `REQ-DOW-MONITOR-HOURLY-AI-CADENCE-001`.
- Stage report: the structured model and parser require a business explanation,
  prior-stage comparison, separate holder/watcher advice and next-stage
  strengthening/risk/invalidation conditions. Indicator-only prose is rejected.
  This satisfies `REQ-DOW-MONITOR-HOURLY-AI-STAGE-REPORT-001`.
- Minute path: deterministic facts are computed before the model and are covered
  by direct semantic tests. Stage and cumulative scopes remain separate, and
  data after cutoff cannot influence either. This satisfies
  `REQ-DOW-MONITOR-HOURLY-AI-MINUTE-PATH-001` without using UI snapshots as
  lower-layer proof.
- View: overview stays light, detail is lazy, new reports have a structured
  dialog and legacy reports retain their rendering. This satisfies
  `REQ-DOW-MONITOR-HOURLY-AI-VIEW-001`.

## Safety and compatibility findings

- The existing ClickHouse logical key and no-TTL table are retained. New columns
  are additive, and an empty legacy `report_json` is read as no structured
  report rather than being rewritten.
- AI processing remains a dedicated worker and does not block WebSocket quote
  ingestion, minute-result asynchronous persistence, formal signals or the 3018
  panel service.
- Longbridge/offline minute data can populate the first eligible hourly report
  immediately; a newly started worker does not need to wait an hour for a new
  WebSocket buffer.
- Model output cannot replace backend-owned numeric facts. The UI shows the
  deterministic data-quality limitations alongside the narrative.

## Independent acceptance conclusion

The direct minute-structure tests, worker/repository/API tests, full backend and
frontend regressions, production TypeScript build, Ruff check, specification
compliance check and clean-diff check collectively support local acceptance.
The real `NBIS.US` replay additionally confirms that a late-volume repair is not
misclassified as a mature reversal solely because the last price recovered.

## Production evidence review

- Commit `1c0cc309d932c6fea176d6f9f590c0fd78d5e144` is the exact revision baked
  into image
  `sha256:176e2e17502a4fddb31cca3ae17e0f3d3964d0e2bf703aeb45bc8860fbda8c53`.
  Both the 3018 app and dedicated AI worker run that image with zero restarts.
- The additive ClickHouse migration preserved AI rows `156/156` and minute rows
  `16438/14356` across physical/logical counts. Direct production readback of a
  legacy `NBIS.US` row proves compatibility rather than relying on a snapshot.
- 3018 health, production static-bundle resolution and the WebSocket
  hello/snapshot/unsubscribe protocol were checked after the switch. The 19912
  PID and start time remained unchanged and `/api/health` passed.
- No release-window fatal log pattern was found. The worker configuration still
  enforces model concurrency 1 and keeps the model path outside the real-time
  WebSocket process.
- The release occurred outside all applicable regular sessions. Therefore no
  synthetic ClickHouse row or model output was created to manufacture live
  evidence. The first real hourly report and its cross-market time rendering
  must be observed at the next A/H/US sessions; this is a follow-up operational
  observation, not a reason to weaken the already accepted deterministic
  semantics.

## Independent release conclusion

The production switch satisfies the four traced requirements without changing
their lower-layer acceptance basis. Storage compatibility, service isolation,
static delivery and real-time protocol continuity are directly evidenced. The
release is accepted with one explicit operational follow-up: observe the first
naturally scheduled hourly report in each market instead of injecting a fake
analysis outside trading hours.
