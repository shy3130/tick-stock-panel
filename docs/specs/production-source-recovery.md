# Production Source Recovery

## REQ-TICKFLOW-PRODUCTION-SOURCE-RECOVERY-001

The repository MUST reproduce image
`sha256:7ea697ab7204eed75a26d83ca3a2dda6743397c461003097a7563e8b5b66ddc2`
without replacing the running production container during recovery.

Backend source MUST match the image manifest. Frontend source MUST come from
the frozen matching build archive, contain no source newer than the image
build, pass its executable tests, and build a candidate that preserves the
`/dow-monitor`, its stock code/name search and suggestion panel,
`/api/dow-monitor/symbols`, Dow screener view and strategy proxy API,
single-stock preview, and `/api/intraday/stream` behaviors.

The recovery MUST also preserve the 1542 production behavior that treats
zero-padded and non-zero-padded Hong Kong symbols as one monitor identity.

If the frozen frontend source cannot type-check while the authoritative image
contains a working static bundle, the recovery MAY include the smallest
test-first source-consistency repair needed to reproduce the existing runtime.
That repair MUST preserve the existing API contract and have executable
behavioral coverage.
