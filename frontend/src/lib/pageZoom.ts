/**
 * 页面整体缩放 — 桌面客户端 (pywebview) 里没有浏览器缩放快捷键,
 * 由 设置→系统→界面缩放 控制。CSS zoom 作用于 <html>, 全组件等比缩放。
 *
 * - 值域 0.8–1.5, 步进 0.05, localStorage 持久化 (storage.pageZoom)。
 * - index.html 的预渲染脚本会在首屏前应用, 避免加载后尺寸跳变。
 * - 应用后派发 window resize, echarts 等监听 resize 的图表随之重排。
 */
import { storage } from '@/lib/storage'

export const ZOOM_MIN = 0.8
export const ZOOM_MAX = 1.5
export const ZOOM_STEP = 0.05

function clamp(v: number): number {
  if (!Number.isFinite(v)) return 1
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.round(v * 100) / 100))
}

/** 把缩放应用到 <html> 并通知图表重排, 返回 clamp 后的实际值。 */
export function applyPageZoom(v: number): number {
  const z = clamp(v)
  document.documentElement.style.zoom = String(z)
  window.dispatchEvent(new Event('resize'))
  return z
}

/** 读取持久化的缩放值 (越界/缺失回落 1)。 */
export function getPageZoom(): number {
  return clamp(storage.pageZoom.get(1))
}

/** 持久化并立即生效。 */
export function setPageZoom(v: number): number {
  const z = clamp(v)
  storage.pageZoom.set(z)
  return applyPageZoom(z)
}
