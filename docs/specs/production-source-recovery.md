# Production Source Recovery

## REQ-TICKFLOW-PRODUCTION-SOURCE-RECOVERY-001

The repository MUST reproduce image
`sha256:de3adcadb363856453df72227ff92b69ada818f331c8ccfc4e3001b48f41a721`
without replacing the running production container during recovery.

Backend source MUST match the image manifest. Frontend source MUST come from
the frozen matching build archive, contain no source newer than the image
build, pass its executable tests, and build a candidate that preserves the
`/dow-monitor`, its stock code/name search and suggestion panel,
`/api/dow-monitor/symbols`, Dow screener view and strategy proxy API,
single-stock preview, and `/api/intraday/stream` behaviors.

If the frozen frontend source cannot type-check while the authoritative image
contains a working static bundle, the recovery MAY include the smallest
test-first source-consistency repair needed to reproduce the existing runtime.
That repair MUST preserve the existing API contract and have executable
behavioral coverage.
