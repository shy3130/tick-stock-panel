# Collection Monitor TickFlow Integration Design

## Decision

Integrate the existing observation-only collection monitor as a native TickFlow
page on port 3018. TickFlow's backend will expose a same-origin, GET-only proxy
to the already deployed Longbridge API on port 19912. The frontend will use the
existing TickFlow router, navigation, authentication shell, and visual tokens.

This is the recommended option selected under the user's standing instruction
to use the recommended choice by default. An iframe was rejected because it
duplicates the application shell and complicates authentication and origin
policy. A second standalone frontend was rejected because it creates another
deployment and support surface.

## Scope

- Add four allowlisted read-only proxy routes: overview, market, tasks, gaps.
- Add a native page at `/collection-monitor` and a navigation entry.
- Preserve upstream `503 collection_monitoring_evidence_unavailable` honestly.
- Show daily summary, market matrix, task detail, gap detail, filters, freshness,
  mode, and provenance.
- Keep alerts, actions, acknowledgements, scheduling, repair, and collector
  control out of scope.

## Security and Failure Semantics

The browser never receives the internal Longbridge host or credentials.
TickFlow constructs upstream paths from fixed route templates, validates and
bounds every query parameter, uses a finite timeout, and returns sanitized
errors. It must not accept arbitrary paths or mutation methods.

No cached, synthetic, fallback, or stale value may be relabeled as live.
Unavailable evidence remains unavailable; bounded `lastConfirmed` evidence may
be displayed only with its original timestamp and state.

## Deployment Exception

The user explicitly authorized deployment before Monday's live-data acceptance:
"先部署出来，周一有真实数据了，再进行调试。" This permits only the
observation-only proxy and page to be deployed. It does not waive Monday's
lower-layer semantic acceptance and does not authorize notifications, actions,
collector restarts, Chronicle schedule mutation, or any claim that live
collection is accepted.

## Verification

Automated tests cover proxy allowlisting, validation, sanitization, honest 503
propagation, page route/navigation, and the main evidence states. Deployment
verification checks the new and existing 3018 routes, confirms the container
is stable, and verifies that existing collectors are not restarted. Monday's
live acceptance will validate real minute bars, capital flow, order book, and
large/medium/small-order evidence at the lower layer before the page is judged
semantically correct.
