# AI 接入与二次开发

本指南对应 `tsp-0.4.1` 候选工作区。AI 可以使用工作台、调用已有接口，也可以帮你开发可导入的扩展。先根据任务选入口，再按部署实际提供的契约执行。

| 任务 | 入口 | 验证结果 |
| :--- | :--- | :--- |
| 在面板里问行情、分析持仓或生成策略草稿 | 「设置 → AI 设置」测试模型连接，再打开工作台的 AI 助手 | 能读到本机工作台的数据并核对引用 |
| 在外部 AI 客户端查行情、读策略或提交回测 | [MCP stdio](../mcp-server/README.md) | 客户端能列出并调用已授权工具 |
| 让脚本按固定参数读取、写表或执行回测 | [Open API 的 Tier A 契约](./features.md#-开放接口open-api--tier-a) | 能用对应 scope 的 Token 调通一个最小请求 |
| 让 AI 做新指标、接入数据源或定制页面 | [开发并导入扩展](#开发并导入扩展) | 交付物能在同版工作台预览、导入并生效 |
| 复跑有固定输入和归档的研究任务 | [引擎研究](./engine-platform/workbench-research-jobs.md) | 能预检输入、读取任务结果并复核价格口径 |

## 连接已有工作台

1. 从「设置 → 开放接口」新建 Token。行情查询用 `read:market`，策略和告警用 `read:analysis`，扩展表查询用 `read:ext`。按任务选择权限。
2. 将工作台的后端地址设为 `TSP_BASE`。本机默认 `http://127.0.0.1:3018`。源码模式的 3011 是开发前端。桌面版或改过端口的部署使用实际服务地址。
3. 将 Token 保存在客户端的环境变量或密钥管理中。脚本从 `TSP_TOKEN` 读取。分享日志和提示词时使用脱敏值。
4. 启动工作台、同步所需数据后，先调用只读查询。使用返回的标的代码、资源 ID、字段名和日期范围执行后续任务。

### 通过 Open API 核对契约

以下命令假定当前终端已设置 `TSP_TOKEN`，该 Token 授权了 `read:market`。

```bash
export TSP_BASE=http://127.0.0.1:3018
curl -fsS "${TSP_BASE}/api/openapi.json?tier=a" -o tsp-openapi.json
curl -fsS -H "Authorization: Bearer ${TSP_TOKEN}" \
  "${TSP_BASE}/api/kline/instruments/search?q=600519&limit=2"
```

- 把 `tsp-openapi.json` 交给 AI 作为路径和参数依据。仅使用目标部署实际公开的路径和 scope。
- 回测前读策略清单，确认股票池、区间和本地行情覆盖。普通策略回测 `POST /api/backtest/strategy/run` 需要 `run:backtest`。请求体的 `price_basis` 支持 `qfq`（默认）、`hfq` 和 `dynamic`。动态复权要求目标工作台已启用并授权 Tivelo Pro 0.1.0a2 或更高版本，并使用 A 股日线策略和日线成交方式。[回测示例](../examples/open-api/README.md)
- 写入扩展表前，先用 `read:ext` 读取已建表的字段定义。写入使用 `write:ext` 调用 `POST /api/ext-data/{id}/ingest`，先写入少量符合定义的真实样本，再读回核对。
- 401 通常是 Token 无效或已吊销。403 应核对 scope、路径和本机/内网门禁。429 按响应的 `Retry-After` 等待。远程访问可复用部署指南中的 SSH 隧道。[开放接口网络门禁](./configuration.md#开放接口网络门禁默认仅本机内网)

### 通过 MCP 使用工具

让支持 stdio 的客户端按 [MCP 配置示例](../mcp-server/README.md#配置到-mcp-客户端)拉起 `tsp-mcp-server`。进程收到 `TSP_BASE`、`TSP_TOKEN` 和可选的 `TSP_SCOPES`。前两项指向要访问的工作台和对应的 Token，`TSP_SCOPES` 控制工具展示，实际权限由工作台网关裁决。

用 `search_instrument` → `get_kline` 验证标的查询和日期。需要使用 `run_backtest` 时，先经 `list_strategies` 获取策略 ID，再用小范围行情验证。MCP 的回测工具使用普通回测默认价格口径。需要后复权或动态复权时，通过开放接口的 `price_basis` 指定，或在面板的「复权方式」选择。

可以这样给 AI 交代任务：

> 使用已配置的 TSP MCP 查询 600519 对应的标的和最近 10 根日 K。请返回实际查询到的代码、最后一根日期、是否缺数及收盘价。优先调用搜索，再用返回的代码取数。遇到接口错误时保留报错和已成功读到的数据。

## 开发并导入扩展

把目标、所用版本、实际字段和一份脱敏样本交给 AI。让它从现有契约和示例开始，并首先交付可以预览和验证的最小产物。

| 你想增加 | 给 AI 的依据 | 交付物和验收 |
| :--- | :--- | :--- |
| 一个只用于显示或研究的外部字段 | [扩展表配置](./extension-packages.md#单个扩展数据配置分享)与脱敏接口样本 | 创建扩展表或导入单表 ZIP。填本机凭据、试拉并核对时间列、标的代码和单位 |
| 一个可接管日 K、复权、实时等能力的供应商 | [YAML 方案](./custom-data-source.md)、[Provider 契约](./plugin-development.md)与能力清单 | YAML 配置，或根目录带 `plugin.yaml` 的 ZIP。验证后只选择拟切换的能力 |
| 策略、信号和因子 | [策略指南](./strategy.md)与[实际数据契约](../CONTRIBUTING.md#3-不可混用的数据契约) | 原文件或依赖齐全的资源 ZIP。预览冲突，导入后运行一次扫描或小范围回测 |
| 个股图表公式 | [公式和指标导入指南](./custom-chart-indicators.md)及参考样值 | 明文公式或 `tick-chart-indicators` v1 JSON。先解析预览，再按日 K 或分时保存方案 |
| 数据分析页、自定义页面或导航 | [页面 SDK](./page-sdk.md)及[模板](../frontend/page-pack-template/README.md) | 声明式页面配置或 `extension.js`。导入后刷新，检查新页面、菜单和已有页面 |
| 后端路由或通知文案 | [后端扩展规范](./secondary-development.md#4-后端扩展规范)和[扩展包契约](./extension-packages.md#后端--前后端完整包) | 根目录带 `extension.yaml` 的 ZIP。预检并重启后核对注册状态、响应和回退 |
| 数据引擎的历史快照或商业价格视图 | [引擎研究](./engine-platform/workbench-research-jobs.md)和[发行契约](./engine-platform/extensions-and-releases.md) | 匹配的数据快照、研究计划，或签名引擎扩展。按本机发布信任、启用和授权状态预检 |

**给编码助手的任务模板**

> 在当前 TSP 工作区接入我的数据源，先阅读 AGENTS.md、CONTRIBUTING.md、docs/secondary-development.md 和 docs/plugin-development.md。根据我提供的脱敏样本确认行情类型、日期、时区、单位、分页与空响应。优先复用 YAML 或 Provider 插件，交付可预览的配置或 ZIP，并列出试拉步骤、生效方式及对应测试。使用临时数据目录完成验证，由我在面板填写真实凭据并选择接管的能力。

管理操作以面板登录会话执行，包括导入配置、安装扩展、建表、改变能力路由和引擎研究任务。外部 AI 可用的 Bearer Token 以 Tier A 清单为准。项目内源码扩展随对应服务构建或启动加载。[统一导入](./extension-packages.md) · [源码开发工作流](./secondary-development.md#6-ai-开发工作流)

## 记录可复验的结果

- 数据开发记录一次成功试拉的样本日期、字段映射、单位和空响应行为。
- 页面开发记录包标识、`apiVersion`、实际路由和刷新后的表现。
- 回测记录策略版本、标的、区间、价格口径、交易成本和输出文件。需要历史可知时间或数据快照时使用对应研究契约。
- 上报问题时提供 TSP 部署方式、组件版本、预检和失败步骤，以及脱敏日志。区分已验证的结果和仍待试装的第三方包。

[文档导航](./README.md) · [策略迭代](./strategy-iteration.md) · [回测证据](./backtest-evidence.md)
