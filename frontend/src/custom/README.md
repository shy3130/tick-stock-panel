# 前端扩展目录

每个子目录一个 `extension.tsx`，**单文件即完整扩展**（页面组件 + 路由 + 导航注册在同一个文件里），构建时自动发现挂载，无需修改核心路由和导航文件：

```text
frontend/src/custom/<namespace>/extension.tsx
```

- **摘除**：删除目录即整体卸载该页面；**导入**：把目录复制进来重新构建即可。
- 冲突（路由/ID 撞车）由扩展注册表在启动时检测，冲突的扩展整体拒绝（原因输出在浏览器控制台），不影响核心页面。

## 目录现状

官方扩展页面全部以单文件扩展形式落在本目录（id 前缀 `official.`）：

| 目录 | 页面 |
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

**新装用户默认隐藏这些页面**，在 设置 → 菜单设置 一键开启；老用户升级自动保持原有菜单位置与排序。

## 开发新的源码级扩展

以 [`_template/extension.tsx.example`](_template/extension.tsx.example) 为起点（模板文件不参与构建），并遵循 [`docs/secondary-development.md`](../../../docs/secondary-development.md)。

## 不想重建宿主？用页面插件

若目标是把页面打包成产物、在运行中的面板上直接上传使用（本地 / Docker / 桌面端一致），走**页面插件**：用 [`frontend/page-pack-template`](../../page-pack-template) 打包单文件 ES 模块，在 设置 → 页面插件 上传，刷新生效。契约与边界见 [`docs/page-sdk.md`](../../../docs/page-sdk.md)。
