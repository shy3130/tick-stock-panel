# Quant Lab Research Agent

Quant Lab creates a persistent, evidence-first research memo for one A-share
instrument. It is available from `/quant-lab` after the normal application
login.

## Flow

Each run has its own record and follows a fixed server-side path:

```text
validated instrument -> bounded plan -> allowlisted collectors
-> redacted evidence record -> cited synthesis
```

The planner records the requested scope, while the full research action
deliberately collects every supported evidence group so missing data is visible
rather than silently omitted. A collector failure becomes an `unavailable`
evidence card; it does not make unrelated evidence disappear.

## Evidence groups

- local daily data, indicators, and calculated price levels
- realtime cache with its actual market timestamp and service status
- local financial tables plus enabled finance/valuation provider responses
- concepts, industry, funds, popularity, and Dragon Tiger queries
- enriched technical signals, strategy cache, and monitor results
- Eastmoney research reports and announcements
- optional Hibor research reports when its plugin is available
- optional fixed public RSS search results

Public RSS titles and summaries are leads only. They must be verified against
their original source, a company disclosure, or a primary data provider before
being treated as factual. The agent does not fetch arbitrary model-provided
URLs or execute any content returned by a provider.

## API

All endpoints use the application's normal session authentication.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `POST` | `/api/research-agent/runs` | Create an asynchronous run. |
| `GET` | `/api/research-agent/runs` | List recent runs. |
| `GET` | `/api/research-agent/runs/{id}` | Retrieve a run, its evidence, and result. |

`symbol` must use the canonical `000001.SZ`, `600000.SH`, or `430001.BJ`
format. `include_web_news` defaults to `true`.

## Reliability and storage

Run records are stored in `data/user_data/research_agent_runs.json` with mode
`0600`. Creation has a small active-run limit and each queued record has an
atomic claim step, so duplicate requests cannot run the same collection twice.
Interrupted or stalled active records are marked failed after the bounded stale
window and retain any evidence already collected.

Evidence is JSON-normalized before persistence. Credential-shaped fields,
provider tokens, cookies, and Hibor session URLs are redacted. Source URLs are
restricted to `http` and `https` and query parameters with credential-shaped
names are removed. The configured AI provider receives only this bounded
evidence pack; it does not receive provider credentials, filesystem access,
shell access, or arbitrary network tools.

The synthesis input has both per-source and total size limits. It reserves
space for later source cards so a large response cannot hide announcements,
reports, or web-search evidence. Full compacted records remain available in
the UI for review.

## Operating notes

- A successful task means collection and cited synthesis completed, not that
  every external source had data. Check each evidence card's status and time.
- Outside A-share trading hours, a realtime cache is a last-known snapshot,
  not a current executable price.
- Finance, report, minute, order-book, and Dragon Tiger coverage depends on
  enabled providers and their permissions. Missing capabilities are shown as
  unavailable evidence rather than silently substituted data.
- Research output is a review artifact, not a trading instruction, forecast,
  position-sizing recommendation, or guarantee.
