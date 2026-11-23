# 页面插件模板 (Page Pack Template)

把一个页面打包成**单文件 ES 模块产物**（`dist/extension.js`），上传到运行中的面板（设置 → 导入与备份），刷新后即成为完整页面——自动注册路由与侧栏入口，可在菜单设置里排序/隐藏。本地 dev、Docker、桌面端一致生效，**宿主无需重新构建**。

## 打包

```bash
pnpm install
pnpm build        # → dist/extension.js
```

产物自包含（React 等依赖由本模板打进 bundle，不与宿主共享模块实例），典型体积 60–150KB，上限 5MB。

## 上传

打开「设置 → 导入与备份」，选择 `dist/extension.js`。核对页面标识、来源及同名替换提示，完成导入后刷新页面。通过「已安装扩展」查看状态，在「页面管理 → 菜单与页面」调整侧栏。

## 开发要点

- `src/extension.tsx` — 产物清单（id / routes / navigation）与示例页面；`mount(el, ctx)` 渲染进容器，返回清理函数。
- `src/tsp.ts` — 从 `window.__TSP_EXT__` 取宿主能力桥 `ctx`：`ctx.api`（宿主完整 API 封装）、`ctx.QK`、`ctx.cn`、`ctx.format`。
- 个股详情：`ctx.ui?.openStockPreview({ symbol: '159558.SZ', name, navList })`，关闭用 `ctx.ui?.closeStockPreview()`；模板已包含调用按钮。使用完整交易所后缀，旧宿主没有 `ui` 时检测后提示升级。
- **禁止** import 宿主内部模块（`@/lib/...` 在产物构建中不可解析）；数据请求一律经 `ctx.api`。
- 样式：宿主 Tailwind 按需编译，任意类名不保证存在——布局关键样式用内联样式，主题色用宿主 CSS 变量（如 `var(--accent)`）。

完整契约（apiVersion、图标白名单、路由规则、安全边界）见 [`docs/page-sdk.md`](../../docs/page-sdk.md)。

将模板交给 AI 时说明目标页面和宿主版本。验收以实际生成的单文件、导入预览和刷新后的路由为准。[AI 开发与验收](../../docs/ai-integration.md#开发并导入扩展)
