# 页面插件模板 (Page Pack Template)

把一个页面打包成**单文件 ES 模块产物**（`dist/extension.js`），上传到运行中的面板（设置 → 页面插件），刷新后即成为完整页面——自动注册路由与侧栏入口，可在菜单设置里排序/隐藏。本地 dev、Docker、桌面端一致生效，**宿主无需重新构建**。

## 打包

```bash
pnpm install
pnpm build        # → dist/extension.js
```

产物自包含（React 等依赖由本模板打进 bundle，不与宿主共享模块实例），典型体积 60–150KB，上限 5MB。

## 上传

面板 **设置 → 页面插件**：填 ID（字母/数字/下划线）→ 选择 `dist/extension.js` → 上传并校验（即时试载，清单不合法或路由冲突会被拒绝并显示原因）→ 启用 → 刷新页面。

## 开发要点

- `src/extension.tsx` — 产物清单（id / routes / navigation）与示例页面；`mount(el, ctx)` 渲染进容器，返回清理函数。
- `src/tsp.ts` — 从 `window.__TSP_EXT__` 取宿主能力桥 `ctx`：`ctx.api`（宿主完整 API 封装）、`ctx.QK`、`ctx.cn`、`ctx.format`。
- **禁止** import 宿主内部模块（`@/lib/...` 在产物构建中不可解析）；数据请求一律经 `ctx.api`。
- 样式：宿主 Tailwind 按需编译，任意类名不保证存在——布局关键样式用内联样式，主题色用宿主 CSS 变量（如 `var(--accent)`）。

完整契约（apiVersion、图标白名单、路由规则、安全边界）见 [`docs/page-sdk.md`](../../docs/page-sdk.md)。
