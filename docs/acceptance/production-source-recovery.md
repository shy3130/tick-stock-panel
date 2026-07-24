# Production Source Recovery Acceptance

Status: not yet accepted

Requirement: `REQ-TICKFLOW-PRODUCTION-SOURCE-RECOVERY-001`

Authoritative image:
`sha256:fcf690148cb121e4abf328ae8d38a90a39ee83c9ef3ae4bf3d8e298348d2793a`

## Lower-layer verification

Pending final commit recording. The preliminary `build-source.tar.gz` was
rejected because its broad `--exclude=data` rule omitted
`frontend/src/components/data/`. The authoritative 1502 source input is
`build-source-v2.tar.gz`, verified by `SHA256SUMS-v2`; the authoritative 1542
advance is `20260724-1542/build-source.tar.gz`, followed by the authoritative
`20260724-1605/build-source.tar.gz`; all are checksum-verified and explicitly
contain `EnrichedRebuildPanel.tsx`.

The corrected source exposed a pre-existing date-dependent backend test. Its
market date is now fixed inside the test without changing image-authoritative
backend code. The recovered frontend also has executable coverage proving that
an Enriched rebuild propagates the API `job_id` to the owning data page. The
1542 HK alias test was observed RED against the recovered 1502 source and must
be GREEN after importing the image-authoritative 1542 backend.
The 1605 transient-connectivity test was likewise observed RED against the
recovered 1542 frontend and must be GREEN after importing the 1605 card.

Record timestamps, exact recovered commit, manifest result, backend test
counts, frontend test counts, and frontend build result here.

Commands:

```text
python scripts/check_spec_compliance.py
uv run --project backend pytest tests/spec_contracts/test_spec_guard_contract.py tests/spec_contracts/test_production_source_recovery.py tests/spec_contracts/test_production_source_semantics.py -v
pnpm --dir frontend exec vitest run src/pages/dow-monitor-route.test.tsx src/pages/DowMonitor.test.tsx src/components/dow-monitor/DowMonitorDetailDialog.test.tsx src/pages/Screener.dow-strategy.test.tsx src/components/screener/DowStrategyCard.test.tsx src/components/data/EnrichedRebuildPanel.test.tsx
pnpm --dir frontend build
```

## Runtime semantic acceptance

Pending. Record candidate image ID, `/dow-monitor`, stock search suggestions,
`/api/dow-monitor/symbols`, `/screener`, Dow strategy proxy, health,
single-stock preview, SSE observations, and confirmation that production
remained on the authoritative image.
