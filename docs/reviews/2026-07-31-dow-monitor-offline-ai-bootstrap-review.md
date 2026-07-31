status: pending

# Dow Monitor Offline AI Bootstrap Independent Review

The independent review will trace each approved requirement to the materializer
and worker implementation, executable backend tests, and the semantic
acceptance evidence. It will verify that the resolved precedence decision is
implemented narrowly: one latest completed startup checkpoint may precede
`created_at`, while every older checkpoint remains prohibited.

The review will reject screenshots, snapshots, or passing downstream metrics as
substitutes for the lower-layer evidence. It will independently inspect the
logical-key deduplication, data cutoff, bounded backfill, insufficient-data
outcome, per-symbol failure isolation, and proof that no WebSocket, realtime
render, or formal-signal path changed.
