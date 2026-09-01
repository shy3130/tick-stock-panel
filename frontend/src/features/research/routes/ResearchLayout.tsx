import { Suspense } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import {
  Activity,
  BarChart3,
  Database,
  FlaskConical,
  History,
  LayoutDashboard,
  Loader2,
  Network,
  Target,
} from 'lucide-react'
import { cn } from '@/lib/cn'

const PRIMARY = [
  { to: '/research/overview', label: '总览', icon: LayoutDashboard },
  { to: '/research/factors', label: '因子目录', icon: FlaskConical },
  { to: '/research/runs', label: '运行中心', icon: History },
  { to: '/research/evidence', label: '证据', icon: FlaskConical },
  { to: '/research/data', label: '数据谱系', icon: Database },
  { to: '/research/automation', label: '自动化', icon: Activity },
] as const

const ANALYTICS = [
  { to: '/research/analytics/symbol', label: '个股分析', icon: BarChart3 },
  { to: '/research/analytics/signals', label: '信号记分卡', icon: Target },
  { to: '/research/analytics/cross-section', label: '横截面', icon: Network },
] as const

export function ResearchLayout() {
  return (
    <div className="workspace-page h-full min-h-0">
      <nav className="border-b border-border bg-surface/80" aria-label="研究工作台">
        <div className="workspace-toolbar overflow-x-auto px-3 py-2 sm:px-4">
          {PRIMARY.map((item) => (
            <SubLink key={item.to} {...item} />
          ))}
          <span className="hidden h-6 w-px bg-border md:block" aria-hidden />
          {ANALYTICS.map((item) => (
            <SubLink key={item.to} {...item} />
          ))}
        </div>
      </nav>
      <div className="min-h-0 min-w-0 flex-1 overflow-hidden">
        <Suspense fallback={<div className="grid min-h-[40vh] place-items-center text-xs text-muted" role="status"><Loader2 className="h-4 w-4 motion-safe:animate-spin" />加载研究页</div>}>
          <Outlet />
        </Suspense>
      </div>
    </div>
  )
}

function SubLink({ to, label, icon: Icon, end }: { to: string; label: string; icon: typeof FlaskConical; end?: boolean }) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        cn(
          'inline-flex min-h-11 shrink-0 items-center gap-1.5 rounded-btn px-3 text-xs font-medium transition-colors duration-fast ease-smooth',
          isActive ? 'bg-accent/15 text-accent' : 'text-secondary hover:bg-elevated/60 hover:text-foreground',
        )
      }
    >
      <Icon className="h-3.5 w-3.5" aria-hidden />
      {label}
    </NavLink>
  )
}
