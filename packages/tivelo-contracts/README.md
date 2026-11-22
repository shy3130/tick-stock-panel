# Tivelo Contracts

Public, application-independent contracts for Tivelo data and backtest engines.
This is an **alpha API**, extracted incrementally alongside the unchanged TSP v1 interfaces.
It is not a backtest implementation or a license to access professional features.

## License

Tivelo-owned implementation is licensed under [PolyForm Noncommercial 1.0.0](LICENSE).
Commercial use outside the license's permissions requires separate written
authorization from the relevant rights holder.

Material previously provided under [MIT](LICENSE-MIT) retains its original
copyright and permission notices. See [NOTICE](NOTICE).

```sh
uv build packages/tivelo-contracts --out-dir dist/tivelo-contracts
uv pip install ./dist/tivelo-contracts/tivelo_contracts-0.1.0a1-py3-none-any.whl
```

From an installed package, use `tivelo_contracts.data`, `market`, `execution`, `capabilities`
and `protocols`. `tivelo_contracts.testing.InMemoryReader` provides a synthetic
reference reader for implementing the public protocol without TSP or Tivelo Data.
The package imports neither `app.*` nor a provider, web framework or storage engine.
The supported Pydantic floor is 2.13.4. Pydantic 2.7 passed model tests but emitted
different, incorrect decimal defaults in the generated schema; it is deliberately
outside the supported dependency range.

## Wire format and field dictionary

The complete field names, types, required fields, enum values, defaults and null
states are exported in [`schemas/contracts-v2.schema.json`](schemas/contracts-v2.schema.json).
Reference a model through `#/$defs/Bar`, `#/$defs/Instrument`, `#/$defs/RunLock`, etc.
JSON Schema describes structure; the Python models also enforce cross-field
invariants (OHLC bounds, time ordering, coverage totals and content hashes).

| Value | Wire representation and meaning |
| --- | --- |
| Prices, quantities, money, factors | Exact decimal **strings**, fitting decimal128(38,12); floats, NaN and infinity rejected |
| Prices | Per quoted unit; OHLC share `quote_currency`; a market policy decides whether negative prices are valid |
| Volume and positions | Shares, contracts or units as explicitly declared; never implicitly lots |
| Proportions | Decimal ratios, e.g. `"0.05"` is 5%; slippage explicitly uses bps |
| Event and knowledge timestamps | ISO 8601 with an explicit timezone; normalized to UTC; naive times and numeric timestamps rejected |
| Trading date | Exchange-assigned calendar date, independent of the UTC date |
| Historical validity | `[valid_from, valid_to)`; unknown end is null |
| Listing/delisting | `known` plus a date, `unknown`, or `not_yet`; the latter two have no date |
| Revisions | Stable record/action/factor identity plus revision ID and availability; cancelled actions/factors retain their historical revisions |
| Bar query range | `[start, end)` by bar start; final bars must also end and be available by `as_of` |
| Coverage | Complete, partial, explained empty and error are different states |
| Parameters | Canonical JSON object stored as immutable text; duplicate keys and non-finite values rejected |
| Content IDs | SHA-256 over `tivelo-json-v1`; package/license signatures use separate protocols |

The v2.0 wire dictionary covers the following identities. Physical field names, types
and nullable flags are also available through `tivelo_contracts.facts.fact_fields`.
Every fact revision adds `source_id` and `revision_id` to the identity in the table.

| Record | Identity within its source | Related values |
| --- | --- | --- |
| Instrument, SymbolMapping | `instrument_id, valid_from` | A display symbol can refer to different instruments at different venues or times. The listing state distinguishes unknown and future dates. |
| Bar | `instrument_id, timeframe, bar_start` | The dataset declares raw, front-adjusted or back-adjusted prices. Missing turnover, VWAP and trade count use JSON null. |
| OpeningObservation | `instrument_id, event_at` | Only the tradable opening state has a raw price. The reference price and its origin travel together. |
| OrderBookObservation | `instrument_id, event_at` | One revision contains raw best bid and ask, latest trade and its timestamp, trading state, currency and knowledge time. Null prices mean confirmed absence. |
| TradePrint | `instrument_id, trading_date, sequence` | One executed trade with raw price, exact amount and unit, aggressor side, event time and knowledge time. The source supplies the exchange sequence. |
| CorporateAction | `instrument_id, action_id` | Split ratios multiply quantity. Cash per unit has a currency, with optional record and payment instants. A later cancellation is a revision. |
| AdjustmentFactor | `instrument_id, definition_id, baseline_id, factor_id` | The factor is strictly positive. Its kind identifies an event ratio or a cumulative series. |
| ExtensionValue | `field_sha256, instrument_id, effective_at` | One versioned research field per dataset. Market-level fields use a null instrument. The observed value, publication, ingestion and optional expiry are recorded separately. |
| Coverage | Embedded in a dataset version | Requested and completed units measure coverage. Row count measures facts. Empty, partial and error responses include their reasons. |
| DatasetVersion | `version_id` hashes the immutable contents | Contents bind scope, coverage, source selection, normalization and artifact digests. |
| SnapshotManifest | `snapshot_id` hashes the sorted dataset references | Each reference contains a dataset ID and its exact version. |

The installed wheel includes the published schema at `tivelo_contracts/wire/contracts-v2.schema.json`.
The JSONL and storage adapters consume the same versioned models. A v2 record with unknown
properties fails validation. Consumers check the negotiated API major before interpreting
a new combination. A higher major requires a compatible reader or an explicit adapter.
Valid wire examples and deliberately invalid replacements cover coverage, symbol mapping,
dataset fingerprints and snapshot references. [Wire examples](src/tivelo_contracts/fixtures/versioned-facts-v2.json)

### Check an independent reader

`OrderBookReader` is an optional protocol. `OrderBookQuery` pins a `DatasetReference`,
selects events in `[start, end)` and returns the latest whole revision visible at
`as_of` for each instrument and event. An order-book dataset declares its instrument
and time scope. Use the same snapshot and dataset references to reproduce a query.

`TradePrintReader` is optional. `TradePrintQuery` pins the dataset version and
trading date and may select a single exchange sequence. Readers select the latest
visible revision before applying the event-time range. A correction that changes
the trade time cannot cause the prior version to reappear.

`ExtensionFieldReader` is an optional reader protocol for user-defined research inputs.
An `extension_value` dataset binds one table and field version, type, unit, time basis and
instrument or market granularity in its scope. `ExtensionQuery` identifies the dataset and
field digest. For the requested subject, filter to known revisions at `as_of` and observations
effective by `effective_at`, select the latest revision of the most recent observation, then
apply its exclusive `valid_until`. An explicit null observation supersedes a previous value.
Market-wide facts use a null instrument and an empty query universe. Numeric values use the
declared integer or exact decimal column. A missing field binding fails before execution.

Load the public synthetic records into a dedicated snapshot and pass the resulting reader
to `verify_reader`. Each query carries an explicit knowledge time. Expected rows include
all prices, units and revision fields. The fixture records and named expected rows are
published as JSON so implementations in other languages can run the same checks.
[Reader test vectors](src/tivelo_contracts/fixtures/reader-v2.json)

```python
from tivelo_contracts.conformance import reader_fixture, verify_reader
from tivelo_contracts.testing import InMemoryReader

records = reader_fixture()
reader = InMemoryReader(**records, knowledge_basis="historical_versions")
checked = verify_reader(reader, execution=True)
assert len(checked) == 18
```

The base suite checks 15 queries for bars, instruments, actions and factors.
The execution suite adds 3 opening queries. It also verifies that the reader's
dataset versions agree with its snapshot manifest. A failed comparison names
the query so the same inputs can be replayed. The Data engine's persisted reader
and the Backtest engine's portable reader use this suite in their independent
package tests. Third-party users can run it with just `tivelo-contracts`.

History readers **first select the latest revision known at `as_of`, then test
business validity**. Filtering validity before revision selection can resurrect
an older listing or a cancelled corporate action. Conflicting latest revisions
and overlapping active instrument specifications are errors.

`Bar.price_basis` explicitly distinguishes raw, qfq and hfq source data. A reader
must not return adjusted prices to a raw query. Merely storing an availability
timestamp does not establish historical version coverage; datasets also declare
`knowledge_basis`. Strict engine admission must validate that requirement.

`BacktestSpec` separates legacy and strict semantics. Strict daily close signals
cannot request same-close fills. A static adjusted view always names a fixed
anchor and factor definition; **the schema alone does not prove a future anchor
safe**. That proof belongs to engine admission, before execution.

`RunLock` binds the request, snapshot, package hashes, dependency lock and verifier
version. It does not contain credentials or licenses. Hash equality detects
content changes; it is not a publisher signature or a trust decision.

`RunLockContents.market_adapter` optionally binds an explicitly installed market
entry-point ID, distribution name and canonical configuration JSON. It is omitted
when absent, retaining existing lock fingerprints. The Backtest CLI requires this
binding and verifies the actual distribution before executing its factory.

`RunArchive` and `ResultArchive` describe immutable input/result directories.
Their sorted, portable file inventories bind paths, SHA-256, sizes and table row
counts to a RunLock; the final `manifest.json` is not included in its own inventory.
The result format uses typed Parquet tables plus summary and metrics JSON. These
contracts do not publish files, bundle an installed runtime, or authenticate an
archive's author. Implementation, table layout and limits are documented by Backtest.

`DefinitionBundle` (`tivelo_contracts.definitions`) locks a research definition's
kind, language, entrypoint, declared source files, direct definition references
and runtime requirements. Its content digest excludes invocation parameters;
`DefinitionReference.parameters_json` binds those parameters in each RunLock.
Paths are portable, sorted and unique, and cannot collide with the manifest or
generated bytecode. The loader verifies the complete graph before execution.
`trusted_python` uses a module factory. `controlled_daily_v1` permits only one
declared JSON strategy file, with no dependency definitions or runtime requirements;
the Backtest core validates its bounded DSL program before admitting execution.

`RunLockContents.scale_proof` optionally binds the strategy reference, normalized
program digest, core verifier version, numeric semantics and proof scope. It is
omitted when absent, preserving existing lock encoding. A proof record alone is
not admission: the executor must reconstruct and verify the sealed program.
`anchor_cancelled_float64_v1` does not claim equivalence to rounded chart prices.

A `PackageReference` records its digest source in `artifact_kind`. The default `wheel`
kind identifies the release archive and is omitted from canonical JSON to preserve
existing locks. `installed_tree_v1` covers an installed distribution and
`source_tree_v1` covers an editable project. In a PyInstaller desktop release,
`frozen_image_v1` binds a host-fixed dependency to the deployed executable and
bundle containing its code and native libraries. Independently updated engines
retain their own installed-tree identities. `RuntimeReference` can also record
interpreter and standard-library digests. In a frozen release, the latter
covers the deployed image including its standard library and native resources.

`TradingTerms` distinguishes historical validity from knowledge time; market
implementations resolve explicit security specifications rather than guess from
symbols. `ReferencePrice` and `PriceBounds` preserve the exchange reference and
rule revision. `FeeQuote` requires unique, itemized components whose exact sum
matches the total. These records contain no default market rates or calendar.
`ExecutionRules` extends the original `MarketRules` protocol without removing its
methods. Order events retain timestamps and fee detail; `LedgerEntry` validates
the direction and conservation of cash, quantity and dividend receivables.

`TradingTerms.order_quantity_rules` optionally contains sorted, unique
`OrderQuantityTerms` for `limit` and/or `market`. A nonempty list is exhaustive:
omitted order types are unsupported. Each entry specifies both sides' minimum,
increment, maximum and odd-lot policy; it shares the parent rule's history.
The empty field is omitted from canonical encoding, preserving old records.
Market configuration formats must explicitly gate this new capability; the CN
implementation requires rule configuration schema `1.1`, `1.2` or `1.3`.

The explicit `remainder_once` odd-lot policy allows a sell quantity consisting
of whole lots, or the entire residual part of the available holding plus zero
or more whole lots. It requires `sell_minimum == sell_increment`; for a 100-share
lot and 299 available shares, 99 and 199 are valid while 98 and 198 are not.
Order ceilings and available-quantity bounds still apply. This policy requires
CN configuration `1.3` and a supporting consumer. The existing `all_remaining`
policy still only exempts full liquidation from the ordinary quantity grid.

The separate optional `OrderExecutionRules` protocol provides `quantity_terms`
and `order_quantity`. Its quantity inputs/outputs use the same exact decimal128
contract. Existing `MarketRules` and `ExecutionRules` signatures remain valid;
consumers must not pass new keywords to an old provider or ignore a declared
order-specific rule when the extension is absent.

`Signal.order_type` is a derived Python property: a `limit_price` implies `limit`,
otherwise `market`. It adds no new signal wire field. New `OrderEvent` records
retain the type and the converted `raw_limit_price`; only limit orders carry
that price, fills must honor it, and both values are immutable within an order's
event history. Null/absent type means historical metadata was not recorded, not
an inferred market order. Both new event fields are omitted when absent.
The raw limit uses a signed exact decimal like the raw fill; whether nonpositive
prices are legal belongs to the market/account model. This does not enable
futures execution in the cash-equity account, which still requires positive prices.

Result archives explicitly pair format `1.0` with `parquet-v1`, or format `1.1`
with `parquet-v2`. The latter adds nullable order type and exact raw-limit columns.
The reader must select the declared layout and reject mismatches. Backtest reads
both; older readers reject the new format/layout rather than dropping metadata.

`OpeningObservation` separates opening execution evidence from final daily bars.
`ExecutionDataReader` adds as-of opening queries; `DailyExecutionRules` supplies
known calendar events and declares the fee monotonicity needed by default sizing.
`StrategyContext.openings` is also subject to causal checks. Structured
`ExecutionDiagnostic` records keep full instrument IDs and event times without
encoding them into a short error-code string. Optional warmup/decision-delay and
per-event diagnostics omit their neutral defaults from canonical JSON so existing
default request fingerprints are unchanged.

Valid and invalid wire examples are in [`examples/`](examples/). UTC timestamps
use at most microsecond precision; finer timestamps are rejected rather than
silently truncated. Calendar dates cannot be inferred from numeric timestamps.

## Verification and updating the schema

```sh
uv run --project packages/tivelo-contracts --extra test pytest packages/tivelo-contracts/tests -q
PYTHONPATH=packages/tivelo-contracts/src uv run --project backend --no-sync python -m tivelo_contracts.schemas packages/tivelo-contracts/schemas/contracts-v2.schema.json --check
```

Omit `--check` only when intentionally regenerating the artifact after a contract
change. The contract tests include ambiguity, historical revision/cancellation,
DST, exact decimal limits, corrupt locks, invalid coverage and capability states.
They do not certify provider data, exchange rules, the execution engine or
professional licensing. See the repository's engine-platform implementation
record for completed and outstanding work.

When building a TSP extension, use the host contract for installation and routing. Select the Tivelo contracts only for portable data and research boundaries. In the TSP source tree, see the [extension guide](../../docs/secondary-development.md) and the [data and market contract](../../docs/engine-platform/data-and-market-contracts.md).
