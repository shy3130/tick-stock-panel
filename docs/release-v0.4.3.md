# v0.4.3 版本说明

本页记录 v0.4.3 的变更及升级步骤。请从对应版本下载适用的安装包。[GitHub Releases](https://github.com/shy3130/tick-stock-panel/releases)

## 主要变化

- **修复桌面版覆盖升级启动报错（重要）。** 修复从 `v0.4.1` 覆盖升级至 `v0.4.2` 时因工作区基线未同步导致弹出「当前可执行文件与活动工作区不匹配」及重复 PyInstaller 异常堆栈框的问题。对未拆分多工作区且未安装独立引擎覆盖的默认工作区，新版在确认无活动研究任务后自动接管原工作区并保留全部用户数据，同时支持升级后并发双击启动容错；已升级至 `v0.4.2` 遇报错的用户直接覆盖安装 `v0.4.3` 即可自动恢复，无需清理数据目录。发版流水线同步补齐“先运行旧版再覆盖安装新版”的真实升级校验。
- **内置扩展采集器（`ExtCollector`）与 9:25 竞价扫描。** 新增 `ExtCollector` 扩展契约与 `GET /api/ext-data/collectors` 发现接口，内置 9:25 集合竞价扫描扩展；支持在自定义数据表中以 `collector` 拉取模式直接运行扩展采集器，并将表结构与板块标签等信息写入扩展数据表。[二次开发指南](./secondary-development.md)
- **可组合扩展引擎能力。** 新增只读 `MarketAccess` 门面（`ctx.market`），让内外扩展采集器无需依赖私有服务模块即可读取实时行情、5 日均量基准与股票名称映射；支持通过 `collector_params` 参数化复用同一采集器派生多张扩展数据表（含前端「扩展采集器」配置页签）；为运行时 Page Pack 扩展补充本地插槽挂载/就地更新以及跨扩展受限 API 调用 `ctx.api.extensionRequest()`。[页面扩展 SDK](./page-sdk.md)

## 升级与回滚

1. **升级前。** 在「设置 → 导入与备份」导出配置，停止旧实例后备份实际数据目录。保留旧安装包或镜像版本及原启动参数，以便回滚。Compose 和 Docker 用户先核对容器的 `DATA_DIR` 与宿主机挂载。[数据目录核对](./deployment.md#docker-升级前核对数据目录)
2. **升级。** 桌面版直接覆盖安装到原位置（从 `v0.4.1` 或遇报错的 `v0.4.2` 覆盖升级均会自动恢复并保留原数据）；Docker 重建时沿用原数据挂载，源码部署按锁文件同步依赖。[桌面升级](./deployment.md#桌面版覆盖升级与旧配置恢复) · [部署步骤](./deployment.md)
3. **升级后。** 在「设置 → 关于」确认版本号为 `v0.4.3`。
4. **回滚。** 先停新版并保留本次运行的数据。用旧程序或旧镜像读取升级前备份的副本，核对设置、行情和策略可用后再恢复运行。

## 验证记录

| 项目 | 验证结果 |
| :--- | :--- |
| 版本同步 | `python3 scripts/sync_versions.py --check` 通过，源码版本一致为 `v0.4.3`，`backend/uv.lock` 同步锁定。 |
| PR 合入前 CI | [PR #592](https://github.com/shy3130/tick-stock-panel/pull/592)、[PR #593](https://github.com/shy3130/tick-stock-panel/pull/593)、[PR #594](https://github.com/shy3130/tick-stock-panel/pull/594) 全部 8/8 项 CI 作业通过（后端全量测试、前端 Vitest 与 `pnpm build`、Windows 锁回归、DCO 及 4 组独立引擎分发验证）。 |

[桌面发布工作流](../.github/workflows/release.yml) · [Docker 构建工作流](../.github/workflows/docker.yml)
