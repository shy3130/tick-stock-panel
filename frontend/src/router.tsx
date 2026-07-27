import { lazy } from 'react'
import { createBrowserRouter, Navigate, useLocation } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Layout } from './components/Layout'
import { Onboarding } from './pages/Onboarding'
import { Auth } from './pages/Auth'
import { InviteAccess } from './pages/InviteAccess'
import { Landing } from './pages/Landing'
import { useSettings } from './lib/useSharedQueries'
import { Logo } from './components/Logo'
import { api } from './lib/api'
import { resolvePublicEntry } from './lib/publicEntry'
import { BRAND_NAME } from './lib/brand'
import { SYCEE_ROUTES } from './features/sycee/registry'
import { SYCEE_PUBLIC_ROUTES } from './features/sycee/registry'

// Keep public entry components eager; authenticated application pages load on demand.
const Watchlist = lazy(() => import('./pages/Watchlist').then(m => ({ default: m.Watchlist })))
const Screener = lazy(() => import('./pages/Screener').then(m => ({ default: m.Screener })))
const Backtest = lazy(() => import('./pages/Backtest').then(m => ({ default: m.Backtest })))
const Financials = lazy(() => import('./pages/Financials').then(m => ({ default: m.Financials })))
const Data = lazy(() => import('./pages/Data').then(m => ({ default: m.Data })))
const Monitor = lazy(() => import('./pages/Monitor').then(m => ({ default: m.Monitor })))
const Dashboard = lazy(() => import('./pages/Dashboard').then(m => ({ default: m.Dashboard })))
const AnalysisDetail = lazy(() => import('./pages/AnalysisDetail').then(m => ({ default: m.AnalysisDetail })))
const ConceptAnalysis = lazy(() => import('./pages/ConceptAnalysis').then(m => ({ default: m.ConceptAnalysis })))
const IndustryAnalysis = lazy(() => import('./pages/IndustryAnalysis').then(m => ({ default: m.IndustryAnalysis })))
const StockAnalysis = lazy(() => import('./pages/StockAnalysis').then(m => ({ default: m.StockAnalysis })))
const Review = lazy(() => import('./pages/Review').then(m => ({ default: m.Review })))
const LimitUpLadder = lazy(() => import('./pages/LimitUpLadder').then(m => ({ default: m.LimitUpLadder })))
const Branding = lazy(() => import('./pages/Branding').then(m => ({ default: m.Branding })))
const Settings = lazy(() => import('./pages/Settings').then(m => ({ default: m.Settings })))
const Indices = lazy(() => import('./pages/Indices').then(m => ({ default: m.Indices })))
const Dev = lazy(() => import('./pages/Dev').then(m => ({ default: m.Dev })))

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
    ? { authenticated: authStatus.data.authenticated, invite_enabled: authStatus.data.invite_enabled }
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

  if (target === 'invite') return <Navigate to={`/invite?redirect=${encodeURIComponent(location.pathname + location.search)}`} replace />

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
  { path: '/invite', element: <InviteAccess /> },
  { path: '/onboarding', element: <Onboarding /> },
  { path: '/login', element: <Auth /> },
  ...SYCEE_PUBLIC_ROUTES,
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
      ...SYCEE_ROUTES,
      { path: 'watchlist', element: <Watchlist /> },
      { path: 'screener', element: <Screener /> },
      { path: 'backtest', element: <Backtest /> },
      { path: 'financials', element: <Financials /> },
      { path: 'data', element: <Data /> },
      { path: 'monitor', element: <Monitor /> },
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
