import { createBrowserRouter, Navigate, useLocation } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Layout } from './components/Layout'
import { Watchlist } from './pages/Watchlist'
import { Screener } from './pages/Screener'
import { Backtest } from './pages/Backtest'
import { Financials } from './pages/Financials'
import { Onboarding } from './pages/Onboarding'
import { Auth } from './pages/Auth'
import { Landing } from './pages/Landing'
import { Data } from './pages/Data'
import { Monitor } from './pages/Monitor'
import { Trading } from './pages/Trading'
import { Dashboard } from './pages/Dashboard'
import { AnalysisDetail } from './pages/AnalysisDetail'
import { ConceptAnalysis } from './pages/ConceptAnalysis'
import { IndustryAnalysis } from './pages/IndustryAnalysis'
import { StockAnalysis } from './pages/StockAnalysis'
import { Review } from './pages/Review'
import { LimitUpLadder } from './pages/LimitUpLadder'
import { Branding } from './pages/Branding'
import { Settings } from './pages/Settings'
import { Indices } from './pages/Indices'
import { Dev } from './pages/Dev'
import { useSettings } from './lib/useSharedQueries'
import { Logo } from './components/Logo'
import { api } from './lib/api'
import { resolvePublicEntry } from './lib/publicEntry'
import { BRAND_NAME } from './lib/brand'

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

function RootGate() {
  const location = useLocation()
  const authStatus = useQuery({
    queryKey: ['auth-status'],
    queryFn: api.authStatus,
    retry: false,
    staleTime: 5_000,
  })

  const status = authStatus.data
    ? { authenticated: authStatus.data.authenticated }
    : authStatus.isError
      ? { authenticated: false }
      : null

  const target = resolvePublicEntry(status, location.pathname + location.search)

  if (target === 'loading') {
    return (
      <div className="grid min-h-screen place-items-center bg-[#151410] text-[#b9a46a]">
        <div className="font-mono text-xs tracking-[0.28em]">{BRAND_NAME}</div>
      </div>
    )
  }

  if (target === 'landing') return <Landing />

  if (target === 'login') {
    const redirect = encodeURIComponent(location.pathname + location.search)
    return <Navigate to={`/login?redirect=${redirect}`} replace />
  }

  return (
    <OnboardingGuard>
      <Layout />
    </OnboardingGuard>
  )
}

export const router = createBrowserRouter([
  { path: '/onboarding', element: <Onboarding /> },
  { path: '/login', element: <Auth /> },
  {
    path: '/',
    element: <RootGate />,
    children: [
      { index: true, element: <Dashboard /> },
      { path: 'overview', element: <Navigate to="/" replace /> },
      { path: 'analysis', element: <Navigate to="/settings?tab=ext-pages" replace /> },
      { path: 'analysis/:menuId', element: <AnalysisDetail /> },
      { path: 'concept-analysis', element: <ConceptAnalysis /> },
      { path: 'industry-analysis', element: <IndustryAnalysis /> },
      { path: 'stock-analysis', element: <StockAnalysis /> },
      { path: 'review', element: <Review /> },
      { path: 'watchlist', element: <Watchlist /> },
      { path: 'screener', element: <Screener /> },
      { path: 'backtest', element: <Backtest /> },
      { path: 'financials', element: <Financials /> },
      { path: 'data', element: <Data /> },
      { path: 'monitor', element: <Monitor /> },
      { path: 'trading', element: <Trading /> },
      { path: 'limit-ladder', element: <LimitUpLadder /> },
      { path: 'indices', element: <Indices /> },
      { path: 'branding', element: <Branding /> },
      { path: 'settings', element: <Settings /> },
      // 隐藏路由：开发者工具（不暴露在菜单，仅供调试）
      { path: 'dev', element: <Dev /> },
      // 旧路由兼容重定向
      { path: 'settings/keys', element: <Navigate to="/settings?tab=account" replace /> },
      { path: 'settings/ai', element: <Navigate to="/settings?tab=ai" replace /> },
      { path: 'settings/queries', element: <Navigate to="/settings?tab=queries" replace /> },
    ],
  },
])
