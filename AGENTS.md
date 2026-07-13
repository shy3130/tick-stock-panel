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
| `fquant_local` | 本地 DuckDB web 快照：`fstore-web.duckdb` + `fstore-markets-web.duckdb` / `fstore-klines-web.duckdb` / `fstore-minutes-web.duckdb` + `tdx*.duckdb` / `tdx-hk*-web.duckdb` | 日 K / 分钟 / 复权 / 财务 / realtime 快照 / universes；扩展逐笔/日级资金流；港股 K/minutes/trans；**stock raw mirror 禁写**；**depth 缺口** | ✅ 默认 | `DATA_PROVIDER=fquant_local` 或 settings API |
| `fquant` | 同一 DuckDB 实现，保留 provider 名称兼容 | 同上；**depth 缺口** | ❌ | `DATA_PROVIDER=fquant` 或 settings API |

**fquant 本地源**：

| 上游 | 协议 | 用途 | 默认地址 |
|------|------|------|---------|
| fstore DuckDB | DuckDB read-only | 标的列表 / 财务报表 / 复权事件 / universes / 小表 | `FQUANT_FSTORE_DUCKDB_PATH`（默认 `/Volumes/WD1/fstore-web.duckdb`） |
| fstore markets DuckDB | DuckDB read-only | realtime 快照 / 每日行情 | `FQUANT_FSTORE_MARKETS_DUCKDB_PATH`（默认 `/Volumes/WD1/fstore-markets-web.duckdb`） |
| fstore klines DuckDB | DuckDB read-only | fstore K 线兼容表 | `FQUANT_FSTORE_KLINES_DUCKDB_PATH`（默认 `/Volumes/WD1/fstore-klines-web.duckdb`） |
| fstore minutes DuckDB | DuckDB read-only | fstore 分钟 K 线 | `FQUANT_FSTORE_MINUTES_DUCKDB_PATH`（默认 `/Volumes/WD1/fstore-minutes-web.duckdb`） |
| TDX DuckDB | DuckDB read-only | 日 K wide/day / xdxr / 日级资金流 | `FQUANT_TDX_DUCKDB_PATH`（默认 `/Volumes/WD1/tdx.duckdb`） |
| TDX A 股 minutes 路由 | 发布 catalog + DuckDB read-only | 按交易日定位 2023 年前归档或当前 minutes 快照 | `FQUANT_SNAPSHOT_ROOT_CATALOG` + `FQUANT_SNAPSHOT_ROOT_ENGINE_A{,_MINUTES_ARCHIVE}` |
| TDX A 股 trans 路由 | 发布 catalog + DuckDB read-only | 按交易日定位逐年 trans 快照 | `FQUANT_SNAPSHOT_ROOT_CATALOG` + `FQUANT_SNAPSHOT_ROOT_ENGINE_A{,_TRANS_ARCHIVE}` |
| TDX HK DuckDB | DuckDB read-only | 港股日 K / 多周期 K | `FQUANT_TDX_HK_DUCKDB_PATH`（默认 `/Volumes/WD1/tdx-hk-web.duckdb`） |
| TDX HK minutes DuckDB | DuckDB read-only | 港股分钟 K | `FQUANT_TDX_HK_MINUTES_DUCKDB_PATH`（默认 `/Volumes/WD1/tdx-hkminutes-web.duckdb`） |
| TDX HK trans DuckDB | DuckDB read-only | 港股逐笔成交 | `FQUANT_TDX_HK_TRANS_DUCKDB_PATH`（默认 `/Volumes/WD1/tdx-hktrans-web.duckdb`） |

**已知缺口**：

- **depth（5 档盘口）当前缺口**：FQuantProvider 目前不暴露 depth capability，`depth_service.py` 已做能力门控降级
- **realtime 已接入**：只读本地 `fstore-markets-web.duckdb.daily_markets` 最新快照；不再调用 `tdx-api` / sina / tencent / `../fquant` HTTP
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
| `services/quote_service.py` | +46 / -17 | realtime 走 provider；fquant 走 `fstore-markets-web.duckdb.daily_markets` 快照 |
| `services/financial_sync.py` | +87 / -34 | 财务报表走 fstore |
| `services/index_sync.py` | +28 / -31 | universes 走 provider，FQuant 走 fstore |
| `services/watchlist.py` | +20 / -5 | realtime 走 provider；fquant 走本地源 fallback |
| `services/depth_service.py` | +20 / -0 | 能力检查模式：fquant 直接降级返回空 |

### 文档（团队权威）

| 文件 | 作用 |
|------|------|
| **`backend/docs/FQUANT_INTEGRATION_PROGRESS.md`** | **进度文档（权威）**——阶段划分、决策记录、风险、变更日志，每次 commit 前校对 |
| `backend/docs/FQUANT_PROVIDER_DESIGN.md` | 846 行设计稿（三源实测 + 架构） |
| `backend/docs/FQUANT_PROVIDER.md` | 旧 PoC 说明（已被 v2 覆盖，仅供回溯） |
| `README.md` | 用户向快速开始；末尾有"本地开发与数据源"开发者附录 |

### 测试

| 文件 | 作用 |
|------|------|
| `backend/scripts/test_fquant_provider.py` | 16 项端到端冒烟，真实源不可达项单独列 skip |

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

**绝对不能**直接连接：

- 外部 Tencent / 新浪 / 第三方行情接口（当前 FQuantProvider 只读本地 DuckDB，禁止业务层或 provider 绕过 DuckDB 直连外部行情）
- 任何绕过 `data_providers` 抽象层的 HTTP / DB 直连

---

## 5. 本地开发流程

### 环境变量

```bash
# 必填：provider 切换
export DATA_PROVIDER=fquant_local   # 或 fquant

# 可选：DuckDB 路径，不填则使用 /Volumes/WD1 默认挂载
export FQUANT_FSTORE_DUCKDB_PATH=/Volumes/WD1/fstore-web.duckdb
export FQUANT_FSTORE_MARKETS_DUCKDB_PATH=/Volumes/WD1/fstore-markets-web.duckdb
export FQUANT_FSTORE_KLINES_DUCKDB_PATH=/Volumes/WD1/fstore-klines-web.duckdb
export FQUANT_FSTORE_MINUTES_DUCKDB_PATH=/Volumes/WD1/fstore-minutes-web.duckdb
export FQUANT_TDX_DUCKDB_PATH=/Volumes/WD1/tdx.duckdb
export FQUANT_TDX_HK_DUCKDB_PATH=/Volumes/WD1/tdx-hk-web.duckdb
export FQUANT_TDX_HK_MINUTES_DUCKDB_PATH=/Volumes/WD1/tdx-hkminutes-web.duckdb
export FQUANT_TDX_HK_TRANS_DUCKDB_PATH=/Volumes/WD1/tdx-hktrans-web.duckdb

# A 股 minutes/trans 按日期从 engine 发布的 catalog 解析；测试和 staging 可重定向各 root
export FQUANT_SNAPSHOT_ROOT_CATALOG=/Volumes/WD1/snapshots/catalog
export FQUANT_SNAPSHOT_ROOT_ENGINE_A=/Volumes/WD1/snapshots/engine-a
export FQUANT_SNAPSHOT_ROOT_ENGINE_A_MINUTES_ARCHIVE=/Volumes/WD1/snapshots/engine-a-minutes-archive
export FQUANT_SNAPSHOT_ROOT_ENGINE_A_TRANS_ARCHIVE=/Volumes/WD1/snapshots/engine-a-trans-archive

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
| fquant/fquant_local 模式下 realtime 接口返回空 | 检查 `fstore-markets-web.duckdb` 的 `daily_markets` 覆盖 |
| fquant 模式下 depth 接口返回空 | 正常降级（当前 provider 不暴露 depth capability） |
| fquant_local 盘后管道不生成 `kline_daily` | 正常：stock raw mirror 被 repository 层禁写；只生成/更新 `kline_daily_enriched` |
| A 股 minutes/trans 返回空并出现 catalog warning | 检查 catalog 的 `current.json`、目标 root 的 generation，以及 `require_current` 路由是否与目标 root 当前 generation 一致；该路径刻意不降级 raw |
| fquant_local 其它数据 freshness 落后 | 检查 `/Volumes/WD1/fstore*.duckdb`、`/Volumes/WD1/tdx*.duckdb` 是否更新 |

---

## 6. 不要做的事（红线汇总）

1. **❌ 不要重新引入 TickFlow SDK 或 `app.tickflow.*` 兼容层**
2. **❌ 不要在业务层或 FQuantProvider 内直接连接外部行情接口**（Tencent / 新浪 / 第三方）——当前 FQuantProvider 只读本地 DuckDB
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

**最后更新**：2026-07-13（补充 engine 发布 route catalog 的 A 股 minutes/trans 读路径）
**维护者**：tickflow-stock-panel contributors
**风格参考**：Hermes `~/.hermes/profiles/oc-hq/SOUL.md`（项目身份卡范式）
