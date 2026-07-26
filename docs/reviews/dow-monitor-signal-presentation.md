# Dow Monitor Signal Presentation Independent Review

Status: complete

The independent requirements-to-evidence review will verify that:

- every visible signal is a strict trend-line plus structure-level break;
- cross-session retest confirmation is not discarded;
- incomplete retests remain hidden;
- stable `B` and `S` pin labels survive the production build while hover
  content remains Chinese;
- mini and detail charts consume the same mapped signal semantics.
- one expanded-chart switch controls trend, support, and resistance lines
  across intraday and daily timeframes without controlling signal markers.
- replay-failed raw signals remain available but are presented as false
  breakouts rather than actionable trades;
- marker display coordinates do not replace backend trigger prices.

## Result

The requirements are satisfied by the mapping tests, production build,
immutable release checks, payload inspection, browser inspection, and
post-restart verification.

The review found no remaining semantic mismatch:

- direct and primary paths still require their established double-break codes;
- buy retests require `FIRST_ACCEPTANCE_HIGH_BROKEN`;
- sell and risk retests require `FIRST_ACCEPTANCE_LOW_BROKEN`;
- incomplete retests remain excluded;
- the pin glyph no longer depends on CJK Canvas rendering;
- Chinese reader-facing hover content is preserved.
- the line switch is present exactly once, defaults on, persists across
  timeframe changes, and leaves buy/sell markers independently visible.
- the quality layer changes only presentation: `FAILED` produces an orange
  ASCII `F`, while valid buy and sell signals retain red `B` and green `S`;
- renderer tests and production inspection independently confirm that signal
  pins use candle-high coordinates with upward clearance and hover content
  still reports the original backend price.
