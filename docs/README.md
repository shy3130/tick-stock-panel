# TSP 文档导航

这些入口以 `tsp-0.4.1` 候选工作区为准。安装器与公开镜像以 [Releases](https://github.com/shy3130/tick-stock-panel/releases) 中对应制品为准，本文提供当前源码的使用与扩展说明。

## 使用工作台

| 要完成的任务 | 文档 |
| :--- | :--- |
| 首次部署、升级、保留原数据 | [0.4.1 版本说明](./release-v0.4.1.md) · [部署指南](./deployment.md) · [访问密码](./deploy-password.md) · [环境和数据配置](./configuration.md) |
| 使用自选、看板、监控、模拟盘等页面 | [操作说明书](../操作说明书.md) · [功能手册](./features.md) · [截图索引](../screenshots/README.md) |
| 在个股详情配置字段、日 K 显示范围和指标 | [个股详情与自定义图表指标](./custom-chart-indicators.md) |
| 自建策略、因子、信号或导入资源 | [策略指南](./strategy.md) · [导入与备份](./extension-packages.md) |
| 分析市场阶段、挖掘因子与验证结果 | [市场环境](./market-phase.md) · [因子与策略挖掘](./mining.md) · [回测证据](./backtest-evidence.md) |
| 选择前复权、后复权或动态复权，运行归档研究任务 | [策略回测](./features.md#-回测引擎backtest) · [动态复权](./engine-platform/workbench-research-jobs.md#动态复权扩展) · [引擎研究](./engine-platform/workbench-research-jobs.md) |

## 让 AI 接入或开发

| 要完成的任务 | 文档 |
| :--- | :--- |
| 选择内置助手、外部 MCP、REST 或扩展开发 | [AI 接入与二次开发](./ai-integration.md) |
| 连接外部 AI 客户端、脚本或工具 | [MCP 客户端配置](../mcp-server/README.md) · [Open API 可运行示例](../examples/open-api/README.md) |
| 让 AI 生成和迭代策略 | [策略写法](./strategy.md) · [迭代、台账与验证](./strategy-iteration.md) |
| 让 AI 改源码、接入插槽或注册后端能力 | [AI 开发入口](../AGENTS.md) · [二次开发与任务模板](./secondary-development.md) · [贡献指南](../CONTRIBUTING.md) |

## 扩展契约与示例

| 扩展类型 | 开发与试装 |
| :--- | :--- |
| 自定义 HTTP 数据源 | [YAML 格式](./custom-data-source.md) · [示例与 mock 联调](./examples/custom-data-source/README.md) |
| Python 数据源插件 | [Provider 契约](./plugin-development.md) · [ZIP 模板](../examples/data-source-plugin/README.md) · [外部 ZIP 安装](./external-data-source-packages.md) · [双端数据源示例](../examples/dual-host-data-source/README.md) |
| 自定义数据表、页面与菜单 | [单表配置分享](./extension-packages.md#单个扩展数据配置分享) · [Page Pack SDK](./page-sdk.md) · [页面模板](../frontend/page-pack-template/README.md) · [源码页面目录](../frontend/src/custom/README.md) |
| 后端能力、通知、仓位和模拟盘 | [后端注册入口](../backend/app/custom/README.md) · [外部扩展包](./extension-packages.md#后端--前后端完整包) · [模拟盘执行契约](../backend/app/custom/paper_execution/README.md) |
| 指标公式与图表宿主 | [指标公式与适用范围](./custom-chart-indicators.md) · [模块边界与接入记录](./custom-chart-indicators-integration.md) |
| 引擎数据、规则、扩展和更新 | [数据与市场契约](./engine-platform/data-and-market-contracts.md) · [发行、授权及回退](./engine-platform/extensions-and-releases.md) |
| 独立组件接口 | [Tivelo Data](../packages/tivelo-data/README.md) · [Tivelo Backtest](../packages/tivelo-backtest/README.md) · [Contracts](../packages/tivelo-contracts/README.md) · [Extensions](../packages/tivelo-extensions/README.md) · [A 股规则](../packages/tivelo-market-cn/README.md) |
| 维护品牌资源与应用图标 | [品牌资产](../brand/README.md) |

## 设计和历史记录

接口、UI 操作和安装步骤以上面的使用及开发文档为准。以下材料保留设计取舍和当时的验证条件。

- [扩展分层设计](./ext-architecture-plan.md) · [开放接口路线图](./open-platform-plan.md)
- [因子系统设计](./factor-system-design.md) · [因子平台规划](./factor-platform-plan.md) · [自动策略实验室](./auto-strategy-lab-plan.md) · [稳健优化设计](./robust-optimizer-plan.md)
- [模拟盘设计](./paper-trading-plan.md) · [个股图表技术设计](./stock-detail-lightweight-charts-plan.md) · [后端性能计划](./backend-data-performance-plan.md)
- [TickFlow 早期探测](./tickflow-pro-phase1-probe.md) · [共享限流设计](./tickflow-pro-shared-rate-limit.md)

应用运行时使用的策略提示词见 [策略模板与编写规范](../backend/app/strategy/prompts/strategy-guide.md)。它们随代码使用，外部 AI 任务从 [AI 接入指南](./ai-integration.md)开始。
