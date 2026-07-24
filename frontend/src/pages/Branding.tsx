import { motion } from 'framer-motion'
import {
  RadioTower,
  Square,
  GitFork,
  Sparkles,
  Star,
  LineChart,
  ScanSearch,
  History,
  Signal as SignalIcon,
  Eye,
  FileText,
} from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'

interface Variant {
  id: string
  name: string
  tagline: string
  hint: string
  icon: React.ComponentType<{ className?: string }>
  iconAccent: string                  // tailwind text color
  nameClass: string                   // 文字本身样式(字号/字重/字距/字体)
  glow?: string                       // 名字下方的发光线条 hex
}

// 同一个名字 "TickFlow Stock Panel" 在 4 种风格语言里的呈现
// 长字符串自动用更小字号 + 更窄字距,免得撑爆卡片;但风格语言(字体/字重/配色/图标)保持不变
const VARIANTS: Variant[] = [
  {
    id: 'pulsar',
    name: 'TickFlow Stock Panel',
    tagline: 'CN · HK · US · SIGNAL TERMINAL',
    hint: '脉冲星、雷达波纹 — 青绿强调色,字重黑体,中等字距',
    icon: RadioTower,
    iconAccent: 'text-[#3DD68C]',
    nameClass: 'font-sans font-black text-base tracking-[0.10em]',
    glow: '#3DD68C',
  },
  {
    id: 'vanta',
    name: 'TickFlow Stock Panel',
    tagline: 'MARKET · INTELLIGENCE',
    hint: 'Vantablack — 纯白单色,字重最重,字距最宽,monochrome 高级感',
    icon: Square,
    iconAccent: 'text-[#FAFAFA]',
    nameClass: 'font-sans font-black text-base tracking-[0.18em]',
    glow: '#FAFAFA',
  },
  {
    id: 'helix',
    name: 'TickFlow Stock Panel',
    tagline: 'QUANT · TERMINAL',
    hint: 'DNA 螺旋 — 紫色强调,等宽字体,赛博朋克经典意象',
    icon: GitFork,
    iconAccent: 'text-[#8B5CF6]',
    nameClass: 'font-mono font-bold text-base tracking-[0.08em]',
    glow: '#8B5CF6',
  },
  {
    id: 'aurora',
    name: 'TickFlow Stock Panel',
    tagline: 'CN · HK · US · DASHBOARD',
    hint: '极光 — 青色强调,细字优雅,适中字距,与涨跌语义色不冲突',
    icon: Sparkles,
    iconAccent: 'text-[#22D3EE]',
    nameClass: 'font-sans font-light text-base tracking-[0.12em]',
    glow: '#22D3EE',
  },
]

const MOCK_NAV = [
  { icon: Star, label: '自选' },
  { icon: LineChart, label: 'K 线' },
  { icon: ScanSearch, label: '策略' },
  { icon: History, label: '回测' },
  { icon: SignalIcon, label: '信号' },
  { icon: Eye, label: '监控' },
  { icon: FileText, label: '财务分析' },
]

export function Branding() {
  return (
    <>
      <PageHeader
        title="视觉风格预览"
        subtitle="名字保持 TickFlow Stock Panel,4 种赛博朋克 + 高级感的视觉处理 — 字重、字距、配色、图标各不同。挑你最喜欢的告诉我。"
      />

      <div className="px-3 py-4 sm:px-5 lg:px-8 lg:py-6">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
          {VARIANTS.map((v) => (
            <Sample key={v.id} v={v} />
          ))}
        </div>

        <div className="mt-8 rounded-card border border-border bg-surface p-5 text-sm text-secondary leading-relaxed max-w-2xl">
          <div className="font-medium text-foreground mb-2">挑哪个?</div>
          回复 <code className="font-mono text-accent">pulsar / vanta / helix / aurora</code> 任一,
          我把该风格的字体、配色、图标、发光效果应用到真实侧栏。
          也可以告诉我你想微调哪里(比如"用 VANTA 但换青色"),都行。
        </div>
      </div>
    </>
  )
}

function Sample({ v }: { v: Variant }) {
  const Icon = v.icon

  return (
    <motion.article
      aria-label={`${v.id} 视觉预览`}
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
      className="flex min-w-0 flex-col overflow-hidden rounded-card border border-border bg-base xl:flex-row"
    >
      {/* 模拟侧边栏 */}
      <div className="flex w-full shrink-0 flex-col border-b border-border bg-surface xl:w-56 xl:border-b-0 xl:border-r">
        {/* Logo 区 */}
        <div className="px-5 py-5 border-b border-border">
          <div className="flex items-center gap-2.5">
            <div
              className="grid place-items-center h-7 w-7 rounded-md"
              style={{
                background: `${v.glow}1a`,
                boxShadow: `0 0 12px ${v.glow}33`,
              }}
            >
              <Icon className={`h-4 w-4 ${v.iconAccent}`} />
            </div>
            <div className={`${v.nameClass} text-foreground leading-none`}>
              {v.name}
            </div>
          </div>
          <div className="mt-2 text-[10px] uppercase tracking-[0.18em] text-secondary">
            {v.tagline}
          </div>
          <div
            className="mt-3 h-px"
            style={{
              background: `linear-gradient(90deg, ${v.glow}66, transparent)`,
            }}
          />
          <div className="mt-2 text-xs text-secondary">
            档位 · <span className="text-foreground font-medium font-mono">Pro</span>
          </div>
        </div>

        {/* 模拟导航 */}
        <nav className="px-2 py-3 space-y-0.5">
          {MOCK_NAV.slice(0, 5).map(({ icon: I, label }, i) => (
            <div
              key={label}
              className={`flex items-center gap-3 px-3 py-2 rounded-btn text-sm ${
                i === 0
                  ? 'bg-elevated text-foreground font-medium'
                  : 'text-foreground/80'
              }`}
            >
              <I className="h-4 w-4" />
              {label}
            </div>
          ))}
        </nav>
      </div>

      {/* 右侧说明 + 大字预览 */}
      <div className="flex min-w-0 flex-1 flex-col p-5">
        <div className="flex-1">
          <div className="text-xs font-medium text-muted uppercase tracking-widest">{v.id}</div>
          <div className="mt-2 leading-relaxed text-sm text-secondary">
            {v.hint}
          </div>
        </div>

        {/* 大字 wordmark 预览 */}
        <div className="mt-6 pt-6 border-t border-border">
          <div
            className={`${v.nameClass} text-foreground`}
            style={{
              textShadow: `0 0 24px ${v.glow}55`,
            }}
          >
            {v.name}
          </div>
          <div className="mt-1.5 text-[10px] uppercase tracking-[0.2em] text-secondary">
            {v.tagline}
          </div>
        </div>

        {/* 模拟一个数据卡片,看与配色协调度 */}
        <div className="mt-5 rounded-btn bg-surface border border-border px-3 py-2 flex items-baseline justify-between">
          <span className="text-xs text-secondary">600519.SH</span>
          <span className="font-mono text-sm" style={{ color: v.glow }}>
            +1.85%
          </span>
        </div>
      </div>
    </motion.article>
  )
}
