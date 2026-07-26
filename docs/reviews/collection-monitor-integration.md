# Collection Monitor TickFlow Integration Independent Review

Status: pending live semantic acceptance

Reviewed source commit: `f723af11e76ab05aa3b08ef19db0d652ddd5c813`.

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

No blocking requirement-to-evidence finding remains for this observation-only
deployment. The overview aggregator currently reports a live evidence envelope,
but the Sunday task/gap and dataset evidence remains gray or unavailable; this
is a freshness/availability signal, not semantic acceptance. The pre-existing
`longbridge-core-index-quotes.service` restart counter is 549 and was unchanged
by this deployment.

Final approval remains blocked on the lower-layer live semantic acceptance.
