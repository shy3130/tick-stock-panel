# Canonical factor/replay audit — 2026-07-26

This document summarizes the deterministic replay after fixing universe sampling.
It is an audit record, not fresh out-of-sample evidence.

## Universe contracts

- P7/P8 range `2025-01-01` through `2026-06-24`: 400 requested symbols,
  SHA-256 `4830c52fc92d05ed2aea8abeaa43ccec8b6558668cebc11c37e62e75de6c744a`.
- P9/B/regime/composition range `2024-09-24` through `2026-06-30`: 400 symbols,
  SHA-256 `5e2a6b75dcfb4d617d55c8fbbfda6480ca608d05f2b44bed364452fd47e62efd`.
- Selection is deduplication, lexicographic sorting, then local
  `random.Random(seed).sample`. Full symbol lists are stored in each manifest.

## Results

- P8 semantic test: all six semantic factors failed Bonferroni significance.
  `mom_trend` had test Sharpe `+3.098`, but adjusted p-value `0.213` and therefore
  did not pass.
- P9 factor-level replay: `mom_trend` was positive in 4/4 folds but beat the
  random null in only 2/4. Every semantic factor failed the preregistered robust rule.
- Real engine replay: `mom_trend` averaged `-3.65%` with 1/4 positive folds;
  `pullback_to_support` averaged `-0.34%` with 2/4 positive folds;
  `bullish_alignment` averaged `-1.74%` with 2/4 positive folds.
- Regime replay: `switch_ew` averaged `+0.27%` with 3/4 positive folds, but lost
  `-17.19%` in F4 while its equal-weight MA60 signal remained bearish throughout.
  It did not pass promotion.
- Fixed 30/70 strategy composition improved some historical relative contrasts,
  but all configurations lost money in the 15-day fresh OBS1 window. The gate is
  `PENDING_DATA`: 15 observed, 60 minimum, 120 target, 45 days remaining to minimum.

## Authority

Machine-readable JSON is authoritative. Old Markdown/HTML reports from the
nondeterministic runs are quarantined under `legacy_nondeterministic_20260726/`.
No strategy currently passes the robust promotion boundary.
