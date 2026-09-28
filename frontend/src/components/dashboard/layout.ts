/**
 * 看板自定义布局 — 布局数据模型与规范化。
 *
 * 布局以 12 列网格存储(x/y/w/h 均为网格单元), 由 react-grid-layout 渲染;
 * 持久化 blob 存 preferences.dashboard_layout, 未自定义时使用内置默认布局。
 * normalizeLayout 是外部数据(后端/旧版本)进入渲染前的唯一入口:
 * 越界坐标 clamp、未知组件剔除、内置组件去重、外链 URL 消毒。
 */

export const GRID_COLS = 12
/** 单行高度(px); 实际步进 = rowHeight + margin(6) */
export const GRID_ROW_HEIGHT = 32
export const GRID_MARGIN = 6
export const MAX_ITEMS = 40
/** 单个组件高度上限(网格行), 防误操作拉出超长条 */
export const MAX_H = 30

export type WidgetType =
  | 'indices' | 'kpi' | 'distribution' | 'radar' | 'trend'
  | 'concept-rank' | 'industry-rank'
  | 'gainers' | 'losers' | 'turnover' | 'activity'
  | 'ladder' | 'monitor' | 'ext-link'

export interface DashboardItem {
  /** 实例 id: 内置组件 = type; 外部链接 = ext-link-<seq> */
  i: string
  t: WidgetType
  x: number
  y: number
  w: number
  h: number
  /** 外部链接组件参数: { title, url } */
  p?: Record<string, string>
}

export interface DashboardLayoutBlob {
  v: 1
  items: DashboardItem[]
}

/** 外链 URL 消毒: 仅 http(s)、≤500 字符、拒绝与本站同源(防同源 iframe 借道 sandbox 提权)。 */
export function sanitizeExtUrl(raw: string): string | null {
  const s = (raw ?? '').trim()
  if (!s || s.length > 500) return null
  let u: URL
  try {
    u = new URL(s)
  } catch {
    return null
  }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return null
  if (typeof window !== 'undefined' && u.origin === window.location.origin) return null
  return u.toString()
}

function num(v: unknown, fallback: number): number {
  return typeof v === 'number' && Number.isFinite(v) ? Math.round(v) : fallback
}

/**
 * 规范化布局 blob → 可渲染 items。
 * 无效输入(空/损坏/全被剔除)回退默认布局, 保证看板永远可用。
 */
export function normalizeLayout(
  blob: unknown,
  defaults: DashboardItem[],
  isKnownType: (t: string) => boolean,
  minSizeOf: (t: WidgetType) => { w: number; h: number },
): DashboardItem[] {
  if (typeof window !== 'undefined' && blob === null) return cloneItems(defaults)
  if (typeof blob !== 'object' || blob === null) return cloneItems(defaults)
  const raw = blob as { v?: unknown; items?: unknown }
  if (raw.v !== 1 || !Array.isArray(raw.items) || raw.items.length === 0) return cloneItems(defaults)

  const seenIds = new Set<string>()
  const seenTypes = new Set<string>()
  const out: DashboardItem[] = []
  for (const it of raw.items.slice(0, MAX_ITEMS)) {
    if (typeof it !== 'object' || it === null) continue
    const r = it as Record<string, unknown>
    const t = typeof r.t === 'string' ? r.t : ''
    if (!isKnownType(t)) continue
    // 内置组件单实例: 重复类型只保留首个
    if (t !== 'ext-link' && seenTypes.has(t)) continue
    const i = typeof r.i === 'string' && r.i ? r.i : (t === 'ext-link' ? `ext-link-${out.length}` : t)
    if (seenIds.has(i)) continue
    seenIds.add(i)
    seenTypes.add(t)
    const min = minSizeOf(t as WidgetType)
    let x = num(r.x, 0)
    let w = Math.min(Math.max(num(r.w, min.w), min.w), GRID_COLS)
    let h = Math.min(Math.max(num(r.h, min.h), min.h), MAX_H)
    x = Math.min(Math.max(x, 0), GRID_COLS - w)
    const y = Math.max(num(r.y, 0), 0)
    let item: DashboardItem = { i, t: t as WidgetType, x, y, w, h }
    if (t === 'ext-link') {
      const p = (typeof r.p === 'object' && r.p !== null ? r.p : undefined) as Record<string, unknown> | undefined
      const url = typeof p?.url === 'string' ? sanitizeExtUrl(p.url) : null
      if (!url) continue // 外链缺合法 URL → 整项剔除
      const title = typeof p?.title === 'string' && p.title.trim() ? p.title.trim().slice(0, 30) : new URL(url).hostname
      item = { ...item, p: { title, url } }
    }
    out.push(item)
  }
  // 已保存 blob 可能含重叠坐标(旧版本残留/异常保存), 渲染前消解, 避免组件互相压盖
  return out.length > 0 ? compactLayout(out) : cloneItems(defaults)
}

export function cloneItems(items: DashboardItem[]): DashboardItem[] {
  return items.map(it => ({ ...it, p: it.p ? { ...it.p } : undefined }))
}

/**
 * 碰撞消解 — 对含重叠坐标的布局做兜底(异常保存的 blob / 拖拽回传边界情况)。
 * 按 (y,x) 行序放置, 与已放置项碰撞者整体下沉到首个不冲突行:
 * 只下沉不上浮, 用户刻意留的空隙不被吞掉。返回顺序与入参一致。
 */
export function compactLayout<T extends { i: string; x: number; y: number; w: number; h: number }>(
  items: T[],
): T[] {
  const placed: Array<{ x: number; y: number; w: number; h: number }> = []
  const settled = [...items]
    .sort((a, b) => a.y - b.y || a.x - b.x)
    .map((it) => {
      let y = it.y
      const hits = (yy: number) => placed.some((p) =>
        !(p.x + p.w <= it.x || p.x >= it.x + it.w || p.y + p.h <= yy || p.y >= yy + it.h))
      while (hits(y)) y += 1
      placed.push({ x: it.x, y, w: it.w, h: it.h })
      return { ...it, y }
    })
  const order = new Map(items.map((it, idx) => [it.i, idx]))
  return settled.sort((a, b) => (order.get(a.i) ?? 0) - (order.get(b.i) ?? 0))
}

/** items → 持久化 blob */
export function toBlob(items: DashboardItem[]): DashboardLayoutBlob {
  return { v: 1, items: cloneItems(items) }
}
