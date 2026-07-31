# Dow Monitor Daily Payload Safety Acceptance

Requirement: `REQ-DOW-MONITOR-DAILY-PAYLOAD-SAFETY-001`

Status: local semantic acceptance passed; production verification pending.

## Required evidence

- A failing-then-passing HTTP-boundary test proving non-finite values are not
  serialized.
- Pure normalization tests covering invalid OHLC, negative volume, duplicate
  timestamps, and insufficient history.
- A service test proving one insufficient timeframe does not suppress another
  timeframe.
- Production evidence from 192.168.10.28 showing RNG.US no longer receives a
  daily evaluation HTTP 400.

## Local semantic evidence

At `2026-07-31T10:19:58+08:00`:

```powershell
Set-Location backend
uv run pytest ../tests/backend/test_dow_monitor_bar_safety.py tests/test_dow_monitor_api.py -q
```

Result: `44 passed`.

The HTTP-boundary test first failed with
`ValueError: Out of range float values are not JSON compliant: nan`, then
passed after the sanitizer was connected. Separate literal fixtures verify
invalid OHLC rejection, negative-volume rejection, latest-valid duplicate
selection, and the two-bar lower bound. The service-level fixture verifies a
valid 5-minute result is saved while an insufficient daily timeframe is
classified `ANALYSIS_PAUSED`.

Production evidence remains required before this requirement is complete.
