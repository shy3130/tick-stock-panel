# Dow Monitor Hourly AI Decision-First View Design

## Status and authority

- Status: design approved in conversation; pending written-spec review
- Approved direction: user selected option A on 2026-08-02
- Parent specification: `SPEC-DOW-MONITOR-HALF-HOUR-AI-ANALYSIS-001`
- Proposed requirement: `REQ-DOW-MONITOR-HOURLY-AI-DECISION-FIRST-VIEW-001`

## Problem

The hourly intraday AI dialog currently renders every structured report section
open in sequence. It is not a Markdown-rendering problem: the frontend renders
structured JSON with React components. The resulting hierarchy is too weak for
fast decisions, and the three next-stage condition lists are laid out side by
side, causing long Chinese sentences to crowd or appear mixed together.

The user needs the conclusion before the evidence and should not need to read
the whole report to understand the current opportunity, risk, and next action.

## Chosen approach

Use one decision-summary card at the top and put the complete analytical
evidence behind one explicit disclosure control. This preserves all existing
report facts while changing the default reading path from evidence-first to
decision-first.

Tabs were rejected because they hide report scope and add navigation. Multiple
independent accordions were rejected because they create too many controls and
make it harder to restore the complete report context.

## Default visible content

The first screen MUST show, in this priority order:

1. A prominent one-sentence conclusion using the existing report headline and
   headline summary.
2. The current opportunity/risk direction.
3. One concise conditional sentence for existing holders.
4. One concise conditional sentence for users not yet involved.
5. Exactly three separately labelled next-stage condition groups when data is
   available:
   - strengthening confirmation;
   - weakening/risk signal;
   - invalidation condition.

Report metadata such as checkpoint, stage interval, trading-minute count, and
model/data context MUST remain available but MUST be visually secondary to the
conclusion.

Empty optional values MUST be omitted instead of producing empty blocks.

## Complete-analysis disclosure

The remaining structured report MUST be closed by default behind a single
control labelled in the form `展开完整分析（分钟路径、形态、量价、数据质量）`.
The control MUST expose its expanded/collapsed state accessibly and allow the
user to close the content again.

Expanded content MUST preserve the semantic facts required by
`REQ-DOW-MONITOR-HOURLY-AI-STAGE-REPORT-001`, grouped for readability as:

1. **本小时发生了什么**
   - current-stage minute path;
   - minute changes hidden by the endpoint;
   - comparison with the previous completed stage.
2. **当日整体结构与量价资金**
   - cumulative day-to-now structure;
   - channel and pattern assessment;
   - volume, price, and capital-flow interpretation.
3. **分析依据与数据质量**
   - confidence;
   - limitations and missing data;
   - report metadata that is not needed for the first decision scan.

Holder guidance, watcher guidance, and next-stage conditions MUST NOT be
duplicated inside the expanded content.

## Next-stage condition layout

The three condition groups MUST render as independent vertical rows or cards,
not as an inline heading followed by a nested list inside a three-column grid.
Each condition item MUST occupy its own list row. Long text MUST wrap naturally
without overlapping adjacent groups.

The visual meaning is:

- strengthening confirmation: positive/accent treatment;
- weakening/risk signal: warning treatment;
- invalidation condition: neutral treatment.

Colour MUST not be the only way to distinguish the groups; each group retains
its text label.

## Responsive behavior

Desktop and mobile use the same information order. Mobile MUST remain a single
column. The decision summary, both audience-specific guidance lines, and the
three next-stage condition groups MUST remain visible without horizontal
scrolling. The complete analysis continues to open inside the existing
separate AI dialog.

## Data and compatibility

This change is presentation-only. It MUST NOT alter the hourly worker, model
prompt, report schema, ClickHouse persistence, formal signals, real-time key
interpretation, or WebSocket ingestion.

Existing structured report fields remain authoritative. Legacy 30-minute
reports continue through the current legacy renderer. The new view MUST NOT
introduce Markdown parsing. Plain structured strings and arrays are rendered
as text and list items so model-produced markup cannot alter page structure.

## Acceptance scenarios

1. A complete hourly report opens with its conclusion and actionable guidance
   visible before any minute-path evidence.
2. The full evidence is absent from the initial visible region, can be opened
   with one control, and can be closed again.
3. Strengthening, risk, and invalidation conditions render as three vertical,
   clearly labelled groups with no mixed or overlapping text.
4. Missing optional guidance or condition items do not create empty headings.
5. Expanding the report exposes all required analytical evidence and data
   limitations from the existing structured payload.
6. The layout remains single-column and readable at the supported mobile width.
7. Legacy report rendering and backend report generation remain unchanged.

## Planned evidence

- Component tests will verify default visibility, disclosure behavior, section
  grouping, next-stage condition layout, omission of empty values, and mobile-
  safe structure.
- The existing frontend contract test will verify that the separate AI entry
  and structured-report fetch behavior remain intact.
- A semantic acceptance record and independent requirements-to-evidence review
  will be completed before release.
