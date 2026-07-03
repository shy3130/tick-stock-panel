# fquant_local TickFlow 依赖审计

日期：2026-07-03  
范围：只读核实 `fquant_local` 本地数据源模式下仍走 TickFlow / 依赖 TickFlow VIP 等级的数据路径。

## 总结

`DATA_PROVIDER=fquant_local` 生效时，核心行情数据面基本已不走 TickFlow SDK。仍需处理的是：

- TickFlow 时代的 `CapabilitySet/Cap/tier_label` 仍是全局功能门控壳。
- 设置页仍保留 TickFlow key / endpoint / tier 展示与探测接口。
- 少数功能仍按 TickFlow 档位语义判断，最明确的是分钟K按月扩展要求 Expert。
- `app.tickflow.repository` 实际是本地 Parquet/DuckDB 仓储，不是 SDK，但包名会阻碍删除 `app.tickflow`。

## High

1. 分钟K按月扩展仍硬依赖 TickFlow Expert 档语义。
   - 证据：`backend/app/api/kline.py:718-727` 先检查 `Cap.KLINE_MINUTE_BATCH`，但 `unit == "month"` 又读 `tier_label()` 并要求 `expert`。
   - 影响：`fquant_local` 有分钟K provider，但按月扩展可能被旧 VIP 语义挡住。
   - 移除前动作：改成 provider capability / 本地配置门控，不再按 TickFlow tier。
   - 硬约束：修法不是裸删 Expert 判断；原注释里的“month 扩展成本较高”仍成立，本地模式抽多月分钟K也贵，必须替换成 provider capability 或本地配置门控，否则会变成无条件放行。

2. capability 体系仍是 TickFlow 专属命名，但被用作全局门控。
   - 证据：`backend/app/tickflow/capabilities.py:11-27` 定义 `quote/kline/depth/financial/adj_factor` 等 Cap。
   - 证据：`backend/app/tickflow/policy.py:255-288` 把非 TickFlow provider capability 翻译成这些 Cap。
   - 证据：`backend/app/tickflow/policy.py:291-302` 非 TickFlow 时跳过 TickFlow API 探测。
   - 影响：本地模式不依赖 TickFlow key，但 UI/API 仍依赖 TickFlow-era Cap 语义。
   - 移除前动作：把 Cap/CapabilitySet/CapabilityDenied 迁到中性模块，并把文案从套餐升级改为数据源能力。

3. provider capability 路由不完整。
   - 证据：`backend/app/data_providers/registry.py:23-46` 只对 `daily/minute/realtime/adj_factor` 做 capability 参数分流。
   - 证据：`backend/app/services/financial_sync.py:49-60` 财务同步使用 global provider。
   - 证据：`backend/app/services/kline_sync.py:31-42` kline_sync provider 固定用 `"daily"`；`backend/app/services/depth_service.py:271-288` depth 又复用该 provider。
   - 影响：如果不是全局 `DATA_PROVIDER=fquant_local`，只按单能力切换时财务/depth 可能不跟随。
   - 移除前动作：给 financial/depth/instruments 等能力补齐 provider 选择，或明确只支持全局 provider。

## Medium

1. 5档盘口已有本地路径，但接口文案仍是 Pro+。
   - 证据：`backend/app/data_providers/fquant_provider.py:152-172` 声明 `depth=True`。
   - 证据：`backend/app/data_providers/fquant_provider.py:620-624` 调 `sina_tencent.get_depth()`。
   - 证据：`backend/app/data_providers/fquant/sina_tencent_client.py:209-220` Tencent depth 拉取与解析。
   - 证据：`backend/app/services/depth_service.py:569-582` 非 TickFlow provider 只看 provider depth capability，不看套餐。
   - 证据：`backend/app/api/settings.py:1050-1087` 设置接口仍 `Cap.DEPTH5_BATCH`，注释为“需 Pro+”。
   - 影响：数据路径不依赖 TickFlow，但用户语义仍误导。

2. TickFlow SDK 直接耦合仍存在，但多数只在 TickFlow provider 或 guarded fallback 中。
   - SDK 封装：`backend/app/tickflow/client.py:13-27`、`backend/app/tickflow/client.py:48-58`。
   - TickFlowProvider：`backend/app/data_providers/tickflow_provider.py:16-34`、`backend/app/data_providers/tickflow_provider.py:36-134`。
   - TickFlow pools：`backend/app/tickflow/pools.py:18`、`backend/app/tickflow/pools.py:72-132`。
   - guarded 使用：`backend/app/jobs/daily_pipeline.py:54-72` 仅 `provider_name == "tickflow"` 时 import pools；`backend/app/api/kline.py:511`、`backend/app/api/kline.py:881` 同样只在 TickFlow provider 下 fallback。
   - 影响：fquant_local 正常路径不走 SDK；删除 TickFlow provider 时这些文件可一起移除或替换。

3. `app.tickflow.repository` 是本地仓储但命名污染。
   - 证据：8 个导入方：`backend/app/main.py:21`、`backend/app/services/kline_sync.py:22`、`backend/app/jobs/daily_pipeline.py:26`、`backend/app/services/screener.py:17`、`backend/app/backtest/engine.py:22`、`backend/app/services/index_sync.py:23`、`backend/app/services/extend_history.py:30`、`backend/app/services/backtest.py:18`。
   - 影响：不能把整个 `app.tickflow` 包直接删掉；需先把 repository 移到中性包名。

## Capability 覆盖

| 能力 | fquant_local 路径 | TickFlow 依赖结论 |
|---|---|---|
| A股日K | `FQuantProvider.get_daily()` 走 disk wide/day + fstore fallback：`backend/app/data_providers/fquant_provider.py:264-320`；disk wide/day：`backend/app/data_providers/fquant/engine_data_disk.py:61-68` | 不依赖 TickFlow |
| 指数日K | `index_sync.sync_and_persist_index_daily()` 调 `kline_sync.sync_daily_batch(asset_type="index")`：`backend/app/services/index_sync.py:181-245` | 不依赖 TickFlow |
| ETF日K | `index_sync.sync_and_persist_etf_daily()` 调 `asset_type="etf"`：`backend/app/services/index_sync.py:284-345` | 不依赖 TickFlow |
| 分钟K | `FQuantProvider.get_minute()`：`backend/app/data_providers/fquant_provider.py:532-565`；disk minutes：`backend/app/data_providers/fquant/engine_data_disk.py:111-126` | 数据不依赖；按月扩展仍有 Expert gate |
| 实时行情 | tdx-api -> sina/tencent -> fstore：`backend/app/data_providers/fquant_provider.py:580-618`；QuoteService 非 TickFlow full_market：`backend/app/services/quote_service.py:251-274`、`backend/app/services/quote_service.py:392-430` | 不依赖 TickFlow |
| 复权因子 | engine xdxr + fstore `chuquan_chuxi`：`backend/app/data_providers/fquant_provider.py:418-500` | 不依赖 TickFlow |
| instruments/universes | FQuantProvider capability 声明：`backend/app/data_providers/fquant_provider.py:164-172` | 不依赖 TickFlow |
| 财务 | fstore financial tables：`backend/app/data_providers/fquant_provider.py:1003-1024`；API 仍用 `Cap.FINANCIAL`：`backend/app/api/financials.py:21-26`、`backend/app/api/financials.py:58-76` | 数据不依赖；门控仍 TickFlow-era |
| 资金流 | disk fund + moneyflow fallback：`backend/app/data_providers/fquant_provider.py:1029-1089`；disk fund：`backend/app/data_providers/fquant/engine_data_disk.py:145-160` | provider 有实现；当前未见业务 API/service 消费者 |
| 5档盘口 depth | Tencent depth：`backend/app/data_providers/fquant/sina_tencent_client.py:209-220` | 不依赖 TickFlow；文案/Cap 仍 Pro+ |
| 龙虎榜 | ext preset Eastmoney：`backend/app/services/ext_presets.py:97-124` | 不依赖 TickFlow |
| 概念/行业 | ext preset `files.688798.xyz/ths`：`backend/app/services/ext_presets.py:30-32`、`backend/app/services/ext_presets.py:39-75` | 不依赖 TickFlow |
| 连板梯队/大盘概览 | repo enriched + quote/depth/ext：`backend/app/services/market_overview_builder.py:396-445`、`backend/app/services/market_overview_builder.py:500-550` | 不依赖 TickFlow |
| RPS | repo enriched + ext 概念：`backend/app/services/rps_rotation.py:146-175` | 不依赖 TickFlow |
| AI复盘输入 | 装配 market overview：`backend/app/services/market_recap.py:253-271` | 不依赖 TickFlow |
| intraday/websocket | Cap 定义存在：`backend/app/tickflow/capabilities.py:21-25`；非 TickFlow provider 映射未提供 | 当前未见业务消费者；删除时可一并清理旧能力 |

## VIP / 等级门控

- `tiers.yaml` 是 TickFlow 套餐表：`tiers.yaml:27-73`。
- 无 key / free / starter / pro / expert 的运行逻辑在 `backend/app/tickflow/client.py:30-58`。
- 非 TickFlow provider 会直接生成 provider capset 并持久化 label：`backend/app/tickflow/policy.py:291-302`。
- `/api/capabilities` 和 `/health` 仍走 TickFlow policy/client 命名：`backend/app/api/routes.py:13-30`。
- 设置页仍返回 TickFlow key、tier、endpoint、probe log：`backend/app/api/settings.py:52-81`。
- 保存/清除 TickFlow key 仍存在：`backend/app/api/settings.py:114-218`。
- provider 切换后会重新探测 capability：`backend/app/api/settings.py:387-419`。

结论：`fquant_local` 不需要 TickFlow key/VIP 才能跑主要功能；但旧 Cap/tier 仍影响 UI 状态、错误文案、分钟K按月扩展和 depth 设置。

## 直接耦合清单

| 触点 | 证据 | 分类 |
|---|---|---|
| TickFlow SDK client | `backend/app/tickflow/client.py:13-27`、`backend/app/tickflow/client.py:48-58` | 删除 TickFlow provider 后可删 |
| TickFlowProvider | `backend/app/data_providers/tickflow_provider.py:16-134` | 若彻底移除 TickFlow，则整体删除 |
| TickFlow pools | `backend/app/tickflow/pools.py:18`、`backend/app/tickflow/pools.py:72-132` | provider=TickFlow fallback；先替换/删除 guarded fallback |
| settings key/endpoint | `backend/app/api/settings.py:52-81`、`backend/app/api/settings.py:114-218` | 本地模式下遗留 UI |
| health/mode | `backend/app/api/routes.py:13-20`、`backend/app/main.py:32-57` | 改成 provider mode |
| CapabilityDenied handler | `backend/app/main.py:274-286` | 迁到中性 capability 模块 |
| tickflow scheduler | `backend/app/tickflow/scheduler.py`；`rg` 未发现 app/tests 引用 | 可优先删除 |
| tickflow repository | 已迁到 `app.storage.repository`，旧 `app.tickflow.repository` 仅保留兼容 shim | A3 已完成；A6 删除 shim |

## 移除就绪度清单

> 2026-07-03 更新：A1-A7 已按本清单执行；产品决策已落档为不再保留 `DATA_PROVIDER=tickflow`。`TickFlowProvider`、`app/tickflow/`、`tiers.yaml`、TickFlow key/endpoint UI 和 SDK 依赖已删除，默认 provider 改为 `fquant_local`。

### A. 本地模式下已死/可先清理

- `backend/app/tickflow/scheduler.py`：未发现运行时引用。
- settings 中 TickFlow key / endpoint / probe UI：本地模式不需要，但删除前要改前端展示。
- `tiers.yaml`：只服务 TickFlow 探测/label，替换 Cap/tier 后可删。

### B. 仍是某能力唯一来源或需先替换

- `TickFlowProvider`：如果还要保留 `DATA_PROVIDER=tickflow`，不能删；若产品决策彻底去 TickFlow，可整体移除。
- `tickflow.pools`：当前仅 provider=TickFlow fallback 使用；删除 TickFlow provider 后同步删除 fallback。
- `app.tickflow.repository`：已迁到 `app.storage.repository`；旧路径仅为兼容 shim，A6 删除。

### C. 移除会改变语义

- `CapabilitySet/Cap/tier_label`：当前 UI 和 API 都用它判断功能可用性。
- 分钟K按月扩展：从 Expert-only 改成 provider capability 后，本地模式语义会放开。
- depth 设置：从 Pro+ 语义改成 provider depth capability 后，本地模式应直接可用。

## 建议顺序

1. ✅ Quick-win：先去掉分钟K按月扩展的 `tier_label()==expert` 判断，改成 provider capability / 本地配置。
2. ✅ 把 `Cap/CapabilitySet/CapabilityDenied` 从 `app.tickflow` 迁到中性 capability 模块。
3. ✅ 把 `app.tickflow.repository` 迁到中性包名。
4. ✅ 把 settings/health/capabilities 的 TickFlow key/tier/endpoint 展示拆除。
5. ✅ 删除 `tickflow.scheduler`、`tickflow.pools` guarded fallback、`tiers.yaml`。
6. ✅ 最后删除 `TickFlowProvider` 和 `tickflow.client`。
