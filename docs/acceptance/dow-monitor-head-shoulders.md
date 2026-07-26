# Dow Monitor Head-And-Shoulders Frontend Acceptance

## Scope

- Requirement: `REQ-DOW-HEAD-SHOULDERS-SIGNAL-001`
- Frontend branch: `codex/dow-monitor-clean`
- Backend contract reviewed from `longbridge-stock` commit `704105ac`
- Existing Dow double-break markers and signal price lines remain separate and unchanged.

## TDD Evidence

### RED

Command:

```text
pnpm test --run src/components/dow-monitor/DowMonitorDetailDialog.test.tsx -t "isolates the head-and-shoulders switch"
```

Observed failure:

```text
Unable to find an accessible element with the role "switch" and name "头肩形态"
```

The failure demonstrated that the independent detail-chart control did not
exist before the production implementation.

The projected-neckline test was also observed failing until the second
neckline anchor date and price were retained independently in the overlay
contract.

## Semantic Acceptance

The executable detail-dialog tests verify:

- complete causal A/N1/B/N2/C/D point mapping and projected neckline geometry;
- preservation of both backend neckline anchors and trigger-time neckline value;
- independent red buy markers only for confirmed head-and-shoulders bottoms;
- independent green sell markers only for confirmed head-and-shoulders tops;
- no formal marker for forming, watch, weak-break, failed, or false-break states;
- orange warning presentation for false breaks;
- Chinese opaque hover content with point dates/prices, volume ratio,
  confirmation stage, invalidation price, scores, and translated evidence;
- omission of incomplete patterns and internal enum/rule codes;
- default-on `头肩形态` switch isolation from moving averages, existing Dow
  markers, and existing Dow trend/level lines.

## Requirements-To-Evidence Review

The implementation was reviewed from the authoritative requirement through
the typed payload, runtime mapping, chart-series construction, detail-dialog
control, and executable assertions. The independent head-and-shoulders stream
does not call or modify the strict Dow double-break mapper. The switch changes
only the `headShouldersOverlays` prop, while existing `markers` and
`priceLines` remain byte-for-byte stable in the interaction test.

## Production Payload Preservation

The backend client now validates `headShoulders` through dedicated strict
Pydantic models while retaining the engine contract's global
`extra="forbid"` policy. The monitor service copies the validated payload into
`detail.chart.headShoulders`, preserving the independent signal family from
the engine response through the detail API.

The backend RED run failed both focused tests because `headShoulders` was an
extra forbidden field. After the production change, the same focused command
passed `2 passed, 19 deselected`.

The frontend contract fixture now uses detector-produced lifecycle evidence
codes (`BREAK_WATCH` and `CONFIRMED`). The RED run rendered
`暂无补充证据`; after adding contract-faithful translations, the focused test
renders meaningful Chinese evidence and verifies that internal codes are
absent.

## Final Verification

Final command outputs after the implementation freeze:

- `uv run --extra dev pytest tests/test_dow_monitor_api.py -q` passed:
  `22 passed`.
- `uv run --extra dev ruff check --ignore RUF001 ...` passed for the changed
  backend client, service, and integration test. Without that narrow ignore,
  Ruff reports three pre-existing full-width comma warnings in unrelated
  monitor-service text.
- `pnpm test --run src/components/dow-monitor/DowMonitorDetailDialog.test.tsx
  src/components/dow-monitor/useDowMonitor.test.tsx` passed: `22 passed`.
- `pnpm test --run src/components/dow-monitor/DowMonitorDetailDialog.test.tsx`
  passed: `15 passed`.
- `pnpm test --run` completed with `141 passed` and one unrelated existing
  failure in `src/pages/Screener.dow-strategy.test.tsx`: the test expects
  the legacy multi-timeframe Dow strategy title, but the rendered Screener
  page does not contain it. The failing file is outside this task's allowed
  scope and also fails
  when run independently.
- `pnpm build` passed (`tsc -b && vite build`, 2706 modules transformed).
- `python scripts/check_spec_compliance.py` reported repository indexing
  constraints: the monorepo checker rejects executable tests under
  `backend/tests/` and frontend co-located Vitest paths because they are not
  under the repository-root `tests/`; it also continues to report the existing
  indexed review-path issue.
