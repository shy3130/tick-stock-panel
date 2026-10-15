/**
 * 运行时页面插件加载 — 从后端拉取已启用清单, 在启动引导窗口动态 import。
 *
 * 时机关键: 必须在 initializeFrontendExtensions() 之后、import('./router')
 * 之前调用(main.tsx 的 bootstrap)。router 模块求值时会 finalize 注册表
 * 并以冻结快照建路由表, 错过窗口的页面插件只能等下次刷新生效。
 *
 * fail-open: 后端不可达 / 未登录(401) / 超时一律静默跳过 —— 页面插件是
 * 增强能力, 不得拖慢或阻断宿主启动; 单个包 import 失败由注册表记入
 * loadErrors, 不影响其他包与静态扩展。
 *
 * 契约(v1 自包含包): 页面插件自带 React/路由(由 SDK 模板封装打包), 宿主不
 * 与其共享模块实例 —— 页面通过 mount(el, ctx) 挂到宿主提供的容器里,
 * ctx 暴露宿主 api/QK/cn/format 能力桥。这与源码级扩展的 component 契约
 * 不同: 此处由 packToExtension 把 mount 包装成宿主 React 组件再交给注册
 * 表, 因此路由注册、冲突检测、错误隔离全部复用源码扩展的既有机制。
 * 后续若升级为 import map 共享实例, 只需改 SDK 与本文件, 已发布页面插件
 * 源码不受影响。
 */
import { useEffect, useRef, type ComponentType } from 'react'
import type { LucideIcon } from 'lucide-react'
import type { ClassValue } from 'clsx'
import { loadFrontendExtensions } from './registry'
import type { FrontendExtension, FrontendExtensionModule } from './types'

/** mount 挂载上下文: 宿主能力桥(window.__TSP_EXT__ 安装) + 页面卸载通知 */
export interface PagePackCtx {
  api: unknown
  QK: unknown
  cn: (...inputs: ClassValue[]) => string
  format: unknown
}

export interface PagePackRoute {
  id: string
  path: `/${string}`
  /** 挂载到容器; 返回清理函数则在卸载时调用 */
  mount: (el: HTMLElement, ctx: PagePackCtx) => void | (() => void)
}

export interface PagePackNavigation {
  id: string
  routeId: string
  label: string
  /** lucide 图标名(见 ICON_ALLOWLIST), 缺省 Sparkles */
  icon?: string
}

/** 页面插件产物 default 导出契约 — apiVersion 与源码扩展同源治理 */
export interface PagePackExtension {
  id: string
  apiVersion: 1
  routes: PagePackRoute[]
  navigation: PagePackNavigation[]
}

export interface RuntimePageMeta {
  id: string
  enabled: boolean
  label?: string
  path?: string
  error?: string
}

/** 产物分发 URL: 同源相对路径, dev(vite 代理)/生产(后端直挂)/桌面端一致。 */
export function runtimeBundleUrl(id: string): string {
  return `/api/ext-pages/${id}/bundle.js`
}

// ── 图标名白名单: 控制宿主侧 lucide 引入面(全量引入会膨胀主包) ──
import { Activity, BarChart3, Bell, BookOpen, Brain, Calendar, Database, Globe, LineChart, PieChart, Shield, Sparkles, Star, Table, TrendingUp, Zap } from 'lucide-react'
const ICON_ALLOWLIST: Record<string, LucideIcon> = {
  Activity, BarChart3, Bell, BookOpen, Brain, Calendar, Database, Globe, LineChart, PieChart, Shield, Sparkles, Star, Table, TrendingUp, Zap,
}

function resolvePackIcon(name: string | undefined): LucideIcon {
  return (name && ICON_ALLOWLIST[name]) || Sparkles
}

/** mount 包装为宿主 React 组件: 容器 div + 生命周期桥接 */
function createMountComponent(mount: PagePackRoute['mount']): ComponentType {
  function RuntimePackPage() {
    const ref = useRef<HTMLDivElement>(null)
    useEffect(() => {
      const el = ref.current
      if (!el) return
      const bridge = (window as unknown as { __TSP_EXT__?: PagePackCtx }).__TSP_EXT__
      if (!bridge) return
      return mount(el, bridge)
    }, [])
    return <div ref={ref} className="min-h-full" />
  }
  return RuntimePackPage
}

/**
 * 页面插件清单 → 注册表扩展。形态问题(缺字段/版本不符/路径非法)在此抛出,
 * 经 loader 传给 loadFrontendExtensions 记入 loadErrors —— 与源码扩展
 * 的失败隔离同一出口。
 */
export function packToExtension(pack: unknown): FrontendExtension {
  if (!pack || typeof pack !== 'object') throw new Error('页面插件必须 default 导出清单对象')
  const p = pack as Partial<PagePackExtension>
  if (p.apiVersion !== 1) throw new Error(`apiVersion 必须为 1, 收到 ${String(p.apiVersion)}`)
  if (typeof p.id !== 'string' || !p.id.includes('.')) throw new Error('id 必须是带命名空间的字符串 (如 demo.hello)')
  if (!Array.isArray(p.routes) || p.routes.length === 0) throw new Error('routes 不能为空')
  if (!Array.isArray(p.navigation) || p.navigation.length === 0) throw new Error('navigation 不能为空')
  for (const r of p.routes as PagePackRoute[]) {
    if (typeof r?.id !== 'string' || typeof r?.path !== 'string' || !r.path.startsWith('/') || r.path === '/') {
      throw new Error(`路由 ${r?.id} 的 path 必须是非根绝对路径`)
    }
    if (typeof r?.mount !== 'function') throw new Error(`路由 ${r.id} 缺少 mount(el, ctx) 函数`)
  }
  return {
    id: p.id,
    apiVersion: 1,
    routes: (p.routes as PagePackRoute[]).map(r => ({
      id: r.id,
      path: r.path,
      component: createMountComponent(r.mount),
    })),
    navigation: (p.navigation as PagePackNavigation[]).map(n => ({
      id: n.id,
      routeId: n.routeId,
      label: n.label,
      icon: resolvePackIcon(n.icon),
    })),
  }
}

async function defaultFetchList(): Promise<RuntimePageMeta[]> {
  const res = await fetch('/api/ext-pages')
  if (!res.ok) throw new Error(`ext-pages ${res.status}`)
  return res.json() as Promise<RuntimePageMeta[]>
}

function withTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('timeout')), ms)
    p.then(
      (v) => { clearTimeout(timer); resolve(v) },
      (e) => { clearTimeout(timer); reject(e) },
    )
  })
}

export interface LoadRuntimeOptions {
  fetchList?: () => Promise<RuntimePageMeta[]>
  /** 动态 import 注入点(测试替换); 生产走浏览器原生 import */
  importModule?: (url: string) => Promise<{ default: unknown }>
  /** 注册注入点(测试替换) */
  loadInto?: (modules: Record<string, () => Promise<FrontendExtensionModule>>) => Promise<void>
  timeoutMs?: number
}

export async function loadRuntimePageExtensions(opts: LoadRuntimeOptions = {}): Promise<void> {
  const { fetchList = defaultFetchList, importModule, loadInto = loadFrontendExtensions, timeoutMs = 3000 } = opts
  let list: RuntimePageMeta[]
  try {
    list = await withTimeout(fetchList(), timeoutMs)
  } catch {
    return // fail-open: 静默跳过, 不影响启动
  }
  const loaders: Record<string, () => Promise<FrontendExtensionModule>> = {}
  for (const page of list) {
    if (!page?.id || page.enabled !== true) continue
    const url = runtimeBundleUrl(page.id)
    loaders[`runtime:${page.id}`] = () => {
      const impl = importModule ? importModule(url) : import(/* @vite-ignore */ url)
      return impl.then(m => ({ default: packToExtension(m.default) }))
    }
  }
  if (Object.keys(loaders).length === 0) return
  try {
    await loadInto(loaders)
  } catch {
    // loadFrontendExtensions 自身按源记录错误不会抛; 兜底防意外拖垮启动
  }
}

/**
 * 宿主应用层能力桥: 页面插件经 ctx 读取宿主 api/QK/cn/format。
 * 安装在 main.tsx 引导早期, 生命周期与页面一致。
 */
export function installTspExtBridge(bridge: PagePackCtx): void {
  ;(window as unknown as { __TSP_EXT__?: PagePackCtx }).__TSP_EXT__ = bridge
}
