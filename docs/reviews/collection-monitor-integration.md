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

## Final-wave independent re-review

Independent reviewer
`/root/collection_deploy_impl/collection_final_reviewer` completed the final
re-review at `2026-07-26T13:03:55+08:00`.

The exact review target was:

- base:
  `55d6d43bf8945d83c96608f51e7e43da5a141e1a`;
- head:
  `63162ba1b83f6e802e1f2f722df0fbff5c02ebeb`;
- range:
  `55d6d43bf8945d83c96608f51e7e43da5a141e1a..63162ba1b83f6e802e1f2f722df0fbff5c02ebeb`;
- package:
  `.superpowers/sdd/2026-07-26-collection-monitor-integration/review-55d6d43..63162ba.diff`;
- package SHA-256:
  `854875285a019b6edcd7a464d575940dab595e24999ffb7a0337da2349274384`.

The reviewer confirmed that the 35,466-byte package is byte-for-byte identical
to native `git diff --binary`, reverse-applies at the reviewed head, and passes
`git diff --check`.

### Final verdict

Verdict: PASS for the final-wave code/spec requirements and the exact
`63162ba` cutover.

Critical findings: none. Important code/spec findings: none. Minor code/spec
findings: none.

This verdict does not grant live semantic acceptance. Monday lower-layer
evidence remains required, and the repository status remains
`pending live semantic acceptance`.

### Independently verified requirements and checks

- `REQ-COLLECTION-MONITOR-PROXY-001`: PASS. The proxy streams decoded bytes,
  accepts exactly 2,097,152 bytes, rejects a larger response, validates
  route-specific shapes and counts, and returns only the sanitized 502 for
  invalid upstream evidence. Market response evidence accepts the six
  authoritative keys, including response-only `market_temperature`; task and
  gap query filters remain restricted to the five authoritative query keys.
- `REQ-COLLECTION-MONITOR-PAGE-001`: PASS. The page has GET-only offset
  pagination, totals, truncation indication, offset reset on filter changes,
  and a distinct market-temperature evidence label without adding that
  response-only key to task/gap filter options.
- `REQ-COLLECTION-MONITOR-PREACCEPTANCE-001`: PASS. The page explicitly shows
  `Observation only` and `Live semantic acceptance pending`, and the exact root
  behavioral wrapper is mapped to this requirement.

Independent execution evidence:

- focused backend, contract, and root behavioral wrapper: 47 passed;
- complete frontend suite: 32 files and 132 tests passed;
- production frontend build: passed, 2,709 modules transformed;
- repository-wide Python sweep: 599 passed and 8 failed. The reviewer
  independently classified all eight failures as outside the reviewed range:
  two pre-existing backend behaviors, four Windows/Linux launcher or path
  incompatibilities, one locale-decoding failure, and one unrelated vendored
  checksum mismatch.

The recorded RED chronology was also reviewed and accepted:

- the legacy eager proxy accepted oversize or invalid-shape evidence instead of
  the asserted sanitized 502 and did not satisfy the streaming call contract;
- the page lacked the asserted task/gap pagination groups, totals, offsets, and
  pending-acceptance label;
- `REQ-COLLECTION-MONITOR-PREACCEPTANCE-001` lacked the exact behavioral
  wrapper mapping;
- the initially five-key market response validator returned 502 for the
  authoritative six-key response;
- the first six-key frontend response rendered `market_temperature` with an
  empty heading instead of the asserted `市场温度`.

All five RED cases became GREEN before production deployment.

### Final production evidence

Reviewed image:

`tickflow-stock-panel-app:collection-monitor-final-63162ba1-20260726T043758Z`

Image ID:

`sha256:f2ce9c786d486509225bd84640758b4ea2b4e9631c8989afd5079cb9eb187bac`

The image embeds reviewed revision
`63162ba1b83f6e802e1f2f722df0fbff5c02ebeb` and all three requirement IDs.

The exact cutover created container
`5d9cca5ab335c586fe65cab88bd29bd5550d87a9dab812df0d4cea68b04eaf74`
at `2026-07-26T04:46:04.083012605Z`. Authenticated probes returned:

- overview: 200;
- HK market: 200 with exactly six unique expected dataset keys, including
  `market_temperature`;
- tasks: 503 with the exact sanitized evidence-unavailable detail;
- gaps: 200.

The root, health, Dow-monitor, and collection-monitor pages returned 200, and
all four anonymous monitor API probes returned 401. The exact cutover preserved
environment values, mounts, command, host network, restart policy, and Compose
runtime labels except for the expected image/revision labels. All six
Longbridge unit PID/restart records were byte-identical immediately before and
after this cutover.

Protected backup:

`/home/alwin/backups/tickflow-collection-monitor-market-temperature-predeploy-20260726T043626Z`

The backup is mode 700 with evidence files mode 600. The exact stopped rollback
container is
`TickFlow_Stock_Panel_pre_market_temperature_20260726T043626Z`
(`dfb442fd672072f854e7f075389b9002df439aab66234ed4b7e8dd337c8e2e3e`)
at image
`sha256:bbaa03271f05265dd8d003cf4d1d526fb638e9d6a51e254e65a9c83268e49bf5`.
The dedicated rollback tag is
`tickflow-stock-panel-app:rollback-market-temperature-20260726T043626Z`.

### Operational audit exceptions

The final record does not claim that the broader review/deployment window was
event-free:

- `longbridge-api.service` gracefully stopped at
  `2026-07-26T04:35:04.835119Z`; its `Restart=always` policy started PID
  `2798212` at `2026-07-26T04:35:09.922423Z`, changing the broader-wave record
  from PID `2706714` / `NRestarts=0` to PID `2798212` / `NRestarts=1`. This
  preceded the final backup, build, and cutover. The service log shows a
  graceful Uvicorn shutdown/start, but no initiating actor was recorded.
  Therefore only immediate cutover identity, not end-to-end six-service
  identity, is asserted.
- The prior `dfb442...` TickFlow container received an explicit Docker restart
  at `2026-07-26T04:39:14Z` during the image-build window. The new `5d9cca...`
  container received an explicit Docker restart at
  `2026-07-26T04:51:18Z`, after the guarded cutover check. Both were SIGTERM
  followed by SIGKILL after ten seconds and were Docker `restart` actions, not
  application crashes or kernel OOMs. The reviewer confirmed its commands were
  read-only. A read-only attribution sweep found no autoheal/watchtower
  container, matching system/user timer, crontab entry, cron/systemd/app
  restart reference, or Chronicle TickFlow restart event. No third restart
  occurred at the next expected boundary; the active container remained the
  same ID, healthy, and continuously up from
  `2026-07-26T04:51:28.615036393Z` through the closing probe at
  `2026-07-26T05:03:19Z`. The initiating actor remains unattributed.
- During the first `df5944` Compose fallback, Compose removed original
  container `a4979fb0...` before the fallback recreated the old runtime as
  container `9d2ef535...` from the exact old image/config. The original
  `a4979fb0...` inspect/config remains protected in
  `/home/alwin/backups/tickflow-collection-monitor-final-review-predeploy-20260726T040743Z`.
  Subsequent manual cutovers preserved their exact stopped rollback
  containers.

These disclosed operational exceptions do not identify a defect in the
reviewed code and do not invalidate the exact final cutover evidence. They do
prohibit an unqualified broader-window no-restart claim.
