# Task 3 Report: Contract Evidence, Independent Review, and Deployment

Status: deployed; pending live semantic acceptance

## Outcome

The reviewed collection-monitor integration at
`f723af11e76ab05aa3b08ef19db0d652ddd5c813` is deployed to TickFlow on
`192.168.10.28`.

Production URL:
`http://192.168.10.28:3018/collection-monitor`

The deployment is observation-only. It does not accept Monday live collection
semantics and does not change collectors, Longbridge services, Chronicle,
schedules, alerts, or notifications.

## Specification and traceability gate

The applicable source is the authoritative
`SPEC-COLLECTION-MONITOR-INTEGRATION-001`. It declares:

- `REQ-COLLECTION-MONITOR-PROXY-001`;
- `REQ-COLLECTION-MONITOR-PAGE-001`;
- `REQ-COLLECTION-MONITOR-PREACCEPTANCE-001`.

`docs/spec-index.yaml` has no unresolved conflict. The recorded exception
`EXC-COLLECTION-MONITOR-PREACCEPTANCE-DEPLOY-001` authorizes only the
observation-only deployment through 2026-07-28 and explicitly prohibits a claim
of live semantic acceptance.

Task 3 added
`tests/spec_contracts/test_collection_monitor_integration_contract.py`. It
asserts that every requirement ID occurs in the spec, index, traceability,
acceptance, and independent-review records; checks mapped implementation and
executable test paths; requires test evidence under root `tests/`; and enforces
the pending acceptance status.

The first contract run failed because the acceptance record did not yet name the
three requirements. After adding the explicit mappings and replacing the
frontend-outside-`tests/` traceability entry with the root contract, the
contract and repository checker passed.

`frontend/src/pages/CollectionMonitor.test.tsx` remains the executable component
suite and is run directly, but it is not used as the root traceability path
because the repository checker requires all declared test evidence under
`tests/`.

## Verification before deployment

- Contract plus specification checker: 4 passed.
- Backend plus contract suite with `PYTHONPATH=backend`: 31 passed, 1 skipped.
- Frontend focused suite: 2 files, 8 tests passed.
- Frontend production build: passed; 2,709 modules transformed and the
  `CollectionMonitor` lazy chunk was emitted.

The one backend skip is the pre-existing provisioned-runtime import assertion
that requires `polars`, which is absent from the local Python environment. The
deterministic AST registration assertion ran, and the full production routes
were exercised after deployment.

## Independent requirements-to-evidence review

The review followed each authoritative requirement through implementation,
root executable evidence, component evidence where applicable, pending
acceptance, and production behavior.

- Proxy: four fixed GET routes, canonical validation, bounded timeout/results,
  sanitized non-503 failures, and the sanitized evidence-unavailable 503 are
  implemented and tested.
- Page: authenticated native route, desktop/mobile navigation, four evidence
  levels, filters, freshness, observation mode, provenance, bounded
  last-confirmed evidence, and no mutation control are implemented and tested.
- Pre-acceptance: UI, docs, deployment evidence, and this report remain
  observation-only and do not substitute automated or deployment signals for
  live lower-layer semantic evidence.

No blocking requirement-to-evidence finding remained for the pre-acceptance
deployment.

## Exact pre-deployment backup

The first production write created:

`/home/alwin/backups/tickflow-collection-monitor-predeploy-20260726T024329Z`

The directory is owned by `alwin:alwin` and has mode 700. Before the source
fast-forward, build, or container switch it captured:

- clean source commit
  `46a968e353ca2c2801296ec4b6e849cc8ad816e5`;
- old TickFlow container
  `1976db45fc6e5668aec0d041aab61cf01e70822ecc826e084292a5c73503e0f6`;
- old image reference
  `tickflow-stock-panel-app:dow-data-integrity-completed-segments-ba3a913e-20260724`;
- old image ID
  `sha256:5d3b6abd64daaa1ddfe72edfc1de42d3dd1e75ccce39b64c5ccd46e50ffd84c5`;
- resolved and no-interpolation Compose configurations;
- sanitized container inspect;
- HTTP 200 health response and body;
- six Longbridge collector/API PID and `NRestarts` baselines.

The backup also contains the reviewed Git bundle, build log/exit status,
post-deployment route bodies and statuses, post-deployment sanitized inspect,
collector diff, container log, and verification summary. Sensitive runtime
values were not placed in the report.

## Build and deployment

The production source worktree was cleanly fast-forwarded from `46a968e3` to
reviewed commit `f723af11` using a verified Git bundle. The resulting worktree
remained clean.

Versioned image:

`tickflow-stock-panel-app:collection-monitor-f723af11-20260726T024329Z`

Image ID:

`sha256:c2312745e75975c496f5f1c042fd4e02b231dac385c09482b1e526e160d034e0`

The image labels record the full reviewed revision and all three requirement
IDs. The Docker build completed with exit code 0.

The reviewed source worktree had no `.env`, and its local `data` directory was
not the bind source used by the running container. Recreating directly from
those unresolved paths would have changed data and runtime configuration.
Therefore the new image was built from the reviewed worktree, while the
container was recreated with the existing runtime Compose directory:

`/home/alwin/apps/tickflow-builds/market-snapshot-realtime-20260723-1125`

Only `TICKFLOW_IMAGE` was overridden. This preserved the exact production
`.env`, data symlink target, tiers file, host network, restart policy, and bind
mounts. `LONGBRIDGE_API_URL` resolved to the required
`http://host.docker.internal:19912`.

Only `TickFlow_Stock_Panel` was stopped and recreated. No volume was deleted.
The new container is:

`a4979fb0b5a401f3310e57f3da157115edbd321dc1e822fa77418603ec35de47`

It started at `2026-07-26T02:53:41.675086099Z`.

## Production verification

From the production host and the workstation:

- `/` — 200;
- `/health` — 200;
- `/dow-monitor?market=hk` — 200;
- `/collection-monitor` — 200.

All four anonymous API requests returned the existing authentication gate, 401.
Using an existing persisted authenticated session:

- `/api/collection-monitor/overview` — 200;
- `/api/collection-monitor/markets/hk` — 200;
- `/api/collection-monitor/tasks?limit=1&offset=0` — sanitized 503;
- `/api/collection-monitor/gaps?market=hk&dataset=capital_flow&limit=1&offset=0`
  — 200 with `evidenceState: "unavailable"`.

The tasks response was exactly
`{"detail":"collection_monitoring_evidence_unavailable"}`. The HK matrix
retained gray/unavailable dataset display states, and the gaps response retained
unavailable rather than inventing healthy evidence. The overview aggregator's
live envelope indicates current aggregator evidence, not accepted collection
semantics.

The new TickFlow container had restart count 0 after the multi-minute stability
window, one port-3018 listener, preserved mount sources/destinations, and no
`ERROR`, `CRITICAL`, or `Traceback` entry in the deployment log.

Before/after Longbridge service evidence was byte-identical:

| Service | MainPID | NRestarts |
| --- | ---: | ---: |
| `longbridge-core-index-quotes.service` | 269929 | 549 |
| `longbridge-realtime-quotes.service` | 1559269 | 0 |
| `longbridge-quote-subscription.service` | 1681499 | 0 |
| `longbridge-monitor-capital-collector.service` | 2371367 | 0 |
| `longbridge-collection-monitor.service` | 2704474 | 0 |
| `longbridge-api.service` | 2706714 | 0 |

## Rollback

Rollback was not required.

The exact old container remains stopped as:

`TickFlow_Stock_Panel_pre_collection_monitor_20260726T024329Z`

The exact rollback image remains:

`tickflow-stock-panel-app:dow-data-integrity-completed-segments-ba3a913e-20260724`

at:

`sha256:5d3b6abd64daaa1ddfe72edfc1de42d3dd1e75ccce39b64c5ccd46e50ffd84c5`

## Concerns and remaining acceptance

- Live minute K-line, capital-flow, order-book, and large/medium/small-order
  capital evidence is still required. Status remains
  `pending live semantic acceptance`.
- `longbridge-core-index-quotes.service` had a pre-existing `NRestarts` value of
  549. It did not change during this deployment.
- The exact stopped rollback container consumes container metadata and its image
  remains on disk intentionally until rollback retention is no longer needed.

Task 3 local evidence is committed with subject
`docs: record collection monitor deployment evidence`.

## Fix round 1/5: independent review remediation

Independent reviewer `/root/collection_deploy_review` completed its review at
`2026-07-26T11:30:49+08:00`. It reviewed:

- deployed commit
  `f723af11e76ab05aa3b08ef19db0d652ddd5c813`;
- Task 3 evidence commit
  `3b3bee5ee612d7a5d465aaaee31fcec37a58b90c`;
- Git range
  `f723af11e76ab05aa3b08ef19db0d652ddd5c813..3b3bee5ee612d7a5d465aaaee31fcec37a58b90c`;
- diff package
  `.superpowers/sdd/2026-07-26-collection-monitor-integration/review-f723af1..3b3bee5.diff`.

The reviewer independently verified the test results, diff package, old and new
image/container identities, rollback target, Docker event scope, runtime
mount/network/restart configuration, four route families, and unchanged
Longbridge services. It returned three findings without requesting production
rollback.

### P1: behavioral page traceability

The original page mapping pointed only to the structural integration contract.
A new root executable wrapper now exists at
`tests/spec_contracts/test_collection_monitor_frontend_behavior.py`. It resolves
`pnpm` from `PATH` on supported platforms and falls back to the repository's
recovered Node/pnpm runtime. It executes:

- `frontend/src/pages/CollectionMonitor.test.tsx`;
- `frontend/src/pages/dow-monitor-route.test.tsx`.

`REQ-COLLECTION-MONITOR-PAGE-001` now maps to that wrapper in
`docs/traceability.yaml`. The integration contract asserts the exact mapping,
in addition to verifying the path and evidence type.

RED evidence:

- `python -m pytest -q
  tests/spec_contracts/test_collection_monitor_integration_contract.py` —
  1 failed, 3 passed because the page requirement did not map to the wrapper.

GREEN evidence:

- Contract/specification gate — 5 passed.
- Root behavioral wrapper — 1 passed; it executed both frontend files and all 8
  focused tests.
- Direct focused frontend command — 2 files and 8 tests passed.
- Direct specification checker — passed.

### P2: auditable review provenance

`docs/reviews/collection-monitor-integration.md` now records the exact reviewer,
review completion time, reviewed implementation/evidence commits, Git range,
diff package, independently verified evidence, original verdict, all three
findings, and their implementation resolution status. It does not claim Monday
live semantic acceptance.

### P3: traversable backup evidence

Read-only verification found:

- top backup directory: mode 700, `alwin:alwin`;
- `post-routes/`: mode 600, `alwin:alwin`;
- `post-routes-authenticated/`: mode 600, `alwin:alwin`.

Only those two exact subdirectories were changed with `chmod 700` at
`2026-07-26T11:31:10.391973592+08:00`. Post-change verification confirmed the
top directory and both subdirectories are mode 700, while every contained
evidence file remains mode 600 and owned by `alwin:alwin`.

No container or service was restarted. Read-only verification after the
permission repair confirmed:

- TickFlow container ID remained `a4979fb0...`;
- TickFlow start time remained `2026-07-26T02:53:41.675086099Z`;
- TickFlow restart count remained 0;
- all six Longbridge service PIDs and restart counters remained unchanged.

Live semantic acceptance remains pending.

Fix-round commit subject:
`test: add collection monitor behavioral evidence`.

## Final review wave: evidence-boundary hardening and exact cutover

Status: code/spec review and exact cutover passed; live semantic acceptance
remains pending.

### Implementation and TDD

Final-wave implementation commits:

- `df5944fe7d6f9eaac74a4e4f8524988ab233d55a` —
  `fix: harden collection monitor evidence boundaries`;
- `41a57c00898eac5cfb0784ff5e2572ea205e9f40` —
  `fix: accept market temperature response evidence`;
- `63162ba1b83f6e802e1f2f722df0fbff5c02ebeb` —
  `fix: render market temperature evidence`.

The final wave used RED/GREEN execution before production edits:

- proxy streaming/shape tests demonstrated that the eager implementation did
  not enforce the decoded 2 MiB boundary and accepted invalid route shapes
  instead of the asserted sanitized 502;
- component tests demonstrated the absence of task/gap pagination groups,
  totals, offset navigation, and the explicit pending-acceptance label;
- the contract test demonstrated that
  `REQ-COLLECTION-MONITOR-PREACCEPTANCE-001` did not map to the exact root
  behavioral wrapper;
- the first five-key market validator rejected production's authoritative
  six-key response with 502 rather than the asserted 200;
- the first frontend six-key response rendered `market_temperature` with an
  empty heading. The component assertion failed with
  `Unable to find an element with the text: 市场温度`.

The final design deliberately keeps two types/allowlists:

- five query keys for task and gap filters;
- six market response/display keys, adding response-only
  `market_temperature`.

This prevents the sixth observation-only row from becoming an unsupported
task/gap query while rendering it distinctly as `市场温度`.

Final verification:

- backend, contract, and root wrapper: 47 passed;
- frontend: 32 files, 132 tests passed;
- frontend production build: passed, 2,709 modules transformed;
- repository Python sweep: 599 passed and 8 unrelated failures. Independent
  review confirmed the failures were outside the final-wave range.

### Final independent review

Reviewer:

`/root/collection_deploy_impl/collection_final_reviewer`

Completed:

`2026-07-26T13:03:55+08:00`

Reviewed range:

`55d6d43bf8945d83c96608f51e7e43da5a141e1a..63162ba1b83f6e802e1f2f722df0fbff5c02ebeb`

Diff package:

`.superpowers/sdd/2026-07-26-collection-monitor-integration/review-55d6d43..63162ba.diff`

SHA-256:

`854875285a019b6edcd7a464d575940dab595e24999ffb7a0337da2349274384`

The 35,466-byte package matched native `git diff --binary`, reverse-applied,
and passed `git diff --check`.

Verdict: PASS for all three final-wave requirements and the exact `63162ba`
cutover, with no critical, important, or minor code/spec findings. The verdict
does not grant Monday live semantic acceptance.

### Final image, container, and rollback

Final image:

`tickflow-stock-panel-app:collection-monitor-final-63162ba1-20260726T043758Z`

Image ID:

`sha256:f2ce9c786d486509225bd84640758b4ea2b4e9631c8989afd5079cb9eb187bac`

Active container:

`5d9cca5ab335c586fe65cab88bd29bd5550d87a9dab812df0d4cea68b04eaf74`

Final protected backup:

`/home/alwin/backups/tickflow-collection-monitor-market-temperature-predeploy-20260726T043626Z`

Exact stopped rollback:

- container:
  `TickFlow_Stock_Panel_pre_market_temperature_20260726T043626Z`
  (`dfb442fd672072f854e7f075389b9002df439aab66234ed4b7e8dd337c8e2e3e`);
- image:
  `sha256:bbaa03271f05265dd8d003cf4d1d526fb638e9d6a51e254e65a9c83268e49bf5`;
- tag:
  `tickflow-stock-panel-app:rollback-market-temperature-20260726T043626Z`.

The authenticated final probes returned overview 200, HK market 200 with
exactly six unique known keys including `market_temperature`, tasks 503 with
the exact sanitized evidence-unavailable body, and gaps 200. The public routes
returned 200 and anonymous proxy requests returned 401. Runtime environment,
mount, command, host-network, restart-policy, and Compose-label comparisons
passed. All six Longbridge unit identities were unchanged immediately across
the exact cutover.

### Deployment-attempt and lifecycle audit

The broader final wave contains three disclosed limitations:

1. The first `df5944` Compose attempt removed original container
   `a4979fb0...` before the fallback recreated the exact old image/config as
   container `9d2ef535...`. The original inspect/config remains protected in
   `/home/alwin/backups/tickflow-collection-monitor-final-review-predeploy-20260726T040743Z`.
   Subsequent manual cutovers preserved exact stopped containers.
2. `longbridge-api.service` gracefully stopped at
   `2026-07-26T04:35:04.835119Z` and restarted under its `Restart=always`
   policy at `2026-07-26T04:35:09.922423Z`, changing PID
   `2706714 → 2798212` and `NRestarts 0 → 1` before the final backup/build.
   The initiating actor is not recorded. Only immediate-cutover service
   identity is asserted.
3. The prior and current TickFlow containers received explicit Docker restart
   actions at `04:39:14Z` and `04:51:18Z`, outside the guarded final cutover.
   Neither was an application crash or OOM. No autoheal/watchtower, matching
   timer/crontab/script, or Chronicle TickFlow event was found. The independent
   reviewer confirmed its commands were read-only. No third restart occurred
   at the next expected boundary, and the same final container remained healthy
   through `2026-07-26T05:03:19Z`. The initiating actor remains unattributed.

These limitations prohibit an unqualified claim that all six services and
TickFlow were event-free across the broader wave. They do not replace or
invalidate the exact cutover evidence, and they do not establish live
collection semantics.

Final status: deployed and independently reviewed; pending live semantic
acceptance.
