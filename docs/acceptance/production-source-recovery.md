# Production Source Recovery Acceptance

Status: not yet accepted

Requirement: `REQ-TICKFLOW-PRODUCTION-SOURCE-RECOVERY-001`

Authoritative image:
`sha256:214529b2aae4356c1b22c872d111aa1b425fc9b9184d7b8349b4e9590471b0b6`

## Lower-layer verification

Pending. Record timestamps, exact recovered commit, manifest result, backend
test counts, frontend test counts, and frontend build result here.

Commands:

```text
python scripts/check_spec_compliance.py
uv run --project backend pytest tests/spec_contracts/test_spec_guard_contract.py tests/spec_contracts/test_production_source_recovery.py tests/spec_contracts/test_production_source_semantics.py -v
pnpm --dir frontend exec vitest run src/pages/dow-monitor-route.test.tsx src/pages/DowMonitor.test.tsx src/components/dow-monitor/DowMonitorDetailDialog.test.tsx src/pages/Screener.dow-strategy.test.tsx src/components/screener/DowStrategyCard.test.tsx
pnpm --dir frontend build
```

## Runtime semantic acceptance

Pending. Record candidate image ID, `/dow-monitor`,
`/api/dow-monitor/symbols`, `/screener`, Dow strategy proxy, health,
single-stock preview, SSE observations, and confirmation that production
remained on the authoritative image.
