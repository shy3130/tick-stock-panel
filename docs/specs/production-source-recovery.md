# Production Source Recovery

## REQ-TICKFLOW-PRODUCTION-SOURCE-RECOVERY-001

The repository MUST reproduce image
`sha256:214529b2aae4356c1b22c872d111aa1b425fc9b9184d7b8349b4e9590471b0b6`
without replacing the running production container during recovery.

Backend source MUST match the image manifest. Frontend source MUST come from
the frozen matching build archive, contain no source newer than the image
build, pass its executable tests, and build a candidate that preserves the
`/dow-monitor`, `/api/dow-monitor/symbols`, Dow screener view and strategy
proxy API, single-stock preview, and `/api/intraday/stream` behaviors.
