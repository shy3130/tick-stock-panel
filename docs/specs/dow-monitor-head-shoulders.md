# Dow Monitor Head-And-Shoulders Presentation

## REQ-DOW-HEAD-SHOULDERS-SIGNAL-001

The Dow monitor MUST present head-and-shoulders as an independent signal
family. It MUST NOT merge, suppress, rewrite, or reorder the existing Dow
double-break signal stream.

Only a `BOTTOM` pattern in `CONFIRMED` or `RETEST_CONFIRMED` stage produces an
independent buy marker. Only a `TOP` pattern in either confirmed stage produces
an independent sell marker. Forming, watch, weak-break, wick-cross, failed, and
false-break states MUST NOT appear as formal buy or sell markers.

The expanded chart MUST expose an independent `头肩形态` switch. When enabled,
it renders the causal A/N1/B/N2/C/D points and projected neckline. When
disabled, it hides only this shape layer and its independent markers; moving
averages, Dow lines, and existing Dow signals remain unchanged.

Reader-facing hover content MUST be Chinese and include shoulder/head dates and
prices, both neckline anchors, the neckline value at D, breakout volume ratio,
confirmation stage, invalidation price, and quality evidence. Internal enum or
rule codes MUST NOT be shown.

Candidate shapes use neutral styling, confirmed bottoms use red buy styling,
confirmed tops use green sell styling, and false-break shapes use orange
warning styling. Labels and markers MUST remain outside candle bodies.
