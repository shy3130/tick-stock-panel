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
