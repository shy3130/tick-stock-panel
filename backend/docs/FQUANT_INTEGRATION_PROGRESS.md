# FQuant 数据源接入进度

> 主线任务：**让 tickflow-stock-panel 通过 `data_providers` 抽象层读取本地 DuckDB 发布快照，并保留可切换 provider 的业务契约。**
>
> 最后更新：2026-09-03（Issue #56 dataquery v2 cutover）
> 状态：A 股点查/窄区间行情已切换 engine dataquery v2 HTTP（`FQUANT_DATAQUERY_ENABLED` 默认开，关=0 整链回退 legacy DuckDB）；本地 DuckDB provider 仍是其余全部数据面；A 股 minutes/trans 已改为按 `(route_key, market, trade_date)` 读取 engine 发布 catalog，严格校验 freshness，解析失败不降级到 writer-owned raw 文件。
> 范围：本文是**给团队看的项目状态文档**，不是技术设计文档。设计稿见 [`FQUANT_PROVIDER_DESIGN.md`](./FQUANT_PROVIDER_DESIGN.md)（846 行，全实测字段），旧 PoC 现状见 [`FQUANT_PROVIDER.md`](./FQUANT_PROVIDER.md)。

---

## 0. 2026-08-18 当前状态

- `FQuantProvider` 的行情主路径是只读本地 DuckDB；旧的 PG / engine-data HTTP 阶段说明保留在下文，仅作为迁移历史，不再代表当前运行架构。
- A 股分钟线通过 `catalog_resolver.resolve_route("tdx_minutes", "a", trade_date)` 定位 2023 年前归档或当前快照；A 股逐笔通过 `catalog_resolver.resolve_route("tdx_trans", "a", trade_date)` 定位历史归档年片或活跃年的月度快照。route catalog 每次查询重新解析；校验失败 fail-closed，绝不降级 writer-owned raw。
- A 股 canonical enriched 全历史使用独立 generation：回填在启动时固定已发布 `tdx/fstore/markets/klines/extended` 的具体文件路径，worker 不再跟随 `current.json`；可配置 1–8 个独立只读 provider worker，主线程串行写 staging，完整成功后才原子切换 `current.json`。schema v2 为 15 列，在复权前原生保存 `raw_open/raw_high/raw_low/raw_close`，禁止从复权价反推；输出位于 `TICKFLOW_CANONICAL_HISTORY_ROOT`，不写用户 `data/`。研究 reader 构造时再次 pin generation 与 manifest 字节哈希，且不合并近期 overlay。
- 新增只读市场数据工作台（研究页“市场数据”）：`tdx_chip` 筹码、个股/板块日级及分钟资金流、集合竞价和 A 股逐笔成交均提供 capability/status、受限 API 与前端查询入口。所有路径只读已发布 snapshot/catalog，输入按 symbol/date/frequency/limit 限界，不进入选股、回测或监控输入。
- 独立 snapshot root：`fstore-extended`、`tdx_moneyflow_minute`、`tdx_callauction` 分别由 `FQUANT_SNAPSHOT_ROOT_FSTORE_EXTENDED`、`FQUANT_SNAPSHOT_ROOT_ENGINE_A_MONEYFLOW_MINUTE`、`FQUANT_SNAPSHOT_ROOT_ENGINE_A_CALLAUCTION` 配置；筹码与日级资金流跟随只读 `engine-a` generation。
- 港股事实边界已显式化：日 K/minutes/trans 可用；本地发布快照中没有港股公司行动/复权事件，也没有港股财务报表。`hk_adjustment` / `hk_financial` 状态明确为 unavailable，provider 对港股复权、公司行动和财务查询 fail-closed 返回空，不借用同码 A 股数据。
- 2026-08-11 真盘验证：筹码/个股日资金流/个股分钟资金流/板块日资金流/集合竞价/A 股逐笔分别返回 `1/1/254/3/6/6` 行；status 覆盖筹码 `2,501,804` 行、日级个股资金流 `5,629,184` 行、分钟个股资金流 `193,424,721` 行、集合竞价 `14,844,313` 行。
- 自由 Agent 的 Pi Agent Harness sidecar 仅替换 `/api/agent/*` 的可选 LLM 会话循环；13 个工具仍由 Python 进程执行并继续只读现有 repository/provider 公开接口。试点未新增行情源、provider capability、DuckDB 写入或 canonical/enriched 消费路径，默认 Python runtime 不变。

### 0.1 2026-09-03 dataquery v2 cutover（Issue #56）

- **范围**：A 股 stock 的点查/窄区间行情读切换到 engine dataquery v2 HTTP（`backend/app/data_providers/fquant/dataquery_client.py`）：series `day|wide|minutes|trans|xdxr`（cache_id 规则 `^(sh|sz|bj)\d{6}$`，6/5→sh、4/8/9→bj、其余→sz，见 `symbols.symbol_to_cache_id`）+ moneyflow daily/minute 点查（批量 ≤16 标的，`MAX_POINT_SYMBOLS`）。
- **契约**：7 码 typed error 信封（`invalid_query`/`not_found`/`schema_mismatch`/`version_pinned_unavailable`/`stale`/`incomplete`/`unavailable`），`DataVersion` fail-closed 解析（schema_version 必须匹配 dataset：series=`legacy_csv/v1`、moneyflow=`tdx_moneyflow[_minute]/v1`）；`main.py` 全局 handler 把 `DataQueryError` 转 `{code,dataset,detail,retryable}` + `Retry-After`；`/api/kline/minute` 与 `/api/market-data/status`（`dataquery_versions`）透传版本元数据。
- **语义对齐**：v2 series rows 是紧凑 `YYYYMMDD` 日期 + 升序返回，`_query_series` 出口统一 `_v2_date_to_iso` 归一为 ISO，legacy DuckDB 链路输出形状不变；wide 路径保留 `_get_raw_oracle_rows` + `reconstruct_raw_rows` 前复权 raw 重建；`get_daily_freshness` 改读 v2 status 的 `tdx_day/a` coverage。
- **诚实映射**：v2 moneyflow daily 只有 total 四字段——`total_net/total_inflow/total_outflow` 如实映射，`main_*` 显式 None，不用 total 冒充 main；minute moneyflow 走专用 snake_case mapper `moneyflow_minute_v2_to_df`，`main_traditional_net/main_broad_net/neutral_amount` 无 v2 来源置 None。
- **显式 blocked（等 engine #9/#11 pinned Parquet bundle）**：全市场/批量扫描一律 `DataQueryBlockedError`（`version_pinned_unavailable` 语义，fail-loud 不静默回退）：`get_daily`/`get_minute` A 股 symbols>16、单标的区间>2500 行（wide/trans）、minutes 跨>31 日、moneyflow 任意 range/多日查询；v2 series 返回 `truncated=true` 时客户端直接抛 typed `incomplete`（503, retryable），绝不把截断序列当完整结果。bulk 的正式归宿是 pinned bundle（Issue #56 验收「生产路径不再打开 engine DuckDB」以 engine #11 交付为前提），期间夜管道/canonical history 需 `FQUANT_DATAQUERY_ENABLED=0` 运行或接受显式 blocked。
- **不迁移（本轮显式留在本地链）**：chips（v2 无路由）、HK/ETF/index daily 与 minutes、call auction、板块/截面 moneyflow 快照（`get_moneyflow_daily_snapshot` 等 engine-owned bulk 读）、fstore 域（instruments/realtime/financial/LHB/margin/universes）维持只读本地 DuckDB——它们迁往 pinned Parquet bundle 的批次等 engine #9/#11。
- **上线前置**：(1) engine 必须先在 `:8099` 部署 v2 再启用本 cutover，否则点查路径全部 `unavailable`（错误信息带 `FQUANT_DATAQUERY_ENABLED=0` 回退指引）；(2) 启用前抽标的做一次 v2 legacy-CSV 缓存 vs 本地 `tdx.duckdb` 的内容 parity 核验（点查/窄区间覆盖面），结论记录进本文件。
- **回退**：`FQUANT_DATAQUERY_ENABLED=0` 恢复整条 legacy DuckDB 链（wide/xdxr/minutes/trans/moneyflow/freshness 全部回退），生产 ：8099 未部署 v2 前可用此开关。
- **验证**：`tests/data_providers` + `tests/api/test_market_data.py` + `tests/api/test_kline_minute_source.py` 共 346 passed（新增 `test_dataquery_client.py` 52 项契约测试 + `test_provider_dataquery_v2.py` 27 项 wiring/blocked/语义测试，含 httpx.MockTransport 线格式端到端 smoke：成功 + 409 + 404 + version 透传）；零网络、零生产依赖。

下文第 1～7 节记录 2026-07-02 前后的迁移过程。涉及 PG、HTTP、未提交状态或旧单文件 minutes/trans 的描述，以本节和仓库当前代码为准。

---

## 1. 任务全景（一图看完）

| 阶段 | 范围 | 状态 | 验证手段 |
|------|------|------|---------|
| 阶段 1 | **FQuantProvider v2 架构**（直连 fstore / engine-data / moneyflow / 可选 tdx-api，8 子模块，8 capability） | ✅ 完成 | `test_fquant_provider.py` 无失败；真实源不可达项单列 skip |
| 阶段 2 | **Service 层解耦**（7 个 service 文件按统一模式替换 SDK→provider） | ✅ 完成（7/7） | provider 切 `fquant` 端到端跑通 + tickflow 回归无变化 |
| 阶段 3 | **补 FQuantProvider 缺口**（realtime / universes / depth） | ✅ realtime/universes 已实现；depth 标注当前缺口 | `test_fquant_provider.py` + live fstore 验证 |
| 阶段 4 | **commit + 沉淀**（沉淀文档 / 配 env / 删 PoC） | ⏳ 未开始 | — |
| 阶段 5 | **完全去掉 TickFlow SDK 依赖**（可选远期） | ⏳ 非当前目标；需先决定 depth 官方源保留策略 | — |
| 阶段 6 | **`fquant_local` 本地磁盘数据源模式**（TDX disk daily + raw 重建 + stock raw mirror 禁写 + realtime fallback） | ✅ 工作区完成，未提交 | `pytest tests -q` 71 passed（含 raw_reconstruct volume/amount 合并 + 逆运算缩放 2 项新增用例）；coverage/raw/provider 真盘 smoke ✅ |

整体结论：阶段 1 + 阶段 2 已**实测验证通过**，service 层确实可以脱离 TickFlow SDK 工作；阶段 3 是把"还能用 TickFlow 补的洞"也填上，让 v2 provider 能独立支撑全部数据面。

---

## 2. 阶段 1：FQuantProvider v2 架构（✅）

### 2.1 目标

把旧 PoC `FQuantProvider`（只透传 fquant 自身 HTTP，仅 `instruments` / `daily` 两个 capability）升级到**直连底层本地源**，覆盖 `MarketDataProvider` 协议的 8 个 capability 字段（`instruments` / `daily` / `adj_factor` / `minute` / `realtime` / `financial` / `depth` / `universes`）。

### 2.2 实际产出

#### 设计稿
- `backend/docs/FQUANT_PROVIDER_DESIGN.md`（846 行）—— 包含三个上游源的能力清单（每张表/接口都**实测**）、模块设计、字段映射、配置项、降级矩阵、测试方案。

#### 代码（未跟踪新增）
```
backend/app/data_providers/fquant/             ← 8 文件子模块
├── __init__.py           35  行   符号归一重导出
├── symbols.py           148  行   split_symbol / code_and_market_to_symbol 等
├── fstore_client.py     183  行   psycopg v3 PG 客户端（fallback psycopg2）
├── engine_data_client.py 120 行   engine-data HTTP 客户端
├── engine_data_disk.py  140+ 行   TDX 磁盘 CSV 客户端（fquant_local）
├── moneyflow_client.py  135  行   moneyflow HTTP 客户端
├── sina_tencent_client.py      受控 external fallback 的源客户端（由 service 适配层按 opt-in 调用，FQuantProvider 不实例化）
├── raw_reconstruct.py          TDX 前复权 raw 修复
├── mapping.py           385  行   上游字段 → 内部 schema
├── adj_factor.py        123  行   xdxr 事件 → 累积 ex_factor
└── fallback.py           57  行   本地源降级策略表

backend/app/data_providers/fquant_provider.py  593 行   聚合 Provider（fstore/engine-data/moneyflow/tdx-api）
backend/app/data_providers/registry.py          +2 行   注册 fquant
backend/scripts/test_fquant_provider.py        420+ 行  16 项端到端测试（`DATA_PROVIDER=fquant|fquant_local`）
```

#### 能力声明（`fquant_provider.py:98`）
```python
capabilities = ProviderCapabilities(
    instruments=True, daily=True, adj_factor=True,
    minute=True, realtime=True, financial=True,
    depth=False, universes=True,
)
```

### 2.3 关键设计决策

1. **两阶段工作流**：Claude 出设计稿（846 行设计文档） → Codex 照设计执行（实现 + 测试）。设计稿里所有字段、URL、schema 都经过实测，避免空想。
2. **FQuantProvider 直连底层本地源**，不走 fquant HTTP API 中转。理由：fquant 自身是聚合层，再叠一层会损失可控性；各源独立故障可降级。
3. **daily 主源选 engine-data `wide`**：实测 fstore `day_klines` 600519 最后数据是 2025-10-31，`daily_markets` 返回 0 行；engine-data `wide` 数据最全（含内盘外盘 / 开盘收盘量 / 上笔收盘）。
4. **adj_factor 主源选 engine-data `xdxr`**：fstore `chuquan_chuxi` 作为 fallback，`xdxr` 字段语义更直接（fenhong/fenshu 直接换算成 ex_factor）。
5. **`chips` 端点不接入**：实测 8s 内未返回，引擎在 NAS 慢。本期不接。
6. **财务报表不再缺口**：fstore 有完整 `financial_report_income_statement` / `balance_sheet` / `cash_flow` / `annual` / `quick` / `forecast` 六张表，**`get_financial` capability 升为 ✅**。
7. **realtime 只读本地 DuckDB**：`FQuantProvider` 读取 `fstore-markets.duckdb.daily_markets` generation 快照；先探测全局 `MAX(trade_date)`，再按该交易日和 `asset_type` 点查。外部公共源仅允许由默认关闭的 service 侧受控 fallback 补真缺口。

### 2.4 验证结果（`scripts/test_fquant_provider.py`）

| # | 用例 | 期望 | 实际 |
|---|------|------|------|
| 1 | capabilities 字段 | depth=False, 其余=True | ✅ |
| 2 | get_instruments('stock') 全量 | > 5000 条 | 5857 条 ✅ |
| 3 | get_daily(['600519.SH']) | 250 行左右 | 250 行 ✅ |
| 4 | get_adj_factors(['600519.SH']) | 非空 | 45 行 ✅ |
| 5 | get_financial('600519.SH', 'income') | 4 行 | 4 行 27 列 ✅ |
| 6 | get_realtime(['600519.SH']) | fstore `daily_markets` 最新 generation 快照 | 1 行 ✅ |
| 7 | get_minute | 0 行（上游暂时不可达） | 0 行 ✅ |
| 8 | 符号归一（`split_symbol` / `code_and_market_to_symbol`） | 6 类全过 | ✅ |
| 9 | 字段映射（`base_infos_rows_to_instruments`） | 必填列齐 | ✅ |
| 10 | xdxr → ex_factor 反推 | 单调累积 | ✅ |
| 11 | fstore 连接断开 → warning 不抛异常 | 优雅 | ✅ |
| 12 | engine-data 502 → 切 day_klines | 自动降级 | ✅ |
| 13 | moneyflow 502 → 0 行不阻断 | 自动降级 | ✅ |
| 14 | instruments 24h 缓存 | 二次调用走缓存 | ✅ |
| 15 | `__init__` 不会因 fstore 不可用而失败 | 懒加载 | ✅ |

---

## 3. 阶段 2：Service 层解耦（✅ 7/7）

### 3.1 目标

把 service 层对 TickFlow SDK 的直接调用替换为 `data_providers` 抽象层调用，通过 `DATA_PROVIDER` 环境变量或 settings 偏好（`tickflow` / `fquant`）切换后端，**默认保持 tickflow 不破坏现有行为**。环境变量优先级最高。

### 3.2 解耦模式（每个 service 统一遵循）

```python
# 1. 工厂：读 registry 有效 provider，单例缓存
def _get_data_provider():
    global _provider_instance
    if _provider_instance is None:
        from app.data_providers.registry import get_active_provider_name, get_provider
        provider_name = get_active_provider_name()
        _provider_instance = get_provider(provider_name)
    return _provider_instance

# 2. 业务函数：把之前的 TickFlowClient().xxx() 换成 provider.xxx()
def sync_daily(...):
    provider = _get_data_provider()
    df = provider.get_daily(symbols, start, end, asset_type)
    ...
```

注册中心 `registry.py` 新增 `"fquant": FQuantProvider` 一行（+2 行），业务调用方完全不知道底层是哪个 provider。

### 3.3 各文件改动一览

| 文件 | 改动量 | SDK→provider 处数 | 策略 | 真实数据验证 |
|------|--------|-------------------|------|-------------|
| `kline_sync.py` | +105 / -92 | 6 处 | 试点文件 | 250 行日K ✅ |
| `instrument_sync.py` | +35 / -40 | 4 处 | 标准解耦 | 5857 条标的 ✅ |
| `quote_service.py` | +46 / -17 | 3 处 | tickflow 回归 + fquant 降级 | ✅ |
| `financial_sync.py` | +87 / -34 | 6 处 | 财务报表走 fstore | 22101 行利润表 ✅ |
| `index_sync.py` | +28 / -31 | 5 处 | universes 走 provider `get_by_universes()` | CN_Index/ETF/Sector live 验证 ✅ |
| `watchlist.py` | +20 / -5 | 3 处 | realtime 走 provider；本地快照缺失或过期时仅可由受控 external fallback 补展示数据 | 本地 fstore 快照 ✅ |
| `depth_service.py` | +20 / -0 | 0 处 | 能力检查模式：fquant 直接降级返回空，tickflow 保留 SDK | 降级逻辑验证 ✅ |
| **合计** | **+341 / -219** | **24 处** | — | — |

加 `registry.py` 改 +2 / -0，加 `fquant_provider.py` 和 `fquant/` 子模块为 untracked 文件。

### 3.4 端到端验证（DATA_PROVIDER=fquant）

| 调用 | 结果 | 耗时 |
|------|------|------|
| `get_instruments('stock')` | 5857 条 | 0.3s |
| `get_daily(['600519.SH'])` | 250 行 | 0.2s |
| `get_adj_factors` | 45 行 | 0.2s |
| `get_financial('600519.SH', 'income')` | 4 行 27 列 | 0.0s |
| `get_realtime` | 1 行（fstore `daily_markets` 最新 generation 快照） | — |
| `get_minute` | 0 行（上游暂时不可达） | — |

### 3.5 TickFlow 回归（DATA_PROVIDER=tickflow）

`get_daily(000001.SZ)` → 250 行，耗时 3.1s，行为与改动前一致 ✅。默认 `DATA_PROVIDER=tickflow` 对线上完全透明。

### 3.6 已知保留点

- `depth_service.py` 不解耦 5 档盘口：当前 FQuantProvider 未暴露 depth capability，保留 TickFlow。
- `realtime` 已接本地源：禁止 `../fquant` / `tdx-api` / sina / tencent 进入 provider；`FQuantProvider` 只读 fstore `daily_markets`，外部公共源只能由 service 侧受控 fallback 在用户 opt-in 后补展示缺口。

---

## 4. 阶段 3：补 FQuantProvider 缺口（✅ 已实现并验证）

把以下三项填上，FQuantProvider 才能完全独立支撑 service 层（阶段 5 摘除 TickFlow 的前提）。

| # | 缺口 | 方案 | 状态 |
|---|------|------|------|
| 3.1 | `get_realtime()` | 只读 fstore `daily_markets` generation 快照；全局最新交易日 + `asset_type` 点查；外部源不得进入 provider | ✅ 已实现，live 验证通过 |
| 3.2 | `get_by_universes()`（指数/ETF/板块标的） | 接 fstore `chengfen_gu` + `base_infos`，TickFlowProvider 保留 SDK 兼容实现 | ✅ 已实现，live 验证：CN_Index=2256 / CN_ETF=1930 / CN_Sector=1021 |
| 3.3 | `get_depth()` 5 档盘口 ❌ | 当前 FQuantProvider 未提供 depth capability，已在 `depth_service.py` 能力门控降级（阶段 2 已完成） | ✅ 标注完成 |

3.1 / 3.2 已接入 provider 路径并验证，3.3 当前按 capability 降级。

---

## 5. 阶段 4：commit + 沉淀（⏳ 未开始）

- [ ] commit 所有成果（用户自行 review 后 `git add` + `git commit`）
- [ ] 配置 `FSTORE_DATABASE_PASSWORD` 到 tickflow backend `.env`（避免依赖 fquant 的 `.env`）
- [ ] 删除 PoC 版 `fquant_provider.py` —— 实际是**保留**的，因为 `fquant_provider.py` 现在已经是 v2 实现；旧 PoC 代码在 v2 重写时被覆盖。需要确认 `git log -p` 中没有遗留旧 `FQuantProvider` 类。
- [ ] 更新 `FQUANT_PROVIDER_DESIGN.md` 补实测结果：把"伪代码/接口骨架"标注成"已实现"，并补上阶段 2 端到端验证的实测数字。

---

## 6. 阶段 5：完全去掉 TickFlow SDK 依赖（可选远期）

**当前结论**：本项目仍需要支持切换 TickFlow 官方数据源，因此不应在本阶段移除 TickFlowProvider。只有在产品决定放弃官方源，或为 depth 找到本地替代源后，才进入本阶段。

**前置条件**：只剩 depth 若未来找到本地盘口源；否则保留 TickFlowProvider 作为官方盘口源。
**预期动作**：
1. 从 `requirements.txt` / `pyproject.toml` 摘除 tickflow-sdk
2. `registry.py` 移除 `"tickflow": TickFlowProvider` 注册（保留类文件以备回滚）
3. `_get_data_provider()` 默认值改为 `fquant`
4. service 层继续清理 TickFlow 术语与远期兼容分支

---

## 6B. 阶段 6：`fquant_local` 本地磁盘模式（✅ 工作区完成，未提交）

### 6B.1 范围

- 新增 `fquant_local` provider：`FQuantProvider(engine_mode="disk")`，registry/preferences/settings/frontend 均可切换。
- 日 K 主链：`TDX_DATA_DIR/wide` 优先，缺文件降级 `day`；`xdxr` 事件用于前复权逆运算。
- 分钟/逐笔：`TDX_DATA_DIR/minutes/YYYY/YYYYMMDD/*.csv` 和 `trans/YYYY/YYYYMMDD/*.csv` 已接入 `EngineDataDiskClient`，复用现有 minute/trans mapping；5m/15m/30m/60m 等由 1m 本地聚合。
- 日级资金流：`TDX_DATA_DIR/fund` 已接入 `get_moneyflow_daily()` 的本地优先路径，提供 Main/SuperLarge/Large/Medium/Small 净额和比例；单批次内缺日期/缺文件的 symbol 会继续走 moneyflow HTTP fallback。
- raw 污染修复：`raw_reconstruct.py` 在 mapping 前还原 `open/high/low/close/last_close`；fstore `t_1_day_klines` 作为历史 raw oracle，2025-11 后缺口用 xdxr 逆运算补。
- raw oracle 仅用于 stock：index/ETF 直接使用磁盘行情，避免 `000001.SH` 等指数被同 code 股票 oracle 污染。
- 本地模式禁写 stock raw mirror：repository 层门控收口在 7 个写方法（`append_daily` / `append_index_daily` / `append_etf_daily` / `append_daily_asset` / `merge_live_daily_asset` / `flush_live_daily` / `flush_live_daily_asset`）统一调用 `_skip_raw_daily_write()`，实际仅拦截其中 stock 范围的写（`append_daily`、`append_daily_asset("stock")`、`merge_live_daily_asset("stock")`、`flush_live_daily`、`flush_live_daily_asset("stock")`）；`kline_daily_enriched` 仍作为计算缓存保留。index/ETF raw 暂留给现有页面、统计和 fallback 路径。
- pipeline 新入口：`run_pipeline_local(provider, ...)` 直接 provider→enriched，不依赖 `data/kline_daily`；增量起点使用 `kline_daily_enriched` 最新分区，干净环境不会每天回退一年重算。
- 单股 K fallback：本地模式缓存空时 provider 直读并计算返回，不落 raw。
- realtime：fstore `daily_markets` 最新 generation 快照；所有输出统一 `normalize_realtime()`。受控外部 fallback 独立位于 service 层，默认关闭且仅供展示。
- 数据状态：本地模式 stock raw mirror 缺失时，`/api/data/status` 的 daily 口径用 enriched 分区日期并标记 `raw_mirror_disabled=true`。

### 6B.2 验证证据（2026-07-02）

| 验证 | 结果 |
|------|------|
| 覆盖闸门 `spike_disk_day_coverage.py --limit 2` | instruments=5534；`tdx_day_exists=5315`；`missing_has_fstore_after_2025_11=219`；`true_gap_active_after_2025_11=0` |
| raw 重建 spike | 600519 / 300059 / 600186 全 PASS；oracle=`t_1_day_klines`；纯逆运算对 close/high 有分级误差，混合 oracle 策略为准 |
| provider 真盘 smoke | `fquant_local EngineDataDiskClient freshness=2026-07-02`；600519 2012-10-26 raw close=241.0；`DATA_PROVIDER=fquant_local ... scripts/test_fquant_provider.py` → 全部通过 0 skip |
| minute/trans 真盘 smoke | `TDX_DATA_DIR=/Volumes/vol3/tdx` 下 600519 2026-07-01：1m=240 行，5m=48 行，trans=4552 行 |
| fund 真盘 smoke | `TDX_DATA_DIR=/Volumes/vol3/tdx` 下 600519/300059 2026-07-01：`get_moneyflow_daily()` 返回 2 行，source=`fquant_local:moneyflow:daily` |
| API 真盘 smoke | `DATA_PROVIDER=fquant_local` 下 `/health` mode=`fquant_local`；`/api/kline/daily-batch` 600519 最新 close=1168.63；`/api/kline/daily?symbol=600519.SH&start_date=2026-06-25` source=`local_disk`、close=1212.1，无 `.075769` 尾巴 |
| 指数真盘 smoke | `get_daily(['000001.SH'], 2026-07-01..2026-07-02, asset_type='index')` → close=4112.45 / 4028.904，未被 `000001.SZ` 股票 oracle 覆盖 |
| index_sync 真盘 smoke | 临时目录下 `sync_and_persist_index_daily(... symbols_override=['000001.SH'])` → 写入 `kline_index_daily` close=4112.45；指数详情 daily/minute fallback 显式 `asset_type=index` |
| 后端测试 | `cd backend && uv run --with pytest pytest tests -q` → 71 passed（含新增 raw_reconstruct oracle volume/amount 合并 + 送转缩放 2 项用例），1 个 pytest 配置 warning；新增 minute/trans/freq/fund/fallback/realtime/源标记 针对性测试通过 |
| 后端编译 | `uv run python -m py_compile ...` → 通过 |
| 前端类型 | `cd frontend && pnpm tsc --noEmit` → 通过 |
| 前端构建 | `cd frontend && pnpm build` → 通过；仅保留动态/静态重复 import 与 chunk size warning |

### 6B.3 残留与边界

- `fquant_local` 不提供 depth；盘口/封单仍按 capability 降级，且**无历史 depth 数据源**可回补（TDX 磁盘、fstore 均未见 5 档盘口历史表），非近期上线可修的缺口。
- 任务 0 覆盖闸门实测缺口 219 只（`instruments=5534` − `tdx_day_exists=5315`），均落入 `missing_has_fstore_after_2025_11`、`true_gap_active_after_2025_11=0`；这 219 只只按 fstore tail 边界（2025-10-31 / 2025-11-01）粗分类为"可 fallback"，未逐只核实缺失原因（退市 / 新股未同步 / 曾用代码变更等），构成明细待人工抽查复核（原计划预估口径为 868，与全量分类实测 219 不一致，以本次实测为准）。
- `minutes/` CSV 字段格式仅对 600519 2026-07-01 单日单标的做过真盘 smoke（1m=240 行、5m=48 行聚合一致）；历史久远日期、停牌日、半日交易等边界格式未做进一步 spike，`get_minutes()` 目前假设行序即连续分钟序列（无独立时间戳分组），跨边界场景行为未知。
- `5min/` 等物理聚合分钟目录未接；当前通过 `minutes/` 的 1m 路径聚合生成。
- `fund/` 只覆盖日级净额分类；完整 minute moneyflow（inflow/outflow、有效/无效笔数等）仍走 moneyflow HTTP 或降级空。
- `holding` / `fhold` 是个人持仓数据源，不属于公共行情 provider 契约；如接入应走独立用户数据入口。
- `refresh_polluted_daily.py` 是一次性迁移脚本，只用于旧 `fquant` HTTP 模式污染分区重刷；`fquant_local` 日常路径不写 raw。
- sina/tencent 客户端只供 service 侧受控 external fallback 使用，不是 `fquant_local` provider 来源；必须由用户显式开启对应 scope，且返回带 provenance/degraded 标记。
- 当前变更仍在工作区，未 commit；提交前需用户 review。

### 6B.4 ordered-trans 研究 generation（2026-08-27）

- 新增 `ordered_trans_research` capability 与 request-owned `open_ordered_trans_reader()`；runtime FQuantProvider 只读 `/Volumes/WD1/duckdb/snapshots/engine-a-ordered-trans/<generation>/` 的 hash-pinned Parquet，不扫描 raw CSV。
- 离线 publisher 对 raw trans 使用单 FD 完成 fstat/hash/header/parse；同 raw minute 保留物理 source sequence。正常收盘集合竞价的 `volume=0` 指示价不冒充成交，artifact 保存 sparse true-trade 1m；消费端按 timestamp bucket 强制 48×5m/16×15m anchors。
- 首个 bounded generation `20260827T134357Z-f751ea5b08e3b4da` 覆盖 `600519.SH/000001.SZ/300750.SZ`、30 个完整交易日；manifest SHA-256 `cf5e2dc98fae3bd249f4a1c402b09ce6102cd2fe64a7ab490d03fbcc424ab475`。
- `oos_start=2026-08-04` 在运行结果前冻结；真实 service/API smoke 均 `status=ok`，horizon 1 common OOS=88，最终 `rejected`（post-cost 非正、Wilson 下界未超过基线），不进入短线池/Agent/默认策略。
- publisher/reader/provider/service/API 聚焦测试 25 passed；后端全量 `3428 passed, 3 skipped, 8 warnings`；独立 coding review 修复后复审无 blocker/major。完整命令、hash probe 与 purge 计数见 `../../docs/ISSUE-10/verification.md`。

---

## 7. 技术架构简述

### 7.1 数据依赖与可选受控外部源

| 上游 | 协议 | 用途 | 配置文件 |
|------|------|------|---------|
| **fstore PostgreSQL** | psycopg v3 | 标的列表 / 财务报表 / 复权事件 / 分钟级备份 | `FSTORE_DATABASE_HOST/PORT/USER/PASSWORD/NAME`（默认 `pve.wf:5432/fstore`） |
| **engine-data** | HTTP GET | 日 K 主源（wide）/ 分钟 / xdxr / trans | `http://192.168.5.99:8099` |
| **moneyflow** | HTTP GET | 资金流日 / 资金流分钟 | `http://pve.wf:8090`（上次测试 502，已自动降级） |
| **Tencent 公共行情（可选）** | HTTPS GET | 仅补 realtime/depth 的真实本地缺口，以及 `chart_live` 当前交易日单标的日 K/分时临时展示 | 设置中的 `external_fallback_enabled` + 独立 scope，默认关闭 |

### 7.2 调用链

```
service 层（kline_sync / quote_service / ...）
    ↓ _get_data_provider()
registry.get_provider("fquant"|"tickflow")
    ↓
FQuantProvider（v2）                  TickFlowProvider（v1）
    ↓                                    ↓
fstore_client / engine_data_client      TickFlow SDK
       / moneyflow_client / tdx-api
    ↓
PG / HTTP
```

### 7.3 关键约束

- **密码从环境变量读**，不硬编码：`FSTORE_DATABASE_PASSWORD` / `FQUANT_DB_PASSWORD` 任一即可。
- **fstore 连接懒加载**：provider 初始化不会因 fstore 不可用而失败；首次查询时建立。
- **单源故障不阻断**：各本地源独立工作，fstore 挂了只影响 `get_instruments` / `get_financial` / `get_adj_factors`（部分），其余走 engine-data / moneyflow / tdx-api。
- **Provider 契约集中修改**：`base.py` 只新增 `depth` / `universes` 字段和 `get_by_universes()`，service 层无感切换。

---

## 8. 关键决策（团队对齐用）

| # | 决策 | 取舍 |
|---|------|------|
| D1 | 直连上游源，不走 fquant HTTP 中转 | +可控性 / -复杂度 |
| D2 | daily 主源选 engine-data `wide` 而非 fstore `day_klines` | +数据全 / -多一跳 HTTP |
| D3 | `realtime` 不接 fquant/tdx-api/公网代理，provider 只读 fstore `daily_markets`；外部公共源走独立受控 fallback | +主链路可审计 / -本地快照发布前会明确 stale |
| D4 | `financial` capability 升级 ✅（fstore 报表表完整） | 原 PoC 是 ❌，现在打通 |
| D5 | `chips` 端点不接入（8s 超时） | 阶段 3 路线 3 再议 |
| D6 | service 层默认 `tickflow`，`DATA_PROVIDER` 可覆盖 settings 偏好 | +安全 / -运行时切换需刷新 provider 单例与能力缓存 |
| D7 | service 层完全不改公开 API | service 公开签名零修改，仅内部取数路径切换 |
| D8 | `index_sync.py` 的 universes 走 provider `get_by_universes()` | fquant 直连 fstore `chengfen_gu`，tickflow 保留官方 provider 内部实现 |

---

## 9. 风险与注意事项

### 9.1 已识别风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| fstore `chuquan_chuxi` 增量 vs engine-data `xdxr` 全量历史 | adj_factor 行数差异 | 阶段 1 已在 `adj_factor.py` 实现"xdxr 主源 + chuquan fallback"；测试通过 |
| moneyflow 502（pve.wf:8090 不可达） | 资金流接口 0 行 | 自动降级 + warning，**不阻断** 其它接口 |
| `fstore.day_klines` 缺 7 月数据（600519 最后 2025-10-31） | fstore daily 备份源也 0 行 | daily 主源走 engine-data 不受影响；fstore 仅做 fallback |
| engine-data 当前网络不可达 | daily / minute / xdxr live 测试走 skip/降级 | 当前验证环境报 `[Errno 65] No route to host`；fstore/moneyflow/realtime fallback 已验证 |
| engine-data `chips` 8s 超时 | realtime 衍生指标缺失 | 本期不接；如确需筹码/逐笔衍生再单独评估 |
| `FQuantProvider` 旧 PoC 代码未删 | 旧 `__init__` 与新 v2 行为差异 | v2 直接覆盖原文件；建议阶段 4 跑 `git log -p` 确认无残留 |

### 9.2 注意事项

1. **`FSTORE_DATABASE_PASSWORD` 必须配**：fstore 端所有能力（instruments / financial / adj_factor 部分）都依赖它。未配置时这些方法返回空 df + warning，不抛异常。
2. **fquant 的 `.env` 与 tickflow backend 的 `.env` 是两个文件**：当前测试走前者；阶段 4 会迁移到 tickflow backend 的 `.env`。
3. **默认仍不切到 fquant**：保留默认 `tickflow` 避免线上环境未配 fstore/tdx 时误判；`DATA_PROVIDER` 环境变量可作为最高优先级覆盖。
4. **capabilities 检查必须在业务入口**：fquant 的 depth 是空降级，realtime 虽有 fallback 但本地源不可用时仍可能返回空。
5. **不要直接接 `../fquant` HTTP API**：fquant provider 只能直连底层本地源（fstore / engine-data / moneyflow / 后续 tdx-api 等）或已有 provider 抽象。
6. **PoC `FQuantProvider` 行为变化**：旧 PoC 的 `__init__` 接受 `base_url` 参数；v2 改为环境变量。如果有外部脚本 import 旧签名，会 break。

---

## 10. 相关文件索引

| 类别 | 路径 | 说明 |
|------|------|------|
| **进度文档** | `backend/docs/FQUANT_INTEGRATION_PROGRESS.md` | **本文件** |
| 设计稿 | `backend/docs/FQUANT_PROVIDER_DESIGN.md` | 846 行，三源实测 + 架构设计；部分内容已被 realtime/universes 实现更新 |
| 旧 PoC 说明 | `backend/docs/FQUANT_PROVIDER.md` | 旧版 FQuantProvider（fquant HTTP 透传版） |
| 测试脚本 | `backend/scripts/test_fquant_provider.py` | 16 项端到端测试，可用 `DATA_PROVIDER=fquant_local` 验证本地磁盘 provider |
| 聚合 Provider | `backend/app/data_providers/fquant_provider.py` | v2 实现 |
| 本地源子模块 | `backend/app/data_providers/fquant/{symbols,fstore_client,engine_data_client,engine_data_disk,moneyflow_client,sina_tencent_client,mapping,adj_factor,raw_reconstruct,fallback}.py` | 10+ 个文件 |
| Provider 注册 | `backend/app/data_providers/registry.py` | 注册 fquant + 统一 active provider 解析 |
| Service 改动 | `backend/app/services/{kline_sync,instrument_sync,quote_service,financial_sync,index_sync,watchlist,depth_service}.py` | 7 个文件按统一模式解耦 |
| Provider 契约 | `backend/app/data_providers/base.py` | 新增 depth/universes capability 和 universes 方法 |
| 数据规范化 | `backend/app/data_providers/normalizer.py` | 新增显式 realtime 契约 `normalize_realtime()` |

---

## 11. 变更记录

| 日期 | 阶段 | 变更 | 验证 |
|------|------|------|------|
| 2026-07-02 | 1 | 完成 FQuantProvider v2 架构（设计稿 + 9 文件 + 测试） | 冒烟无失败；本机 2 skip |
| 2026-07-02 | 2 | 完成 service 层 7/7 解耦 | 端到端 + tickflow 回归 ✅ |
| 2026-07-02 | 6 | 完成 `fquant_local` 本地磁盘模式工作区实现 | 71 tests + 真盘 smoke ✅ |
| 2026-07-02 | — | 撰写本进度文档 | — |
| 2026-08-06 | 跨域校对 | PA_Agent P4 结构化计划检查与 P5 PushPlus 完成；计划检查的行情输入只读既有 `data_providers`/canonical enriched 路径，PushPlus 仅分发用户已配置的监控告警/复盘报告，均未新增或绕过数据源 | 终审修复后后端全量 1075 tests + `import app.main` + 前端 build + 开发服务/UI smoke ✅ |
| 2026-08-10 | 受控缺口与研究入口校对 | `FQuantProvider.depth` 仍为 false；数据页新增默认关闭的 `realtime`/`depth` 独立 fallback scope。外部 depth 已与 authoritative sealed cache 隔离，仅在连板当前展示响应中携带 `sealed_degraded`/`sealed_source`，不修正 counts/状态、不进入总览/研究/选股/回测/监控。研究中心、横截面、信号记分卡、组合策略、参数网格和 M25 连续性 UI 完成接线，均保持本地 DuckDB/append-only/provenance 与无自动执行边界 | 最终集成定向回归 `303 passed`；`import app.main`、前端 TypeScript/build、真实服务 `/health` 与浏览器多页面诊断通过 |
| 2026-08-10 | 数值与风险边界校对 | provider mapping 将 `NaN/±Inf` 统一视为缺失，实时/分钟展示和 AI K 线上下文不得输出非标准 JSON 数值；交易组合风险只读既有 canonical 日 K，不新增 provider capability、外部源或写入路径 | 映射/行情/分析/交易定向回归通过；真实服务 `/health` 与 `/api/trading/portfolio/risk` smoke 通过 |
| 2026-08-10 | realtime 可靠性 | `daily_markets` 改为全局最新交易日 + `asset_type`/code 点查，并使用独立 DuckDB 客户端/锁；engine compat 缺状态时只缺兼容指标、不删除基础 enriched 行；QuoteService 防重入且按源日期 fail-closed；自选页合并只读 snapshot，明确区分本地过期与外部降级 | 后端全量 `1475 passed`；前端 `tsc -b --force`；真盘 provider `5892` 行/`3.022s`/源日期 `2026-08-10`，snapshot API 两标的 `0.74s`；浏览器验证空 enriched、外部降级和本地过期三种展示 |
| 2026-08-11 | 全链路数据审计 | 盘后 `run_now` 在 freshness/分区覆盖率验证后立即发布并刷新 enriched canonical 水位；7 个看板/回测/复盘关键指数缺早期 canonical 数据时按需补齐；无参 realtime snapshot 恢复默认指数快照兼容行为；数据页改用分区元数据展示大表覆盖，避免全表行数扫描阻塞 | 后端全量 `1578 passed`；关键指数真实回填 `17183` 行，`000300.INDEX` API 返回 2015-01-05～2026-08-10 共 `2819` 行；`/health`、`/api/data/status`、`/api/overview/market`、无参 snapshot 与浏览器数据页 smoke 通过，页面无错误 |
| 2026-08-11 | 分钟 K 数据源校对 | 数据页在本地 `kline_minute` 缓存为空时改为展示 active provider 的 catalog 发布水位，不再把“未缓存”误报成“DuckDB 无数据”；移除已退役 `fstore-minutes.duckdb` 的客户端 ATTACH/兼容 view 与配置文档；修复指数分钟/逐笔查询未把 `asset_type=index` 传入 TDX 前缀映射而误读同代码深市股票的问题 | 后端全量 `1582 passed`；真实 catalog 返回 preliminary `2026-08-11`，`600519.SH` 与 `000001.INDEX` 当日分钟 API 均返回 240 行，指数首价 `3951.59`；前端 build 与数据页浏览器 smoke 通过 |
| 2026-08-18 | 跨域校对 | 自由 Agent 新增默认关闭的 Pi Agent Harness source/dev-only sidecar；Node 仅负责 LLM loop，工具和业务状态仍归 Python，不新增或绕过数据源，不进入选股/回测/监控/交易 AI 主链 | Agent 测试族 `100 passed`；后端全量 `2482 passed, 3 skipped`；真实 Pi SDK 本地假 provider 工具往返、attempt 落盘与子进程取消回收通过 |
| 2026-08-18 | 日 K 水位与日期一致性 | canonical 启动水位改为取 `get_daily` 完整本地链的最新日期，不再只看滞后的 TDX wide；有区间的日 K 按日期合并 engine 与 fstore，指数再用 `daily_markets(asset_type=10)` 补齐缺失交易日；大盘总览在本地指数 parquet 滞后时经 provider 补同一 `as_of`，避免把前一交易日指数拼到当前股票广度；复盘/看板日期 UI 隔离跨日 store 与分表水位 | 定向后端 `92 passed`；前端 TypeScript + Vite build；真实接口 canonical/overview/review 各 Tab 均为 `2026-08-17`，四个核心指数均带同日 `date`，浏览器验证复盘标题、历史报告、看板 DatePicker 与指数页 |
| 2026-08-21 | 筛选模块 S1-S4（见 `SCREENER_PRODUCT_REVIEW_2026-08-20.md`） | S1 正确性：删 `PRESET_STRATEGIES`/`run_preset` 双执行路径（统一 StrategyEngine，缺失 503），断板反包改 filter_history 真名实，缓存同日合并；S2 工作台：方案持久化 `user_data/screener_screens.json` + CRUD API、批量自选/CSV/行详情、日期与 latest_only 徽标、裸 SQL `/run` 收口 410；S3 方案→生产：新 `strategy/screen_bridge.py` 把保存方案注册为 `screen:<hex>` 策略（监控 type=strategy 与回测同源），回测面板缺外部 join 字段时 fail-closed 显式拒绝；S4 选股语言：条件分组（组内 AND/组间 OR）、9 个多日序列字段（历史窗口独立求值路径）、行业分布 facet（PIT，limit 前全量聚合）、EPS 改标准 TTM 累计口径 | 后端全量 `3054 passed, 3 skipped`；前端 `tsc -b`+build+16 测试脚本全过；真机浏览器 E2E：方案保存→回测此方案（真实回测 45.9s 10 笔交易）→监控规则创建→清理；live API OR 分组+序列+facet 返回 1146 命中 |
| 2026-08-21 | AI 模块评审与 S1/S2 局部落地（见 `AI_PRODUCT_REVIEW_2026-08-21.md`） | Report 三入口 + Agent 系统提示去指令化（禁词测试锁死）；`POST /api/agent/chat` 410；策略生成 Step1 仅预览、确认后落盘；财务/复盘/Agent/策略生成登记 `ai_budgets`；Agent 归研究域；个股分析前端接 attempt 取消（气泡/弹窗 ×）。财务/复盘取消、报告带走、runtime 角标仍待 S2/S3 | 定向 `test_ai_report_prompts`/`test_ai_budgets`/`test_agent` 等 99 passed；前端 `tsc -b` |
| 2026-08-21 | AI 模块 F1–F17 全量落地 + 双 reviewer 独立复审 | F7 取消对称（财务/复盘注册 attempt + `X-AI-Attempt-ID`，取消不落盘）；F8 报告带走（个股/财务加自选/送回测、Agent pool 卡片）；F9 流连接态 connecting/open/closed 三入口；F12 策略保存后回测此策略；F13 程序化 `StockReportSummary`（extra=forbid，无二次 LLM）；F14 as_of/source/adjustment 上屏；F15 同标的报告两栏 diff；F16 进程内 Agent 并发上限 2（python/pi 共槽、非阻塞、取消 `aclosing` 归还）；F17 前端 bun 测试。复审修复：reviewStore catch/finally 加旧流所有权守卫（取消后立即重启不被旧流 abort 改写 cancelled，`reviewStoreOwnership.test.ts` 变异验证）；agent_loop 工具轮与终流补传 `budget.timeout`（90s，不再走 provider 默认 180s） | 后端 agent/loop/runtime/concurrency/budgets/cancel `25+42 passed`；前端 `tsc -b` + 守卫测试全绿 |
| 2026-08-24 | 当前日个股图表兜底 | 新增默认关闭的 `chart_live` scope：provider 成功返回本地目标日空行时，单 A 股日 K/分钟 K 才可从腾讯当日分时生成响应内 `provisional` 数据；保留行级 `source=tencent_chart` 和响应级 `degraded/sources`，catalog 503 与历史 minutes/trans 均维持 fail-closed，绝不写入本地/选股/监控/回测 | 后端 chart_live/API 定向 `47 passed`；前端 TypeScript + Vite build 见本次验证 |
| 2026-08-26 | AI 短线池（见 `AI_PRODUCT_REVIEW_2026-08-21.md` §9） | 扩展既有 `screen_stock_pool` 的固定 `short_momentum_quality_v1` 分支，仍保持 13 个只读 Agent 工具；候选只经 canonical `QueryService` 生成，结构化结果卡展示逐股证据，最终自然语言使用服务端确定性摘要且不枚举候选；结果为 request-local、内容寻址 `pool_id`，不写 user_data artifact，确认时服务端重算；不进入外部 fallback、监控或自动交易链 | 后端 Agent/短线池定向 `114 passed`；前端短线池与既有 Agent/handoff Bun 契约测试及 `pnpm build` 通过；真实 canonical 水位 `2026-08-25` 命中 38、输出 5 只且每只 12 条证据；真实后端封套通过前端 parser，浏览器验证结果卡常显与个股详情可打开 |
| 2026-08-27 | canonical schema v2 与研究生产化 | canonical 全历史新增复权前原生 `raw_open`，全量构建固定 `tdx/fstore/markets/klines/extended` 具体 generation，并支持 1–8 个独立只读 worker；主线程单写 staging DuckDB，完整后 COPY 为 Parquet 并原子发布。盘后管道新增 immutable generation 增量发布：克隆父代旧分区、复制已验证新分区、仅 pin `tdx/markets` 日历源检查连续性、继承父代数据血统、记录分区哈希并做 coverage/父代 CAS；六个研究因子补齐状态机、OOS/成本或精确数据缺口 | schema v2 真盘 `17,220,261` 行/`5,679` 标的/`8,766` 日；真实 MACD `243 IS + 149 OOS`，单阳 `12` 事件/0 删失；后端累计 `351 passed, 7 warnings`，改动文件 ruff 硬错误检查与前端 TypeScript 通过 |
| 2026-08-27 | N 字因子双 generation 生产数据链（Issue #8） | 新增 request-scoped composite sealed reader：canonical generation 提供 raw OHLCV/交易日历，fstore markets generation 提供历史 universe、同日 `name`、日期有效 ST/板块制度与 source exact `ztj`；两份 manifest 身份独立记录，请求结束关闭 facts connection，任一来源或日期事实缺失即 fail-closed/删失。一字首板按 `raw_open == raw_high` 形态排除；不进入短线池、Agent 或交易链 | 研究域回归 `155 passed, 1 warning`，改动 Python `ruff --select F,E9` 通过，二次独立 review 无 blocker/major；真实只读 smoke 固定 canonical `20260827T054651-63f500a4` + markets `20260827T102014`，`600519.SH` 2022 年样本返回 201 个完整制度事实且研究路径 `status=ok` |


---

**维护说明**：本文件与代码同源（每次 commit 前校对"已完成"和"待完成"两节）。阶段 4 完成后在变更记录加一行。
