# Production Source Recovery Acceptance

Status: accepted on 2026-07-24 16:23 +08:00

Requirement: `REQ-TICKFLOW-PRODUCTION-SOURCE-RECOVERY-001`

Authoritative image:
`sha256:fcf690148cb121e4abf328ae8d38a90a39ee83c9ef3ae4bf3d8e298348d2793a`

## Lower-layer verification

Recovered commit:
`23a2ae4eda7fecae26ecb14275f536fb7eb58531`

The preliminary 1502 `build-source.tar.gz` was
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
recovered 1542 frontend and became GREEN after importing the 1605 card.

Verified results:

- `SHA256SUMS` for the 1605 source, backend, and static archives: 3/3 OK.
- Backend image manifest: all recovered backend files match image
  `sha256:fcf690148cb121e4abf328ae8d38a90a39ee83c9ef3ae4bf3d8e298348d2793a`.
- Specification compliance and the three root contract files: 4/4 passed.
- Backend characterization: 27/27 passed.
- Frontend characterization: 43/43 passed across six behavior suites.
- Frontend TypeScript and Vite production build: passed, 2705 modules.

Commands:

```text
python scripts/check_spec_compliance.py
uv run --project backend pytest tests/spec_contracts/test_spec_guard_contract.py tests/spec_contracts/test_production_source_recovery.py tests/spec_contracts/test_production_source_semantics.py -v
pnpm --dir frontend exec vitest run src/pages/dow-monitor-route.test.tsx src/pages/DowMonitor.test.tsx src/components/dow-monitor/DowMonitorDetailDialog.test.tsx src/pages/Screener.dow-strategy.test.tsx src/components/screener/DowStrategyCard.test.tsx src/components/data/EnrichedRebuildPanel.test.tsx
pnpm --dir frontend build
```

## Runtime semantic acceptance

Candidate image:
`sha256:b926f4555743d421a73370eec12602d4d36b065f8b7d612ad249f65b7e61032d`

Candidate label ties the image to baseline
`sha256:fcf690148cb121e4abf328ae8d38a90a39ee83c9ef3ae4bf3d8e298348d2793a`
and source commit `23a2ae4e`.

Loopback candidate observations:

- `/health`: HTTP 200, version `0.1.86`.
- `/dow-monitor` and `/screener`: HTTP 200.
- ClickHouse built-in provider registered and four capabilities became active.
- Authenticated `/api/dow-monitor/symbols`: HTTP 200 with 9 symbols.
- Authenticated HK `TENCENT` search: 2 suggestions including `700.HK`.
- Authenticated `700.HK` daily preview query: HTTP 200 with 7 rows.
- Authenticated Dow strategy pool proxy: HTTP 200.
- `/api/intraday/stream`: two 15-second SSE ping frames observed.

The candidate ran as `TickFlow_Recovery_1605` on loopback port 13018. The
production container remained running throughout on the authoritative 1605
tag and image ID; recovery did not replace, stop, or restart it.
