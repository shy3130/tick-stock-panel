# 数据查询逻辑清单与本地数据源替代标注

日期：2026-07-02  
基线：`12d1c98 feat(provider): 完善 fquant 本地数据源切换`

本文只盘点当前代码里的数据查询/读取路径，并标注能否用本地数据源替代。这里的“本地数据源”分三类：

- **本地缓存**：项目 `data/` 下 Parquet/JSON/JSONL，已经是本地读。
- **本地直连源**：`FQuantProvider` 当前直连的 fstore PG、engine-data、moneyflow、tdx-api 等内网源。
- **本地磁盘源**：`/Volumes/vol3/tdx` 这类 TDX CSV 文件。当前尚未实现，只能作为替代方案标注。

## 1. 总入口

| 层 | 入口 | 当前逻辑 | 本地替代结论 |
|---|---|---|---|
| Provider 注册 | `backend/app/data_providers/registry.py` | `DATA_PROVIDER` 或 preferences 选 `tickflow`/`fquant`，返回 provider 实例 | 可扩展为 `fquant_local`，但不只是 registry 加一行；还要改 preferences 白名单、设置 API、前端类型/下拉 |
| Provider 契约 | `backend/app/data_providers/base.py` | instruments/daily/adj_factor/minute/realtime/universes 等统一接口 | 本地磁盘源应复用该契约，避免 service 直接读 CSV |
| 本地仓库 | `backend/app/tickflow/repository.py` | DuckDB view + Polars scan/read Parquet，负责 `data/` 热/冷读 | 已是本地缓存；若跳过 `kline_daily` 镜像，需要给 pipeline/repo 新输入路径 |
| 前端 API | `frontend/src/lib/api.ts` | 前端统一 `fetch('/api/...')`；SSE 用 `EventSource` | 前端不直接读数据源；替换应在后端完成 |

## 2. 外部/上游数据源查询

| 查询域 | 代码入口 | 当前上游 | 本地磁盘可替代 | 替代方式 |
|---|---|---|---|---|
| TickFlow SDK | `backend/app/tickflow/client.py`, `backend/app/data_providers/tickflow_provider.py` | `free-api.tickflow.org` / `api.tickflow.org` | 部分可替 | daily/adj/minute/instruments 可切 `FQuantProvider` 或未来 `fquant_local`；depth 有 TDX `snapshot` 五档候选但未对拍 |
| TickFlow 能力探测 | `backend/app/tickflow/policy.py` | TickFlow API 探测 capability | 不需要 | 非 TickFlow provider 已走 provider capabilities 映射；`fquant_local` 也应走这条 |
| TickFlow universes 缓存 | `backend/app/tickflow/pools.py` | `quotes.get_by_universes`，并缓存到 `data/pools` | 可替 | `FQuantProvider.get_by_universes()` 已可走 fstore `chengfen_gu`；仍需清掉 legacy fallback 调用点 |
| fstore PG | `backend/app/data_providers/fquant/fstore_client.py` | PostgreSQL `pve.wf:5432/fstore` | 不属于磁盘替代 | 保留为 instruments/financial/universes/realtime snapshot 主源；若要纯磁盘，需要先导出这些表到 Parquet/CSV |
| engine-data HTTP | `backend/app/data_providers/fquant/engine_data_client.py` | `GET /api/v1/{wide,day,minutes,trans,xdxr,chips}` | 可替部分 | 新增 `EngineDataDiskClient`；day/wide/xdxr/fund 可由 symbol 映射路径，minutes/trans 是 `dataset/YYYY/YYYYMMDD/{market}{code}.csv` 日期分区路径；chips 不是纯读 CSV，需读 `chips/*.bin` 后计算，或改读 `chips-summary/*.turnover.json` 预计算结果；禁止目录扫描 |
| moneyflow HTTP | `backend/app/data_providers/fquant/moneyflow_client.py` | `/api/v1/moneyflow/{daily,minute}/stocks` | 可部分替 | TDX `fund/*.csv` 有日级净额分类（`Main=SuperLarge+Large`，当前覆盖 4994 只）；moneyflow 目前零业务消费者，可先把契约缩窄为净额型并做字段对拍 |
| tdx-api realtime | `backend/app/data_providers/fquant_provider.py:get_realtime` | 可选 `FQUANT_TDX_API_BASE` `/api/quote` | 可替实时/五档候选 | `../tdx-api` 已实现 `/api/quote` 和 `/api/quote-snapshot`，直接连通达信 TCP 服务器取实时五档，不是读磁盘 snapshot；当前本机默认端口 26688/8080 未启动 |
| fstore realtime snapshot | `FQuantProvider._get_fstore_realtime()` | `t_{asset_type}_daily_markets` 最新快照 | 可替部分实时快照 | fstore 数字资产表可读；不是盘中流式行情，只能作为 snapshot fallback |
| depth 5 档盘口 | `backend/app/services/depth_service.py` | TickFlow depth API，写 `data/depth5` | 有候选 | TDX `snapshot` 有 Bid/Ask 五档字段；需验证封单、单位、时间覆盖后才能替 `depth5` |
| 外部扩展数据拉取 | `backend/app/services/ext_pull.py`, `ext_presets.py` | 用户配置 URL、内置 THS JSON URL | 已有本地落盘 | 拉取后写 `data/ext_data`；替代方式是手工上传/JSON ingest/预置文件，不属于行情源替代 |
| AI 生成/分析 | `backend/app/services/ai_provider.py` 及 analyzer | OpenAI 兼容、Codex CLI 等 | 不替代 | 这是文本生成，不是行情查询；结果已存 JSON |
| TickFlow 端点清单/测速 | `backend/app/api/settings.py` | `https://tickflow.org/endpoints.json`，以及用户选择的 endpoint `/health` | 不需要行情替代 | 这是配置辅助查询；失败已有内置端点列表 fallback |
| 飞书 webhook | `backend/app/services/webhook_adapter.py` | 用户配置的飞书机器人 URL | 不替代 | 这是通知输出通道，不是行情数据源；失败静默降级 |
| AI 策略生成 | `backend/app/strategy/ai_generator.py` | 读取 `docs/strategy-guide.md` + 调用 AI provider | 不替代 | 读取指南是本地；LLM 生成不属于行情数据查询 |
| fhold 本地持仓 | `../fhold` SQLite / XLSX importer | `~/.fhold/fhold.db`、导入表格 | 可作为用户持仓源 | 属于个人账户持仓，不是公共行情 provider；可单独读取 `positions` / `position_snapshots`，需要隐私边界和显式授权 |

## 3. Provider 方法矩阵

| Provider 方法 | 当前 TickFlow | 当前 fquant | 本地磁盘替代 | 风险/备注 |
|---|---|---|---|---|
| `get_instruments(asset_type)` | `exchanges.get_instruments` | fstore `base_infos` | 不直接替 | TDX 目录可推导部分 symbol，但缺 name/asset_type/上市日/股本；建议继续用 fstore |
| `get_daily(symbols, range)` | `klines.batch(adjust=none)` | engine-data `wide`，fallback fstore `day_klines fq=0` | 可替 | TDX `day/*.csv` 可替 engine-data，但当前 CSV 是前复权序列，需要 raw 重建 |
| `get_adj_factors(symbols, range)` | `klines.ex_factors` | engine-data `xdxr`，fallback fstore `chuquan_chuxi` | 可替 | TDX `xdxr/*.csv` 可替；现金分红因子依赖 raw pre_close，必须和 daily raw 重建一起做 |
| `get_minute(symbols, date)` | provider 当前返回空，service 仍有同步路径 | engine-data `minutes` | 可替部分 | engine-data HTTP 已能读 `price/volume` 简表；直接磁盘路径不是 `{prefix}/{code}.csv`，时间戳需客户端重建 |
| `get_realtime(universes/symbols)` | `quotes.get` / `get_by_universes` | tdx-api + fstore snapshot | 磁盘 snapshot 可替部分 | TDX `snapshot/YYYY/YYYYMMDD/{symbol}.csv` 有实时价/OHLC/五档字段；要对齐 `_quote_row` 并验证刷新完整性 |
| `get_by_universes()` | `quotes.get_by_universes` | fstore `chengfen_gu` + `base_infos` | 不直接替 | 成分股/板块需要结构化表；磁盘目录不足以提供权重和成分 |
| `get_financial(symbol, table)` | 当前 provider 未实现 | fstore `financial_report_*` | 不直接替 | 可预导出财务 Parquet，但当前没有磁盘源 |
| `get_moneyflow_daily/minute` | 无 | moneyflow HTTP | 缩窄契约后可替 | TDX `fund/*.csv` 可直接适配净额型日级分类；缺 inflow/outflow、volume、amount，minute 仍需另评估；当前 provider-only、零业务消费者 |
| `get_transactions(symbol, date)` | 无 | engine-data `trans` | 可替 | engine-data HTTP 已能读 `time/price/volume/amount/order_count/direction`；当前业务零消费者 |
| `get_corp_action()` | 无 | fstore `chuquan_chuxi` + engine xdxr | 可替部分 | TDX xdxr 可提供事件；fstore 仍可做对拍 |
| `get_universe_constituents()` | 无 | fstore `chengfen_gu_items` | 不直接替 | 需 fstore 或导出表 |

## 4. 同步/写入链路

| 链路 | 代码入口 | 当前读取 | 当前写入 | 本地磁盘替代标注 |
|---|---|---|---|---|
| A 股日 K 同步 | `services/kline_sync.py:sync_and_persist_daily_batch` | `provider.get_daily()` | `data/kline_daily/date=*/part.parquet` | `fquant_local` 如取消 raw 镜像，必须在这里 no-op 或只触发 enriched 重算 |
| 实时行情写当日 K | `kline_sync.sync_daily_by_quotes` | `provider.get_realtime(CN_Equity_A)` | 覆写 `data/kline_daily` 当日分区 | 本地磁盘模式应禁写 raw daily，只保留内存/当日 enriched 路径 |
| QuoteService 全市场刷新 | `services/quote_service.py` | `provider.get_realtime()` | `kline_daily`、`kline_etf_daily`、enriched 当日分区、告警 | 这是 D-L5 容易漏的写入点；本地磁盘模式需加禁写策略 |
| 自选实时刷新 | `quote_service._fetch_watchlist_quotes` | `provider.get_realtime(symbols)` | merge 当日 `kline_daily` 和 enriched | 同上，不能让少量自选写回 raw daily |
| 分钟 K 同步 | `kline_sync.sync_and_persist_minute` | `provider.get_minute()` | `data/kline_minute/date=*` | engine-data HTTP `minutes` 已验证可读 `price/volume`；若直读磁盘需按日期分区路径实现并重建时间戳 |
| 除权因子同步 | `kline_sync.sync_adj_factor` | `provider.get_adj_factors()` | `data/adj_factor/all.parquet` | 可用 TDX xdxr 替代，但 raw pre_close 要同步修 |
| 标的维表同步 | `services/instrument_sync.py` | `provider.get_instruments("stock")` | `data/instruments/instruments.parquet` | 不建议用磁盘目录替；继续 fstore |
| 指数/ETF 标的同步 | `services/index_sync.py` | `provider.get_instruments(index/etf)` + `get_by_universes` | `instruments_index` / `instruments_etf` | fstore 可替 TickFlow；磁盘目录只能补行情，不补元数据 |
| 指数/ETF 日 K 同步 | `index_sync.sync_and_persist_*_daily` | 复用 `kline_sync.sync_daily_batch` | `kline_index_daily` / `kline_etf_daily` 和 enriched | TDX `day/sh000/sz399/sh880/...` 可替，但路径规则和 asset_type 要覆盖 |
| 财务同步 | `services/financial_sync.py` | `provider.get_financial()` | `data/financials/{table}/part.parquet` | 保留 fstore；无 TDX 磁盘替代 |
| 盘后管道 | `jobs/daily_pipeline.py` | instruments、provider、已有 Parquet | raw/enriched/adj/minute 多目录 | 本地磁盘模式要绕过 raw 抓取，直接从 DiskClient/CSV 生成 enriched |
| 历史扩展 | `services/extend_history.py` | `kline_sync.sync_and_persist_daily_batch` + adj | raw daily + enriched 全量重算 | 若无 `kline_daily` 镜像，需改为“扩展 enriched 历史范围” |
| 数据清理/重建 | `api/data.py`, `api/kline.py` | `data/` 目录和 DuckDB views | 删除/重建 Parquet views | 本地磁盘源只读，不应被 `/api/data/clear` 删除 |

## 5. 运行时本地读取链路

| 功能 | 代码入口 | 当前查询逻辑 | 是否还需要上游 | 本地替代标注 |
|---|---|---|---|---|
| 单股日 K | `api/kline.py:/daily` → `repo.get_daily()` | 读 `kline_daily_enriched`，缓存覆盖今日；空时 live fetch provider | 通常不需要；空数据时需要 | 本地磁盘模式应把空数据 fallback 改为直读 CSV + compute，而不是写 raw parquet |
| 批量日 K | `api/kline.py:/daily-batch` | `repo.get_daily_batch()` 读 enriched | 不需要 | 已本地；只依赖 enriched 生成 |
| 单股分钟 K | `api/kline.py:/minute` | 读 `kline_minute`；缺失时 `fetch_minute_single()` | 缺失时需要 | 可用 engine-data `minutes` 或同路径 DiskClient 做 fallback，字段只有 `price/volume` 时需重建时间 |
| 指数日 K | `api/indices.py:/daily` | `repo.get_index_daily()`；空时 provider 拉取并即时 compute | 缺失时需要 | 本地磁盘可替 provider fallback |
| 指数分钟 | `api/indices.py:/minute` | `kline_sync.fetch_minute_single()` | 需要 provider | 可用 engine-data `minutes` 或同路径 DiskClient 替，需先对拍指数覆盖 |
| 自选列表 | `services/watchlist.py` | `data/user_data/watchlist.parquet` | 不需要 | 已本地 |
| 自选行情 | `api/watchlist.py:/quotes` | `watchlist.fetch_quotes()` 走 provider realtime | 需要实时源 | 磁盘不可替；可用 tdx-api/sina/tencent/fstore snapshot |
| 自选 enriched | `api/watchlist.py:/enriched` | `repo.get_enriched_latest()` + ext join | 不需要 | 已本地 |
| 策略筛选 | `services/screener.py` | `repo` 缓存、`kline_daily_enriched` scan、DuckDB SQL | 不需要 | 已本地；质量取决于 enriched |
| 市场快照 | `api/screener.py:/market-snapshot` | latest enriched + instruments | 不需要 | 已本地 |
| 连板梯队 | `api/screener.py:/limit-ladder` | latest enriched + depth sealed map + ext join | depth 可能需要 | 价格本地；封单真假需要 depth，TDX `snapshot` 有五档候选但未对拍 |
| 大盘概览 | `api/overview.py`, `services/market_overview_builder.py` | quote_service 指数缓存、enriched、ext_data、depth | 部分需要实时/depth | 盘后可本地；盘中仍需 realtime/depth |
| RPS 轮动 | `services/rps_rotation.py` | enriched 历史 + ext concept map | 不需要 | 已本地 |
| 回测 | `services/backtest.py`, `backtest/engine.py` | scan `kline_daily_enriched` 构造面板 | 不需要 | 已本地；依赖 enriched 覆盖范围 |
| 个股技术分析 | `api/stock_analysis.py` | `repo.get_daily()` + levels compute | 不需要 | 已本地 |
| 个股 AI 分析 | `services/stock_analyzer.py` | `repo.get_daily()` + financial parquet + AI | 行情/财务本地；AI 外部 | 数据可本地，文本生成不替 |
| 财务页面 | `api/financials.py` | `get_financial_df()` 读 `data/financials` | 同步时需要 fstore | 查询已本地；更新仍要 fstore |
| 财务 AI 分析 | `services/financial_analyzer.py` | financial parquet + AI | AI 外部 | 数据本地 |
| 市场复盘 | `api/market_recap.py` | overview builder + AI + saved reports | 实时/depth/AI 部分需要 | 盘后行情可本地，AI 不替 |
| 盘中 SSE | `api/intraday.py` | QuoteService 内存缓存 + repo fallback | 需要 realtime | TDX `snapshot` 可作盘中快照候选，但不是已接入的流式 realtime |
| 数据状态 | `api/data.py` | 目录扫描、DuckDB `DESCRIBE`、Parquet 统计 | 不需要 | 已本地；若无 stock raw mirror 需调整统计口径 |
| 扩展数据 | `api/ext_data.py` | `data/ext_data` config/parquet，上传 CSV/XLSX，外部 pull | pull 时需要 URL | 查询已本地；更新可手工上传替代 |
| 告警历史 | `services/alert_store.py` | `data/user_data/alerts.jsonl` | 不需要 | 已本地 |
| AI 报告历史 | `ai_reports.py`, `stock_reports.py`, `market_recap_reports.py` | `data/user_data/*.json` | 不需要 | 已本地 |
| 偏好/认证/任务 | `preferences.py`, `auth.py`, `pipeline_jobs.py` | `data/user_data/*.json`, `data/job_store/*.json` | 不需要 | 已本地 |
| 自定义策略/信号/监控规则 | `strategy/*`, `api/signals.py`, `api/monitor_rules.py` | `data/strategies`, `data/user_data` JSON/Python 文件 | 不需要 | 已本地 |
| 设置/端点测试 | `api/settings.py` | `secrets.json`、`preferences.json`、TickFlow endpoints manifest/health probe | 仅端点测试需要外部 | 与本地行情源无关；`fquant_local` 需要补设置白名单和前端下拉 |
| 通知推送 | `webhook_adapter.py` | 飞书 webhook HTTP POST | 需要外部 webhook | 不是数据查询；本地只能保存配置和告警记录 |
| AI 策略生成 | `strategy/ai_generator.py` | 本地策略指南 + AI provider | AI 外部 | 数据源不可替；生成代码禁止 import `httpx/requests` 等外部访问 |
| 维护脚本 | `backend/scripts/*.py` | `test_fquant_provider.py` 走真实/模拟 provider；`cleanup_halt_days.py` 扫 `kline_daily*` | 按脚本用途 | 测试脚本应随 provider 增补；清理脚本在取消 stock raw mirror 后需确认仍只处理存在目录 |

## 6. DuckDB/Parquet 视图与物理目录

| 目录/视图 | 主要读取者 | 主要写入者 | 本地磁盘替代影响 |
|---|---|---|---|
| `kline_daily` | pipeline、数据状态、历史扩展、部分 raw fallback | `kline_sync`、`QuoteService` | 如果本地磁盘模式取消镜像，需要替换 pipeline 输入和统计口径 |
| `kline_daily_enriched` | K 线、筛选、回测、RPS、分析、概览 | pipeline、QuoteService live enriched | 保留，作为计算缓存 |
| `kline_minute` | 分钟图、分钟同步状态 | `kline_sync` | 可由 TDX minute CSV 生成，或改为按需读磁盘 |
| `adj_factor` | pipeline、单股 live compute | `kline_sync.sync_adj_factor` | 可由 TDX xdxr 生成，但需 raw pre_close |
| `instruments` | 搜索、名称、筛选、涨跌停、市值 | `instrument_sync` | 继续 fstore；磁盘目录不足 |
| `instruments_index` / `instruments_etf` | 指数页、ETF 同步 | `index_sync` | 继续 fstore；磁盘只补行情 |
| `kline_index_daily/enriched` | 指数页、侧边栏指数 | `index_sync` | 可由 TDX day CSV 替行情 |
| `kline_etf_daily/enriched` | ETF 相关缓存 | `index_sync`、QuoteService | 可由 TDX day CSV 替行情 |
| `financials/*` | 财务页面、AI 财报 | `financial_sync` | 继续 fstore 或预导出 parquet |
| `depth5` | 连板真假封单、深度服务 | `DepthService` | TDX `snapshot` 有五档候选；未完成业务语义对拍 |
| `ext_data/*` | 概念/行业/自定义扩展列 | 上传、pull、presets | 已本地；外部更新可用手工上传替代 |
| `user_data/*` | 偏好、报告、告警、认证、策略配置 | 各 service/API | 已本地 |

## 7. 本地磁盘源替代实施标注

| 优先级 | 替代目标 | 需要做的最小改动 | 不做的事 |
|---|---|---|---|
| P0 | 日 K raw 重建 | 在 fquant mapping 内处理 TDX 前复权 CSV，输出原始 OHLCV；用 fstore `day_klines fq=0` 对拍到 2025-10-31，2025-11 之后缺口用 xdxr 减法逆运算补齐 | 不在业务层到处修价格 |
| P0 | `fquant_local` 模式切换 | registry 加 factory；preferences 白名单；settings API；前端类型/下拉；capability cache provider 名 | 不新增第二套 service |
| P0 | 禁止本地模式写 stock raw mirror | repository 层单一收口，门控 stock raw 写入口；index/ETF raw 暂留给现有缓存和页面 | 不在每个调用方重复加门控；不删除 enriched 缓存 |
| P1 | pipeline 直算 enriched | 给 pipeline 一个 daily source 参数或 provider reader，读 DiskClient 输出后写 `kline_daily_enriched` | 不让 `/api/data/clear` 删除 TDX 磁盘源 |
| P1 | 单股 K fallback | `/api/kline/daily` 空缓存时直读 DiskClient + compute | 不落 `kline_daily` |
| P1 | 指数/ETF 日 K | 扩展路径规则覆盖 `sh000/sz399/sh880/sh881` 和 ETF symbol | 不用目录扫描发现 symbol |
| P2 | 分钟 K | engine-data HTTP `minutes` 已验证；直读 DiskClient 按 `minutes/YYYY/YYYYMMDD/{symbol}.csv` 路径实现并重建时间戳 | 不假设格式等同标准 OHLCV 分钟线 |
| P2 | 实时行情 | 明确 tdx-api/sina/tencent/fstore snapshot 的字段单位，输出 `_quote_row` 兼容字段 | 不用磁盘 CSV 冒充实时 |
| P3 | 财务/成分 | 保留 fstore，或另做导出 parquet 作只读快照 | 不从 TDX day CSV 推导财务/成分 |
| P3 | 资金流 | 用 TDX `fund/*.csv` 适配日级净额分类契约，先做字段/覆盖对拍；minute 或 inflow/outflow 全量契约仍保留 moneyflow HTTP 或降级空 | 不从 TDX day CSV 推导资金流 |

门控上线后的调用方验证用例：

- `kline_sync.sync_and_persist_daily_batch` → `append_daily`
- `kline_sync.sync_daily_by_quotes` → `flush_live_daily`
- `QuoteService` 全市场股票/ETF → `flush_live_daily` / `flush_live_daily_asset("etf")`
- `QuoteService` 自选 → `merge_live_daily_asset("stock")`
- `index_sync.sync_and_persist_index_daily` → `append_index_daily`
- `index_sync.sync_and_persist_etf_daily` → `append_etf_daily`

## 8. 目前不能完全本地磁盘替代的点

- **实时行情**：watchlist、盘中 SSE、QuoteService、监控告警需要实时源；`../tdx-api` 能通过 `/api/quote` 提供实时五档，但当前本机默认端口未启动；磁盘日 K 只能盘后。
- **盘口 depth**：`../tdx-api` `/api/quote` 返回 `BuyLevel/SellLevel` 五档，可作为 depth 候选；业务 depth 契约仍需做字段单位、封单语义、批量限制和错误降级对拍。TDX 磁盘 `snapshot` 也有 Bid/Ask 五档候选，但采集完整性未验证。
- **财务报表**：当前可信源是 fstore `financial_report_*`。
- **指数/板块成分**：当前可信源是 fstore `chengfen_gu` / `chengfen_gu_items`。
- **资金流**：TDX `fund/*.csv` 可部分替代日级净额分类（当前覆盖 4994 只，且 moneyflow 零业务消费者，契约可缩窄为净额型）；仍需字段口径对拍，minute 与 inflow/outflow 全量契约暂不视为已替代。
- **`day/` 覆盖缺口**：按现有 `instruments.parquet` 路径规则统计，`day_exists=4989/5857`（85.2%），868 只无文件；基准表无退市字段、BJ 仅 6 条，真实 A 股构成待用 fstore `base_infos` 交叉核对。
- **AI 文本生成**：不是行情数据源问题，不能用本地行情替代。

## 9. 扫描证据

本清单基于以下当前代码面扫描：

- 路由注册：`backend/app/main.py`
- 后端 API：`backend/app/api/*.py`
- 后端 service：`backend/app/services/*.py`
- Provider：`backend/app/data_providers/**/*.py`
- 仓库/缓存：`backend/app/tickflow/repository.py`
- 计算管道：`backend/app/indicators/pipeline.py`
- 前端调用：`frontend/src/lib/api.ts`, `frontend/src/lib/useQuoteStream.ts`, `frontend/src/lib/backtestTask.ts`
- 维护脚本：`backend/scripts/*.py`
- 搜索命令：`rg "read_parquet|scan_parquet|httpx|requests|get_provider|get_daily|get_realtime|get_minute|get_financial|execute_one|execute_all|write_parquet" backend/app frontend/src`

## 10. 本地数据源实测校验

日期：2026-07-02。所有校验均为只读；未调用 fquant HTTP API。

### 10.1 TDX 磁盘 CSV/JSON

路径规则：`/Volumes/vol3/tdx/{dataset}/{prefix}/{prefix_code}.csv`，`prefix` 为 `sh600`、`sz000`、`sh000`、`sz399` 等目录。

| dataset | 实测获取方式 | 样本 | 实测字段 | 数据真实性/完整性结论 |
|---|---|---|---|---|
| `day` | 直接读 `/Volumes/vol3/tdx/day/sh600/sh600519.csv`、`/day/sh000/sh000001.csv` | 600519 共 5951 行，2001-08-27 到 2026-07-01；上证指数 1990-12-19 到 2026-07-01 | `date/open/close/high/low/volume/amount/up/down/datetime/adjustment_count` | 可获取真实行情序列；但 A 股个股历史段是 TDX 前复权序列，600519 早期出现负价，2026-06-25 仍带 `.075769` 尾巴且 `adjustment_count=1`，不能直接当 raw |
| `wide` | 直接读 `/Volumes/vol3/tdx/wide/sh600/sh600519.csv` | 600519 共 5951 行 | `day` 字段 + `last_close/change_rate/open_volume/open_turnz/open_unmatched/close_volume/close_turnz/close_unmatched/inner_volume/outer_volume/inner_amount/outer_amount` | 字段完整度高于 `day`，可替 engine-data `wide`；同样继承前复权污染，raw 修复必须共用 |
| `fund` | 直接读 `/Volumes/vol3/tdx/fund/sh600/sh600519.csv` | 600519 共 123 行，2025-12-24 到 2026-07-01；按现有 instruments 路径规则覆盖 4994 只 | `Date/Code/Main/MainRatio/SuperLarge/SuperLargeRatio/Large/LargeRatio/Medium/MediumRatio/Small/SmallRatio` | 可获取真实日级资金净额分类；样本满足 `Main=SuperLarge+Large`、`MainRatio≈SuperLargeRatio+LargeRatio`；缺 inflow/outflow、volume、amount |
| `xdxr` | 直接读 `/Volumes/vol3/tdx/xdxr/sh600/sh600519.csv` | 600519 共 45 行，2002-07-25 到 2026-06-26 | `Date/Category/Name/FenHong/PeiGuJia/SongZhuanGu/PeiGu/SuoGu/QianLiuTong/HouLiuTong/QianZongGuBen/HouZongGuBen/FenShu/XingQuanJia` | 可获取真实除权除息/股本变化事件；字段足够做 xdxr 逆运算，但需和 fstore `chuquan_chuxi` 对拍事件口径 |
| `5min` | 直接读 `/Volumes/vol3/tdx/5min/sz300/sz300773.csv` | 23609 行，2024-06-12 起 | `date/open/close/high/low/volume/amount/up/down/datetime/adjustment_count` | 文件可读且字段与 `day` 同构；当前只发现少量样本，不能直接承诺全市场分钟覆盖 |
| `minutes` | 直接读 `/Volumes/vol3/tdx/minutes/2026/20260701/sh600519.csv` | 600519 当日样本可读 | `Price/Vol` | 可作为 engine-data `minutes` 的真实磁盘源；路径是日期分区，不是 `{prefix}/{symbol}.csv`；字段只有价格/量，需要重建时间戳或另查采样频率 |
| `trans` | 直接读 `/Volumes/vol3/tdx/trans/2026/20260701/sh600519.csv` | 600519 当日样本可读 | `time/price/vol/num/amount/buyorsell` | 可作为 engine-data `trans` 的真实磁盘源；`num` 可映射 order_count，`buyorsell` 可映射 direction；当前业务零消费者 |
| `chips-summary` | 直接读 `/Volumes/vol3/tdx/chips-summary/sh601/sh601398.turnover.json`；`find` 统计 98 个 `.turnover.json` | JSON dict，601398 样本 `count=270`，`start=2024-06-29`、`end=2026-06-29` | `dataset/code/cache_id/profile/generated_at/start/end/count/days[].date/current_price/profiles[].avg_cost/peak_price/.../decay_mode` | 可获取预计算 turnover 筹码汇总；当前 provider 契约没有对应方法；全量 chips 当前瓶颈更偏磁盘/计算性能，今晚换盘后需复测覆盖和延迟，再决定是否直接接 engine chips 或读预计算 summary |
| `snapshot` | 读 `/Volumes/vol3/tdx/snapshot/2026/20260702/sh688305.csv`；另查 `../tdx-api` quote 实现 | 磁盘样本 40 行，09:15:02 到 09:27:09；`tdx-api` 提供 `/api/quote` 实时接口 | 磁盘字段 `Date/SecurityCode/Price/.../Bid1..AskVol5/TimeStamp`；`tdx-api` 字段 `K/TotalHand/Amount/BuyLevel/SellLevel` | 磁盘 snapshot 是可读候选但刷新完整性未验证；`tdx-api` 不是读磁盘，而是连通达信 TCP 实时取五档，更适合作为 realtime/depth 上游 |
| `flash` | 查看 `/Volumes/vol3/tdx/flash/2026/` | `box/f10/history/ism/misc/rzrq.YYYY-MM-DD` 等日文件 | 非统一 CSV header | 是扩展/资讯类批量文件，不是 OHLCV/realtime 契约的直接替代 |
| `infoq` | 流式读 `/Volumes/vol3/tdx/infoq/2013Q2/preview.csv` 头部 | 单文件约 149MB，CSV header 可读 | `SecurityCode/SecurityName/OrgCode/NoticeDate/ReportDate/PredictFinance*...` | 财务预告类数据真实存在；文件很大，必须流式/列投影读取；当前财务 provider 仍以 fstore `financial_report_*` 为主 |
| `holding` | 查看 `/Volumes/vol3/tdx/holding/2026Q2/` | 季度目录存在，样本只见 `sz002/` 子目录 | 未取到统一 preview 文件 | 可能是持仓/股东类数据；当前 provider 契约未覆盖，暂不纳入本地行情替代 |
| `qmt` | 读 `/Volumes/vol3/tdx/qmt/888xxxxxxx-positions.csv` 头部 | CSV header 可读 | `account_type/account_id/strategy_code/order_flag/stock_code/volume/...` | 是账户持仓数据，不属于行情/财务公共数据源；不应并入 provider |

指数/ETF 样本：

- `/Volumes/vol3/tdx/day/sh000/sh000001.csv` 可读，上证指数 2026-07-01 close=4112.45，字段与 `day` 同构。
- `/Volumes/vol3/tdx/day/sz399/sz399001.csv` 可读，深证成指 2026-07-01 close=16119.17。
- `/Volumes/vol3/tdx/day/sh510/sh510300.csv` 可读，ETF 字段同构，但历史段也可能有 `adjustment_count`。
- `/Volumes/vol3/tdx/day/sh881/sh881001.csv` 可读；`sh880/sh880001.csv` 未命中，行业/概念指数路径需要逐类对拍。

覆盖校验：

- 按 `data/instruments/instruments.parquet` 的 5857 个 symbol 套 `day/{market}{code[:3]}/{market}{code}.csv`：`day_exists=4989/5857`（85.2%），868 只无文件；该基准表无退市字段、BJ 仅 6 条，真实 A 股构成仍需 fstore `base_infos` 交叉核对。
- 同一基准下 `fund_exists=4994`、`both(day,fund)=4988`；`fund/` 覆盖接近 `day/`，但不覆盖 BJ。

### 10.2 fstore PostgreSQL 直连

获取方式：加载 `/Users/wf2311/Projects/wf2311/fm/fquant/.env` 中的 `FSTORE_DATABASE_*`，通过 `backend/app/data_providers/fquant/fstore_client.py` 的 psycopg 直连查询；密码未写入本文档。

| 表/用途 | 实测获取方式 | 样本结果 | 字段完整性/真实性结论 |
|---|---|---|---|
| `base_infos` 标的 | `select code,name,stype,asset_type,ssdate,day from base_infos ...` | 可返回 `000001`、`600519` 等多资产记录 | 可作为 instruments 权威源；同 code 可能跨资产类型重名，不能只按 6 位 code 判定交易所/资产 |
| `day_klines` raw oracle | `where code='600519' and fq=0 order by tdate desc limit 3` | 600519 最新 fq=0 样本为 2025-10-31，close=1430.0100 | 可作为 raw 对拍 oracle，但覆盖只到 2025-10-31，之后需 xdxr 逆运算补齐 |
| `chuquan_chuxi` 除权 | `where code='600519' order by t_date desc limit 3` | 可取 2025-12-19 `pxbl=23.957` 等事件 | 可对拍 TDX `xdxr`；字段名与 TDX 不同，需要 mapping |
| `financial_report_*` 财务 | `financial_report_income_statement/balance_sheet/cash_flow` 抽样 | income 样本含 `total_oper_income/operate_profit/parent_net_profit`；balance/cash_flow 均可读 | 财务数据真实可取；TDX 磁盘当前不替代，fstore 或导出 parquet 是可行路径 |
| `chengfen_gu` / `chengfen_gu_items` | `select * ... limit 1` | `chengfen_gu` 有 BK 行业成分 JSON；`chengfen_gu_items` 有 `index_code/stock_code/weight/join_date/t_date` | 成分股/行业结构化字段完整，继续作为 universes/constituents 权威源 |
| realtime snapshot | `t_1_daily_markets/t_20_daily_markets/t_10_daily_markets` 抽样 | 2026-07-01 可取 `price/zgj/zdj/jrkpj/zrspj/cjl/cje` | snapshot 表真实存在；实际表名是数字资产类型（如 `t_1_daily_markets`），不是 `t_stock_daily_markets`，接入需校准 asset_type 到表名 |
| 基金持仓/基金行情 | 对照 `../fstore/fdata-common/models/fund_position.go` 后查 `stock_positions/fund_ranks/fund_navs/fund_max_backs/fund_base_infos` | `stock_positions` 6438 行，`tdate=2024-12-20..2025-12-18`；`fund_ranks` 1776705 行，`fund_navs` 1817459 行；`fund_max_backs`、`fund_base_infos` 为空表 | fstore 已有部分基金持仓/基金净值数据，可作为公共“机构/基金持仓”源；与个人持仓不同，不应混入 watchlist/realtime |
| 主力/机构持仓统计 | 扫描 PG 字段后查 `stock_zhuli` | 65693 行，`t_date=2021-06-30..2026-03-31`；字段含 `fund/qfii/social/broker/insurer/trust *_holders_count/*_holdings_total/*_holdings_ratio/*_holdings_value/*_change*` | 原 `stock_fund_flow*` 模型表未落库，但机构持仓统计实际在 `stock_zhuli`；它是季度/报告期持仓类，不是分钟资金流 |
| 股东/解禁/高管增减持 | 对照 `../fstore/fdata-common/models/shareholder.go` 后查 `shareholder_count/top10/unlock/change/executive` | `shareholder_count` 122452 行、`shareholder_top10` 264420 行、`shareholder_unlock` 12738 行、`shareholder_change` 72035 行、`shareholder_executive` 70115 行 | fstore 已有结构化股东/解禁/增减持数据，可作为补充基本面/事件源；当前 provider/base 契约未覆盖 |
| 日级资金流字段 | 按 `zljlr/cddlr/cddlc/cddjlr/ddjlr` 字段扫描 PG | `t_1_daily_markets` 2137790 行，`tdate=2022-03-04..2026-07-01`；多资产 `t_N_daily_markets` 均有同类字段；空壳 `daily_markets` 0 行 | 日级主力/超大/大单等资金流不在 `stock_fund_flow*`，而在 `t_N_daily_markets`；可作为日级 moneyflow 本地源 |
| 分钟资金流字段 | 按 `money_flow_minutes` 表名/字段扫描 PG | `t_1_money_flow_minutes` 1242607 行，5296 只，日期 `2026-06-18`；`t_20` 196719 行，`2026-05-29`；`t_37/t_40/t_41/t_42` 也有板块分钟样本 | 完整 minute moneyflow 契约在 `t_N_money_flow_minutes` 分表，字段含 inflow/outflow/net 和大小单分类；覆盖目前是少量日期 |
| 沪深港通资金流 | 查 `hsgt_money_flow/hsgt_money_flow_summary/hsgt_money_flow_stats` | `hsgt_money_flow` 15517 行，`t_date=2014-12-16..2026-06-30`；summary/stats 为空 | 北向/南向资金有独立表，不能等同个股/板块 moneyflow |
| 未落库模型表 | 对照 `../fstore/fdata-common/models/fund_flow.go` 后查 `stock_fund_flow/stock_fund_flow_summary/fund_institution_types` | 三表在当前 fstore PG 中不存在；按表名和字段扫描也未发现同名落库 | 仅说明这组三模型未落表；实际相关能力分散在 `stock_zhuli`、`t_N_daily_markets`、`t_N_money_flow_minutes`、`hsgt_money_flow` |

常用查询方式：

```sql
-- 机构/主力持仓统计：基金、QFII、社保、券商、保险、信托
SELECT *
FROM stock_zhuli
WHERE code = '600519'
ORDER BY t_date DESC
LIMIT 20;

-- 日级资金流：A 股
SELECT
  code, name, tdate,
  zljlr,                 -- 主力净流入
  cddlr, cddlc, cddjlr,  -- 超大单流入/流出/净流入
  ddlr, ddlc, ddjlr      -- 大单流入/流出/净流入
FROM t_1_daily_markets
WHERE code = '600519'
ORDER BY tdate DESC
LIMIT 20;

-- 分钟资金流：A 股
SELECT
  code, trade_date, bucket_time,
  total_amount, total_volume,
  inflow_amount, outflow_amount, net_amount,
  super_large_inflow, super_large_outflow, super_large_net,
  large_inflow, large_outflow, large_net,
  medium_inflow, medium_outflow, medium_net,
  small_inflow, small_outflow, small_net,
  main_traditional_net,
  main_broad_net,
  retail_net,
  source
FROM t_1_money_flow_minutes
WHERE code = '600519'
  AND trade_date = '2026-06-18'
ORDER BY bucket_time;

-- 沪深港通资金流
SELECT *
FROM hsgt_money_flow
WHERE t_date >= '2026-06-01'
ORDER BY t_date DESC, m_type
LIMIT 50;
```

分表规则：A 股用 `t_1_*`，ETF 用 `t_20_*`，可转债用 `t_30_*`；板块类分钟资金流当前可查 `t_37/t_40/t_41/t_42_money_flow_minutes`。

项目内 Python 直连示例：

```bash
cd backend
set -a; . /Users/wf2311/Projects/wf2311/fm/fquant/.env; set +a
uv run python - <<'PY'
from app.data_providers.fquant.fstore_client import FStoreClient

db = FStoreClient()
rows = db.query("""
SELECT code, trade_date, bucket_time, net_amount, main_traditional_net
FROM t_1_money_flow_minutes
WHERE code=%s AND trade_date=%s
ORDER BY bucket_time
LIMIT 10
""", ("600519", "2026-06-18"))

for r in rows:
    print(dict(r))
PY
```

### 10.3 engine-data 本地镜像 HTTP

获取方式：`curl --noproxy '*' http://192.168.5.99:8099/api/v1/{dataset}/600519?limit=2`。健康检查返回 `base_dir=/volume3/tdx`、`base_dir_exists=true`，说明它是 TDX 磁盘的 HTTP 包装层；不是 fquant HTTP API。

| dataset | 实测 endpoint | 返回字段 | 结论 |
|---|---|---|---|
| `day` | `/api/v1/day/600519?limit=2` | envelope: `cache_id/code/count/dataset/date/end/rows/source/start/truncated`；row: `adjustment_count/amount/close/date/datetime/down/high/low/open/up/volume` | 可获取真实 JSON 行，`source=day/sh600/sh600519.csv`；仍继承 TDX 前复权问题 |
| `wide` | `/api/v1/wide/600519?limit=2` | `day` OHLCV + `change_rate/last_close/open_* / close_* / inner_* / outer_*` | 字段与 provider 当前 `wide` 主源契约吻合；可作为 DiskClient 对拍对象 |
| `xdxr` | `/api/v1/xdxr/600519?limit=2` | `category/date/fenhong/fenshu/houliutong/houzongguben/name/peigu/peigujia/qianliutong/qianzongguben/songzhuangu/suogu/xingquanjia` | 可获取真实除权事件 JSON；字段名已是 provider mapping 使用的小写形态 |
| `minutes` | `/api/v1/minutes/600519?date=20260701&limit=5` | `price/volume`，envelope `source=minutes/2026/20260701/sh600519.csv` | 可获取真实分钟/tick 简表；provider mapping 当前靠客户端重建时间戳，字段完整性低于标准 OHLCV 分钟线 |
| `trans` | `/api/v1/trans/600519?date=20260701&limit=5` | `time/price/volume/amount/order_count/direction`，`source=trans/2026/20260701/sh600519.csv` | 可获取真实分笔成交；可支撑未来 `get_transactions`，但当前业务零消费者 |
| `chips` | `/api/v1/chips/600519?limit=2`；并查看 `../engine/dataserver/chips.go`、`../engine/factors/dataset_chip.go` | HTTP 样本 8 秒超时；代码先读 `chips/{prefix}/{cache_id}.bin` protobuf，再按日期计算 short/mid/long/turnover profile，并读取 `day` close、`xdxr` 股本、`flash/YYYY/misc.*` 换手率 | 不是简单磁盘 CSV 读取。当前超时先按磁盘/计算性能风险记录；今晚换盘后复测，如果延迟恢复可直接作为本地 chips 源，否则再退到 `chips-summary/*.turnover.json` 预计算或异步缓存 |

### 10.4 tdx-api 实时接口复核

获取方式：只读查看 `../tdx-api`。`web/server.go` 启动时 `tdx.DialDefault()` 连接通达信 TCP 服务器，`/api/quote` 调 `client.GetQuote()`；`web/server_engine.go` 的 `/api/quote-snapshot` 同样复用 `client.GetQuote()`。

| 接口 | 代码证据 | 字段/限制 | 结论 |
|---|---|---|---|
| `/api/quote?code=000001,600519` | `web/server.go:92-111` → `client.GetQuote(codes...)` | 返回 `protocol.Quote`：`K.Last/Open/High/Low/Close`、`ServerTime`、`TotalHand`、`Amount`、`InsideDish/OuterDisc`、`BuyLevel[5]`、`SellLevel[5]`；文档标注价格单位为厘、成交量单位为手、挂单量单位为股 | 能提供实时行情和五档盘口候选；不是本地磁盘源，而是通达信 TCP 实时源 |
| `/api/batch-quote` | `web/server_api_extended.go:70-104` | POST `codes[]`，最多 50 只 | 可支撑批量 realtime；需要 FQuantProvider 按批量 50 分片 |
| `/api/quote-snapshot` | `web/server_engine.go:26-31`、`:171-197` | POST `codes[]`，最多 50 只，返回同 `GetQuote` | 与 `/api/quote` 数据来源相同，可作为批量 snapshot 入口 |
| 当前本机状态 | `lsof` 仅发现 `server` 监听 8088；`curl 26688/8080` 连接失败；`curl 8088/api/health` ok 但 `/api/quote` 404 | `tdx-cli` 默认 `--base-url http://127.0.0.1:26688` | 项目能力成立，但当前默认 tdx-api 服务未运行；接入时需启动服务并设置 `FQUANT_TDX_API_BASE=http://127.0.0.1:26688` 或实际端口 |

### 10.5 项目 `data/` 本地缓存

获取方式：用 Polars 只读 `data/**/*.parquet`，JSON 用标准库读取。

| 本地缓存 | 实测文件 | 样本字段/行数 | 结论 |
|---|---|---|---|
| `instruments` | `data/instruments/instruments.parquet` | 5857 行；`symbol/name/code/exchange/asset_type/source/as_of` | 可读，当前 source=`fquant`；可作为覆盖统计基准，但缺退市字段 |
| `kline_daily` | `data/kline_daily/date=2025-11-27/part.parquet` | 5207 行；`symbol/date/open/high/low/close/volume/amount` | 可读；样本价格带小数尾巴，已受前复权污染，不能作为 raw truth |
| `kline_daily_enriched` | `data/kline_daily_enriched/date=2025-11-27/part.parquet` | 5207 行；raw OHLCV + `raw_close/raw_high/raw_low/turnover_rate/consecutive_limit_*` | 可作为业务查询缓存；质量依赖 raw 修复与 enriched 重算 |
| `instruments_index` | `data/instruments_index/instruments_index.parquet` | 2256 行；`symbol/name/code/asset_type/exchange/source` | 可读；样本 `000001.INDEX` 的 exchange 显示 `SZ`，需与 fstore/TDX 指数路径对拍 |
| `kline_index_daily` | `data/kline_index_daily/date=2026-07-01/part.parquet` | 130 行；`symbol/date/open/high/low/close/volume/amount` | 可读但真实性有风险：`000001.INDEX` close=10.16，明显像股票价而非上证指数；指数 raw 缓存疑似 code 映射污染，不能作为 truth |
| `financials/income` | `data/financials/income/part.parquet` | 22101 行；`symbol/source` + fstore income 字段 | 可读，财务缓存真实可用；当前只有 income 文件，balance/cash/metrics 目录未见 part 文件 |
| `ext_data/ext_gn_ths` | `data/ext_data/ext_gn_ths/part.parquet` | 5535 行；`股票代码/股票简称/所属概念/symbol/code` | 可读，属于本地扩展数据；更新仍依赖上传或 pull |
| `preferences` | `data/user_data/preferences.json` | 含 `data_provider/daily_data_provider/minute_data_provider/realtime_data_provider/adj_factor_provider` | 可读，配置缓存真实存在 |
| `adj_factor` / ETF / minute / depth | `data/adj_factor/all.parquet`、`data/kline_etf_daily`、`data/kline_minute`、`data/depth5` | 当前样本路径未命中 | 这些缓存不能假定可用；本地模式要有缺失降级或重建路径 |

### 10.6 fhold 个人持仓库

获取方式：只读查看 `../fhold` 代码与本机 `~/.fhold/fhold.db`。`../fhold/cli/internal/config/config.go`、`server/internal/repo/db.go` 默认均指向 `~/.fhold/fhold.db`；`server/internal/importer/parser.go` 从 XLSX 工作表 `持仓数据`、`交易记录`、`已清仓` 导入。

| 数据 | 获取方式 | 实测结果 | 结论 |
|---|---|---|---|
| 当前持仓 | SQLite `positions` | 本机 10 行；字段 `account_id/code/name/quantity/amount/cost_price/current_price/hold_days/daily_pnl/holding_pnl/position_ratio/source_date` | 可作为个人持仓源读取；含账户资产/盈亏，必须按用户数据处理，不并入公共 provider |
| 持仓快照 | SQLite `position_snapshots` | 本机 172 行，`snapshot_date=2026-05-11..2026-06-15`；字段与 `positions` 同构并带 `import_id/snapshot_date` | 可用于持仓历史/账户回看；与行情、机构持仓语义不同 |
| 交易/清仓 | SQLite `transactions` / `closed_positions` | 本机 `transactions=2315`、`closed_positions=931` | 可辅助个人组合分析；不属于可替代行情/财务的本地市场数据源 |

### 10.7 结论收敛

- 能直接取到真实数据且字段足够接入：TDX `day/wide/xdxr/fund/minutes/trans`、engine-data HTTP `day/wide/xdxr/minutes/trans`、fstore `base_infos/day_klines/chuquan_chuxi/financial_report_*/chengfen_gu*/shareholder_*/stock_zhuli/t_N_daily_markets/t_N_money_flow_minutes/hsgt_money_flow`、项目 `instruments/kline_daily_enriched/financials/income/ext_data/preferences`。
- 能取到但必须先修真实性或语义：TDX `day/wide/5min` 和项目 `kline_daily` 均存在前复权污染；`tdx-api` 可提供 realtime/五档但当前默认服务未运行，接入还需批量分片、单位和 depth 契约对拍；磁盘 `snapshot` 有实时/五档候选字段但采集完整性未验证；指数缓存 `kline_index_daily` 还存在 code 映射污染迹象；`fhold` 是个人持仓源，需隐私边界和独立功能入口。
- 待复测：engine-data `chips` 同步 HTTP 路径当前会超时且含计算逻辑，但预计今晚换盘后性能瓶颈可能消失；先保留为可接入候选，复测失败再退到 `chips-summary` 预计算或异步缓存。项目 `adj_factor/kline_etf_daily/kline_minute/depth5` 当前未取到可用样本。
- `../tdx-api` 项目具备 `/api/quote` HTTP realtime 能力；本机当前只确认默认端口未启动，不再把 realtime 归为“本地数据源获取不到”，而归为“可接入但需运行服务和契约对拍”。
