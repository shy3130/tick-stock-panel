# Production Source Recovery

## REQ-TICKFLOW-PRODUCTION-SOURCE-RECOVERY-001

The repository MUST reproduce image
`sha256:fcf690148cb121e4abf328ae8d38a90a39ee83c9ef3ae4bf3d8e298348d2793a`
without replacing the running production container during recovery.

Backend source MUST match the image manifest. Frontend source MUST come from
the frozen matching build archive, contain no source newer than the image
build, pass its executable tests, and build a candidate that preserves the
`/dow-monitor`, its stock code/name search and suggestion panel,
`/api/dow-monitor/symbols`, Dow screener view and strategy proxy API,
single-stock preview, and `/api/intraday/stream` behaviors.

The recovery MUST also preserve the 1542 production behavior that treats
zero-padded and non-zero-padded Hong Kong symbols as one monitor identity.
It MUST preserve the 1605 behavior that safely probes and retries service
connectivity before starting the non-idempotent Dow strategy scan.

If the frozen frontend source cannot type-check while the authoritative image
contains a working static bundle, the recovery MAY include the smallest
test-first source-consistency repair needed to reproduce the existing runtime.
That repair MUST preserve the existing API contract and have executable
behavioral coverage.
