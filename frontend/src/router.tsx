import { Component, lazy, Suspense } from 'react'
import type { ReactNode } from 'react'
import { createBrowserRouter, Navigate } from 'react-router-dom'
import { AlertCircle, Loader2, RefreshCw } from 'lucide-react'
import { Layout } from './components/Layout'
import { Watchlist } from './pages/Watchlist'
import { Screener } from './pages/Screener'
import { Onboarding } from './pages/Onboarding'
import { Auth } from './pages/Auth'
import { Data } from './pages/Data'
import { Monitor } from './pages/Monitor'
import { Trading } from './pages/Trading'
import { TradeJournal } from './pages/TradeJournal'
import { Dashboard } from './pages/Dashboard'
import { Review } from './pages/Review'
import { LimitUpLadder } from './pages/LimitUpLadder'
import { Branding } from './pages/Branding'
import { Settings } from './pages/Settings'
import { Indices } from './pages/Indices'
import { useSettings } from './lib/useSharedQueries'
import { Logo } from './components/Logo'

// 非首屏重模块懒加载：拆分为独立 chunk，避免全部进入首包。
// 这些页面均为具名导出，需重映射为 default 形式以适配 React.lazy。
const AnalysisDetail = lazy(() => import('./pages/AnalysisDetail').then((m) => ({ default: m.AnalysisDetail })))
const ConceptAnalysis = lazy(() => import('./pages/ConceptAnalysis').then((m) => ({ default: m.ConceptAnalysis })))
const IndustryAnalysis = lazy(() => import('./pages/IndustryAnalysis').then((m) => ({ default: m.IndustryAnalysis })))
const StockAnalysis = lazy(() => import('./pages/StockAnalysis').then((m) => ({ default: m.StockAnalysis })))
const ConditionScreener = lazy(() => import('./pages/ConditionScreener').then((m) => ({ default: m.ConditionScreener })))
const Backtest = lazy(() => import('./pages/Backtest').then((m) => ({ default: m.Backtest })))
const Regime = lazy(() => import('./pages/Regime').then((m) => ({ default: m.Regime })))
const Financials = lazy(() => import('./pages/Financials').then((m) => ({ default: m.Financials })))
const Optimizer = lazy(() => import('./pages/Optimizer').then((m) => ({ default: m.Optimizer })))
const Agent = lazy(() => import('./pages/Agent').then((m) => ({ default: m.Agent })))
const Dev = lazy(() => import('./pages/Dev').then((m) => ({ default: m.Dev })))
const FeatureGuide = lazy(() => import('./pages/FeatureGuide').then((m) => ({ default: m.FeatureGuide })))
const Research = lazy(() => import('./pages/Research').then((m) => ({ default: m.Research })))
const SignalScorecard = lazy(() => import('./pages/SignalScorecard').then((m) => ({ default: m.SignalScorecard })))
const CrossSection = lazy(() => import('./pages/CrossSection').then((m) => ({ default: m.CrossSection })))


// 懒加载页面占位：渲染在 Layout Outlet 内，不触碰 Layout/OnboardingGuard 结构。
function PageFallback() {
  return (
    <div className="min-h-[60vh] grid place-items-center">
      <div className="flex items-center gap-2 text-muted">
        <Loader2 className="h-5 w-5 animate-spin" />
        <span className="text-xs">加载中…</span>
      </div>
    </div>
  )
}

// 动态 chunk 加载失败时，React.lazy 抛出的错误会越过 Suspense。
// 用局部 ErrorBoundary 兜住：显示可访问的失败提示与"重新加载"按钮，
// 避免整个应用卸载成白屏。只包裹懒加载页面，不触及 Layout/守卫。
class PageErrorBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false }
  static getDerivedStateFromError() {
    return { failed: true }
  }
  render() {
    if (!this.state.failed) return this.props.children
    return (
      <div className="min-h-[60vh] grid place-items-center">
        <div className="flex flex-col items-center gap-3">
          <AlertCircle className="h-5 w-5 text-danger" />
          <div className="text-xs text-danger">页面加载失败</div>
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="px-3 py-1.5 rounded-btn border border-border text-secondary hover:text-foreground text-xs font-medium transition-colors flex items-center gap-1.5"
          >
            <RefreshCw className="h-3.5 w-3.5" />
            重新加载
          </button>
        </div>
      </div>
    )
  }
}

function lazyPage(node: ReactNode) {
  return (
    <PageErrorBoundary>
      <Suspense fallback={<PageFallback />}>{node}</Suspense>
    </PageErrorBoundary>
  )
}

// 首次使用守卫 —— 未完成向导则重定向到 /onboarding
// 只挂在根路由上;/onboarding 本身不被守卫,避免循环重定向。
// settings 由 Layout 预取,守卫判定不产生额外请求。
function OnboardingGuard({ children }: { children: React.ReactNode }) {
  const settings = useSettings()

  // 仅首次加载(本地无缓存)时显示占位。
  // 后台重取 (isFetching) 时本地已有上一份缓存可用, 直接放行, 避免切页时整屏 logo 闪烁。
  // 防误重定向已由 Onboarding/AI 等处 invalidate 前的 setQueryData 同步缓存兜底。
  if (settings.isLoading) {
    return (
      <div className="min-h-screen bg-base grid place-items-center">
        <div className="flex flex-col items-center gap-3 text-muted">
          <Logo size={28} className="text-foreground" />
          <div className="text-xs">加载中…</div>
        </div>
      </div>
    )
  }

  // 查询出错或字段缺失时不拦截 —— 宁可放行,也不把用户卡在空白页
  if (settings.data && settings.data.onboarding_completed === false) {
    return <Navigate to="/onboarding" replace />
  }

  return <>{children}</>
}

export const router = createBrowserRouter([
  { path: '/onboarding', element: <Onboarding /> },
  { path: '/login', element: <Auth /> },
  {
    path: '/',
    element: (
      <OnboardingGuard>
        <Layout />
      </OnboardingGuard>
    ),
    children: [
      { index: true, element: <Dashboard /> },
      { path: 'overview', element: <Navigate to="/" replace /> },
      { path: 'analysis', element: <Navigate to="/settings?tab=ext-pages" replace /> },
      { path: 'analysis/:menuId', element: lazyPage(<AnalysisDetail />) },
      { path: 'concept-analysis', element: lazyPage(<ConceptAnalysis />) },
      { path: 'industry-analysis', element: lazyPage(<IndustryAnalysis />) },
      { path: 'regime', element: lazyPage(<Regime />) },
      { path: 'stock-analysis', element: lazyPage(<StockAnalysis />) },
      { path: 'review', element: <Review /> },
      { path: 'watchlist', element: <Watchlist /> },
      { path: 'screener', element: <Screener /> },
      { path: 'condition-screener', element: lazyPage(<ConditionScreener />) },
      { path: 'backtest', element: lazyPage(<Backtest />) },
      { path: 'financials', element: lazyPage(<Financials />) },
      { path: 'data', element: <Data /> },
      { path: 'monitor', element: <Monitor /> },
      { path: 'trading', element: <Trading /> },
      { path: 'journal', element: <TradeJournal /> },
      { path: 'research', element: lazyPage(<Research />) },
      { path: 'signal-scorecard', element: lazyPage(<SignalScorecard />) },
      { path: 'cross-section', element: lazyPage(<CrossSection />) },

      { path: 'limit-ladder', element: <LimitUpLadder /> },
      { path: 'indices', element: <Indices /> },
      { path: 'optimizer', element: lazyPage(<Optimizer />) },
      { path: 'agent', element: lazyPage(<Agent />) },
      { path: 'branding', element: <Branding /> },
      { path: 'settings', element: <Settings /> },
      { path: 'guide', element: lazyPage(<FeatureGuide />) },
      // 隐藏路由：开发者工具（不暴露在菜单，仅供调试）
      { path: 'dev', element: lazyPage(<Dev />) },
      // 旧路由兼容重定向
      { path: 'settings/keys', element: <Navigate to="/settings?tab=account" replace /> },
      { path: 'settings/ai', element: <Navigate to="/settings?tab=ai" replace /> },
      { path: 'settings/queries', element: <Navigate to="/settings?tab=queries" replace /> },
    ],
  },
])
