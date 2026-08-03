# Collection Monitor TickFlow Integration Acceptance

Status: pending live semantic acceptance

The user authorized an observation-only deployment on 2026-07-26 before live
trading data is available. Automated and deployment checks may be recorded
here, but they do not satisfy lower-layer semantic acceptance.

## Requirement evidence

- `REQ-COLLECTION-MONITOR-PROXY-001`: implementation and executable backend
  evidence cover the four fixed GET routes, query validation, bounded requests,
  sanitized failures, and the distinct evidence-unavailable 503.
- `REQ-COLLECTION-MONITOR-PAGE-001`: implementation and executable component
  evidence cover the authenticated native route, desktop/mobile navigation,
  four evidence layers, filters, provenance, last-confirmed evidence, and the
  absence of collection mutation controls.
- `REQ-COLLECTION-MONITOR-PREACCEPTANCE-001`: the read-only authorization and
  pending live-acceptance gate remain in force. Neither the tests nor the
  deployment evidence below establish collection correctness.

## Automated and build evidence

- Root contract and specification checker:
  `python -m pytest -q
  tests/spec_contracts/test_collection_monitor_integration_contract.py
  tests/spec_contracts/test_spec_guard_contract.py` — 5 passed after review
  remediation.
- Root frontend behavioral wrapper:
  `python -m pytest -q
  tests/spec_contracts/test_collection_monitor_frontend_behavior.py` — 1
  passed; the wrapper executed the focused component and route suites, 8 tests.
- Backend plus contract suite with `PYTHONPATH=backend` — 31 passed, 1 skipped.
  The skipped full-application import requires the provisioned `polars`
  runtime; the deterministic router-registration assertion ran, and the
  provisioned production routes were verified below.
- Focused frontend suite — 2 files and 8 tests passed.
- Frontend production build — passed, 2,709 modules transformed, including the
  lazy `CollectionMonitor` chunk.

## Pre-deployment backup

Before the source fast-forward, image build, or container recreation, the
production host captured
`/home/alwin/backups/tickflow-collection-monitor-predeploy-20260726T024329Z`
with mode 700. It contains:

- source commit and clean status;
- current image reference and image ID;
- resolved and no-interpolation Compose configurations;
- sanitized container inspect;
- health headers, body, and status;
- Longbridge collector/API PIDs and systemd `NRestarts` counters.

The backed-up source commit was
`46a968e353ca2c2801296ec4b6e849cc8ad816e5`. The previous TickFlow container was
`1976db45fc6e5668aec0d041aab61cf01e70822ecc826e084292a5c73503e0f6`, using
`tickflow-stock-panel-app:dow-data-integrity-completed-segments-ba3a913e-20260724`
at
`sha256:5d3b6abd64daaa1ddfe72edfc1de42d3dd1e75ccce39b64c5ccd46e50ffd84c5`.
Its health response was HTTP 200 with
`{"status":"ok","version":"0.1.86","mode":"none"}` and its restart count was 0.

## Deployment evidence

- Reviewed source commit:
  `f723af11e76ab05aa3b08ef19db0d652ddd5c813`.
- Versioned image:
  `tickflow-stock-panel-app:collection-monitor-f723af11-20260726T024329Z`.
- Image ID:
  `sha256:c2312745e75975c496f5f1c042fd4e02b231dac385c09482b1e526e160d034e0`.
- TickFlow container:
  `a4979fb0b5a401f3310e57f3da157115edbd321dc1e822fa77418603ec35de47`,
  started at `2026-07-26T02:53:41.675086099Z`.
- Runtime endpoint:
  `LONGBRIDGE_API_URL=http://host.docker.internal:19912`.
- Existing bind mounts for `/app/data`, `/app/tiers.yaml`, `/root/.codex`, and
  `/run/longbridge` were preserved exactly. No volume was deleted.
- Only `TickFlow_Stock_Panel` was stopped and recreated. The previous exact
  container remains stopped as
  `TickFlow_Stock_Panel_pre_collection_monitor_20260726T024329Z`.

The deployment used the existing runtime Compose directory
`/home/alwin/apps/tickflow-builds/market-snapshot-realtime-20260723-1125` with
only `TICKFLOW_IMAGE` overridden to the new version. This preserved the running
production `.env`, data symlink target, tiers file, host network, and restart
policy. The reviewed source worktree itself had no `.env` and its local `data`
directory was not the running production bind source, so using those paths
would not have been an image-only recreation.

Exact rollback target:

- image reference:
  `tickflow-stock-panel-app:dow-data-integrity-completed-segments-ba3a913e-20260724`;
- image ID:
  `sha256:5d3b6abd64daaa1ddfe72edfc1de42d3dd1e75ccce39b64c5ccd46e50ffd84c5`;
- stopped exact container:
  `TickFlow_Stock_Panel_pre_collection_monitor_20260726T024329Z`.

Rollback was not invoked because all required checks passed.

## Production verification

- `http://192.168.10.28:3018/` — HTTP 200.
- `http://192.168.10.28:3018/health` — HTTP 200.
- `http://192.168.10.28:3018/dow-monitor?market=hk` — HTTP 200.
- `http://192.168.10.28:3018/collection-monitor` — HTTP 200.
- Anonymous requests to all four proxy routes returned the existing
  authentication gate, HTTP 401.
- With an existing authenticated session, overview returned 200, the HK market
  matrix returned 200, tasks returned the sanitized evidence-unavailable 503,
  and HK capital-flow gaps returned 200 with `evidenceState: "unavailable"`.
- The HK dataset matrix retained gray/unavailable display states. The tasks
  response contained only
  `{"detail":"collection_monitoring_evidence_unavailable"}`; no upstream URL,
  body, or credential was exposed.
- TickFlow had one listener on port 3018, restart count 0, and no
  `ERROR`/`CRITICAL`/`Traceback` entry in the deployment log.
- The following before/after `MainPID` and `NRestarts` values were identical:

  - `longbridge-core-index-quotes.service`: `269929`, `549`;
  - `longbridge-realtime-quotes.service`: `1559269`, `0`;
  - `longbridge-quote-subscription.service`: `1681499`, `0`;
  - `longbridge-monitor-capital-collector.service`: `2371367`, `0`;
  - `longbridge-collection-monitor.service`: `2704474`, `0`;
  - `longbridge-api.service`: `2706714`, `0`.

No collector, Longbridge service, Chronicle event, schedule, alert, or
notification configuration was changed.

Required Monday evidence:

- Real minute K-line evidence across supported intervals and symbols.
- Real capital-flow evidence.
- Real order-book evidence.
- Real large/medium/small-order capital evidence.
- Agreement between source evidence, stored evidence, API output, and page
  presentation, including freshness and provenance.

Deployment evidence: recorded and verified as pre-acceptance evidence.
Live semantic evidence: pending.

## 2026-08-03 migrated observation presentation repair

Requirement `REQ-COLLECTION-MONITOR-PAGE-001` was revalidated after production
rendered migrated collection observations as blank, unavailable cards. The
lower-layer `19912` response already contained canonical evidence for
`intraday_line`, `order_book_depth`, `realtime_quote`, and `trade_tick`; the
3018 frontend lacked names for those keys and inferred health only from legacy
task fields that canonical observations do not carry.

Acceptance evidence:

- red test: the new page test initially failed because `分时线` was absent and
  the live observation rendered `不可用`;
- green tests: 9 focused frontend tests and 46 backend proxy tests passed;
- specification compliance check passed and the production frontend build
  completed after transforming 2,727 modules;
- source commit: `58ab995` (`fix: render migrated collection evidence`);
- deployed image:
  `tickflow-stock-panel-app:collection-evidence-ui-58ab995-20260803`
  (`sha256:d913d084...`);
- the production symbol-authority endpoint returned `authoritative: true`, and
  the one-shot collection aggregation completed with status `ok` and zero
  unavailable verification records;
- fresh Chrome verification on
  `/collection-monitor?market=hk` displayed named `分时线`, `市场温度`,
  `盘口深度`, `实时行情`, and `逐笔成交` rows. Live evidence rendered `健康`,
  cached market-temperature evidence rendered `降级`, and the market matrix
  contained no `不可用` label.

This repair does not promote missing lower-layer data to healthy. The five
legacy rows remain explicitly `未运行`, and absent capital-flow evidence is
not inferred from another dataset. Rollback is the prior image
`tickflow-stock-panel-app:authority-feed-repair-20260803-1348`. The `19912`
collector process was not restarted by this deployment.

## Final review wave evidence

The final implementation head is
`63162ba1b83f6e802e1f2f722df0fbff5c02ebeb`. It adds:

- decoded streaming enforcement at the exact 2,097,152-byte boundary;
- sanitized route-specific shape, count, key, and duplicate rejection;
- task/gap totals and GET-only offset pagination;
- the explicit `Live semantic acceptance pending` page state;
- root behavioral traceability for
  `REQ-COLLECTION-MONITOR-PREACCEPTANCE-001`;
- an authoritative response/query asymmetry: market response evidence supports
  six keys, including `market_temperature`, while task/gap query filters remain
  the five-key allowlist;
- distinct `市场温度` rendering without a market-temperature task/gap filter
  option.

TDD evidence was observed RED before each production edit and GREEN afterward:

- oversize and invalid-shape proxy evidence was not rejected correctly before
  streaming/shape enforcement;
- pagination groups, totals, offsets, and the pending label were absent before
  the page edit;
- the pre-acceptance requirement lacked the exact root wrapper mapping;
- the first strict five-key market validator rejected the valid six-key live
  response with 502;
- the first frontend six-key response rendered an empty heading for
  `market_temperature`.

Final automated evidence:

- focused backend, contract, and root wrapper: 47 passed;
- complete frontend: 32 files, 132 tests passed;
- production build: 2,709 modules transformed;
- full Python sweep: 599 passed, 8 failed, with all eight independently
  confirmed outside the final-wave range.

Independent reviewer
`/root/collection_deploy_impl/collection_final_reviewer` completed review at
`2026-07-26T13:03:55+08:00` and returned PASS with no critical, important, or
minor code/spec findings for range
`55d6d43bf8945d83c96608f51e7e43da5a141e1a..63162ba1b83f6e802e1f2f722df0fbff5c02ebeb`.
The exact 35,466-byte package is
`.superpowers/sdd/2026-07-26-collection-monitor-integration/review-55d6d43..63162ba.diff`
with SHA-256
`854875285a019b6edcd7a464d575940dab595e24999ffb7a0337da2349274384`;
native binary diff equality, reverse apply, and `git diff --check` passed.

## Final deployment and rollback evidence

Protected backup:

`/home/alwin/backups/tickflow-collection-monitor-market-temperature-predeploy-20260726T043626Z`

The backup is mode 700 and its evidence files are mode 600.

Deployed image:

`tickflow-stock-panel-app:collection-monitor-final-63162ba1-20260726T043758Z`

Image ID:

`sha256:f2ce9c786d486509225bd84640758b4ea2b4e9631c8989afd5079cb9eb187bac`

Container:

`5d9cca5ab335c586fe65cab88bd29bd5550d87a9dab812df0d4cea68b04eaf74`

The exact guarded cutover started it at
`2026-07-26T04:46:04.177526865Z`. A later explicit external/unattributed Docker
restart changed its current start time to
`2026-07-26T04:51:28.615036393Z`; it remained the same container and image.

Authenticated final-wave route evidence:

- overview: 200;
- HK market: 200 with exactly six unique known keys, including
  `market_temperature`;
- tasks: 503 with only
  `{"detail":"collection_monitoring_evidence_unavailable"}`;
- gaps: 200.

The four public page/health routes returned 200 and all four anonymous monitor
API routes returned 401. Environment values, mounts, host network, command,
restart policy, and Compose runtime labels matched the prior runtime except for
the expected image/revision labels. One port-3018 listener and clean
application logs were observed. The same active container remained healthy
through `2026-07-26T05:03:19Z`.

Exact stopped rollback:

- container:
  `TickFlow_Stock_Panel_pre_market_temperature_20260726T043626Z`
  (`dfb442fd672072f854e7f075389b9002df439aab66234ed4b7e8dd337c8e2e3e`);
- image ID:
  `sha256:bbaa03271f05265dd8d003cf4d1d526fb638e9d6a51e254e65a9c83268e49bf5`;
- rollback tag:
  `tickflow-stock-panel-app:rollback-market-temperature-20260726T043626Z`.

## Final-wave operational limitations

- The six Longbridge unit records were byte-identical immediately before and
  after the exact `63162ba` cutover. They were not identical across the broader
  final wave: `longbridge-api.service` gracefully restarted before the final
  backup, changing PID `2706714 → 2798212` and `NRestarts 0 → 1`. No initiating
  actor was recorded.
- The prior and current TickFlow containers each received an explicit,
  unattributed Docker restart outside the guarded cutover. They were not
  application crashes or OOMs. The read-only attribution sweep found no
  autoheal/watchtower, matching timer/crontab/script, or Chronicle TickFlow
  restart event, and no third restart occurred at the next expected boundary.
- The first `df5944` Compose fallback recreated the old runtime as `9d2ef535...`
  after Compose removed original container `a4979fb0...`. The exact original
  inspect/config is protected in the first final-review backup. Later cutovers
  preserved exact rollback containers.

These are deployment-audit limitations, not Monday semantic evidence.
Automated checks, HTTP responses, and a clean final-wave code review do not
substitute for lower-layer live collection acceptance.

Deployment evidence: recorded and independently reviewed as pre-acceptance evidence.
Live semantic evidence: pending.

## 2026-08-03 content-update timestamp acceptance

`REQ-COLLECTION-MONITOR-PAGE-001` now distinguishes the timestamp of the
underlying data change from the timestamp of the monitor inspection. Dataset
cards label `latestDataAt` as `内容更新` and label `evidenceAt` as `巡检`; a
refresh must not present a newer inspection time as a content change.

Evidence:

- the behavioral test was observed RED before the labels and source field were
  wired, then the focused component suite passed 10 tests;
- specification compliance passed and the clean production frontend build
  transformed 2,726 modules;
- source commit: `5023a407304c5bfb4c49694f6ba6833fae15db3b`;
- deployed image:
  `tickflow-stock-panel-app:collection-content-time-5023a40-v2-20260803`;
- production entry `/collection-monitor` references
  `index-D20tIt_d.js`, and the immutable image contains the reviewed
  `CollectionMonitor-Kv-JhNao.js` chunk in the actual `/app/static` runtime
  directory;
- authenticated production DOM evidence showed, for example, HK capital
  distribution `内容更新 2026-08-03T15:33:00+08:00` separately from
  `巡检 2026-08-03T15:33:58.861720+08:00`.

This acceptance proves timestamp provenance and presentation. It does not turn
missing dataset rows into healthy evidence and does not substitute for the
remaining lower-layer live semantic acceptance.
