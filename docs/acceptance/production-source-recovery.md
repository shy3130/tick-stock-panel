# Production Source Recovery Acceptance

Status: not yet accepted

Requirement: `REQ-TICKFLOW-PRODUCTION-SOURCE-RECOVERY-001`

Authoritative image:
`sha256:de3adcadb363856453df72227ff92b69ada818f331c8ccfc4e3001b48f41a721`

## Lower-layer verification

Pending final commit recording. The preliminary `build-source.tar.gz` was
rejected because its broad `--exclude=data` rule omitted
`frontend/src/components/data/`. The authoritative source input is
`build-source-v2.tar.gz`, verified by `SHA256SUMS-v2` and by explicit presence
of `EnrichedRebuildPanel.tsx`.

The corrected source exposed a pre-existing date-dependent backend test. Its
market date is now fixed inside the test without changing image-authoritative
backend code. The recovered frontend also has executable coverage proving that
an Enriched rebuild propagates the API `job_id` to the owning data page.

Record timestamps, exact recovered commit, manifest result, backend test
counts, frontend test counts, and frontend build result here.

Commands:

```text
python scripts/check_spec_compliance.py
uv run --project backend pytest tests/spec_contracts/test_spec_guard_contract.py tests/spec_contracts/test_production_source_recovery.py tests/spec_contracts/test_production_source_semantics.py -v
pnpm --dir frontend exec vitest run src/pages/dow-monitor-route.test.tsx src/pages/DowMonitor.test.tsx src/components/dow-monitor/DowMonitorDetailDialog.test.tsx src/pages/Screener.dow-strategy.test.tsx src/components/screener/DowStrategyCard.test.tsx
pnpm --dir frontend build
```

## Runtime semantic acceptance

Pending. Record candidate image ID, `/dow-monitor`, stock search suggestions,
`/api/dow-monitor/symbols`, `/screener`, Dow strategy proxy, health,
single-stock preview, SSE observations, and confirmation that production
remained on the authoritative image.
