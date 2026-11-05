import type { Config } from 'tailwindcss'
import animate from 'tailwindcss-animate'

// 设计语言 §6.0:暗色为主 + 电光蓝强调 + 等宽数字
export default {
  darkMode: ['class'],
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    container: { center: true, padding: '1rem' },
    extend: {
      colors: {
        // §6.0.1 色板 — CSS variables 见 src/index.css
        // 注意: base 与字号 token 撞名, text-base 会生成 color 规则 (文字变背景色)。
        // 字号 16px 一律写 text-[16px] leading-6, 禁用 text-base。
        base:      'hsl(var(--base) / <alpha-value>)',
        surface:   'hsl(var(--surface) / <alpha-value>)',
        elevated:  'hsl(var(--elevated) / <alpha-value>)',
        border:    'hsl(var(--border) / <alpha-value>)',
        foreground: 'hsl(var(--fg-primary) / <alpha-value>)',
        secondary:  'hsl(var(--fg-secondary) / <alpha-value>)',
        muted:      'hsl(var(--fg-muted) / <alpha-value>)',
        accent:     'hsl(var(--accent) / <alpha-value>)',
        // A 股语义色:仅用于价格 / K 线,不用于 UI 状态
        bull:       'hsl(var(--bull) / <alpha-value>)',
        bear:       'hsl(var(--bear) / <alpha-value>)',
        warning:    'hsl(var(--warning) / <alpha-value>)',
        danger:     'hsl(var(--danger) / <alpha-value>)',
      },
      fontFamily: {
        sans: ['Inter', '"HarmonyOS Sans SC"', '"PingFang SC"', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', '"IBM Plex Mono"', 'ui-monospace', 'monospace'],
      },
      /* 字号整体 +1 档 (2026-11): 原始 token 各上调 1px, 行高同步 +2px 保持疏密。
       * 任意值写法 text-[11px] 的 +1 在 index.css「字号整体 +1 档」一节统一覆盖;
       * base 不在此列 — 与 colors.base 撞名, text-base 生成的是 color 规则。 */
      fontSize: {
        xs:   ['13px', '18px'],
        sm:   ['15px', '20px'],
        lg:   ['19px', '26px'],
        xl:   ['21px', '28px'],
        '2xl': ['25px', '32px'],
        '3xl': ['31px', '38px'],
      },
      borderRadius: {
        card: '8px',
        btn: '6px',
        input: '4px',
        dialog: '12px',
      },
      transitionTimingFunction: {
        // §6.0.4 Linear/Vercel 同款缓动
        smooth: 'cubic-bezier(0.16, 1, 0.3, 1)',
      },
    },
  },
  plugins: [animate],
} satisfies Config
