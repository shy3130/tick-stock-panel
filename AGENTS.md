# AGENTS.md — tickflow-stock-panel 项目身份卡

> **给接手这个项目的 AI Agent / 开发者看的"我是谁、我在干什么、怎么动我"速查卡。**
> 这不是设计文档，也不是入门教程——这是**写在仓库根的长期上下文**，让任何后续 Agent 在 5 分钟内搞清项目骨架与红线。
> 权威进度见 `backend/docs/FQUANT_INTEGRATION_PROGRESS.md`。

---

## 1. 项目定位

**tickflow-stock-panel** 是一个 A 股**选股 + 监控 + 回测**一体的工作台（前后端同仓），通过 **`data_providers` 抽象层**对接数据源。

- **前端**：React 18 + Vite + TypeScript + Tailwind + Tanstack Query + Lightweight Charts + ECharts（`frontend/`）
- **后端**：FastAPI + Pydantic v2 + APScheduler + Polars（计算） + DuckDB（查询） + Parquet（存储）（`backend/`）
- **回测**：vectorbt（项目内唯一的 pandas 边界）
- **AI**：可选 OpenAI 兼容接口（DeepSeek / 通义 / Ollama 等），用于生成策略与个股四维分析

**核心架构演进**：原本只接 TickFlow SDK（付费）；从 2026-07-08 起，`FQuantProvider v2` 已收敛为只读本地 DuckDB（`fstore*.duckdb` + `tdx*.duckdb`，含港股拆分库），业务层仍通过 `data_providers` 抽象层切换 provider 名称。

**不是**对标同花顺 / 通达信的全功能客户端，**不**内置 AI 荐股 / 涨停预测。

---

## 2. 数据源矩阵

通过 `DATA_PROVIDER` 环境变量或 `/api/settings/preferences/data-provider` 在两个 provider 之间切换；环境变量优先级最高。

| Provider | 数据来源 | capabilities | 默认 | 切换方式 |
|----------|---------|--------------|------|----------|
| `fquant_local` | 本地 DuckDB（默认 raw 路径，经 `snapshot_or_raw` 解析为 `snapshots/<root>/<gen>/` 只读 generation 快照；快照未发布时回退 raw 只读）：`fstore.duckdb` / `fstore-markets.duckdb` / `fstore-klines.duckdb` / `fstore-minutes.duckdb` + `tdx.duckdb` / `tdx-hk.duckdb` / `tdx-hkminutes.duckdb` / `tdx-hktrans.duckdb` | 日 K / 分钟 / 复权 / 财务 / realtime 快照 / universes；扩展逐笔/日级资金流；港股 K/minutes/trans；**stock raw mirror 禁写**；**depth 缺口** | ✅ 默认 | `DATA_PROVIDER=fquant_local` 或 settings API |
| `fquant` | 同一 DuckDB 实现，保留 provider 名称兼容 | 同上；**depth 缺口** | ❌ | `DATA_PROVIDER=fquant` 或 settings API |

**fquant 本地源**：

| 上游 | 协议 | 用途 | 默认地址 |
|------|------|------|---------|
| fstore DuckDB | DuckDB read-only | 标的列表 / 财务报表 / 复权事件 / universes / 小表 | `FQUANT_FSTORE_DUCKDB_PATH`（默认 `/Volumes/WD1/duckdb/fstore.duckdb`，解析为 `snapshots/fstore/<gen>/` 快照） |
| fstore markets DuckDB | DuckDB read-only | realtime 快照 / 每日行情 | `FQUANT_FSTORE_MARKETS_DUCKDB_PATH`（默认 `/Volumes/WD1/duckdb/fstore-markets.duckdb`，解析为 generation 快照） |
| fstore klines DuckDB | DuckDB read-only | fstore K 线兼容表 | `FQUANT_FSTORE_KLINES_DUCKDB_PATH`（默认 `/Volumes/WD1/duckdb/fstore-klines.duckdb`，解析为 generation 快照） |
| fstore minutes DuckDB | DuckDB read-only | fstore 分钟 K 线 | `FQUANT_FSTORE_MINUTES_DUCKDB_PATH`（默认 `/Volumes/WD1/duckdb/fstore-minutes.duckdb`；fstore generation 未发布 minutes 时回退 raw） |
| TDX DuckDB | DuckDB read-only | 日 K wide/day / xdxr / 日级资金流 | `FQUANT_TDX_DUCKDB_PATH`（默认 `/Volumes/WD1/duckdb/tdx.duckdb`） |
| TDX A 股 minutes 路由 | 发布 catalog + DuckDB read-only | 按交易日定位 2023 年前归档或当前 minutes 快照（staged，preliminary→final） | `FQUANT_SNAPSHOT_ROOT_CATALOG` + `FQUANT_SNAPSHOT_ROOT_ENGINE_A{,_PRELIMINARY,_MINUTES_ARCHIVE}` |
| TDX A 股 trans 路由 | 发布 catalog + DuckDB read-only | 按交易日定位历史归档年片或活跃年的月度 trans 快照（staged，preliminary→final） | `FQUANT_SNAPSHOT_ROOT_CATALOG` + `FQUANT_SNAPSHOT_ROOT_ENGINE_A{,_PRELIMINARY,_TRANS_ARCHIVE}` |
| TDX HK DuckDB | DuckDB read-only | 港股日 K / 多周期 K | `FQUANT_TDX_HK_DUCKDB_PATH`（默认 `/Volumes/WD1/duckdb/tdx-hk.duckdb`，解析为 engine-hk generation 快照） |
| TDX HK minutes DuckDB | DuckDB read-only | 港股分钟 K | `FQUANT_TDX_HK_MINUTES_DUCKDB_PATH`（默认 `/Volumes/WD1/duckdb/tdx-hkminutes.duckdb`，解析为 engine-hk generation 快照） |
| TDX HK trans DuckDB | DuckDB read-only | 港股逐笔成交 | `FQUANT_TDX_HK_TRANS_DUCKDB_PATH`（默认 `/Volumes/WD1/duckdb/tdx-hktrans.duckdb`，解析为 engine-hk generation 快照） |

**已知缺口**：

- **depth（5 档盘口）当前缺口**：FQuantProvider 目前不暴露 depth capability，`depth_service.py` 已做能力门控降级；可通过「受控外部 fallback」（默认关闭，见第 4 节契约）补公共免费源五档，未开启时维持降级返回空
- **realtime 已接入**：只读本地 `fstore-markets.duckdb.daily_markets` 的 generation 快照（最新）；不再调用 `tdx-api` / sina / tencent / `../fquant` HTTP
- **universes 已接入**：阶段 3.2 走 provider `get_by_universes()`；fquant 接 fstore `chengfen_gu` + `base_infos`

---

## 3. 关键文件索引（必读）

### 数据源层（`backend/app/data_providers/`）

| 文件 | 行数 | 作用 | 必读理由 |
|------|------|------|---------|
| `base.py` | 70+ | `MarketDataProvider` 协议 + `ProviderCapabilities` | **接口契约**，新增 capability 必须先改这里 |
| `fquant_provider.py` | 600+ | FQuantProvider（v2，本地 DuckDB 聚合） | 直连 fstore DuckDB / TDX DuckDB |
| `fquant/` | 10+ 文件 | fquant 子模块（symbols / fstore_duckdb_client / engine_data_duckdb_client / mapping / adj_factor / raw_reconstruct / fallback） | 改 fquant 行为时从这里入手 |
| `normalizer.py` | — | 字段规范化（Symbol / Instrument / KLine / Realtime 等） | 既有契约稳定；realtime 契约为追加 |
| `registry.py` | 20+ | provider 注册中心（`get_provider(name)`） | 新增 provider 只需在这里 +1 行 |
| `schemas.py` | — | Pydantic schema | **未修改** |

### Service 层（已解耦 7/7）

| 文件 | 改动量 | 角色 |
|------|--------|------|
| `services/kline_sync.py` | +105 / -92 | **解耦试点**，其他 service 照抄它的 `_get_data_provider()` 模式 |
| `services/instrument_sync.py` | +35 / -40 | 标准解耦 |
| `services/quote_service.py` | +46 / -17 | realtime 走 provider；fquant 走 `fstore-markets.duckdb.daily_markets` generation 快照 |
| `services/financial_sync.py` | +87 / -34 | 财务报表走 fstore |
| `services/index_sync.py` | +28 / -31 | universes 走 provider，FQuant 走 fstore |
| `services/watchlist.py` | +20 / -5 | realtime 走 provider；fquant 走本地源 fallback |
| `services/depth_service.py` | +20 / -0 | 能力检查模式：fquant 直接降级返回空 |

### Trading 纪律域（`backend/app/services/trading/`）

| 文件 | 作用 | 红线 |
|------|------|------|
| `lifecycle.py` / `store.py` | 单笔状态机 + `trade_events.jsonl` / `decision_audit.jsonl` append-only 事实流 | 历史事件和审计只能追加，禁止整份覆盖 |
| `accounts.py` / `portfolio.py` | 账户资金基数、NAV、敞口与健康度派生快照 | 行情估值仍必须走 `data_providers` |
| `fhold_client.py` | 只读调用 `fhold-cli --format json` 获取 `../fhold` 真实券商账户/持仓 | 仅持仓事实，不是行情 provider；禁止绕过 CLI 直读 `~/.fhold/fhold.db`；不可用时 fail-soft |
| `gates.py` / `plans.py` | 五条后端结构红线、用户清单、盘前计划与计划/实际偏差 | 结构红线不可由前端或用户配置关闭 |
| `red_flags.py` / `red_flag_webhook.py` | 放宽止损、亏损加仓、绕门、审计断链、期限超限、仓位超限、门禁膨胀（global 分组）检测；可选去重 Webhook | 红旗与盈亏无关；推送失败不得阻断事实落盘 |
| `review_job.py` | L0/L1/L2 状态驱动盘后归因（L0 零 AI 调用；L1 按事件数去重） | AI 未配置走 `blocked_by_dependency`，不得中断调度 |
| `proposals.py` / `autopsy.py` | AI 四分类归因（12 不一致模式 rubric）、带反证条件的策略变更提案与人工审批状态机；疑似亏损后放宽自动打 `relaxationAfterLoss` | 单笔结果不自动改策略；AI 不能替代人工批准 |
| `services/strategy_profile.py` / `strategy_validator.py` | 策略失效信号、风险/期限声明、family 坐标卡与 playbook、7 项机械体检 | 失效信号必须 `name/observable/action` 三要素齐全；family=mixed 必须声明裁判归属四要素 |

### 文档（团队权威）

| 文件 | 作用 |
|------|------|
| **`backend/docs/FQUANT_INTEGRATION_PROGRESS.md`** | **进度文档（权威）**——阶段划分、决策记录、风险、变更日志，每次 commit 前校对 |
| `backend/docs/FQUANT_PROVIDER_DESIGN.md` | 846 行设计稿（三源实测 + 架构） |
| `backend/docs/FQUANT_PROVIDER.md` | 旧 PoC 说明（已被 v2 覆盖，仅供回溯） |
| `backend/docs/YMOS_PORTING_PLAN.md` | YMOS 纪律层移植设计、契约与完成进度 |
| `README.md` | 用户向快速开始；末尾有"本地开发与数据源"开发者附录 |

### 测试

| 文件 | 作用 |
|------|------|
| `backend/scripts/test_fquant_provider.py` | 16 项端到端冒烟，真实源不可达项单独列 skip |
| `backend/scripts/test_trading_lifecycle.py` | Trading 全链路隔离数据 E2E 冒烟（不修改 `data/` 用户数据） |

---

## 4. 解耦约定（红线）

**所有 service 文件必须遵循**（参照 `kline_sync.py` 试点）：

```python
# ✅ 正确：用 _get_data_provider() 工厂
def _get_data_provider():
    global _provider_instance
    if _provider_instance is None:
        from app.data_providers.registry import get_active_provider_name, get_provider
        provider_name = get_active_provider_name()
        _provider_instance = get_provider(provider_name)
    return _provider_instance

def sync_daily(...):
    provider = _get_data_provider()
    df = provider.get_daily(symbols, start, end, asset_type)
    ...

# ❌ 错误：新增任何绕过 data_providers 的 SDK/HTTP/DB 直连
```

**进入业务入口前必须做 capabilities 检查**（fquant 的 depth 当前是缺口，realtime 依赖本地源可用性）：

```python
provider = _get_data_provider()
if not provider.capabilities.realtime:
    return []  # 或抛 FeatureUnavailable
result = provider.get_realtime(symbols)
```

**绝对不能**直接修改：

- `data_providers/base.py` 的接口契约（除非新增 capability 字段，并同步更新 `schemas.py` + 所有 provider）
- `data_providers/normalizer.py` 的字段语义
- `data_providers/registry.py` 已注册的 provider 名字（`fquant_local` / `fquant`）

**受控外部 fallback 契约**（2026-08-05 起生效；完整设计见 `backend/docs/CONTROLLED_EXTERNAL_FALLBACK_DESIGN.md`）：

外部公共免费行情不再一刀切禁止，但必须走**独立的 fallback 适配层**（`services/` 侧能力门控，不进 FQuantProvider），并同时满足：

1. **默认关闭**：`preferences.external_fallback_enabled` 默认 false，用户显式开启后对应能力才激活；
2. **仅补真缺口**：只覆盖本地源确实没有的能力（depth 五档、快照过期时的 realtime 快照级读取）；本地 DuckDB 有数据的路径一律不走外部；
3. **provenance 全程标记**：返回行带 `source` 字段，API/SSE 响应带 `degraded` 标志，UI 有角标；
4. **绝不污染主链路**：不写 stock raw mirror、不写 enriched 分区、不进入回测 / 选股 / 监控评估输入（它们只读 sealed 分区）；
5. **口径校准 pinning 测试**：每个源必须有锁死单位 / 复权 / 时区 / 符号映射的回归测试（照 `fquant/mapping.py` 校准注释先例）；
6. **限速 + 熔断 + 缓存**：复用 `eastmoney_client` 模式（Host 白名单 + 最小间隔 + `trust_env=False`），连续失败自动熔断并通知。

**永久豁免（不适用 fallback）**：catalog 路由的 A 股 minutes/trans（fail-closed 语义不变）；付费 / 需密钥源（TickFlow SaaS、Tushare Pro）；券商 SDK（Futu / Longbridge 等真实账户接口）。

**绝对不能**直接连接：

- 外部 Tencent / 新浪 / 第三方行情接口——除上述受控 fallback 适配层外，FQuantProvider 保持只读本地 DuckDB，业务层不得自行直连
- 任何绕过 `data_providers` 抽象层（及受控 fallback 适配层）的 HTTP / DB 直连

---

## 5. 本地开发流程

### 环境变量

```bash
# 必填：provider 切换
export DATA_PROVIDER=fquant_local   # 或 fquant

# 可选：DuckDB 路径，不填则使用 /Volumes/WD1 默认挂载
export FQUANT_FSTORE_DUCKDB_PATH=/Volumes/WD1/duckdb/fstore.duckdb
export FQUANT_FSTORE_MARKETS_DUCKDB_PATH=/Volumes/WD1/duckdb/fstore-markets.duckdb
export FQUANT_FSTORE_KLINES_DUCKDB_PATH=/Volumes/WD1/duckdb/fstore-klines.duckdb
export FQUANT_FSTORE_MINUTES_DUCKDB_PATH=/Volumes/WD1/duckdb/fstore-minutes.duckdb
export FQUANT_TDX_DUCKDB_PATH=/Volumes/WD1/duckdb/tdx.duckdb
export FQUANT_TDX_HK_DUCKDB_PATH=/Volumes/WD1/duckdb/tdx-hk.duckdb
export FQUANT_TDX_HK_MINUTES_DUCKDB_PATH=/Volumes/WD1/duckdb/tdx-hkminutes.duckdb
export FQUANT_TDX_HK_TRANS_DUCKDB_PATH=/Volumes/WD1/duckdb/tdx-hktrans.duckdb

# A 股 minutes/trans 按日期从 engine 发布的 catalog 解析；所有 root 共享
# /Volumes/WD1/duckdb 默认挂载根，测试和 staging 可重定向各 root。
# 发布顺序与回滚见下方「catalog/engine 发布顺序」。
export FQUANT_SNAPSHOT_ROOT_CATALOG=/Volumes/WD1/duckdb/snapshots/catalog
export FQUANT_SNAPSHOT_ROOT_ENGINE_A=/Volumes/WD1/duckdb/snapshots/engine-a
export FQUANT_SNAPSHOT_ROOT_ENGINE_A_PRELIMINARY=/Volumes/WD1/duckdb/snapshots/engine-a-preliminary
export FQUANT_SNAPSHOT_ROOT_ENGINE_A_MINUTES_ARCHIVE=/Volumes/WD1/duckdb/snapshots/engine-a-minutes-archive
export FQUANT_SNAPSHOT_ROOT_ENGINE_A_TRANS_ARCHIVE=/Volumes/WD1/duckdb/snapshots/engine-a-trans-archive

# 可选：AI
export AI_PROVIDER=openai_compat
export AI_BASE_URL=https://api.deepseek.com/v1
export AI_API_KEY=...
export AI_MODEL=deepseek-chat
```

### 启动命令

```bash
cd backend
uv sync                              # 安装依赖
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

或前端一起跑（项目根）：

```bash
./dev.sh                             # 后端 3018 + 前端 3011
```

### 验证步骤

1. **Provider capabilities 检查**：
   ```bash
   curl http://127.0.0.1:8000/api/capabilities | jq .
   ```
   应返回 `realtime: true/false` 等布尔字段。

2. **FQuant provider 端到端测试**（独立运行）：
   ```bash
   cd backend
   uv run python scripts/test_fquant_provider.py
   ```
   预期无失败；真实 DuckDB 源不可达或缺数据时脚本会单独列 skip。

3. **健康检查**：
   ```bash
   curl http://127.0.0.1:8000/health
   # {"status":"ok","version":"x.y.z","mode":"none|free|api_key"}
   ```

4. **数据流验证**（dev 模式下手动触发）：
   - 设置页 → 「立即跑盘后管道」拉日 K
   - 自选页加标的 → 选股页跑策略扫描
   - 监控中心 → 配规则 → 命中看持久化记录

### 常见排错

| 现象 | 排查 |
|------|------|
| fquant/fquant_local 模式下接口返回空 | 检查 DuckDB 文件是否挂载、路径 env 是否正确、对应表是否有数据；客户端 fail-soft 返回空 df + warning |
| fquant/fquant_local 模式下 realtime 接口返回空 | 检查 `fstore-markets.duckdb` generation 快照的 `daily_markets` 覆盖（确认 `snapshots/fstore/current.json` 已发布且非陈旧日期） |
| fquant 模式下 depth 接口返回空 | 正常降级（当前 provider 不暴露 depth capability） |
| fquant_local 盘后管道不生成 `kline_daily` | 正常：stock raw mirror 被 repository 层禁写；只生成/更新 `kline_daily_enriched` |
| A 股 minutes/trans 返回空并出现 catalog warning | **staged catalog 是前置条件**：`require_current` 路由必须 `stage=preliminary`/`final`，旧 `stage=NULL` 行会被 fail-closed 拒绝并带可行动迁移指引（不降级 raw）。排查：catalog `current.json`、目标 root generation、路由是否为 staged；详见下方「catalog/engine 发布顺序」 |
| fquant_local 其它数据 freshness 落后 | 检查 `/Volumes/WD1/duckdb/fstore*.duckdb`、`/Volumes/WD1/duckdb/tdx*.duckdb` 是否更新 |

### catalog/engine 发布顺序（staged 迁移运维）

A 股 minutes/trans 是**日期分片**数据，必须经 `catalog_resolver.resolve_route` 解析，**刻意不降级 raw**。staged catalog 是前置条件：只有 `stage=preliminary`/`final` 的 `require_current` 路由能证明一次实时读该 pin 哪个 generation；旧 `stage=NULL` 行 fail-closed 拒绝，错误信息带可行动迁移指引。

**默认挂载根**：`/Volumes/WD1/duckdb`，所有 snapshot root 均在其 `snapshots/` 子目录下：

| root | 默认路径 | env 覆盖 | 用途 |
|------|---------|---------|------|
| catalog | `/Volumes/WD1/duckdb/snapshots/catalog` | `FQUANT_SNAPSHOT_ROOT_CATALOG` | 路由表（最先发布） |
| engine-a | `/Volumes/WD1/duckdb/snapshots/engine-a` | `FQUANT_SNAPSHOT_ROOT_ENGINE_A` | final require_current 快照 |
| engine-a-preliminary | `/Volumes/WD1/duckdb/snapshots/engine-a-preliminary` | `FQUANT_SNAPSHOT_ROOT_ENGINE_A_PRELIMINARY` | preliminary 快照（早发布，质量未校验） |
| engine-a-minutes-archive | `/Volumes/WD1/duckdb/snapshots/engine-a-minutes-archive` | `FQUANT_SNAPSHOT_ROOT_ENGINE_A_MINUTES_ARCHIVE` | pinned_immutable 历史归档 |
| engine-a-trans-archive | `/Volumes/WD1/duckdb/snapshots/engine-a-trans-archive` | `FQUANT_SNAPSHOT_ROOT_ENGINE_A_TRANS_ARCHIVE` | pinned_immutable 历史归档 |

**无中断发布顺序**（先数据后路由，避免读到未发布的物理文件）：

1. 先发布物理 snapshot root（engine-a / engine-a-preliminary / 各 archive），确保 `current.json` 指向新 generation；
2. 再发布 catalog root，写入带 `stage` 的新路由行（preliminary → final）；
3. 校验：`require_current` 路由的 generation 必须与对应 root 的 `current.json` 完全一致。

**安全回滚条件**：catalog 回滚到最后一个 generation 与物理 root `current.json` 仍一致的版本即可；若物理 root 已推进到更新的 generation，则 catalog 必须同步回退到 pin 该 generation 的路由行，否则触发 `StaleCatalogError`（fail-closed，不降级 raw）。preliminary 行可随时撤回而不影响 final 读取。

---

## 6. 不要做的事（红线汇总）

1. **❌ 不要重新引入 TickFlow SDK 或 `app.tickflow.*` 兼容层**
2. **❌ 不要在业务层或 FQuantProvider 内直接连接外部行情接口**（Tencent / 新浪 / 第三方）——FQuantProvider 保持只读本地 DuckDB；外部公共免费行情只允许走第 4 节「受控外部 fallback 契约」的适配层（默认关闭、仅补真缺口、provenance 标记、不污染主链路）
3. **❌ 不要改 `base.py` 接口契约**——除非同步新增 capability 字段并更新所有 provider
4. **❌ 不要假设 `DATA_PROVIDER=fquant` 一定有 depth 数据**；realtime 也要能处理本地源暂时返回空
5. **❌ 不要重新引入 fstore PostgreSQL 密码/HTTP 源依赖**——当前源只允许 DuckDB 只读文件
6. **❌ 不要改 `data_providers/normalizer.py` 字段语义**——所有 provider 共用同一规范化器
7. **❌ 不要删除 `backend/docs/FQUANT_INTEGRATION_PROGRESS.md`**——它是团队权威进度源
8. **❌ 不要直接 `git commit`** 本仓库的任何改动（除非用户明确授权）——所有改动由用户自行 review
9. **❌ 不要跑 `git clean -fdx` / `git reset --hard`**——会删光 `data/` 下所有未跟踪数据
10. **❌ 不要修改 `data/` 目录下的用户数据文件**——行情 / 财务 / 自选 / 回测 / 监控记录都是运行时生成的

---

## 7. 维护说明

- **本文件**与 `FQUANT_INTEGRATION_PROGRESS.md` 同源，每次重大架构变更后两文件一并更新
- 改动 service 层时，**先看 `kline_sync.py`**（试点文件）；新增 service 时复制它的 `_get_data_provider()` 模式
- 改动 provider 时，**先看 `fquant/` 子模块**（8 文件，本地源分得很清楚）
- commit 前**重新校对**「阶段 2：Service 层解耦」「阶段 3：补 FQuantProvider 缺口」两节的"已完成/待完成"标记
- 用户面向说明改 `README.md`；开发者面向说明改 `AGENTS.md` + `backend/docs/`

---

**最后更新**：2026-08-05（红线 2 修订：外部行情从「一刀切禁止」改为「受控外部 fallback」政策——默认关闭、仅补真缺口、provenance 标记、不写 raw/enriched/回测选股输入，catalog minutes/trans 与付费源/券商 SDK 永久豁免；设计见 `backend/docs/CONTROLLED_EXTERNAL_FALLBACK_DESIGN.md`。上一变更：Trading 纪律域 P6 诊断框架）
**维护者**：tickflow-stock-panel contributors
**风格参考**：Hermes `~/.hermes/profiles/oc-hq/SOUL.md`（项目身份卡范式）
