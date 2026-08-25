import type { Config } from 'tailwindcss'
import animate from 'tailwindcss-animate'

// Quant Research Workbench: dark-first, blue accent, system fonts, tabular nums
export default {
  darkMode: ['class'],
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    container: { center: true, padding: '1rem' },
    extend: {
      colors: {
        // Semantic palette — CSS variables in src/index.css
        base:      'hsl(var(--base) / <alpha-value>)',
        surface:   'hsl(var(--surface) / <alpha-value>)',
        elevated:  'hsl(var(--elevated) / <alpha-value>)',
        border:    'hsl(var(--border) / <alpha-value>)',
        foreground: 'hsl(var(--fg-primary) / <alpha-value>)',
        secondary:  'hsl(var(--fg-secondary) / <alpha-value>)',
        muted:      'hsl(var(--fg-muted) / <alpha-value>)',
        accent:     'hsl(var(--accent) / <alpha-value>)',
        // A-share semantics: price / candles only — not UI chrome
        bull:       'hsl(var(--bull) / <alpha-value>)',
        bear:       'hsl(var(--bear) / <alpha-value>)',
        warning:    'hsl(var(--warning) / <alpha-value>)',
        danger:     'hsl(var(--danger) / <alpha-value>)',
        success:    'hsl(var(--success) / <alpha-value>)',
        info:       'hsl(var(--info) / <alpha-value>)',
      },
      fontFamily: {
        // Reliable system stack — no remote font dependency
        sans: [
          'ui-sans-serif',
          'system-ui',
          '-apple-system',
          'BlinkMacSystemFont',
          '"Segoe UI"',
          '"PingFang SC"',
          '"Hiragino Sans GB"',
          '"Microsoft YaHei"',
          '"Noto Sans SC"',
          '"Helvetica Neue"',
          'Arial',
          'sans-serif',
        ],
        mono: [
          'ui-monospace',
          'SFMono-Regular',
          'Menlo',
          'Monaco',
          'Consolas',
          '"Liberation Mono"',
          '"Courier New"',
          'monospace',
        ],
      },
      borderRadius: {
        card: '8px',
        btn: '6px',
        input: '4px',
        dialog: '12px',
        panel: '8px',
      },
      spacing: {
        // 4px grid anchors used by workbench chrome
        control: '32px',
      },
      transitionDuration: {
        fast: '150ms',
        base: '180ms',
        slow: '220ms',
      },
      transitionTimingFunction: {
        smooth: 'cubic-bezier(0.16, 1, 0.3, 1)',
      },
    },
  },
  plugins: [animate],
} satisfies Config
