# Tivelo Data

独立数据引擎（`0.1.0a1`），依赖公共契约、PyArrow、DuckDB 和 Polars，不导入 TSP、FastAPI 或回测实现。当前实现包括显式数据源注入、有限分页同步、事实校验、不可变 Parquet 版本、快照、历史时点查询及 CLI。实际支持范围以本说明和测试为准。

## 许可

**Tivelo 自有实现未经权利人单独书面授权不得商用。** 自有实现采用 [PolyForm Noncommercial License 1.0.0](LICENSE)，按许可条款允许的用途可免费使用。

此前已按 MIT 许可授出的材料保留原有权利和版权声明，见 [MIT 许可](LICENSE-MIT)和[许可说明](NOTICE)。

TSP 用户从工作台的数据源配置和能力路由接入。直接使用本包时，先准备独立 Provider、采集请求、时间口径及发布目录，再由调用方固定快照引用。[工作台数据与市场契约](../../docs/engine-platform/data-and-market-contracts.md)

## 库接口

```python
from tivelo_data import DataEngine
from tivelo_data.providers import FixedSourceResolver

# provider 实现 providers.Provider；request 为 SyncRequest。
# configuration_digest 是不含凭据的配置摘要，由调用方生成。
engine = DataEngine("./research-data", FixedSourceResolver(provider, configuration_digest))
plan = engine.plan_sync(request)  # 只检查能力，不创建数据目录
candidate = engine.sync(plan)    # 独立暂存；尚不可供读器查询
engine.validate(candidate)
version = engine.publish(candidate)
snapshot = engine.create_snapshot((version.version_id,))

with engine.open_reader(snapshot.snapshot_id) as reader:
    records = reader.bars(query)  # BarQuery，返回不可变记录
    for batch in reader.iter_bar_batches(query, batch_size=65536):
        consume(batch)           # Arrow C Data 接口，无逐行模型构造
```

独立使用者可注入自己的 `SourceResolver`；TSP 适配器应委托既有能力矩阵。引擎不另外维护供应商优先级。配置与凭据引用从 `SyncContext` 注入，计划、清单和日志不保存凭据。每个并发同步任务应使用自己的 Provider 实例；`FixedSourceResolver` 适合单次或顺序使用。同步结束时会调用 `provider.close()`，提供方须保证生命周期匹配。

`FetchPage` 最多返回配置允许的记录数（默认 10,000），最后一页必须提供 `Coverage`。循环游标、超限分页、错误类型、未声明来源、错误复权基准、矛盾行数、重复或歧义修订都会被拒绝。取消检查贯穿取数、排序和暂存，失败仅清理该次暂存目录。

同步取得 Provider 后，引擎会在校验锁定来源、创建暂存目录、取数和关闭的各条退出路径释放它。取数与关闭同时失败时，调用方仍收到原始错误码。单独的关闭错误会保留提供方声明的公共错误码和重试时限，普通异常统一返回 `SOURCE_UNAVAILABLE`，该次候选不会交付。实现插件时应能在首次取数前关闭实例。独立 CLI 自行关闭只用于预检的实例，库调用方如直接持有固定 Provider、只运行 `plan_sync`，由该调用方负责其生命周期。

## 字段与历史语义

- 时间采用微秒精度 UTC，物理数值采用 `decimal128(38,12)`；JSON 中十进制是字符串。缺失值不补零。
- `available_at` 决定历史上能否看到一个版本；先选择该时点已知的最新修订，再判断有效期、取消及代码映射，避免旧版本复活。
- `knowledge_basis` 由数据源能力声明，计划不得伪装成更强的历史证据。读器不会替数据源证明时间戳真实性。
- `DatasetScope.coverage_basis=source_response` 表示完整接收该次源响应，**不表示已经验证所有交易时段完整**。严格回测仍须结合市场日历、停牌和缺口证据做准入检查。
- `adapters.V1BarAdapter` 显式声明旧字段、单位、分钟开始/结束标签和日线时段。北京时间 naive 值只在该边界转为 UTC；不改写旧存储。旧快照只能标记为 `latest_reconstruction`，缺失发布时间不能臆造为收盘时间。
- enriched 的 qfq OHLC 不可重标为 raw/hfq；raw 转换要求完整 raw OHLC，缺少 `raw_open` 明确拒绝。
- `opening_observation` 是独立事实类型，保存指定执行开盘的原价、交易状态、可知时间和明确参考价。`reader.openings(EventQuery)` 先选当时已知的最新修订，再检查最终状态；晚公布的记录不会提前出现。完整日 K 线不能被转换为“当时已知”的开盘事实。

## 将 TSP 扩展表作为历史研究输入

管理员先在工作台配置扩展表，照常通过导入、拉取、HTTP 或 MCP 写行。在希望保留这一版数据时运行下列命令。将 `TSP_DATA` 指向当前工作台的数据目录，将 `INPUTS` 指向工作台的 `research/engine/inputs` 或自己的独立输入目录。映射文件声明表、字段、单位、版本、市场、时间范围和股票代码到研究标的的对应关系。[映射示例](examples/tsp-extension-field/mapping.json)

```sh
tivelo-data --root "$JOURNAL" observe-tsp-field --repository "$TSP_DATA" \
  --mapping mapping.json --output "$INPUTS" > receipt.json
jq '.input.request' receipt.json > request.json
tivelo-data --root "$STORE" import-jsonl --request request.json \
  --input "$INPUTS/$(jq -r '.input.path' receipt.json)" \
  --source-id "$(jq -r '.input.source_id' receipt.json)" --knowledge-basis historical_versions
```

每次观察都使用同一日志目录、TSP 目录和映射。命令只读取已登记的字段，在事务中保存变化、修订及已删除的行，再返回一份按内容命名的 JSONL 输入。内容相同的重复观察返回同一输入。修改表结构或映射会建立独立的观察历史。请保留原日志、收据和快照。

- 时序表按分区日期及配置的日内时间列定位。每条观察在该市场本地日期结束时到期。快照表以实际观察时刻生效，后续采集记录变更和删除。市场级表的 `symbols` 为 `{}`，每个有效时刻最多一行。
- 整数、十进制数、布尔值和文本分别保留原类型。浮点字段须能精确表示为至多 12 位小数。对每个字段显式声明单位，缺值以空值传递。
- 旧分区的首次可知时间是本次实际观察时刻。两次观察之间已经被覆盖的版本无法从旧表恢复。供应商若保留原始发布时间和修订记录，可直接通过 `extension_value` Provider 或 JSONL 提交相应历史。
- 将收据的 `input` 加入工作台 `prepare.datasets`，按 `dataset_id` 排序。将 `field_reference` 加入 `prepare.run.extension_fields`。独立回测使用同一个字段引用，行情、标的、开盘及公司行为等必需数据也要装入同一快照。附带的可信 Python 策略使用一个整数排名字段，参数 `field_sha256` 取自收据。[可信 Python 排名策略](examples/tsp-extension-field/strategy.py)
- 受控日线公式将收据中的同一引用和类型写入 `field_operands`。设定 `ext_rank` 后可以按排名选股，也可用 `ts_delay(ext_rank, 1)` 引用上一个行情日当时可知的值。示例对当天没有发布排名的标的跳过信号。[受控排名程序](examples/tsp-extension-field/program.json) · [从收据到独立回测](../tivelo-backtest/README.md#在受控公式中使用扩展字段)
- 查询命令为 `tivelo-data --root "$STORE" query --snapshot "$SNAPSHOT" --record-type extension_value --request query.json`。查询 JSON 包含收据中的 `dataset_id`、`field_sha256`，另加 `instrument_ids`、`effective_at` 和 `as_of`。前者时间指定观察作用的时点，后者指定允许看到的最新修订时间。市场级查询传空标的数组。
- 保存历史盘中信号时，定义证券级布尔字段并设置 `time_basis: "minute_bar_end"`。为每根实际分钟 K 线保留 `effective_at=bar_end` 的真值或假值，附带原始 `available_at` 和修订历史。通过已有 `extension_value` 数据源或 JSONL 发布。回测从同一快照读取行情与信号，以每根 K 线的完成时刻选取已知版本。[回测请求示例](../tivelo-backtest/README.md#接入盘中信号)

## 发布、引用与恢复

事实文件内容寻址，版本与快照清单不可变。SQLite 索引事务提交才代表发布成功；提交前中断可能留下未索引文件，但不会让半成品成为可查询快照。候选文件保留用于重试；不会自动清理源事实或既有用户数据。格式/写入器版本不兼容时拒绝打开，需显式迁移。

行情卷写满、已打开的快照读器无法创建查询临时目录时，读器可在原内存预算内继续查询已提交的事实。此时查询超过内存预算会返回 `RESOURCE_BUSY`。释放磁盘空间后可用原候选重试 `publish`，重复提交同一版本只产生一条发布记录。

读器使用上下文管理器释放快照引用。提前结束列式迭代时关闭迭代器。每个快照有独立的 SQLite 读锁，同时读取不同快照可以并行。读器进程异常退出后，显式注销快照时会核对是否还有活跃读器，并回收已识别的临时引用。任务可用 `store.pin(snapshot_id, owner)` 和 `release` 登记跨进程重启仍有效的持久引用。旧索引中无法证明来源的引用按持久引用保留。注销只移除快照索引，原始事实文件继续保留。[SQLite 事务锁](https://www.sqlite.org/lang_transaction.html)

数据版本写入显式指定的目标目录。可从 TSP v1 日线分区只读导入。既有缓存的发布与修复、API 和 SSE 由 TSP 管理。两个入口共用 v1 代际标记、完整性状态与字段校验。

### 从上次处理的位置读取新发布的数据版本

`DataEngine.publications` 和 `tivelo-data publications` 提供本地发布记录。各供应商、签名源和进程共用数据引擎的发布事务。每个首次成功发布的数据版本只有一条记录。检查候选、同步暂存、导出快照、失败重试和重复发布同一个已提交版本均不会产生额外记录。

```bash
tivelo-data --root "$DATA_ROOT" publications --dataset daily --tail > start.json
python -c 'import json; print(json.dumps(json.load(open("start.json"))["cursor"]))' > cursor.json
# 其他进程可在此期间运行原有的 sync、validate 和 publish 命令。
tivelo-data --root "$DATA_ROOT" publications --after cursor.json --limit 50 > page.json
```

第一次跟随新提交时使用 `--tail`，需要从日志起点重放时省略。每页返回 `items`、`cursor` 和 `has_more`。每条记录给出 `sequence`、`event_id`、`dataset_id`、`version_id` 和本机 UTC 记账时刻 `recorded_at`。顺序由索引的提交序号确定。研究时点仍由事实的业务日期和可知时间决定。消费方处理完整页、确认下游结果后再保存该页游标。若中途退出，使用前一个游标重读，以订阅标识和事件 ID 对下游操作去重。

```python
from pathlib import Path
from tivelo_data import DataEngine
from tivelo_data.publications import PublicationCursor

engine = DataEngine(Path("market-data"))
cursor = PublicationCursor.model_validate_json(Path("cursor.json").read_bytes())
page = engine.publications(after=cursor, limit=50)
for event in page.items:
    snapshot = engine.create_snapshot((event.version_id,))
    print(event.event_id, event.dataset_id, snapshot.snapshot_id)
```

单页可取 1 至 500 条。游标绑定数据目录中的日志身份、过滤的数据集和最后核对的事件收据。跨库、换过滤条件或退回到缺少该收据的备份会返回 `DATA_CONFLICT`。需要恢复另一条数据历史时，显式重建订阅。空页也可以推进游标，避免反复扫描其他数据集。

已有工作区从新版数据引擎首次打开索引时开始记录，既有数据版本仍按原 ID 读取。从那以后，旧版发布器继续向同一索引写入的新版本也会被记录。记录中的 `version_id` 可用于创建准确的研究快照。运行完整回测还需要该任务要求的其他数据版本和回测归档。

## 便携快照

`engine.export_snapshot(snapshot_id, destination, observer=...)` 将已发布快照及全部引用事实复制到显式目录，返回 `tivelo_contracts.artifacts.SnapshotBundle`。`observer` 可省略；提供时在分块复制、链接及最终发布前检查取消，并报告复制/发布进度。目标仅包含 `bundle.json` 和内容寻址的 `datasets/<摘要前两位>/<摘要>.parquet`，不携带 SQLite、数据源凭据或工作台配置，原数据版本和快照 ID 保持不变。

导出期间固定源引用，暂存副本经大小、摘要及物理 schema 校验后，以独占方式创建目标，最后原子发布 `bundle.json`。相同目标只有清单和全部事实一致时才幂等成功，不覆盖未知目录或不同快照。正常失败只清理该次创建的内容；进程中断遗留的目录若缺少清单则不是成功制品，读器必须拒绝。当前不自动接管此类目录，应重新导出到新目录，后续维护流程另行处理遗留物。

Backtest 的 `PortableSnapshotReader` 可直接消费此格式，无需安装 Data。物理字段和事实主键共享公共契约；读器不复制同步、路由或索引。摘要证明内容完整性，不证明数据源的历史发布时间真实，也不代表发行者签名。

## CLI

安装相互兼容的 contracts/data wheel 后可运行 `tivelo-data --help`。本地 JSONL 导入按公共记录 schema 校验，需明确源 ID、历史证据和 bar 时段口径：

```sh
tivelo-data --root ./research-data plan --request sync-request.json --input bars.jsonl --source-id example --knowledge-basis latest_reconstruction --bar-convention example.daily.v1
tivelo-data --root ./research-data import-jsonl --request sync-request.json --input bars.jsonl --source-id example --knowledge-basis latest_reconstruction --bar-convention example.daily.v1
tivelo-data --root ./research-data query --snapshot <snapshot-id> --request query.json
tivelo-data --root ./research-data export --snapshot <snapshot-id> --output ./portable-snapshot
```

`sync-request.json` 为 `SyncRequest`，`query.json` 为 `BarQuery`。`sync` 只输出候选；`validate`、`publish` 和 `snapshot` 支持分阶段发布；`import-jsonl` 执行完整过程并输出快照清单。实际独立子进程用例见 `tests/test_cli.py`。JSONL 是已标准化数据的显式导入入口，不是实时供应商 SDK。

开盘事实使用 `record_type=opening_observation` 的同步请求导入；查询时增加 `--record-type opening_observation`，请求文件为 `EventQuery`。空 JSONL 本身不能证明该区间没有公司行为；严格回测所需的明确空事件覆盖须由来源查询及其覆盖报告提供。

### 保存与查询历史盘口

将来源在同一时刻已确认的买一、卖一、最近成交和交易状态写成一条 `OrderBookObservation`。字段 `price_basis` 为 `raw`，价格使用字符串，已确认无报价或成交时填 `null`。有最近成交时同时提供 `last_trade_at`。保留 UTC 的 `event_at`、`available_at`、`ingested_at`、交易日、时段、币种、来源和修订号。多条事件使用相同时间戳时，先按来源事件顺序确定申报时能看到的完整状态。

导入请求 `capture.json` 使用 `SyncRequest`。沪深股票可声明 `record_type=order_book_observation`、`market_id=cn.equity`、`asset_class=equity`，并提供 `dataset_id`、`instrument_ids`、`start`、`end`。只有来源能提供真实历史可知时间和修订时才声明 `historical_versions`。仅保留最新值或不可证明历史时，盘口的 `available_at` 等于 `ingested_at`。

```sh
tivelo-data --root "$STORE" import-jsonl --request capture.json --input books.jsonl \
  --source-id "$SOURCE_ID" --knowledge-basis historical_versions > snapshot.json
tivelo-data --root "$STORE" query --snapshot "$(jq -r .snapshot_id snapshot.json)" \
  --record-type order_book_observation --request book-query.json
```

`book-query.json` 是 `OrderBookQuery`，其中 `dataset` 取自快照清单的 `contents.datasets`，另提供 `instrument_ids`、`start`、`end` 和 `as_of`。查询输出 JSONL。数据引擎与便携读器均通过 `reader.order_books(query)` 查某个数据集版本，在 `[start, end)` 内按可知时间选择整条修订。晚到的盘口在其可知时刻之前不可读，同时可见的冲突修订返回 `DATA_CONFLICT`。沪深限价校验还要求申报时点与盘口时点准确匹配。[市场包接入](../tivelo-market-cn/README.md#连续竞价申报价格)

### 保存与查询逐笔成交

`TradePrint` 保存一笔已发生的成交。沿用原始交易日、时段、原价、数量单位、成交和可知时间。由来源提供同一标的与交易日内稳定且按成交排序的 `sequence`。已确认的主动方向写 `buy` 或 `sell`，信息不足时写 `unknown`。用于 A 股盘中模拟的记录使用人民币原价和股数。盘口快照、分钟成交总量或单一的最新成交价无法证明一笔新成交的数量和方向。

用 `record_type=trade_print` 构造 `SyncRequest`，经同一 `import-jsonl` 或数据插件发布。查询使用 `tivelo-data query --record-type trade_print` 和 `TradePrintQuery`。在固定数据集版本与 `trading_date` 内，可选传入 `sequence` 查一笔。读器先按 `as_of` 选修订，再按 `[start, end)` 过滤成交时间。仅由最新版本重建的逐笔数据从实际入库时起可见。[逐笔驱动的成交模拟](../tivelo-backtest/README.md#逐笔成交回放)

### 按历史时点查标的、公司行为和供应商代码

脚本可以先把供应商代码解析为稳定的 `instrument_id`，再查当时有效的标的信息、公司行为和复权因子。查询需要指定已经发布的快照。`effective_at` 是业务生效时点，`as_of` 是研究者能使用的信息截止时点。时间都需带时区。

例如将以下内容保存为 `lookup.json`。代码、来源和交易所取自快照里的 `symbol_mapping` 数据集。

```json
{"symbol":"DEMO","source_id":"fixture","venue_id":"XSHG","effective_at":"2025-01-03T00:00:00Z","as_of":"2025-01-04T00:00:00Z"}
```

```sh
tivelo-data --root ./research-data resolve-symbol --snapshot "$snapshot" --request lookup.json
```

成功时输出一个带 `instrument_id` 的 JSON 对象。查询不到映射时返回 `INSTRUMENT_NOT_FOUND`，同一来源与场所有多个有效结果时返回 `AMBIGUOUS_INSTRUMENT`，两种错误均以状态码 1 退出。

假设读回的标的是 `security-2`，将以下内容保存为 `history.json`，在同一快照、同一时点继续查询。

```json
{"instrument_ids":["security-2"],"effective_at":"2025-01-03T00:00:00Z","as_of":"2025-01-04T00:00:00Z"}
```

```sh
tivelo-data --root ./research-data query --record-type instrument --snapshot "$snapshot" --request history.json
tivelo-data --root ./research-data query --record-type corporate_action --snapshot "$snapshot" --request history.json
tivelo-data --root ./research-data query --record-type adjustment_factor --snapshot "$snapshot" --request history.json
```

这三类查询都使用公共 `HistoryQuery`，每行返回一条 JSON 记录。查询成功且无可见记录时，标准输出为空。判断供应商是否明确覆盖这个区间，还需通过 `inspect` 查看对应数据集的 `coverage`。将 `as_of` 移到修订公布前，可以在同一快照查询当时仍有效的记录。需要重放一次已经完成的研究时，使用原任务记录的快照标识。[读器实现与命令行对照用例](tests/test_history.py)

### 独立采集沪深 A 股日线

安装带 `cn-http` 扩展的 Tivelo Data，同时提供相容版本的 Contracts 和 Market CN 安装包。基础安装仍可独立导入 JSONL、管理与读取快照。HTTP 扩展接受与 TSP 自定义源相同的 YAML 或 JSON 声明。下面的示例地址需替换为实际供应商，并按供应商文档核对字段和计量单位。

```yaml
name: example
auth: {type: bearer, token_env: EXAMPLE_MARKET_TOKEN}
datasets:
  daily:
    url: https://feed.example.invalid/daily
    method: GET
    research_profile: cn.equity.regular.raw.lot100.v1
    response_path: data.rows
    field_map: {code: symbol, date: date, open: open, high: high, low: low, close: close, volume: volume, amount: amount}
```

此声明要求普通沪深 A 股常规交易时段的未复权 OHLC、以手计的成交量及人民币元成交额。每手 100 股。来源须能区分成功的空列表与请求失败。将来源保存为 `feed.yaml`，在 `capture.json` 指定供应商代码、稳定标的 ID、日历版本及左闭右开的观察区间。

```json
{
  "expected_provider": "example",
  "calendar_version": "2026.1.0",
  "symbols": [{"instrument_id": "security-1", "symbol": "600000.SH", "venue_id": "XSHG"}],
  "request": {
    "dataset_id": "daily", "record_type": "bar", "market_id": "cn.equity",
    "asset_class": "equity", "instrument_ids": ["security-1"],
    "start": "2025-06-30T16:00:00Z", "end": "2025-07-02T16:00:00Z",
    "timeframe": "1d", "price_basis": "raw"
  }
}
```

```sh
tivelo-data --root ./research-data plan-http --capture capture.json --source feed.yaml > plan.json
tivelo-data --root ./research-data import-http --capture capture.json --source feed.yaml --locked-plan plan.json
```

`plan-http` 在本机生成计划，首次联网发生在同步阶段。`--locked-plan` 核对源配置摘要、证券映射、日历和请求范围，发现变化即终止。`import-http` 输出快照清单。需要人工验收候选数据时，先用 `sync-http` 保存候选清单，再依次运行 `validate`、`publish` 和 `snapshot`。

CLI 在请求时读取 `auth.token_env` 指定的环境变量。库调用可构造 `tivelo_data.http.cn_daily.CNDailyProvider(capture, config)` 并注入 `SyncContext.resolve_credentials(reference)`，返回带 `token` 的映射。计划和快照只保存来源标识及摘要。工作台沿用自己的凭据获取方式，两处使用相同的请求、归一化和交易时段校验。

覆盖按标的与交易日计算。例如区间内有两个交易日，仅收到一根日线会生成 `partial`，并记录缺失日期。任一页发生鉴权、限流或传输错误，整次同步失败并清除暂存。每条行情以实际采集时刻记入 `available_at`，版本标记为 `latest_reconstruction`。严格历史回测还需要供应商的历史可知时间与修订证据。

`plan`、`sync`、`import-jsonl` 支持可选 `--coverage coverage.json`，内容为公共 `Coverage` 模型；库入口为 `JSONLinesProvider(..., coverage=coverage)`。声明进入来源配置摘要，并核对查询标的、行数及完整状态，改变声明会改变计划。明确的空公司行为报告必须记录对应标的和 `no_events`；不提供报告时，空文件保持未证明覆盖的状态。导入声明不构成平台对供应商历史真实性的认证，`source_response` 也不等于交易日历完整性验证。

### 通过 TickFlow SDK 采集原始日线

安装 `tivelo-data[cn-tickflow]` 和相容版本的 Contracts、Market CN。沿用上一节的 `capture.json`，把 `expected_provider` 设为 `tickflow`。按北京时间午夜设置请求范围。下面三条命令使用供应商免费历史日线端点。

```sh
echo '{"mode":"free"}' > tickflow-source.json
tivelo-data --root ./research-data plan-tickflow --capture capture.json --source tickflow-source.json > plan.json
tivelo-data --root ./research-data import-tickflow --capture capture.json --source tickflow-source.json --locked-plan plan.json
```

分步发布时，使用 `sync-tickflow` 保存候选清单，再执行 `validate`、`publish`、`snapshot`。付费接入的配置示例是 `{"mode":"paid","credential_ref":"MY_TICKFLOW_KEY","rpm":24,"batch":100}`。运行进程从指定的环境变量读取 Key。配置可提供 HTTPS 服务地址 `endpoint`，本机联调支持回环 HTTP。配置、映射、日历或 SDK 版本变化时需要重新生成计划。免费通道使用独立的无凭据客户端。

适配器使用官方 Python SDK 0.1.25。每次请求覆盖最多 32 个交易日和 100 个证券，显式请求 `period=1d` 与 `adjust=none`。每页单次请求，超过限制的任务由引擎分页，供应商错误会终止本次同步。成交量沿用 TSP 已核对的沪深股票手数，一手转为 100 股；人民币元成交额原值保留。时间戳仅用于定位北京时间交易日期。快照按完整自然日标记 `cn.equity.tickflow.beijing-date`，可知时间为本次收到响应的时刻。缺日与停牌填值均报告覆盖缺口。供应商返回的当前观察可用于事后研究，严格历史回测需要另有当时可知的版本证据。[官方 SDK](https://github.com/tickflow-org/tickflow) · [上游接口定义](https://docs.tickflow.org/zh-Hans/api-reference/openapi.json) · [TSP 插件计量契约](../../docs/plugin-development.md#单位制)

### 扶摇和通达信的独立日线接入

沿用上文的 `capture.json`，将 `expected_provider` 设为 `fuyao` 或 `tdxquant`。使用沪深股票代码、对应交易所、明确的日历版本及完整北京时间自然日。以下两种来源可以各自安装。现场凭据由操作者配置在指定环境变量中，计划和样例只使用变量名。

| 来源 | 安装配置 | `source.json` 示例 | 一次实际请求 |
| --- | --- | --- | --- |
| 扶摇 | `tivelo-data[cn-fuyao]` | `{"credential_ref":"FUYAO_API_KEY","rpm":120}` | 每批最多十只，显式请求 `1d/none` 并核对响应。缺少某个分组时单独向同一供应商补查，显式空分组直接记入覆盖缺口。 |
| 通达信 | `tivelo-data[cn-tdxquant]` | `{"credential_ref":"TDXQUANT_API_KEY","rpm":60,"timeout_seconds":45}` | 单只、按结束日期和交易日数量取 `1d/none`。每次同步使用独立 SDK 进程和临时配置目录，只发送本次要求的历史请求。 |

以扶摇为例，下面的第一条命令可以离线生成计划。第二条命令实际访问供应商服务。独立 CLI 的 `plan-tdxquant`、`import-tdxquant` 使用相同参数。需要单独的候选版本供校验和发布时，使用 `sync-fuyao` 或 `sync-tdxquant`。

```sh
tivelo-data --root ./lake plan-fuyao --capture capture.json --source source.json > fuyao-plan.json
tivelo-data --root ./lake import-fuyao --capture capture.json --source source.json --locked-plan fuyao-plan.json
```

扶摇默认使用官方 HTTPS 服务。独立部署可通过 `endpoint` 指定自己的 HTTPS 来源或本机 HTTP 测试服务，同时为该来源指定独立的凭据变量。TSP 使用已选日线路由和对应供应商的已配置 Key。通达信由可选依赖安装厂商 SDK 1.2.2，研究进程按次复制运行所需文件并在结束时清理。Tivelo 的公开发行包只包含适配代码。

两家源的成交量均按**股**归档，保留不足一手的余数。扶摇成交额直接计为人民币元，通达信的万元成交额乘以 10,000。无交易、缺少一个交易日、账号无权限、限频、响应格式错误分别进入覆盖报告或结构化错误。SDK 版本、适配器发行信息、绑定与日历摘要进入锁定计划。新版本不能直接执行旧的采集计划，已经发布的快照可以继续按原摘要读取。

这两条接入已覆盖沪深股票原始日线。实时、盘口、分钟、财务、除权等能力仍按每个 TSP v1 插件自己的清单提供。历史日线采集保存实际观察时刻，归类为 `latest_reconstruction`。严格历史回测要求有证据的历史可知时间。

### 从既有 TSP 日线仓库导入

基础安装的 `tivelo-data` 可按旧库已有日期分区导入。命令行与工作台读取同一份 Parquet。沿用上一节的 `capture.json`，增加 `layout` 字段，省略 `calendar_version`，将 `expected_provider` 设为这份本地归档的来源标识，例如 `local_archive`。

| 源布局 | 原目录 | 请求的价格口径 |
| --- | --- | --- |
| `daily_raw_v1` | `kline_daily/date=YYYY-MM-DD/` | `raw` |
| `enriched_v1` | `kline_daily_enriched/date=YYYY-MM-DD/` | `qfq` 读取已存的前复权 OHLC。只有完整保存四个原始价格列的分区才可请求 `raw`。标准 enriched 分区通常缺少 `raw_open` |

```sh
tivelo-data --root ./research-data plan-repository --repository ./old-data --capture capture.json > plan.json
tivelo-data --root ./research-data import-repository --repository ./old-data --capture capture.json --locked-plan plan.json
```

旧仓库按交易日期保存日线。导入区间按北京时间当天零点至次日零点标记，使用 `cn.equity.v1.repository.beijing-date` 约定。请求应覆盖整个日期桶，起止时间可写成带 `+08:00` 偏移的当地零点。默认在已有日期分区内逐标的核对覆盖。北交所旧库用 `venue_id="XBSE"` 和 `.BJ` 代码指定标的。安装 `tivelo-data[cn-repository]` 后可填写 `calendar_version`，按指定日历核对整天缺失的分区。成交量由手转换为股，金额保留人民币元。旧库可知时点取本次观察时间，证据等级为 `latest_reconstruction`。

计划锁定范围内的分区清单、文件摘要、证券映射和显式指定的日历。enriched 同时核对发布代际。同步按页验证实际解码的文件字节，发布中、待修复、盘中报价及范围内分区发生变动都会终止该次导入。`sync-repository` 可只生成候选，然后用通用的验证和发布命令分步完成。新数据写入 `--root` 指定的目录。

导入 `enriched_v1` 的 `qfq` 时，已有浮点开高低收超出 12 位小数的部分按最接近、正中取偶舍入。原价、成交量、成交额及源文件内的十进制列仍按精确值校验。采集计划记录 `enriched_v1.qfq-dec12`，旧前复权计划需重新运行 `plan-repository`。已发布的快照仍可按原 ID 读取。精度检查命令继续报告源文件中的原始数值精度。

### 只读检查旧日线的数值精度

基础安装的 `audit-repository-daily` 可直接读取旧库的物理分区，包括沪深采集预设以外的代码。将下列内容保存为 `audit.json`，按现场数据修改日期。日期两端均包含在检查范围内。

```json
{
  "layout": "daily_raw_v1",
  "start": "2025-07-01",
  "end": "2025-07-02",
  "fields": ["open", "high", "low", "close", "volume", "amount"],
  "volume_multiplier": "100",
  "max_issues": 100
}
```

```sh
tivelo-data --root ./research-data audit-repository-daily --repository ./old-data --request audit.json > audit-report.json
```

命令只读取源文件，`--root` 是独立 CLI 的通用参数。报告采用已选文件的内容摘要，记录所查行数、数值数、受影响行数、各字段与各原因的总数。每条问题含交易日期、源代码、物理列、文件 SHA-256 和该文件内从 1 开始的行号。`max_issues` 只限制输出示例，设为 0 仍统计全部问题。可用 `symbols` 选择源代码，留空会检查所有行，包括停牌占位。日期分区存在多个文件时，按摘要识别文件，行号逐文件重新计数。

默认按原存储单位检查。沪深旧日线以手记成交量，核对导入后的股数时设 `volume_multiplier` 为 `"100"`。enriched 的原价使用 `raw_open`、`raw_high`、`raw_low`、`raw_close` 指定物理列。可选的 `amount` 缺列或为空时跳过。行情覆盖、交易所准入、复权依据和历史可知时间由后续采集与回测分别验证。

| 处理结果 | 退出码 | 自动化读取方式 |
| --- | ---: | --- |
| 已检查的数值均符合 decimal128(38,12) | 0 | 标准输出 JSON，`status=clean` |
| 存在超精度、超范围、无效数值，或没有可检查的数值 | 1 | 标准输出含完整统计，`status=invalid` 或 `empty` |
| 请求、文件或列无效，或源数据在检查期间变化 | 1 | 标准错误含 `code`，本次检查未产生报告 |

锁定计划导入时，首个触发十进制精度错误的交易行会让同步失败。`CONTRACT_INVALID` 的 `source_issues` 列出该行有问题的列。回到供应商或原始记录核实数值，按其真实精度重新生成分区，然后重新生成计划并导入。旧计划遇到文件内容变更返回 `DATA_CONFLICT`。已发布的旧快照仍可按原 ID 重放。

旧仓库行情带有 `quote_ts` 时，导入器先检查报价的北京时间日期、15:30 定版线及实际观察时刻。失败返回 `DATA_COVERAGE_INSUFFICIENT`，同一位置格式会标出 `quote_ts`。原因分别为 `quote_date_mismatch`、`quote_before_finalization`、`quote_after_observation`。先从供应商补采对应交易日的已定版行情，再对新分区运行精度检查。

### 从旧除权事件重建免费前后复权

基础安装可读取 TSP 的沪深北股票 `adj_factor/all.parquet`。例如把下列声明保存为 `factors.json`。请求区间采用北京时间零点，包含起日、截至结束日之前。归档来源标识由使用者声明。文件本身只记录证券、除权日期和当次比率，北交所标的以 `.BJ` 代码和 `XBSE` 绑定。

```json
{
  "layout": "event_factors_v1",
  "representation": "cumulative_forward",
  "baseline_date": "2025-07-01",
  "expected_provider": "local_archive",
  "symbols": [{"instrument_id": "security-A", "symbol": "600000.SH", "venue_id": "XSHG"}],
  "request": {
    "dataset_id": "stock-factors", "record_type": "adjustment_factor",
    "market_id": "cn.equity", "asset_class": "equity", "instrument_ids": ["security-A"],
    "start": "2025-07-01T00:00:00+08:00", "end": "2025-07-04T00:00:00+08:00"
  }
}
```

```sh
tivelo-data --root ./research-data plan-repository --repository ./old-data --capture factors.json > factors-plan.json
tivelo-data --root ./research-data import-repository --repository ./old-data --capture factors.json --locked-plan factors-plan.json
tivelo-data --root ./research-data query --record-type adjustment_factor --snapshot "$snapshot_id" --request history-query.json
```

查询文件包含 `instrument_ids`、要查看的有效时刻 `effective_at` 和信息截止时刻 `as_of`。要复现一次研究，保存本次导出的快照和锚点。时间早于本次归档观察的查询返回零条。缺少初始因子时，可以扩大因子导入窗口，使其涵盖所需锚点。

| 表示 | 产出与精度 |
|---|---|
| `event_ratio` | 输出区间内实际记录的除权前价与除权后价之比，省略 `baseline_date`。定义为 `tivelo.cn.v1.ex-factor.event`。原比率按旧值的十进制表示精确导入，超出 12 位小数精度会报错。 |
| `cumulative_forward` | 指定固定基准日 B，令该日除权后的 `C(B) = 1`。按日期用 Float64 连乘 B 之后的比率，在查询起点和区间内后续事件日期输出累计值。发布单个值时以最接近、正中取偶方式舍入到 12 位小数。乘积的中间步骤保留 Float64 精度。定义为 `tivelo.cn.v1.ex-factor.cum-f64-dec12`，基准标识为 `v1:<expected_provider>:<基准日期>`。 |

回测包可从同一便携快照取得原始日线和累计因子。前复权固定末端锚点 A，价格为 `raw × C(d) / C(A)`。后复权固定早期基准 B，价格为 `raw × C(d) / C(B)`。成交量与成交额沿用原始行情。下面的值对应一份在导入完成后查看的三日合成行情。

| 北京交易日 | 原始收盘 | 累计因子 | 前复权（7 月 3 日锚点） | 后复权（7 月 1 日基准） |
|---|---:|---:|---:|---:|
| 2025-07-01 | 10.00 | 1 | 4.00 | 10.00 |
| 2025-07-02 | 5.50 | 2 | 4.40 | 11.00 |
| 2025-07-03 | 4.80 | 2.5 | 4.80 | 12.00 |

下面的回测包示例直接读取工作台导出的上述组合快照。`bundle_dir` 是含有 `bundle.json` 的导出目录。锚点选择明确的交易日，查看时刻取导入之后。

```python
from datetime import UTC, datetime
from tivelo_backtest.price_views import StaticPriceView
from tivelo_backtest.snapshot import PortableSnapshotReader
from tivelo_contracts.data import BarQuery, HistoryQuery
from tivelo_contracts.execution import PriceViewSpec

observed = datetime.now(UTC)
ids = ("security-A",)
start, end = "2025-07-01T00:00:00+08:00", "2025-07-04T00:00:00+08:00"
with PortableSnapshotReader(bundle_dir) as reader:
    bars = tuple(reader.bars(BarQuery(instrument_ids=ids, start=start, end=end,
                                      timeframe="1d", price_basis="raw", as_of=observed)))
    factors = tuple(reader.factors(HistoryQuery(instrument_ids=ids, effective_at=end, as_of=observed)))
    for mode, anchor in (("qfq", "2025-07-03T00:00:00+08:00"), ("hfq", start)):
        view = StaticPriceView(mode)
        spec = PriceViewSpec(mode=mode, provider_id=view.provider_id, anchor_at=anchor,
                             factor_definition_id="tivelo.cn.v1.ex-factor.cum-f64-dec12",
                             factor_baseline_id="v1:local_archive:2025-07-01")
        print(mode, [b.close for b in view.project(bars, factors, spec, decision_at=observed)])
```

每个标的只检查所选文件对本次请求的覆盖。原始事件请求需要区间内实际有记录。累计请求需要该标的在区间结束前至少有一条有效归档记录，随后可以从固定基准计算期初值。早于基准日的无效比率跳过，且不作为覆盖依据。缺少有效记录标为 `missing`。同日相同事件去重，同日有效比率冲突使导入失败。基准日及之后继续校验原始比率。整个源文件的摘要会锁入计划。更新前保存的累计采集计划需要重新运行 `plan-repository`。

导入 `event_ratio` 遇到超过 12 位小数或 26 位整数的原始比率时，CLI 错误与 TSP 尝试记录都会在 `source_issues` 中给出 `ex_factor`、除权日、证券代码、源文件 SHA-256 和从 1 开始的物理行号。先按摘要找到本次使用的 `adj_factor/all.parquet`，再核对该行的供应商记录。修订源文件后重新生成计划。按原定义定版的累计因子有独立的精度口径。[旧库对照与批量定位](../../docs/engine-platform/data-and-market-contracts.md#复核旧沪深股票因子归档)

第三方 Parquet 的 `ex_factor` 列若声明超过 38 位精度，引擎逐行读取原始十进制数字。能以 `decimal128(38,12)` 精确表示的原始事件可以导入，只有尾部零的额外小数位不会触发错误。同日两个比率即使转成浮点数后相等，只要原值不同，仍会返回 `DATA_CONFLICT`。这类宽十进制列的来源版本是 `2`。升级前已保存的此类采集计划需要重新运行 `plan-repository`，已发布快照可按原 ID 查询。复核旧版读取过的宽列时，请用新计划从源文件重新导入并比较两个快照。普通浮点归档和精度至多 38 位的十进制列沿用版本 `1`。

归档的历史证据等级为 `latest_reconstruction`。其中的除权日只决定价格生效日，因子首次可查询的时刻取本次实际观察时间。严格历史任务仍需带可知时间和修订历史的来源。分红到账、送转股等账户事件另有独立契约。[完整工作台与回测示例](../../backend/tests/test_engine_repository_factors.py)

### 从数据源直接采集沪深除权因子

独立 Data 支持四种沪深股票因子来源。TickFlow 和声明事件口径的 HTTP 源提供逐次比率。扶摇和通达信提供分红送配明细，引擎用同源的不复权日线推导逐次比率。四种来源都支持 `event_ratio` 和固定基准的 `cumulative_forward`。请求中的 `expected_provider` 和每只股票的代码映射会锁入计划。直接采集省去中间的旧仓库文件。

使用 `tivelo-data[cn-http]` 时，在示例目录启动本地合成行情服务，然后在另一终端的同一目录执行采集。端口可通过 `serve.py --port` 调整，同时修改 YAML 中的地址。[本地服务](examples/cn-stock-factors/serve.py) · [数据源 YAML](examples/cn-stock-factors/source.yaml) · [采集请求](examples/cn-stock-factors/capture.json)

```sh
python serve.py
```

```sh
tivelo-data --root ./research-data plan-http-factors --source source.yaml --capture capture.json > plan.json
tivelo-data --root ./research-data import-http-factors --source source.yaml --capture capture.json --locked-plan plan.json
```

示例返回三只股票的覆盖情况。A 的累计因子依次为 1、2、2.5。B 明确无事件，期初因子为 1。C 未在响应出现，因此覆盖率为 2/3，C 没有因子记录。将 `representation` 改成 `event_ratio`，删除 `baseline_date` 并重新生成计划，可得到 A 的两个原始除权比率。供免费前后复权投影使用的累计因子基准标识为 `http:sample_dividends:ex-factors:2025-07-01`。

HTTP 口径 `cn.equity.ex-factor.events.v1` 要求每次请求完整返回所给区间内的事件。响应中的每行映射为 `symbol`、北京时间 `trade_date`（`YYYY-MM-DD`）和正数 `ex_factor`（单次除权前价除以除权后价）。源直接给出这些值。供应商能确认某只股票在整个请求区间无事件时，可再映射 `event_status`，返回一行 `no_events` 并留空日期和比率。漏回标的、成功返回空数组都记为缺少来源证据。任何时间段漏回该股票，整只股票的累计基准均视为未知。HTTP 429 通过错误中的 `retry_after` 传递有效的等待时间。

使用 `tivelo-data[cn-tickflow]` 时，将请求中的 `expected_provider` 改为 `tickflow`。将连接声明保存为 `tickflow.json`，例如 `{"mode":"paid","credential_ref":"MY_TICKFLOW_KEY","batch":50,"rpm":24}`。在环境中配置相应凭据，使用 `plan-tickflow-factors` 和 `import-tickflow-factors` 替换上例命令，`--source` 指向该 JSON。单独生成候选版本时，按来源使用 `sync-http-factors` 或 `sync-tickflow-factors`。TickFlow 从 `/v1/klines/ex-factors` 取得逐标的事件，由供应商授予该接口权限。Tivelo 免费价格视图可以使用任一来源的合格累计因子。TickFlow 基准标识为 `tickflow:ex-factors:<固定日期>`。[TickFlow 官方 API](https://docs.tickflow.org/llms.txt) · [官方复权说明](https://docs.tickflow.org/zh-Hans/faq)

快照将这些结果记为 `latest_reconstruction`。因子从完成覆盖该股票的请求时起可见。再次采集供应商修订值会生成新版本，已发布的快照保留原观察。用严格历史策略重现当时决策时，需要提供带历史可知时间与修订记录的来源。[工作台组合快照](../../docs/engine-platform/workbench-research-jobs.md#分别选择日线和除权因子来源)

#### 从扶摇与通达信重建分红送配因子

复用上方的 [capture.json](examples/cn-stock-factors/capture.json)，按所选供应商调整 `expected_provider`、证券及日期。连接配置与原始日线共用，默认使用 `FUYAO_API_KEY` 或 `TDXQUANT_API_KEY`。在示例目录执行下列命令可离线生成扶摇计划，最后一条命令会使用所配置的账号取数。

```sh
python -c 'import json; from pathlib import Path; p=json.loads(Path("capture.json").read_text()); p["expected_provider"]="fuyao"; Path("fuyao-capture.json").write_text(json.dumps(p))'
echo '{}' > fuyao-source.json
tivelo-data --root ./research-data plan-fuyao-factors --source fuyao-source.json --capture fuyao-capture.json > fuyao-plan.json
tivelo-data --root ./research-data import-fuyao-factors --source fuyao-source.json --capture fuyao-capture.json --locked-plan fuyao-plan.json
```

| 来源 | 独立安装与命令 | 单位、范围及核验 |
| --- | --- | --- |
| 扶摇 | `tivelo-data[cn-fuyao]`，`plan-fuyao-factors`、`sync-fuyao-factors`、`import-fuyao-factors` | 每次采集读取一份注明发布日期的全市场 Parquet。现金、送股和配股数量按每股，配股价按元。文件中的货币与证券都要匹配，日期按上海时区解释。请求末日超出文件发布日期会返回覆盖错误。全文件未出现某只股票时，再用单股接口区分已确认无事件和未知标的。因子修订标识含发布日期与文件 SHA-256。[全市场字段](https://fuyao.aicubes.cn/docs/api-reference/market-dumps/) · [单股事件](https://fuyao.aicubes.cn/docs/api-reference/corporate-actions/) |
| 通达信 | `tivelo-data[cn-tdxquant]`，`plan-tdxquant-factors`、`sync-tdxquant-factors`、`import-tdxquant-factors` | 把请求来源改为 `tdxquant`。SDK 1.2.2 在同一隔离进程内按只取得事件和原始日 K。每十股的现金、送股和配股数量除以 10，配股价直接按元。原生服务越过所给日期时，再按请求区间筛选。权限及限频失败保留错误码。[通达信 SDK](https://help.tdx.com.cn/quant/docs/markdown/mindoc-1hjbgqpdhv114.html) |

设前一个实际交易日收盘价为 P，每股现金为 D，每股送股为 S，每股配股为 R、配股价为 K。同一除权日合并各笔明细后，参考价为 `(P − ΣD + Σ(R×K)) / (1 + ΣS + ΣR)`，使用精确分数计算并按四舍五入取到分。逐次因子为 P 除以参考价，按十进制保留 12 位小数，恰好居中时取偶数。同日重复的相同明细会报冲突。前一笔成交与除权当日的原始收盘都要存在。系统会在有需要时向前补取行情。缺价返回 `DATA_COVERAGE_INSUFFICIENT`，除权日收盘超出兼容自检范围返回 `DATA_CONFLICT`，错误信息带证券和除权日。

逐次定义为 `tivelo.cn.ex-terms.cent-halfup.ratio-dec12.v1`。累计定义为 `tivelo.cn.ex-terms.cent-halfup.ratio-dec12.cum-f64-dec12.v1`，对已定版逐次因子按顺序作 Float64 连乘并发布 12 位检查点。固定基准标识为 `<来源>:ex-terms:<固定日期>`。创建价格视图时，从快照因子读取 `definition_id` 和 `baseline_id`。前、后复权共用这份累计数据，各自选择固定锚点。上述两个内置来源的原始日线和因子适配器都校验运行代码摘要。更新前已保存的采集计划若报告摘要变化，应重新生成计划。

### 从既有 TSP 股票分钟仓库导入

安装 `tivelo-data[cn-repository]`。采集声明沿用日线示例的 `expected_provider`、证券绑定和 `calendar_version`。将 `layout` 设为 `minute_v1`，请求中使用 `record_type="bar"`、`timeframe="1m"`、`price_basis="raw"`。例如下面的声明读取 7 月 1 日北京时间 09:33 至 09:35 的两个完整区间。

```json
{
  "layout": "minute_v1", "minute_label": "end", "price_evidence": "marked_raw",
  "expected_provider": "local_archive", "calendar_version": "2026.1.0",
  "symbols": [{"instrument_id": "security-A", "symbol": "600000.SH", "venue_id": "XSHG"}],
  "request": {
    "dataset_id": "stock-minute", "record_type": "bar", "market_id": "cn.equity",
    "asset_class": "equity", "instrument_ids": ["security-A"], "timeframe": "1m",
    "price_basis": "raw", "start": "2025-07-01T09:33:00+08:00", "end": "2025-07-01T09:35:00+08:00"
  }
}
```

使用上文相同的 `plan-repository` 和 `import-repository --locked-plan` 命令。导入器只读扫描 `kline_minute/date=…` 和选中证券的旧 `symbol=…` 分区。日期分区优先，证券分区只补缺失的键。同一优先级出现相互矛盾的记录会中止导入。股票范围与沪深市场预设一并锁定。

- **分钟标签**由来源约定明确指定。`end` 标签 09:35 对应北京时间 09:34 至 09:35，`start` 标签 09:35 对应 09:35 至 09:36。每根行情按区间起止转换为 UTC，保留北京交易日。日历从连续竞价开盘起覆盖至收盘，含收盘集合竞价，午休、开盘竞价单独报价及盘后交易均在此分钟口径以外。
- **原价依据**默认使用 `marked_raw`，要求 TSP 已写入 `kline_minute/.raw_basis`。在无标记的旧库中，明确选择 `legacy_close_anchor` 才会执行收盘锚定重建。每个证券、交易日需要实际的收盘分钟及独立原始日线收盘价，二者随计划锁定。按 `旧分时价 × 原始日收盘价 ÷ 旧分时收盘价` 换算，派生价格取 12 位小数，正中取偶。缺失、未定版、冲突或超出旧 TSP 有效范围的锚点会报错。原始归档文件维持只读。
- **量额和覆盖**按每个证券和应有分钟核算。每手 100 股，成交额单位为人民币元。缺失和零成交分钟计入缺口，交易日内部分数据可以按实际覆盖归档。时间区间和 `as_of` 可通过标准 `BarQuery` 查询，旧文件的知识等级为 `latest_reconstruction`。数据可用于导入之后的兼容研究。[回测用法](../tivelo-backtest/README.md#从快照读取兼容回测的分钟成交数据)

## 验证与边界

根目录的 `scripts/verify_tivelo_distribution.py` 在临时工作目录中安装 wheel，检查没有宿主和回测包，执行完整包测试及 CLI。`--with-wheel` 指定未发布的 contracts wheel，不从仓库源码偷取依赖。测试涵盖历史修订、取消、精确数值、物理 schema、崩溃重试、并发发布、快照引用、CLI 及旧数据边界。

尚需完成：TSP 仓库与真实供应商的完整 v2 适配、交易日历覆盖校验、持久流水线的引用恢复/维护、正式平台制品验收。包内不包含动态专业实现，也不宣称已支持任何市场的完整交易规则。

## 导入签名数据源包

安装 `tivelo-data[extensions]` 后，`tivelo-extensions` 管理本机扩展，`tivelo-data` 负责计划、采集、发布和查询。数据源包通过 `dataset_provider` API v2 声明支持的事实、市场、资产、周期、价格口径、数据来源和可知时间依据。TSP 的导入中心使用同一安装器。[可运行的发行与升级示例](examples/native-provider/README.md)

```sh
tivelo-extensions --state ./installation inspect --archive feed.zip
tivelo-extensions --state ./installation stage --archive feed.zip --digest DIGEST --revision REVISION
tivelo-extensions --state ./installation switch activate --digest DIGEST --generation 0
tivelo-data --root ./data plan-provider --extension-state ./installation --capture capture.json > plan.json
tivelo-data --root ./data import-provider --extension-state ./installation --capture capture.json --locked-plan plan.json
```

`capture.json` 使用 `{"kind":"engine_data_provider","provider_id":"example.synthetic-bars","profile":"default","request":…}`。其中 `request` 是标准 `SyncRequest`。通过 `tivelo-extensions --state ./installation list` 查到包声明的 `provider_ids` 后，使用 `describe-source --provider SOURCE_ID` 可读取活动版本及其市场、资产和周期口径。计划中保存源配置摘要、发行版本、整个 ZIP 和原生入口的 SHA-256。升级后已有计划仍可运行获准的旧版本。停用或撤回发行者信任会拒绝新的采集，已发布快照继续从数据存储读取。

部署者可以在安装目录下的 `research/engine/extensions/profiles/<provider_id>/<profile>.json` 放置供应商的非秘密配置 JSON 对象。默认配置为空。凭据由宿主的 `SyncContext.resolve_credentials` 按 `<provider_id>/<profile>` 读取。独立命令和 TSP 研究进程支持将 `TIVELO_DATA_CREDENTIALS_FILE` 指向受限的本地 JSON 文件，其结构为 `{"example.synthetic-bars":{"default":{"token":"…"}}}`。原生进程按需获得本次选中来源和配置的凭据。配置变更后，保存的旧计划会报告配置冲突。

发行包在签名文件清单中包含 `payload/providers.json`，格式为 `{"format":"tivelo-dataset-providers-v1","providers":[…]}`。数组逐项对应清单中的能力，每项是一个 `ProviderDescriptor`，版本与发行版本相同。启用时，安装器在独立进程调用 `tivelo_probe()`、`describe_provider(capability_id)`，核对声明，并检查 `create_provider`。任务预览只读静态目录。实际同步在专用进程调用 `create_provider(plan=…, options=…, credential_ref=…, attempt_id=…, deployment=…)`，返回实现 `describe/check_ready/fetch/close` 的对象。商业包还需实现本机凭证校验并接收 `lease`、`device_public_key`、`prove_device`。每页最多 10,000 条，最终页需提供完整覆盖报告。真实原生代码以本机工作进程账号权限运行。[数据源接口](src/tivelo_data/providers.py)
