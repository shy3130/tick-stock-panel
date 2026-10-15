/**
 * tsp.ts — 宿主能力桥读取。
 *
 * window.__TSP_EXT__ 由宿主 main.tsx 在引导时安装; 页面插件经 mount(el, ctx)
 * 收到的 ctx 即本对象。api 为宿主完整 API 封装(同源 fetch, 自带登录态),
 * QK 为集中查询键, cn 为样式合并工具, format 为格式化工具集。
 *
 * 自包含说明: 页面插件产物自带 React/ReactDOM (由本模板的 vite 构建打入),
 * 与宿主不共享模块实例 —— 请勿 import 宿主内部模块, 只经由 ctx 取能力。
 */
type Bridge = {
  api: any
  QK: any
  cn: (...inputs: any[]) => string
  format: any
}

export const ctx = (window as unknown as { __TSP_EXT__: Bridge }).__TSP_EXT__
