/**
 * 页面整体缩放 — 桌面客户端 (pywebview) 里没有浏览器缩放快捷键,
 * 由 设置→系统→界面缩放 控制。
 *
 * 实现: #root 上 transform: scale(z) + origin 0 0 + 宽高按 1/z 补偿。
 * 不用 CSS zoom: zoom≠1 时视觉坐标 (clientX/rect) 与布局坐标分离,
 * ECharts/zrender 的鼠标命中不做换算 (echarts#15103), 十字光标/tooltip
 * 会偏移约 (1-1/z); 而 transform 下浏览器规范对 offsetX 自动逆变换回
 * 元素本地坐标系, 图表与拖拽类库零改动即正确。
 *
 * - 值域 0.8–1.5, 步进 0.05, localStorage 持久化 (storage.pageZoom); 默认 1。
 * - JS 定位的浮层 (#root 内 fixed/absolute) 需把视觉坐标换算回布局坐标:
 *   统一用 getPageScale() 相除 (÷ scale)。
 * - index.html 在 #root 之后有预渲染内联脚本, 首帧绘制前应用, 无跳变。
 * - 应用后派发 window resize, echarts 等监听 resize 的图表随之重排。
 * - z=1 时清空 transform/宽高, 与未开启缩放的状态完全一致。
 */
import { storage } from '@/lib/storage'

export const ZOOM_MIN = 0.8
export const ZOOM_MAX = 1.5
export const ZOOM_STEP = 0.05

export const ZOOM_DEFAULT = 1

function clamp(v: number): number {
  if (!Number.isFinite(v)) return ZOOM_DEFAULT
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.round(v * 100) / 100))
}

/** 把缩放应用到 #root 并通知图表重排, 返回 clamp 后的实际值。 */
export function applyPageZoom(v: number): number {
  const z = clamp(v)
  const root = document.getElementById('root')
  if (root) {
    if (z === 1) {
      root.style.transform = ''
      root.style.width = ''
      root.style.height = ''
    } else {
      root.style.transformOrigin = '0 0'
      root.style.transform = `scale(${z})`
      // 布局占位 = 视口/z, 缩放后视觉正好铺满视口; 不补偿会留白 (z<1) 或溢出 (z>1)。
      root.style.width = `${100 / z}%`
      root.style.height = `${100 / z}%`
    }
  }
  window.dispatchEvent(new Event('resize'))
  return z
}

/** 当前生效的缩放 (读 #root transform 矩阵, 未缩放时 1)。
 *  浮层定位/拖拽做视觉→布局坐标换算时用。 */
export function getPageScale(): number {
  const root = document.getElementById('root')
  const m = root ? getComputedStyle(root).transform : ''
  if (m && m !== 'none') {
    try {
      const s = new DOMMatrixReadOnly(m).a
      if (Number.isFinite(s) && s > 0) return s
    } catch { /* matrix 解析失败按未缩放处理 */ }
  }
  return 1
}

/** 读取持久化的缩放值 (越界/缺失回落默认 1)。 */
export function getPageZoom(): number {
  return clamp(storage.pageZoom.get(ZOOM_DEFAULT))
}

/** 持久化并立即生效。 */
export function setPageZoom(v: number): number {
  const z = clamp(v)
  storage.pageZoom.set(z)
  return applyPageZoom(z)
}
