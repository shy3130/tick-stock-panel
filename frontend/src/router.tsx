import { lazy } from 'react'
import { createBrowserRouter, Navigate } from 'react-router-dom'
import { Layout } from './components/Layout'
import { Onboarding } from './pages/Onboarding'
import { Auth } from './pages/Auth'
import { useSettings } from './lib/useSharedQueries'
import { Logo } from './components/Logo'
import { ExtensionBoundary } from './extensions/ExtensionBoundary'
import {
  finalizeFrontendExtensions,
  getFrontendExtensionLoadErrors,
  getFrontendExtensionRoutes,
} from './extensions/registry'

// 代码分割: 页面全部 lazy 加载, 避免首屏打包所有页面 (ECharts / lightweight-charts /
// framer-motion 等重库) → 大幅减小首屏 bundle。命名导出用 .then 映射为 default。
// Layout / Onboarding / Auth 为应用外壳与入口, 保持同步加载。
const Watchlist = lazy(() => import('./pages/Watchlist').then(m => ({ default: m.Watchlist })))
const Screener = lazy(() => import('./pages/Screener').then(m => ({ default: m.Screener })))
const Backtest = lazy(() => import('./pages/Backtest').then(m => ({ default: m.Backtest })))
const Mining = lazy(() => import('./pages/Mining').then(m => ({ default: m.Mining })))
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
const Regime = lazy(() => import('./pages/Regime').then(m => ({ default: m.Regime })))
const AbnormalMoves = lazy(() => import('./pages/AbnormalMoves').then(m => ({ default: m.AbnormalMoves })))
const AuctionHub = lazy(() => import('./pages/AuctionHub').then(m => ({ default: m.AuctionHub })))
const Dev = lazy(() => import('./pages/Dev').then(m => ({ default: m.Dev })))

const CORE_ROUTE_PATHS = new Set([
  '/',
  '/onboarding',
  '/login',
  '/overview',
  '/analysis',
  '/analysis/:menuId',
  '/concept-analysis',
  '/industry-analysis',
  '/stock-analysis',
  '/review',
  '/watchlist',
  '/screener',
  '/backtest',
  '/mining',
  '/financials',
  '/data',
  '/monitor',
  '/limit-ladder',
  '/indices',
  '/regime',
  '/abnormal',
  '/auction',
  '/branding',
  '/settings',
  '/dev',
  '/settings/keys',
  '/settings/ai',
  '/settings/queries',
])

finalizeFrontendExtensions(CORE_ROUTE_PATHS)
const frontendExtensionRoutes = getFrontendExtensionRoutes()
const frontendExtensionErrors = getFrontendExtensionLoadErrors()
if (frontendExtensionErrors.length > 0) {
  console.error('部分前端扩展加载失败', frontendExtensionErrors)
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
      { path: 'analysis/:menuId', element: <AnalysisDetail /> },
      { path: 'concept-analysis', element: <ConceptAnalysis /> },
      { path: 'industry-analysis', element: <IndustryAnalysis /> },
      { path: 'stock-analysis', element: <StockAnalysis /> },
      { path: 'review', element: <Review /> },
      { path: 'watchlist', element: <Watchlist /> },
      { path: 'screener', element: <Screener /> },
      { path: 'backtest', element: <Backtest /> },
      { path: 'mining', element: <Mining /> },
      { path: 'financials', element: <Financials /> },
      { path: 'data', element: <Data /> },
      { path: 'monitor', element: <Monitor /> },
      { path: 'limit-ladder', element: <LimitUpLadder /> },
      { path: 'auction', element: <AuctionHub /> },
      { path: 'indices', element: <Indices /> },
    { path: 'regime', element: <Regime /> },
      { path: 'abnormal', element: <AbnormalMoves /> },
      { path: 'branding', element: <Branding /> },
      { path: 'settings', element: <Settings /> },
      // 隐藏路由：开发者工具（不暴露在菜单，仅供调试）
      { path: 'dev', element: <Dev /> },
      // 旧路由兼容重定向
      { path: 'settings/keys', element: <Navigate to="/settings?tab=data-sources" replace /> },
      { path: 'settings/ai', element: <Navigate to="/settings?tab=ai" replace /> },
      { path: 'settings/queries', element: <Navigate to="/settings?tab=queries" replace /> },
      ...frontendExtensionRoutes.map(route => {
        const ExtensionPage = route.component
        return {
          path: route.path.slice(1),
          element: (
            <ExtensionBoundary extensionId={route.extensionId}>
              <ExtensionPage />
            </ExtensionBoundary>
          ),
        }
      }),
    ],
  },
])
