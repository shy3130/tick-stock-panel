# 页面插件开发指南 (Page Pack SDK v1)

页面插件是**运行时可挂载的页面产物**：把页面打包成单文件 ES 模块（`extension.js`），在「设置 → 页面插件」上传，刷新后即成为完整页面——自动注册路由、侧栏入口，可在菜单设置里排序/隐藏。本地 dev、生产构建、桌面端三种运行形态一致生效，宿主无需重新构建。

## 快速开始

```bash
cp -r frontend/page-pack-template my-pack && cd my-pack
pnpm install && pnpm build      # → dist/extension.js
```

打开宿主「设置 → 页面插件」→ 填 ID（字母/数字/下划线）→ 选择 `dist/extension.js` → 上传并校验 → 刷新页面。

## 产物契约 (apiVersion 1)

产物必须 `export default` 一个清单对象：

```tsx
const extension = {
  id: 'demo.hello',            // 带命名空间且全局唯一; 冲突时整个包被拒
  apiVersion: 1 as const,
  routes: [{
    id: 'hello',
    path: '/demo-hello',       // 非根绝对路径、静态(不支持 :param/通配), 不得覆盖核心或已有页面
    mount: (el, ctx) => {      // 渲染进容器; 返回清理函数(可选)
      const root = createRoot(el)
      root.render(<HelloPage />)
      return () => root.unmount()
    },
  }],
  navigation: [{
    id: 'hello', routeId: 'hello', label: '页面插件演示', icon: 'Sparkles',
  }],
}
export default extension
```

校验与冲突规则由宿主注册表统一执行（与源码级扩展 `frontend/src/custom/` 同一机制）；上传时即时试载校验，启动时再次校验，失败会停用该包并在面板显示原因。

## 能力桥 ctx

页面插件**自包含**（React 等依赖由模板打进产物，与宿主不共享模块实例），宿主能力经 `ctx` 暴露：

| 能力 | 说明 |
| --- | --- |
| `ctx.api` | 宿主完整 API 封装（同源 fetch、自动携带登录态、统一错误处理） |
| `ctx.QK` | 集中查询键（若你自行使用 TanStack Query 可作 key 规范） |
| `ctx.cn` | 样式类合并工具（clsx + tailwind-merge） |
| `ctx.format` | 数值/日期格式化工具集 |

**禁止** import 宿主内部模块（`@/lib/...` 等别名在产物构建中不可解析）；数据请求一律经 `ctx.api`，不得自行拼后端 URL 绕过封装。

## 图标白名单

`navigation[].icon` 按名称解析，白名单：`Activity BarChart3 Bell BookOpen Brain Calendar Database Globe LineChart PieChart Shield Sparkles Star Table TrendingUp Zap`（缺省 `Sparkles`）。需要扩充时在 `frontend/src/extensions/runtime.tsx` 的 `ICON_ALLOWLIST` 增补。

## 约束与边界

- **样式**：宿主 Tailwind 按需编译，任意类名不保证存在——布局关键样式用内联样式；主题色用宿主 CSS 变量（如 `var(--accent)`）。
- **路由集成**：页面挂在宿主路由容器内（`/你的路径` 可直达、可刷新），但包内不共享宿主 Router 上下文——需要跳转宿主页面时用 `window.location` 或自行包 `MemoryRouter`。
- **生效方式**：上传/启停后**刷新页面生效**（挂载发生在启动引导窗口），不做热插拔。
- **大小**：上限 5MB/包；自包含 React 后典型产物 60–150KB（minify + 可自行加 brotli/gzip 由后端协商）。
- **安全**：页面插件是任意代码，与登录会话同权运行——只上传自己或可信来源的产物；这是单用户产品的既定边界，第三方插件沙箱不在 v1 范围。
- **版本**：宿主升级破坏契约时提升 `apiVersion` 并保留旧版兼容期；产物内 `apiVersion` 不匹配会被明确拒绝。

## 与其他扩展方式的取舍

| 方式 | 适用 | 见 |
| --- | --- | --- |
| 页面插件（运行时产物） | 免重建挂载、独立分发 | 本文档 |
| 源码级扩展 `frontend/src/custom/<ns>/extension.tsx` | 随宿主一起构建维护、可用全部核心模块 | `docs/secondary-development.md` §3 |
| 声明式分析页面（零代码） | 扩展数据 + 模板化列表/分组页 | 设置 → 扩展页面 |
