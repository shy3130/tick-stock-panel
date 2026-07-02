# FQuant 数据源接入进度

> 主线任务：**把 tickflow-stock-panel service 层从 TickFlow SDK 解耦，改走 `data_providers` 抽象层直连 fquant 同款底层本地源（fstore PG / engine-data:8099 / moneyflow:8090 / 可选 tdx-api），逐步摆脱 TickFlow 付费依赖。**
>
> 最后更新：2026-07-02
> 状态：阶段 1（Provider 架构）、阶段 2（service 层解耦）、阶段 3.1（realtime 本地源 fallback）、阶段 3.2（universes）、阶段 3.3（depth 当前缺口标注）已在工作区实现并完成针对性验证；阶段 4（提交/沉淀）未做 ⏳。
> 范围：本文是**给团队看的项目状态文档**，不是技术设计文档。设计稿见 [`FQUANT_PROVIDER_DESIGN.md`](./FQUANT_PROVIDER_DESIGN.md)（846 行，全实测字段），旧 PoC 现状见 [`FQUANT_PROVIDER.md`](./FQUANT_PROVIDER.md)。

---

## 1. 任务全景（一图看完）

| 阶段 | 范围 | 状态 | 验证手段 |
|------|------|------|---------|
| 阶段 1 | **FQuantProvider v2 架构**（直连 fstore / engine-data / moneyflow / 可选 tdx-api，8 子模块，8 capability） | ✅ 完成 | `test_fquant_provider.py` 16 项全过 |
| 阶段 2 | **Service 层解耦**（7 个 service 文件按统一模式替换 SDK→provider） | ✅ 完成（7/7） | provider 切 `fquant` 端到端跑通 + tickflow 回归无变化 |
| 阶段 3 | **补 FQuantProvider 缺口**（realtime / universes / depth） | ✅ realtime/universes 已实现；depth 标注当前缺口 | `test_fquant_provider.py` + live fstore 验证 |
| 阶段 4 | **commit + 沉淀**（沉淀文档 / 配 env / 删 PoC） | ⏳ 未开始 | — |
| 阶段 5 | **完全去掉 TickFlow SDK 依赖**（可选远期） | ⏳ 非当前目标；需先决定 depth 官方源保留策略 | — |

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
├── moneyflow_client.py  135  行   moneyflow HTTP 客户端
├── mapping.py           385  行   上游字段 → 内部 schema
├── adj_factor.py        123  行   xdxr 事件 → 累积 ex_factor
└── fallback.py           57  行   本地源降级策略表

backend/app/data_providers/fquant_provider.py  593 行   聚合 Provider（fstore/engine-data/moneyflow/tdx-api）
backend/app/data_providers/registry.py          +2 行   注册 fquant
backend/scripts/test_fquant_provider.py        382 行   16 项端到端测试
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
7. **realtime 不走 fquant API**：优先可选相邻 `tdx-api` `/api/quote`，否则回退 fstore `daily_markets` 最新快照。

### 2.4 验证结果（`scripts/test_fquant_provider.py`）

| # | 用例 | 期望 | 实际 |
|---|------|------|------|
| 1 | capabilities 字段 | depth=False, 其余=True | ✅ |
| 2 | get_instruments('stock') 全量 | > 5000 条 | 5857 条 ✅ |
| 3 | get_daily(['600519.SH']) | 250 行左右 | 250 行 ✅ |
| 4 | get_adj_factors(['600519.SH']) | 非空 | 45 行 ✅ |
| 5 | get_financial('600519.SH', 'income') | 4 行 | 4 行 27 列 ✅ |
| 6 | get_realtime(['600519.SH']) | tdx-api 或 fstore 快照 | 1 行 ✅ |
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
| `watchlist.py` | +20 / -5 | 3 处 | realtime 走 provider | fstore 快照 fallback ✅ |
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
| `get_realtime` | 1 行（fstore daily_markets fallback） | — |
| `get_minute` | 0 行（上游暂时不可达） | — |

### 3.5 TickFlow 回归（DATA_PROVIDER=tickflow）

`get_daily(000001.SZ)` → 250 行，耗时 3.1s，行为与改动前一致 ✅。默认 `DATA_PROVIDER=tickflow` 对线上完全透明。

### 3.6 已知保留点

- `depth_service.py` 不解耦 5 档盘口：当前 FQuantProvider 未暴露 depth capability，保留 TickFlow。
- `realtime` 已接本地源：禁止 `../fquant` HTTP API，优先 tdx-api，回退 fstore `daily_markets` 最新快照。

---

## 4. 阶段 3：补 FQuantProvider 缺口（✅ 已实现并验证）

把以下三项填上，FQuantProvider 才能完全独立支撑 service 层（阶段 5 摘除 TickFlow 的前提）。

| # | 缺口 | 方案 | 状态 |
|---|------|------|------|
| 3.1 | `get_realtime()` | 不允许调用 `../fquant` HTTP API；优先相邻 `tdx-api`，回退 fstore `daily_markets` 最新快照 | ✅ 已实现，live fstore 验证 1 行 |
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

## 7. 技术架构简述

### 7.1 本地源

| 上游 | 协议 | 用途 | 配置文件 |
|------|------|------|---------|
| **fstore PostgreSQL** | psycopg v3 | 标的列表 / 财务报表 / 复权事件 / 分钟级备份 | `FSTORE_DATABASE_HOST/PORT/USER/PASSWORD/NAME`（默认 `pve.wf:5432/fstore`） |
| **engine-data** | HTTP GET | 日 K 主源（wide）/ 分钟 / xdxr / trans | `http://192.168.5.99:8099` |
| **moneyflow** | HTTP GET | 资金流日 / 资金流分钟 | `http://pve.wf:8090`（上次测试 502，已自动降级） |
| **tdx-api（可选）** | HTTP GET | realtime quote 主源；未配置时回退 fstore 快照 | `FQUANT_TDX_API_BASE` / `DSA_TDX_API_BASE_URL` / `TDX_API_BASE_URL` |

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
| D3 | `realtime` 不接 fquant HTTP 代理，直连 tdx-api / fstore fallback | +不绕聚合层 / -tdx-api 需单独启动 |
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
| 测试脚本 | `backend/scripts/test_fquant_provider.py` | 16 项端到端测试 |
| 聚合 Provider | `backend/app/data_providers/fquant_provider.py` | v2 实现 |
| 本地源子模块 | `backend/app/data_providers/fquant/{symbols,fstore_client,engine_data_client,moneyflow_client,mapping,adj_factor,fallback}.py` | 8 个文件 |
| Provider 注册 | `backend/app/data_providers/registry.py` | 注册 fquant + 统一 active provider 解析 |
| Service 改动 | `backend/app/services/{kline_sync,instrument_sync,quote_service,financial_sync,index_sync,watchlist,depth_service}.py` | 7 个文件按统一模式解耦 |
| Provider 契约 | `backend/app/data_providers/base.py` | 新增 depth/universes capability 和 universes 方法 |
| 数据规范化 | `backend/app/data_providers/normalizer.py` | **未修改** |

---

## 11. 变更记录

| 日期 | 阶段 | 变更 | 验证 |
|------|------|------|------|
| 2026-07-02 | 1 | 完成 FQuantProvider v2 架构（设计稿 + 9 文件 + 测试） | 15/15 ✅ |
| 2026-07-02 | 2 | 完成 service 层 7/7 解耦 | 端到端 + tickflow 回归 ✅ |
| 2026-07-02 | — | 撰写本进度文档 | — |

---

**维护说明**：本文件与代码同源（每次 commit 前校对"已完成"和"待完成"两节）。阶段 4 完成后在变更记录加一行。
