# Dow Monitor Symbol Identity

## REQ-DOW-MONITOR-SYMBOL-IDENTITY-001

The Dow monitor MUST use one canonical identity for every internal data query,
association, cache key, and immutable decision lookup. Numeric Hong Kong
symbols MUST be canonicalized by removing leading zeroes (`01347.HK` becomes
`1347.HK`, `0981.HK` becomes `981.HK`); mainland China and United States
symbols MUST retain their exchange-qualified codes. A monitored display alias
MAY retain leading zeroes, but WebStock, Redis, ClickHouse, capital, minute
bars, and historical aliases MUST associate through the canonical identity and
MUST NOT create duplicate stocks or decisions.

Capital availability MUST preserve a real numeric zero and distinguish no
same-day capital record, delayed capital, insufficient points for minute
windows, and complete capital. A display alias mismatch MUST NOT be reported
as missing capital.
