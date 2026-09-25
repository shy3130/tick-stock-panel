# 核心能力开放与插件化改造设计方案

> 状态: V1/V2 已实现（0.3.2）—— Token + scope 网关 + 限流、五个 scope 全量 Tier A、`/api/openapi.json?tier=a` 契约视图、设置页 Token 管理（「设置 → 开放接口」）。V3+（钩子契约、SSE 票据、模拟盘拆分）仍为设计。使用说明见 [features.md → 开放接口](./features.md#-开放接口open-api--tier-a)。
> 遵循 [CONTRIBUTING.md](../CONTRIBUTING.md) 的数据口径、插件化与测试矩阵要求; 扩展机制现状见 [secondary-development.md](./secondary-development.md)。

## 1. 目标与非目标

**目标**

- 项目自身保留**核心域能力**（数据、策略/回测、监控推送、扩展数据存储）, 并以稳定 HTTP API 对外开放, 二次开发者不修改本项目源码即可在其上构建应用。
- 外围能力**扩展域化**: 通过既有插件机制（数据源 plugin.yaml / 前端插槽 / 后端 custom / 扩展数据表）接入, 而不是长在核心里。
- 开放有护栏: Token + scope 授权、按 Token 限流、CORS 白名单; 读开放、写受控。

**非目标**

- 不做多用户/多租户（数据层全局单用户是既定取舍; 以「单用户 + 多 Token + scope」满足外部调用方隔离需求）。
- 不做前端运行时插件（remote module/微前端）与第三方代码沙箱——等真实第三方插件作者出现再立项。
- 不做热更新; 桌面版更新走「下载安装包 + 静默覆盖安装 + 重启」路径。

## 2. 核心域 / 扩展域边界

**划线原则**: 按「数据与语义的归属 + 依赖箭头方向」划分, 不按页面外观划分。核心域 = 被多个模块消费其数据/语义、且自身不依赖外围的; 扩展域 = 只消费核心接口、可整体替换或移除而不伤核心的。

### 2.1 核心域（保留在主程序, 代码不拆出）

| 域 | 内容 | 现有承载 |
|---|---|---|
| **数据底座** | 日K/除权/分钟/实时/财务的同步管道、TickFlow 与数据源插件路由、enriched 存储、复权与单位口径 | `services/kline_sync` `services/financial_sync`、`plugins/*`、DuckDB/parquet |
| **订阅锚点** | 自选与分组（监控范围、持仓提醒、看板的数据订阅范围锚点） | `api/watchlist`、`data/watchlist` |
| **策略与回测引擎** | 策略注册/运行、回测矩阵引擎、优化器、挖掘运行时、**环境过滤语义**（regime_filter 的 T-1 掩码）、回测候选缓存 | `strategy/`、`backtest/*`、`api/strategies` `api/screener` `api/backtest` |
| **环境数据生产** | regime_builder 的市场状态计算与盘后增量（回测/挖掘/因子归因/AI 四处消费同一口径） | `services/regime_builder`、盘后管道 |
| **监控与推送管道** | 规则引擎、SSE/语音/系统通知/Webhook/留痕五通道 | `services/quote_service`、`api/monitor-rules` `api/alerts` |
| **扩展数据存储** | 扩展表 CRUD、时序分区/快照、市场级表(market_level)、rows/values 查询出口 | `services/ext_data` `ext_pull`、`api/ext-data` |
| **认证与配置** | 访问密码（本机/内网 UI）、**API Token 与 scope（本方案新增）**、secrets | `services/auth`、`api/settings` |

### 2.2 扩展域（通过既有机制接入, 核心只暴露契约）

| 能力 | 接入机制 | 状态 |
|---|---|---|
| 第三方行情数据源 | `backend/app/plugins/<name>/plugin.yaml` + Provider | ✅ 已有 |
| 无代码 HTTP 数据源 | `data/data_sources/*.yaml` | ✅ 已有 |
| 用户自注册数据表（含市场级时序） | 扩展数据配置 + 上传/拉取/回补 | ✅ 已有（本方案补齐市场级与查询出口） |
| 自定义声明式页面 | `analysis_menus` 配置 + 三模板 | ✅ 已有 |
| 前端页面/导航/插槽 | `frontend/src/custom/<ns>/extension.tsx` | ✅ 已有（AI 助手即范例） |
| 后端路由/启动钩子/通知格式化 | `backend/app/custom/<module>.py` | ✅ 已有 |
| 自定义/AI/叠加策略 | `data/strategies/` 目录 | ✅ 已有 |
| AI 分析、大盘复盘、概念/行业/个股/财务分析、监控中心/异动/连板/持仓提醒、模拟盘、品牌页等**展示与外围业务页** | 核心数据的消费端; 二开可用扩展页面平行实现替代 UI | 页面可替换; 模拟盘拆分为官方插件的候选（见 §6 V3） |

## 3. 对外开放形态总览

```
┌─ 二次开发者的应用 ────────────────────────────────┐
│   浏览器 SPA / 脚本 / 自建服务                     │
└──────────────┬────────────────────────────────────┘
               │  HTTPS + Bearer Token (scope 授权)
               ▼
┌─ 开放网关层（新增, 薄） ────────────────────────────┐
│  Token 认证 → scope 校验 → 限流(按 Token) → CORS    │
├─ 核心 API（Tier A 稳定契约, 见 §5） ────────────────┤
│  行情数据 / 扩展数据 / 策略与回测 / 监控事件 / 模拟盘 │
├─ 管理接口（Tier B, 永不开放给 Token 的写 scope） ────┤
│  数据同步、扩展表 CRUD、配置、密码                   │
└────────────────────────────────────────────────────┘
```

## 4. 认证、授权与护栏

### 4.1 Token 体系

- 存储沿用 secrets 模式: `data/user_data/tokens.json`（chmod 0600）: `{id, name, token_hash, scopes, created_at, last_used_at, revoked}`; 明文 Token 只在创建响应中出现一次（前缀 `tsp_` + 32 字节随机 hex）。
- 认证通道与现有单密码**并行不替代**: 本机/内网 UI 登录照旧; `Authorization: Bearer tsp_...` 是外部调用方专用第二通道。
- 中间件顺序: 访问密码门 → Token 识别（有 Bearer 头则走 Token 路径, 免密码会话）→ scope 校验 → 限流。

### 4.2 Scope 矩阵（最小够用, 宁少勿滥）

| Scope | 允许 | 默认签发 |
|---|---|---|
| `read:market` | 行情/指数/标的搜索/分时/日K 读取 | ✅ 新 Token 默认 |
| `read:analysis` | 策略列表与运行结果、回测报告与候选、市场环境、监控事件读取 | ❌ 显式勾选 |
| `read:ext` | 扩展数据 rows/values/schema 读取 | ❌ 显式勾选 |
| `run:backtest` | 触发回测/挖掘任务（受重活并发器约束） | ❌ 显式勾选 |
| `paper:trade` | 模拟盘下单/撤单/建户（写操作, 最高敏感级） | ❌ 显式勾选 |
| `admin` | 管理接口 | ❌ **永不签发给 Token**, 仅 UI 会话可用 |

写敏感端点（扩展表 upload/ingest/backfill/delete、数据同步、设置）一律不进 Token scope —— 外部只读 + 指定的两类写（回测任务、模拟盘）。

### 4.3 限流与 CORS

- 按 Token 滑动窗口: 默认 120 req/min; 触发 429 + `Retry-After`。重活端点（回测/挖掘提交）另受既有 `heavy_job_limiter` 并发约束。
- CORS: `OPEN_API_CORS_ORIGINS` 配置（逗号分隔白名单）, 默认空 = 不放行跨域; 放行后浏览器端二开可直接调用。
- 响应头: `X-RateLimit-Remaining` / `X-RateLimit-Limit`。

## 5. 核心 API 清单（Tier A 契约）

**稳定性分层**: Tier A = 对外承诺稳定（字段只增不改, 破坏性变更需 v2 前缀 + 一个大版本过渡期）; Tier B = 内部实现细节, 随时变化, 不出现在开放文档。以 OpenAPI `x-stability: stable` 标注 + `/openapi.json` 过滤视图 `?tier=a` 实现, 不搬迁路由前缀。

### 5.1 行情数据（read:market）

| 端点 | 说明 |
|---|---|
| `GET /api/kline/instruments/search` | 标的搜索（代码/名称/拼音首字母, 含 ETF/指数） |
| `GET /api/kline/daily?symbol=&start=&end=` | 日K（前复权/不复权口径参数） |
| `GET /api/intraday/...` | 分时/多日分时 |
| `GET /api/index/...` | 指数行情与估值截面 |
| `GET /api/overview/market-snapshot` | 全市场当日快照（一次拉取, 二开图表直供） |

### 5.2 扩展数据（read:ext）

| 端点 | 说明 |
|---|---|
| `GET /api/ext-data/{id}/rows` | 明细查询五件套: date 单日 / start_date+end_date 范围（自动行级 date 列）/ filter 等值 / sort / offset+limit; columns 裁剪 |
| `GET /api/ext-data/{id}/values?field=` | 字段取值枚举（filter 下拉配套） |
| `GET /api/ext-data/schema/{id}` | 表结构 |
| `GET /api/ext-data`（列表） | 只返回 id/label/mode/market_level/字段名, 不含拉取配置等管理细节 |

市场级表（market_level）通过同一组端点消费 —— 这是「市场环境类数据发布为扩展数据」的出口。

### 5.3 策略与回测（read:analysis / run:backtest）

| 端点 | 说明 |
|---|---|
| `GET /api/strategies` / `GET /api/strategies/{id}` | 策略清单与详情（参数定义、数据依赖） |
| `POST /api/screener/run` | 运行策略选股（异步任务） |
| `GET /api/screener/result...` | 运行结果（含扩展列） |
| `POST /api/backtest` | 提交回测（含 regime_filter） → 任务 id |
| `GET /api/backtest/{job}/status` / `GET .../report` | 任务状态 / 回测报告（指标、回合、净值序列） |
| `GET /api/backtest/candidates` | 持久化回测候选摘要 |
| `GET /api/regime/states` 等 | 市场环境状态序列（内置 regime 的读取出口） |

### 5.4 监控与事件（read:analysis）

| 端点 | 说明 |
|---|---|
| `GET /api/alerts` | 告警/事件留痕查询 |
| `GET /api/events/stream`（SSE） | 实时事件流（告警/成交/系统事件）; Token 以 query 参数短期票据接入（SSE 无法带 header） |
| Webhook 订阅注册 | **V2 再做**: 按 Token 注册回调 URL + 事件类型 + 签名密钥（复用现有 custom webhook 通道能力） |

### 5.5 模拟盘（paper:trade）

| 端点 | 说明 |
|---|---|
| `GET /api/paper/accounts` `/overview` `/positions` `/nav` `/stats` `/compare` | 账户与账务读取 |
| `POST /api/paper/orders` `DELETE /api/paper/orders/{id}` | 下单/撤单（写） |
| `GET /api/paper/trades` | 成交台账（append-only 事实源） |

### 5.6 系统

| 端点 | 说明 |
|---|---|
| `GET /api/health` | 探活（无鉴权, 白名单已有） |
| `GET /api/auth/api-tokens`（UI 会话） | Token 管理: 列表/创建/吊销（管理页, 不属于开放面） |
| `GET /api/openapi.json?tier=a` | Tier A 契约视图（机器可读） |

### 5.7 通用约定

- **分页**: `offset` + `limit`, 响应含 `total`; 列表端点统一 `items`/`rows` 字段名在契约化时逐一固定。
- **错误**: FastAPI 默认 `{detail}`; 契约化后统一补 `code` 字段（Tier A 端点）。
- **日期**: 全部 `YYYY-MM-DD` / ISO 时间戳, 北京时间口径（与内部一致, 不做时区换算）。
- **金额单位**: 元; 比例: 百分数数值（与各域现状一致, 契约文档逐一标注, 不强行统一）。

## 6. 分期路线

### V1 开门（Token + 护栏 + 最小契约）— 建议立即

- ✅ Token 存储/创建/吊销 + 设置页管理 UI（「设置 → 开放接口」，`services/api_tokens.py`）。
- ✅ 中间件: Bearer 识别 → scope 校验 → 限流（`services/api_gateway.py`，CORS 原本已全开）。
- ✅ Tier A: 直接覆盖全部五个 scope（`api_gateway._RULES` 规则表）。
- ✅ 测试矩阵: `backend/tests/test_api_gateway.py`（401/403/429/吊销即拒/前缀混淆防护）。

### V2 契约化 + 示例仓库

- ✅ Tier A 已含策略/回测/监控事件/模拟盘; `GET /api/openapi.json?tier=a` 契约视图（规则表单源）。
- 组织下建 `api-examples` 示例仓库: 「拉行情 → 跑策略 → 取结果 → 模拟盘跟单」三个可运行脚本 + README。
- SSE 短期票据（query token）方案落地。

### V3 钩子契约 + 模拟盘拆分试点

- 核心提供 `盘中快照钩子` `盘后结算钩子` 事件契约, 模拟盘改为通过钩子接入 → 验证「核心 + 官方插件」拆分工程可行性（当前反向 import 30 处, 先立契约再搬）。

### V4 市场级过滤契约（按需）

- 引擎把 regime_filter 泛化为「date 对齐的市场级扩展表过滤」: 官方 regime 表迁移为第一个实现, 第三方择时数据集（市场级扩展表）可直接选入回测过滤。触发条件: 第一个真实的第三方择时数据集出现。

## 7. 风险与守住的边界

- **Token 泄漏 = 面板数据泄漏**: 默认只读 scope、可随时吊销、创建时明文只显一次; paper:trade 永不默认。
- **契约冻结过早**: Tier A 从最小两组起步, 按二开反馈逐域升级, 未验证的端点留在 Tier B。
- **限流误伤本机 UI**: Token 限流只作用于 Bearer 路径; UI 会话不受 Token 桶约束。
- **不因开放破坏内部演进**: 开放面是「视图 + 网关」, 不重写内部路由; 内部 API 与开放 API 解耦, 由标注层衔接。
