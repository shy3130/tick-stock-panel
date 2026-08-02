# Independent Review: Dow Monitor Hourly AI Manual Rerun

Status: local and production requirements-to-evidence review passed

Requirement under review:
`REQ-DOW-MONITOR-HOURLY-AI-MANUAL-RERUN-001`.

Reviewed on: 2026-08-02

## Requirement-to-evidence review

| Normative clause | Implementation | Independent evidence | Result |
| --- | --- | --- | --- |
| Selected stored hourly report only | Service validates identity/frequency; dialog sends selected history ID | API eligibility tests and selected-history component test | Pass |
| Durable request | ClickHouse request table and repository round trip | Repository SQL/serialization test | Pass |
| One active request per analysis | Service critical section plus active-request lookup | Sequential API test and concurrent two-caller regression test | Pass for the single 3018 process topology |
| Independent worker execution | API only persists; worker claims queued/stale-running work before scheduled scan | Worker off-session and failure tests | Pass |
| Off-session manual authority | Approved decision `DEC-20260802-DOW-MONITOR-HOURLY-AI-MANUAL-RERUN-SESSION-001`; no calendar creation gate on manual path | Off-session worker test | Pass |
| Exact stored trade date/window/cutoff; no later data | Worker rebuilds from source identity and passes original cutoff to minute repository/snapshot | Test includes a later minute and verifies exclusion | Pass |
| Success-only same-report replacement | Worker preserves `analysis_id`/logical key and increments attempt only after validated model output | Success worker test | Pass |
| Failure preserves prior report and is retryable | Request failure is stored separately; analysis is not overwritten | Failure worker test and frontend failure path | Pass |
| Invalid provider JSON is bounded | Prompt service performs at most one schema repair and then fails closed | Repair-success and repair-failure call-count tests | Pass |
| Legacy 30-minute read-only | Service rejects and frontend hides action unless frequency is hourly | API parametrized eligibility test and component condition | Pass |
| Realtime/formal-signal/minute/WebSocket isolation | New writes are restricted to the rerun request and hourly analysis repositories; model call remains worker-only | Source-path review, full regression suites, unchanged 19912/ingestion PIDs, and live WebSocket smoke | Pass |
| Desktop/mobile usability | Existing single-column report stays intact; a completed rerun retains the explicit action label, completion is a separate hint, and legacy reports are visibly read-only | TypeScript build, component behavior tests, and served production chunk | Pass |

## Lower-layer acceptance

The request model/repository semantics were accepted before the API, the API
before the worker, and the worker before the frontend. Tests assert stored
identity, cutoff isolation, replacement behavior, and failure preservation;
the passing bundle or snapshot was not treated as semantic proof.

## Production verdict

The additive ClickHouse table, exact served bundle, healthy 3018 and worker,
unchanged 19912/market-ingestion PIDs, and live WebSocket path were observed on
10.28. Two invalid provider outputs first demonstrated failure preservation.
After the bounded one-repair fix, a third request completed and replaced the
same NBIS logical report at attempt 2. This supplies the missing lower-layer
success and failure evidence. The requirement is accepted in production.

The follow-up visibility review traced the reported missing action to UI state,
not API availability: a completed request had renamed the action to `已更新`,
while legacy reports intentionally hid it. Commit `5125cf316a9d` keeps the
hourly action explicit, separates completion status, and exposes the legacy
read-only reason without weakening the backend eligibility gate. Component
behavior, the frontend contract, full build, exact production bundle, live DOM,
unchanged worker/19912/ingestion identities, and WebSocket smoke all passed.
No blocking requirement-to-evidence gap remains.
