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

Each successful upstream response MUST be consumed with streaming decoded-byte
iteration and MUST contain no more than exactly 2,097,152 decoded body bytes
(2 MiB). Decompressed output, rather than compressed transfer size, counts
toward this bound. Successful payloads MUST also satisfy their route shape:
`/overview` is a mapping; `/markets/{market}` is a mapping whose `datasets` list
contains at most the five authoritative dataset keys below with no duplicate;
and `/tasks` and `/gaps` are mappings whose returned item lists contain no more
items than the requested `limit`. Every dataset, task, and gap item MUST be a
mapping. Any size, JSON, or shape violation MUST return only the sanitized 502
proxy-unavailable response, never a raw body, upstream URL, or credential.

The authoritative upstream query contract is:

- `market`: `cn`, `hk`, or `us`.
- `date`: optional, strict valid ISO calendar date `YYYY-MM-DD`.
- `status`: optional `green`, `yellow`, `red`, `gray`, or `unavailable`.
- `technology`: optional `rust`, `websocket`, `python`, or `batch`.
- `dataset`: `capital_distribution`, `capital_flow`, `candlestick_1m`,
  `depth`, or `trades`; required by `/gaps`, optional on `/tasks`.
- `mode`: optional `production`, `shadow`, or `backfill`.
- `symbol`: optional uppercase canonical symbol matching
  `^[A-Z0-9][A-Z0-9._-]{0,31}\.(HK|US|SH|SZ)$`.
- `recovered`: optional canonical boolean accepted by FastAPI and forwarded
  only by `/gaps`.
- `limit`: integer 1 through 500, default 100.
- `offset`: integer 0 through 100000, default 0.

`/overview` accepts only `date`; `/markets/{market}` accepts only `date`;
`/tasks` accepts `date`, `status`, `technology`, `market`, `dataset`, `mode`,
`limit`, and `offset`; `/gaps` accepts required `market` and `dataset` plus
`date`, `symbol`, `recovered`, `limit`, and `offset`. Unknown query parameters
MUST be rejected rather than silently ignored.

The four upstream path templates are identical to the TickFlow paths:
`/api/collection-monitor/overview`,
`/api/collection-monitor/markets/{market}`,
`/api/collection-monitor/tasks`, and `/api/collection-monitor/gaps`.

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
