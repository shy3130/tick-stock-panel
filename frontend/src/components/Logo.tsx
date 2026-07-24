// 原创 logo:方括号 [ ] 包裹一根带 wick 的 K 线
//
// 概念:
//   - 外层 brackets:终端 / 代码 / 引用边界 — 赛博 + quant 气质
//   - 中央 wick+body:一根标准 K 线 — 直接的金融指代
//   - body 偏上 + 下影长:bullish 站稳感 (上影短 / 下影长)
//
// 用 currentColor,继承父级 color 设定,方便切换品牌色。
interface LogoProps {
  className?: string
  size?: number
  style?: React.CSSProperties
}

export function Logo({ className, size = 32, style }: LogoProps) {
  return (
    <svg
      viewBox="0 0 32 32"
      width={size}
      height={size}
      fill="none"
      className={className}
      style={style}
      role="img"
      aria-label="TickFlow Stock Panel"
    >
      {/* 左方括号 */}
      <path
        d="M10 4 L4 4 L4 28 L10 28"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinejoin="miter"
        strokeLinecap="butt"
      />
      {/* 右方括号 */}
      <path
        d="M22 4 L28 4 L28 28 L22 28"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinejoin="miter"
        strokeLinecap="butt"
      />
      {/* K 线 wick(上下影线,半透明) */}
      <line
        x1="16" y1="7" x2="16" y2="25"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeOpacity="0.6"
      />
      {/* K 线 body — 偏上,上影短/下影长, bullish 站稳感 */}
      <rect
        x="13" y="9" width="6" height="10"
        fill="currentColor"
        rx="0.5"
      />
    </svg>
  )
}
