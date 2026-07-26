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
