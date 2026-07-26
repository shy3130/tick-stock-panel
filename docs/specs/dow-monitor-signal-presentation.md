# Dow Monitor Signal Presentation

## REQ-DOW-MONITOR-STRICT-DOUBLE-BREAK-001

The Dow monitor mini chart and detail chart MUST use the same turning-signal
mapping.

A visible buy or sell marker MUST contain both a causal trend-line break and a
causal structure-level break. Direct and primary structure paths remain
eligible. A `TWO_BAR_RETEST` path is eligible only when:

- a buy contains `FIRST_ACCEPTANCE_HIGH_BROKEN`; or
- a sell contains `FIRST_ACCEPTANCE_LOW_BROKEN`.

The trend-line value and structure-pivot price MUST both be present. A retest
that only closes on the expected side of the trend line is not sufficient.
Anchors and structure levels MUST be calculated only from bars available at
the signal time. The mapping MUST preserve a valid cross-session confirmation
instead of removing it because the first break occurred on the prior session.

## REQ-DOW-MONITOR-CHINESE-MARKER-001

Visible buy and sell chart pins MUST display the stable labels `B` and `S`.
Risk exits MUST display `R`. The pin label MUST NOT depend on a CJK canvas
glyph and MUST NOT fall back to a question mark.

The hover title and content MUST remain Chinese reader-facing text while retaining the
causal trend-line value, structure level, timestamps, confirmation status,
volume ratio, and evidence.
