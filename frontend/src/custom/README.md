# 前端扩展目录

每个子目录一个 `extension.tsx` 注册清单，构建时自动发现挂载，无需修改核心路由和导航文件。清单保持轻量：路由与导航同步注册，页面实现放在同目录的 `Page.tsx`，使用 `React.lazy` 按访问加载：

```text
frontend/src/custom/<namespace>/extension.tsx
frontend/src/custom/<namespace>/Page.tsx
```

- 源码扩展随前端构建。迁移时复制清单和其依赖，再重新构建并检查路由、插槽及菜单配置。源码部署支持在「页面管理 → 菜单与页面」删除可管理的源码页面，服务端会先生成备份。
- 冲突（路由/ID 撞车）由扩展注册表在启动时检测，冲突的扩展整体拒绝（原因输出在浏览器控制台），不影响核心页面。

## 目录现状

官方扩展的实际清单位于本目录，ID 通常以 `official.` 开头。目录内也有按插槽接入的扩展。复制源码前请检查清单引用的依赖与目标宿主提供的契约。

| 目录 | 页面或入口 |
| --- | --- |
| `stock-analysis` | 个股分析 |
| `limit-ladder` | 连板梯队 |
| `concept-analysis` | 概念分析 |
| `industry-analysis` | 行业分析 |
| `financials` | 财务分析 |
| `monitor` | 监控中心 |
| `abnormal` | 异动监控 |
| `lots` | 持仓提醒 |
| `paper` | 模拟盘 |
| `signals` | 信号库 |
| `review` | 复盘 |
| `assistant` | AI 对话助手（扩展机制参考实现） |
| `market-regime` | 市场环境 |
| `tivelo` | 引擎研究 |
| `source-packages` | 数据源页的安装与管理入口 |

页面显示和排序在「设置 → 页面管理 → 菜单与页面」配置。已有导航布局随偏好加载。

## 开发新的源码级扩展

以 [`_template/extension.tsx.example`](_template/extension.tsx.example) 为起点（模板文件不参与构建），并遵循 [`docs/secondary-development.md`](../../../docs/secondary-development.md)。

## 不想重建宿主？用页面插件

若目标是把页面打包成产物、在运行中的面板上直接上传使用（本地 / Docker / 桌面端一致），走**页面插件**：用 [`frontend/page-pack-template`](../../page-pack-template) 打包单文件 ES 模块，在「设置 → 导入与备份」上传，刷新生效。契约与边界见 [`docs/page-sdk.md`](../../../docs/page-sdk.md)。
