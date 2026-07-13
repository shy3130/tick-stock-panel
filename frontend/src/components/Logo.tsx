// Sycee 品牌标志: 银锭轮廓 + 上升行情折线。
// 轮廓对应品牌名的本义，折线表达量化研究；单色结构可在 24-40px 保持清晰。
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
      aria-label="Sycee"
    >
      {/* 银锭主体 */}
      <path
        d="M4.75 10.25 10 6.25c2.2 1.9 9.8 1.9 12 0l5.25 4c-1.15 6.65-2.65 12.15-6.05 14.25-2.8 1.7-7.6 1.7-10.4 0-3.4-2.1-4.9-7.6-6.05-14.25Z"
        fill="currentColor"
        fillOpacity="0.1"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinejoin="round"
      />
      {/* 锭口内沿，形成向内收束的银锭结构 */}
      <path
        d="M10.3 10.5c2.2 1.6 9.2 1.6 11.4 0"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
        strokeOpacity="0.42"
      />
      {/* 行情折线 */}
      <path
        d="m9.25 22 4.15-4.05 3.35 2.35 6.5-7.05"
        stroke="currentColor"
        strokeWidth="2.25"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <circle cx="23.25" cy="13.25" r="1.65" fill="currentColor" />
    </svg>
  )
}
