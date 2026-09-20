/**
 * AI 助手消息渲染 — 用户气泡 / 助手通栏正文 / 工具足迹卡 / 错误与提示。
 *
 * 视觉规范对齐设计语言 §6.0: 设计令牌(bg-elevated/border-border/text-*),
 * 数字等宽, 红涨绿跌语义色仅用于价格; AI 正文不用气泡(通栏 + 引导线),
 * 工具足迹可展开核对原始参数 — 结果可核对是本模块的体验铁律。
 */
import { memo, useState } from 'react'
import { motion } from 'framer-motion'
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  Loader2,
  ShieldAlert,
  Wrench,
} from 'lucide-react'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'
import { cn } from '@/lib/cn'
import type { ChatMessage, ToolCallRecord } from '../store'
import { DailyChartCard } from './DailyChartCard'

export const UserBubble = memo(function UserBubble({ content }: { content: string }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%] rounded-card bg-elevated px-3 py-2 text-sm leading-relaxed text-foreground">
        {content}
      </div>
    </div>
  )
})

export const AssistantMessage = memo(function AssistantMessage({
  content,
  streaming,
}: {
  content: string
  streaming: boolean
}) {
  return (
    <div className="border-l-2 border-accent/30 pl-3 text-sm leading-relaxed text-foreground">
      {content ? (
        <>
          <MarkdownRenderer content={content} />
          {streaming && <Cursor />}
          {!streaming && <AnswerDisclaimer />}
        </>
      ) : (
        <ThinkingDots />
      )}
    </div>
  )
})

/** 回答完成后的固定合规提示: 风险 + 数据口径。前端固定渲染, 不依赖模型自觉追加。 */
function AnswerDisclaimer() {
  return (
    <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-0.5 border-t border-border/60 pt-1.5 text-[10px] leading-relaxed text-muted">
      <span className="inline-flex items-center gap-1">
        <ShieldAlert className="h-3 w-3 shrink-0" />
        风险提示: AI 生成内容仅供参考, 不构成投资建议
      </span>
      <span>数据为本地快照, 可能存在延迟或口径差异, 请以交易所官方披露为准</span>
    </div>
  )
}

function Cursor() {
  return <span className="ml-0.5 inline-block h-4 w-[7px] translate-y-[2px] animate-pulse rounded-[1px] bg-accent" />
}

function ThinkingDots() {
  return (
    <div className="flex items-center gap-1 py-1" aria-label="思考中">
      {[0, 1, 2].map(i => (
        <motion.span
          key={i}
          className="h-1.5 w-1.5 rounded-full bg-accent/60"
          animate={{ opacity: [0.25, 1, 0.25] }}
          transition={{ duration: 1.2, repeat: Infinity, delay: i * 0.18, ease: 'easeInOut' }}
        />
      ))}
    </div>
  )
}

const TOOL_LABELS: Record<string, string> = {
  list_factors: '检索因子目录',
  list_strategies: '检索策略目录',
  list_data_capabilities: '检索数据源能力',
  run_backtest: '运行策略回测',
  get_stock_quote: '查询个股实时行情',
  get_stock_daily: '查询个股日线',
  get_stock_analysis: '个股关键价位分析',
  get_financials: '查询财务报表',
  get_watchlist: '查询自选列表',
  get_market_overview: '查询市场总览',
  get_indices: '查询指数行情',
  get_sector_rotation: '查询板块轮动',
  get_regime: '查询市场环境',
  get_abnormal: '查询异动监控',
  get_lots: '查询持仓提醒',
  list_signals: '检索信号库',
  run_strategy: '执行选股策略',
  get_factor_values: '查询因子排名',
}

function toolLabel(name: string): string {
  return TOOL_LABELS[name]
}

/** 足迹组: 工具足迹卡 + 附带可绘图数据的走势小卡(始终可见, 不藏在展开区)。 */
export const FootprintGroup = memo(function FootprintGroup({ calls }: { calls: ToolCallRecord[] }) {
  if (!calls.length) return null
  const charts = calls.filter(c => c.status === 'ok' && c.charts).flatMap(c => c.charts!)
  if (!charts.length) return <FootprintCard calls={calls} />
  return (
    <div className="space-y-2">
      <FootprintCard calls={calls} />
      {/* 两张起并排 (日K+分时同屏最常见), 避免全宽长条被横向拉扁 */}
      <div className={`grid gap-2 ${charts.length >= 2 ? 'grid-cols-2' : 'grid-cols-1'}`}>
        {charts.map((chart, i) => (
          <DailyChartCard key={`${chart.kind}-${chart.symbol}-${i}`} chart={chart} />
        ))}
      </div>
    </div>
  )
})

export const FootprintCard = memo(function FootprintCard({ calls }: { calls: ToolCallRecord[] }) {
  const [expanded, setExpanded] = useState(false)
  if (!calls.length) return null

  const done = calls.filter(c => c.status !== 'running')
  const totalMs = done.reduce((sum, c) => sum + (c.elapsedMs ?? 0), 0)
  const hasError = calls.some(c => c.status === 'error')
  const running = calls.some(c => c.status === 'running')

  return (
    <div className="overflow-hidden rounded-card border border-border bg-base/60">
      <button
        type="button"
        onClick={() => setExpanded(v => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs text-secondary transition-colors duration-150 ease-smooth hover:text-foreground"
      >
        <Wrench className="h-3.5 w-3.5 shrink-0 text-muted" />
        <span className={cn(running && 'animate-pulse')}>
          {running ? `正在调用工具 (${done.length}/${calls.length})…` : `已调用 ${calls.length} 个工具`}
        </span>
        {!running && done.length > 0 && (
          <span className="font-mono text-muted">
            · {totalMs >= 1000 ? `${(totalMs / 1000).toFixed(1)}s` : `${totalMs}ms`}
          </span>
        )}
        {hasError && !running && <span className="text-danger">· 部分失败</span>}
        <ChevronDown
          className={cn('ml-auto h-3.5 w-3.5 shrink-0 text-muted transition-transform duration-150 ease-smooth', expanded && 'rotate-180')}
        />
      </button>
      {expanded && (
        <div className="border-t border-border/60 px-3 py-2 space-y-2">
          {calls.map(call => (
            <ToolCallRow key={call.callId} call={call} />
          ))}
        </div>
      )}
    </div>
  )
})

function ToolCallRow({ call }: { call: ToolCallRecord }) {
  const hasArgs = Object.keys(call.args ?? {}).length > 0
  return (
    <div className="text-xs">
      <div className="flex items-center gap-2">
        {call.status === 'running'
          ? <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-accent" />
          : call.status === 'ok'
            ? <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-accent" />
            : <AlertCircle className="h-3.5 w-3.5 shrink-0 text-danger" />}
        <span className="text-foreground">{toolLabel(call.name)}</span>
        {call.elapsedMs !== undefined && (
          <span className="font-mono text-muted">
            {call.elapsedMs >= 1000 ? `${(call.elapsedMs / 1000).toFixed(1)}s` : `${call.elapsedMs}ms`}
          </span>
        )}
      </div>
      {call.summary && (
        <div className={cn('mt-0.5 pl-5', call.status === 'error' ? 'text-danger' : 'text-secondary')}>
          {call.summary}
        </div>
      )}
      {hasArgs && (
        <details className="mt-0.5 pl-5 text-muted">
          <summary className="cursor-pointer select-none text-[11px] hover:text-secondary">参数</summary>
          <pre className="mt-1 overflow-x-auto rounded-btn bg-elevated/60 p-2 font-mono text-[11px] leading-relaxed text-secondary">
            {JSON.stringify(call.args, null, 2)}
          </pre>
        </details>
      )}
    </div>
  )
}

export const ErrorBubble = memo(function ErrorBubble({
  kind,
  message,
  hint,
  onRetry,
}: {
  kind: string
  message: string
  hint?: string
  onRetry?: () => void
}) {
  const title = ERROR_TITLES[kind] ?? '出错了'
  return (
    <div className="border-l-2 border-danger pl-3 text-sm">
      <div className="flex items-center gap-1.5 font-medium text-danger">
        <AlertCircle className="h-4 w-4 shrink-0" />
        {title}
      </div>
      <div className="mt-1 text-secondary">{message}</div>
      <div className="mt-1.5 flex items-center gap-3 text-xs">
        {onRetry && kind !== 'no_key' && kind !== 'provider' && (
          <button
            type="button"
            onClick={onRetry}
            className="cursor-pointer rounded-btn px-2 py-0.5 text-accent transition-colors duration-150 ease-smooth hover:bg-accent/10"
          >
            重试
          </button>
        )}
        {hint && <span className="font-mono text-muted">{hint}</span>}
      </div>
    </div>
  )
})

const ERROR_TITLES: Record<string, string> = {
  no_key: 'AI 未配置',
  provider: '当前供应商不支持工具调用',
  input_too_long: '对话过长',
  rounds: '本轮调用达到上限',
  model: 'AI 服务出错',
  network: '网络错误',
}

export const NoticeMessage = memo(function NoticeMessage({ content }: { content: string }) {
  return (
    <div className="text-center text-xs text-muted">{content}</div>
  )
})

export function AssistantMessageView({ message, onRetry }: { message: ChatMessage; onRetry?: () => void }) {
  switch (message.role) {
    case 'user':
      return <UserBubble content={message.content} />
    case 'assistant':
      return <AssistantMessage content={message.content} streaming={message.streaming} />
    case 'footprint':
      return <FootprintGroup calls={message.calls} />
    case 'error':
      return <ErrorBubble kind={message.kind} message={message.message} hint={message.hint} onRetry={onRetry} />
    case 'notice':
      return <NoticeMessage content={message.content} />
    default:
      return null
  }
}
