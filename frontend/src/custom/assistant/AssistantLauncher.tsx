/**
 * AI 助手宿主 — 挂载于 layout.navigation.extra 插槽(全站常驻渲染点),
 * 但不在侧栏菜单里渲染任何入口。聚合三件套:
 * ① 可拖动悬浮球(位置持久化); ② 左上角 AI 配置徽标右侧的打开入口
 * (DOM 锚定); ③ 从页面右缘滑入的非模态面板。
 * 另负责全局快捷键 ⌘K/Ctrl+K 与 Esc、按当前路由上报页面上下文。
 */
import { useEffect } from 'react'
import type { FrontendSlotContextMap } from '@/extensions/types'
import { closeAssistant, setPageContext, toggleAssistant, useAssistantStore } from './store'
import { AssistantFloatingButton } from './ui/AssistantFloatingButton'
import { AssistantDrawer } from './ui/AssistantDrawer'

// 插槽注册表把组件 props 定为所有插槽上下文的联合类型(保守契约);
// 本组件只在 layout.navigation.extra 渲染, 在此收窄到该插槽的上下文。
type NavigationContext = FrontendSlotContextMap['layout.navigation.extra']
type AnySlotContext = FrontendSlotContextMap[keyof FrontendSlotContextMap]

// 与核心侧栏导航文案保持一致的轻量映射(仅用于上下文提示, 展示不走这里)。
const PAGE_LABELS: Record<string, string> = {
  '/': '看板',
  '/watchlist': '自选',
  '/screener': '策略',
  '/factors': '因子',
  '/backtest': '回测',
  '/stock-analysis': '个股分析',
  '/limit-ladder': '连板梯队',
  '/concept-analysis': '概念分析',
  '/industry-analysis': '行业分析',
  '/financials': '财务分析',
  '/monitor': '监控中心',
  '/regime': '市场环境',
  '/abnormal': '异动监控',
  '/lots': '持仓提醒',
  '/signals': '信号库',
  '/review': '复盘',
  '/indices': '指数',
  '/data': '数据',
  '/settings': '设置',
}

export function AssistantLauncher(props: AnySlotContext) {
  const { pathname } = props as NavigationContext
  const { open } = useAssistantStore()

  useEffect(() => {
    setPageContext({ page: PAGE_LABELS[pathname] ?? '' })
  }, [pathname])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        toggleAssistant()
        return
      }
      if (event.key === 'Escape' && open) closeAssistant()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [open])

  return (
    <>
      <AssistantFloatingButton />
      <AssistantDrawer />
    </>
  )
}
