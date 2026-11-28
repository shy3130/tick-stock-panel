# AI 开发入口

修改、调试或审查本仓库前，必须完整阅读并遵循根目录的 [`CONTRIBUTING.md`](CONTRIBUTING.md)。其中定义了项目架构、数据契约、数据源插件化、缓存与性能要求、测试矩阵以及 PR 复审和合并标准。

涉及代码二次开发、前端插槽、后端可替换策略、扩展注册或上游升级兼容时，还必须阅读 [`docs/secondary-development.md`](docs/secondary-development.md)。该文档区分当前已实现能力与目标扩展契约；不得根据设计示例虚构尚不存在的 API。

同时遵守以下规则：

- 先理解调用链和现有测试，再进行修改。
- 保持实现简单、改动范围最小，不处理无关问题。
- 不覆盖工作区已有修改，不虚构测试或审查结果。
- 以实际验证结果作为完成标准。

## 按任务找现行契约

- 对接已部署的 TSP、选择 MCP 或调用 Open API，先读 [AI 接入指南](docs/ai-integration.md)。
- 编写可导入的配置、指标、资源或插件，先查 [文档导航](docs/README.md) 和 [统一导入](docs/extension-packages.md)，再读对应契约。
- 运行策略回测或引擎研究，先核对 [回测证据](docs/backtest-evidence.md) 与 [引擎研究](docs/engine-platform/workbench-research-jobs.md)。配置、包版本和验收结果按目标部署实际返回值记录。
