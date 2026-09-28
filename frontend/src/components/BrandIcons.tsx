// TSP 品牌图标族 — 侧栏菜单专用
//
// 设计语言与 brand/logo-proposals.html「图标系统」一致:
//   24 网格 / 2px 等宽线幅 / 全直角 (butt 笔帽 + miter 转角) / 方括号与 K 线 DNA
// 描边用 currentColor, 跟随菜单项文字色 (active=accent, 默认=foreground/60)。
// 组件签名兼容 lucide 用法: 接受 className / size / 其余 SVG props。
import type { ReactNode, SVGProps } from 'react'

export type BrandIconProps = SVGProps<SVGSVGElement> & { size?: number | string }

function make(children: ReactNode) {
  return function BrandIcon({ size = 24, ...rest }: BrandIconProps) {
    return (
      <svg
        viewBox="0 0 24 24"
        width={size}
        height={size}
        fill="none"
        stroke="currentColor"
        strokeWidth={2}
        strokeLinecap="butt"
        strokeLinejoin="miter"
        aria-hidden="true"
        {...rest}
      >
        {children}
      </svg>
    )
  }
}

/** 看板 — 2×2 工作台面板, 前板内置 mini K 线 */
export const IconDashboard = make(
  <>
    <rect x="3" y="3" width="7.5" height="7.5" />
    <rect x="13.5" y="3" width="7.5" height="7.5" />
    <rect x="3" y="13.5" width="7.5" height="7.5" />
    <rect x="13.5" y="13.5" width="7.5" height="7.5" />
    <line x1="17.25" y1="15.2" x2="17.25" y2="19.4" strokeWidth="1.5" strokeOpacity="0.75" />
    <rect x="15.6" y="16.2" width="3.3" height="2.9" fill="currentColor" stroke="none" />
  </>,
)

/** 自选 — 方括号 + 收藏星 */
export const IconWatchlist = make(
  <>
    <path d="M9 4H5V20H9" />
    <path d="M15 4H19V20H15" />
    <path d="M12 7.6L13.3 10.9L16.8 11.7L13.3 12.5L12 15.8L10.7 12.5L7.2 11.7L10.7 10.9Z" fill="currentColor" stroke="none" />
  </>,
)

/** 策略 — 直角漏斗 (筛选/选股策略) */
export const IconStrategy = make(
  <path d="M4 5H20L14 12.2V19.2L10 17.2V12.2Z" />,
)

/** 因子 — 散点 + 上升趋势线 */
export const IconFactors = make(
  <>
    <path d="M4.5 18.5L19.5 8" strokeWidth="1.75" strokeOpacity="0.5" />
    <rect x="5" y="14.6" width="2.4" height="2.4" fill="currentColor" stroke="none" />
    <rect x="9.3" y="12.2" width="2.4" height="2.4" fill="currentColor" stroke="none" />
    <rect x="13.2" y="13" width="2.4" height="2.4" fill="currentColor" stroke="none" />
    <rect x="16.6" y="7.6" width="2.4" height="2.4" fill="currentColor" stroke="none" />
  </>,
)

/** 回测 — K 线立柱 + 倒放双三角 (把行情倒回去重放) */
export const IconBacktest = make(
  <>
    <line x1="5.5" y1="5" x2="5.5" y2="19" strokeWidth="1.75" strokeOpacity="0.7" />
    <rect x="4" y="10" width="3" height="6.5" fill="currentColor" stroke="none" />
    <path d="M13.5 8L9 12L13.5 16Z" fill="currentColor" stroke="none" />
    <path d="M19.5 8L15 12L19.5 16Z" fill="currentColor" stroke="none" />
  </>,
)

/** 个股分析 — 取景角锁定单根 K 线 */
export const IconStockFocus = make(
  <>
    <path d="M4 9V4H9" />
    <path d="M15 4H20V9" />
    <path d="M20 15V20H15" />
    <path d="M9 20H4V15" />
    <line x1="12" y1="8" x2="12" y2="16" strokeWidth="1.75" strokeOpacity="0.75" />
    <rect x="9.8" y="10.2" width="4.4" height="4" fill="currentColor" stroke="none" />
  </>,
)

/** 连板梯队 — 逐级抬升的阶梯 */
export const IconLadder = make(
  <path d="M3.5 20.5H8.5V15H13.5V9.5H18.5V4H21" />,
)

/** 概念分析 — 板块标签 */
export const IconConcept = make(
  <>
    <path d="M4 4H11.5L20 12.5L12.5 20L4 11.5Z" />
    <rect x="7" y="7" width="2.2" height="2.2" fill="currentColor" stroke="none" />
  </>,
)

/** 行业分析 — 直角立柱大厦 (sector/行业) */
export const IconIndustry = make(
  <>
    <path d="M4 9.5L12 4.5L20 9.5" />
    <path d="M7 12.5V17.5" />
    <path d="M12 12.5V17.5" />
    <path d="M17 12.5V17.5" />
    <path d="M4 20H20" />
  </>,
)

/** 财务分析 — 报表文档 + 内页走势 */
export const IconFinancials = make(
  <>
    <path d="M6 3H14L18 7V21H6Z" />
    <path d="M14 3V7H18" />
    <path d="M9 16.5L11.5 13.8L13 15.2L15.5 12.2" strokeWidth="1.75" />
  </>,
)

/** 监控中心 — 取景角 + 盘中脉冲线 */
export const IconMonitor = make(
  <>
    <path d="M4 9V4H9" />
    <path d="M15 4H20V9" />
    <path d="M20 15V20H15" />
    <path d="M9 20H4V15" />
    <path d="M7.2 13.6H9.6L11.6 9.9L13.8 15.9L15.8 12H16.8" strokeWidth="1.9" />
  </>,
)

/** 市场环境 — 角度量规 (弧线以直线段逼近, 保持直角语言) */
export const IconRegime = make(
  <>
    <path d="M3.5 16.5L6.8 10L12 7.2L17.2 10L20.5 16.5" />
    <path d="M12 16.5L15.2 10.8" />
    <rect x="11.1" y="15.4" width="1.8" height="1.8" fill="currentColor" stroke="none" />
  </>,
)

/** 异动监控 — 取景角 + 感叹号 [!] */
export const IconAlert = make(
  <>
    <path d="M4 9V4H9" />
    <path d="M15 4H20V9" />
    <path d="M20 15V20H15" />
    <path d="M9 20H4V15" />
    <path d="M12 7.5V13" strokeWidth="2.2" />
    <rect x="11.1" y="15.4" width="1.8" height="1.8" fill="currentColor" stroke="none" />
  </>,
)

/** 持仓提醒 — 仓位箱 + mini K 线 */
export const IconLots = make(
  <>
    <rect x="4" y="8.5" width="16" height="11" />
    <path d="M9.5 8.5V5.5H14.5V8.5" />
    <line x1="12" y1="11.6" x2="12" y2="16.6" strokeWidth="1.5" strokeOpacity="0.75" />
    <rect x="10.5" y="13" width="3" height="2.5" fill="currentColor" stroke="none" />
  </>,
)

/** 模拟盘 — 钱包 */
export const IconPaper = make(
  <>
    <rect x="3.5" y="6.5" width="17" height="13" />
    <path d="M6.5 6.5V4.5H17.5V6.5" />
    <rect x="15" y="11.5" width="3.2" height="3.2" fill="currentColor" stroke="none" />
  </>,
)

/** 信号库 — 信号脉冲线 */
export const IconSignals = make(
  <path d="M3 14.5H8L10.8 9L14 18L16.8 12.2H21" />,
)

/** 复盘 — 摊开的两页行情账本 */
export const IconReview = make(
  <>
    <rect x="4" y="4.5" width="7.75" height="15" />
    <rect x="12.25" y="4.5" width="7.75" height="15" />
    <path d="M12 3.5V20.5" strokeWidth="1.75" strokeOpacity="0.75" />
  </>,
)

/** 指数 — 三根抬升的指数柱 */
export const IconIndices = make(
  <>
    <rect x="4.5" y="13" width="4" height="7" fill="currentColor" stroke="none" />
    <rect x="10" y="8.5" width="4" height="11.5" fill="currentColor" stroke="none" />
    <rect x="15.5" y="4" width="4" height="16" fill="currentColor" stroke="none" />
  </>,
)

/** 数据 — 三层数据存储 */
export const IconData = make(
  <>
    <rect x="4.5" y="3.5" width="15" height="5" />
    <rect x="4.5" y="9.5" width="15" height="5" />
    <rect x="4.5" y="15.5" width="15" height="5" />
  </>,
)
