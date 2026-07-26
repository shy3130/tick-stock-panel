# Collection Monitor TickFlow Integration Independent Review

Status: pending live semantic acceptance

Reviewed source commit: `f723af11e76ab05aa3b08ef19db0d652ddd5c813`.

## Independent review provenance

- Reviewer: `/root/collection_deploy_review`.
- Review completed: `2026-07-26T11:30:49+08:00`.
- Deployed implementation commit:
  `f723af11e76ab05aa3b08ef19db0d652ddd5c813`.
- Reviewed Task 3 evidence commit:
  `3b3bee5ee612d7a5d465aaaee31fcec37a58b90c`.
- Reviewed Git range:
  `f723af11e76ab05aa3b08ef19db0d652ddd5c813..3b3bee5ee612d7a5d465aaaee31fcec37a58b90c`.
- Reviewed diff package:
  `.superpowers/sdd/2026-07-26-collection-monitor-integration/review-f723af1..3b3bee5.diff`.

The independent reviewer verified that the diff package reverse-applied cleanly
and matched the four-file evidence-commit scope.

## Independently verified evidence

- Contract/specification suite: 4 passed on the reviewed evidence commit.
- Backend/relevant suite: 31 passed, 1 skipped for the documented unprovisioned
  local `polars` runtime.
- Focused frontend suite: 2 files and 8 tests passed.
- The backup records old source `46a968e...`, old container `1976db45...`, and
  old image `sha256:5d3b6a...`; the reviewed bundle and production worktree both
  resolved cleanly to `f723af11...`.
- The deployed image and container independently matched
  `sha256:c23127...` and `a4979fb0...`, with restart count 0.
- The stopped exact rollback container and old image/tag remained present.
- Docker events in the deployment window contained only the old TickFlow
  stop/die/rename and the new TickFlow create/start; no other container
  lifecycle event occurred.
- Old and new network mode, restart policy, and mounts matched, and the new
  container contained the required `LONGBRIDGE_API_URL`.
- All six Longbridge collector/API PIDs and restart counters matched before and
  after deployment and remained unchanged during review.
- Public routes returned 200; anonymous monitor APIs returned 401. Saved
  authenticated evidence was 200/200/503/200, with the exact sanitized tasks
  body `{"detail":"collection_monitoring_evidence_unavailable"}`.
- The workspace was clean throughout the independent review, which performed no
  production write or service operation.

The independent verdict was fail pending three review repairs; the deployed
observation-only state could remain and no rollback was indicated.

## Requirements-to-evidence review

- `REQ-COLLECTION-MONITOR-PROXY-001`: the implementation exposes four fixed
  GET-only routes, validates the authoritative query contract, bounds requests,
  preserves the sanitized evidence-unavailable 503, and sanitizes all other
  reviewed upstream failures. The root backend test exercises these boundaries,
  and authenticated production checks exercised all four proxy paths.
- `REQ-COLLECTION-MONITOR-PAGE-001`: the implementation provides the
  authenticated route and shared desktop/mobile navigation, keeps the four
  evidence layers distinct, exposes the required filters and provenance, and
  contains no collection mutation hook or control. The component and route
  tests exercise these boundaries. The production build contains the lazy page
  chunk and the production route is reachable at HTTP 200.
- `REQ-COLLECTION-MONITOR-PREACCEPTANCE-001`: the page and records remain
  observation-only. Automated tests and deployment checks are explicitly not
  treated as Monday live semantic evidence.

The root contract test confirms all three IDs occur in the authoritative spec,
index, traceability, acceptance, and review records; confirms mapped
implementation and test paths exist; requires root `tests/` executable evidence;
and fails if the acceptance status or live semantic evidence stops being
pending.

## Deployment review

- The mode-700 pre-deployment backup contains the exact old source, image,
  Compose, sanitized container, health, and collector baselines.
- The new image labels the exact reviewed commit and all three requirement IDs.
- Only the TickFlow image/container changed. Existing data/status/Codex bind
  mounts and the host-network runtime were preserved, and no volume was deleted.
- The exact old image and stopped old container remain available for rollback.
- Old page and health routes returned 200, `/collection-monitor` returned 200,
  and all four authenticated proxy paths were exercised.
- Honest limitation semantics survived production deployment: tasks returned
  the sanitized evidence-unavailable 503, gaps reported unavailable, and the HK
  matrix retained gray/unavailable dataset states.
- The TickFlow container remained at restart count 0 with a single port-3018
  listener. Six Longbridge collector/API PID and restart-counter records were
  byte-identical before and after deployment.

## Independent conclusion

The independent reviewer recorded the following findings and resolutions:

- P1, blocking traceability defect — addressed. The page requirement now maps to
  `tests/spec_contracts/test_collection_monitor_frontend_behavior.py`, which
  executes both focused frontend suites. The integration contract asserts that
  exact mapping rather than treating file existence as behavioral evidence.
- P2, independence not auditable — addressed by the reviewer identity,
  completion time, reviewed commits/range, diff package, independently verified
  evidence, and resolution record above.
- P3, backup evidence subdirectories had mode 600 — addressed at
  `2026-07-26T11:31:10.391973592+08:00`. Read-only verification first confirmed
  top-level mode 700 and both subdirectories at 600. Only `post-routes/` and
  `post-routes-authenticated/` were changed to 700; subsequent verification
  confirmed the top directory and both subdirectories are 700 and every
  contained evidence file remains 600.

The implementation resolution status is addressed for all three findings.
Independent re-review of the fix commit remains pending, so the original fail
verdict above remains the latest independent verdict until that re-review. The
overview aggregator currently reports a live evidence envelope, but the Sunday
task/gap and dataset evidence remains gray or unavailable; this is a
freshness/availability signal, not semantic acceptance. The pre-existing
`longbridge-core-index-quotes.service` restart counter is 549 and was unchanged
by this deployment.

Final approval remains blocked on the lower-layer live semantic acceptance.
