# Independent Review: Dow Monitor Hourly AI Manual Rerun

Status: local requirements-to-evidence review passed; production verification pending

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
| Realtime/formal-signal/minute/WebSocket isolation | New writes are restricted to the rerun request and hourly analysis repositories; model call remains worker-only | Source-path review plus full existing frontend/backend regression suites | Pass locally |
| Desktop/mobile usability | Existing single-column report stays intact; compact header action uses non-wrapping controls and scrollable history | TypeScript build and component behavior test | Pass locally |

## Lower-layer acceptance

The request model/repository semantics were accepted before the API, the API
before the worker, and the worker before the frontend. Tests assert stored
identity, cutoff isolation, replacement behavior, and failure preservation;
the passing bundle or snapshot was not treated as semantic proof.

## Residual release checks

The implementation is not yet deployed. Release acceptance must verify the new
ClickHouse table on 10.28, one live queued-to-completed transition, replacement
of the chosen checkpoint, the served bundle hash, 3018 health, and unchanged
AI-worker/19912/WebSocket process identities. No claim of production
availability is made by this review.
