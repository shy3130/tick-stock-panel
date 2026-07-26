# Collection Monitor TickFlow Integration

Status: authoritative

Source: `docs/superpowers/specs/2026-07-26-collection-monitor-integration-design.md`

## REQ-COLLECTION-MONITOR-PROXY-001

TickFlow MUST expose only GET routes for `/api/collection-monitor/overview`,
`/markets/{market}`, `/tasks`, and `/gaps`. It MUST construct fixed upstream
paths using `LONGBRIDGE_API_URL`, validate canonical market/date/status/
technology/dataset/mode/symbol/pagination values, bound timeouts and result
sizes, preserve the upstream evidence-unavailable 503 meaning, and sanitize all
other upstream/network failures. It MUST NOT expose an arbitrary proxy, a
mutation method, an internal endpoint, a credential, or a raw upstream error.

## REQ-COLLECTION-MONITOR-PAGE-001

TickFlow MUST provide an authenticated native page at `/collection-monitor`
with desktop and mobile navigation access. The page MUST expose the four
evidence levels (daily overview, market matrix, task rows, gap rows), filters,
freshness, observation mode, provenance, and bounded last-confirmed evidence.
Unavailable, degraded, shadow, stale, and live states MUST remain visually and
semantically distinct. The page MUST NOT provide controls that mutate
collectors, schedules, alerts, or evidence.

## REQ-COLLECTION-MONITOR-PREACCEPTANCE-001

The 2026-07-26 user authorization permits deploying this observation-only
integration before live trading evidence exists. Until lower-layer semantic
acceptance is performed on Monday 2026-07-27, all acceptance records MUST remain
pending and no UI, deployment report, test, or review may claim that live
collection correctness has been accepted.

## Acceptance

Executable contract and component tests are necessary but are not semantic
proof. Deployment evidence MUST include the exact image/version, successful
health and route checks, unchanged collector restart counters, and an honest
pre-acceptance state. Monday acceptance MUST compare real source evidence for
minute K-lines, capital flow, order book, and large/medium/small-order capital
data before the integration may be marked accepted.
