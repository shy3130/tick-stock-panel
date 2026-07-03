/**
 * AI 大盘复盘页 —— 以流式 LLM 复盘报告为主体的盘后复盘工作台。
 *
 * 设计定位:极简专注型。不复刻 Dashboard 的看板(KPI/雷达/板块排名),
 * 仅保留一行「市场摘要条」作为报告上下文参照;AI 报告 + 历史归档是页面主体。
 *  - 摘要数据:GET /api/overview/market
 *  - 报告流式:POST /api/market-recap/analyze
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import {
  BookOpenCheck, RefreshCw, Sparkles, Trash2, History, ChevronRight, AlertTriangle,
  Database, Wand2, Copy, Download, Clock, X, Check,
} from 'lucide-react'

import { api, type OverviewMarket, type AiReviewReport } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { fmtBigNum } from '@/lib/format'
import { PageHeader } from '@/components/PageHeader'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'
import { toast } from '@/components/Toast'
import { usePreferences } from '@/lib/useSharedQueries'
import { useReviewState } from '@/lib/useReviewStore'
import {
  startReviewGeneration, resetReview, isReviewGenerating,
  type ReviewPhase,
} from '@/lib/reviewStore'

// ================================================================
// 涨跌幅格式化(注意单位差异)
// overview 的 indices.change_pct / breadth.up_pct / seal_rate / *_pct / emotion.score
//   都是【已是百分比值】(如 1.2 表示 1.2%),直接 toFixed 即可,不要 *100。
// ================================================================
function fmtPctAlready(v: number | null | undefined, digits = 2, withSign = false): string {
  if (v == null || Number.isNaN(v)) return '—'
  const sign = withSign && v > 0 ? '+' : ''
  return `${sign}${v.toFixed(digits)}%`
}
function pctClass(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v) || v === 0) return 'text-muted'
  return v > 0 ? 'text-bull' : 'text-bear'
}
// A 股惯例: 强势=红, 弱式=绿(对齐 Dashboard scoreColor)
function scoreColor(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '#71717A'
  if (v >= 70) return '#F04438'
  if (v >= 55) return '#FB923C'
  if (v >= 45) return '#F59E0B'
  if (v >= 30) return '#84CC16'
  return '#12B76A'
}

// 归档时刻格式化:ISO → "MM-DD HH:mm"(用于历史列表显示复盘时间)
function fmtArchivedAt(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  const hh = String(d.getHours()).padStart(2, '0')
  const mi = String(d.getMinutes()).padStart(2, '0')
  return `${mm}-${dd} ${hh}:${mi}`
}

// Phase 类型复用 store 的定义(单一来源)

export function Review() {
  const qc = useQueryClient()
  // 复盘日期:当前固定取最新交易日(后续如需日期选择可改回 useState)
  const asOf: string | undefined = undefined
  const [focus, setFocus] = useState('')
  // 生成状态走全局 store:切走页面流不中断,回来可恢复
  const { phase, content, error, meta } = useReviewState()
  const [viewing, setViewing] = useState<AiReviewReport | null>(null)  // 查看历史报告
  const reportEndRef = useRef<HTMLDivElement>(null)

  // 看板数据(与总览页同源)
  const marketQuery = useQuery<OverviewMarket>({
    queryKey: QK.overviewMarket(asOf),
    queryFn: () => api.overviewMarket(asOf),
    staleTime: 5_000,
    placeholderData: (prev) => prev,
  })

  // 历史报告
  const historyQuery = useQuery<{ reports: AiReviewReport[] }>({
    queryKey: QK.reviewReports,
    queryFn: () => api.reviewReportsList(),
  })

  const deleteMut = useMutation({
    mutationFn: (id: string) => api.reviewReportDelete(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.reviewReports })
      toast('已删除', 'success')
    },
    onError: () => { /* request() 已 toast */ },
  })

  // ===== 定时复盘 =====
  const [showSchedule, setShowSchedule] = useState(false)
  const prefs = usePreferences()
  const reviewSched = prefs.data?.review_schedule ?? { enabled: false, hour: 15, minute: 10 }
  const webhookChannels = prefs.data?.webhook_channels ?? {}
  const pushOptions = [
    { id: 'feishu', name: '飞书', hint: '群机器人', configured: !!(webhookChannels.feishu?.url ?? prefs.data?.feishu_webhook_url) },
    { id: 'dingtalk', name: '钉钉', hint: '群机器人', configured: !!webhookChannels.dingtalk?.url },
    { id: 'wecom', name: '企微', hint: '群机器人', configured: !!webhookChannels.wecom?.url },
    { id: 'meow', name: 'MeoW', hint: '个人推送', configured: !!webhookChannels.meow?.nickname },
  ]
  // 推送渠道是独立的顶层偏好(多选), 与定时 / 实时行情无关, 常驻可单独设置
  // []=不推送, ['feishu']=飞书(微信开发中, 仅占位)
  const reviewPushChannels = prefs.data?.review_push_channels ?? []
  // 弹窗内的本地草稿: 开关和时间都在本地改, 点「保存」才真正提交(避免开关一拨就关弹窗)
  const [draft, setDraft] = useState(reviewSched)
  const openSchedule = useCallback(() => {
    setDraft(reviewSched)  // 每次打开同步最新服务端值
    setShowSchedule(true)
  }, [reviewSched])
  const reviewMut = useMutation({
    mutationFn: ({ enabled, hour, minute }: { enabled: boolean; hour: number; minute: number }) =>
      api.updateReviewSchedule(enabled, hour, minute),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: QK.preferences })
      setShowSchedule(false)
      toast(vars.enabled ? '已开启定时复盘' : '已关闭定时复盘', 'success')
    },
    onError: () => { /* request() 已 toast */ },
  })
  // 推送渠道(多选): 独立常驻, 即时生效(勾选渠道即开关, 改了立刻提交)
  const pushMut = useMutation({
    mutationFn: (channels: string[]) => api.updateReviewPush(channels),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: QK.preferences })
      toast(vars.length === 0 ? '已关闭复盘推送' : '已更新复盘推送渠道', 'success')
    },
    onError: () => { /* request() 已 toast */ },
  })
  const togglePushChannel = useCallback((ch: string) => {
    const next = reviewPushChannels.includes(ch)
      ? reviewPushChannels.filter(c => c !== ch)
      : [...reviewPushChannels, ch]
    pushMut.mutate(next)
  }, [reviewPushChannels, pushMut])

  // 自动滚动到报告底部(streaming 时)
  useEffect(() => {
    if (phase === 'streaming') {
      reportEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
    }
  }, [content, phase])

  // 当进入生成中(streaming)时, 清掉「查看历史」状态, 让主区域显示流内容。
  // 手动 generate 已自带 setViewing(null), 这里主要补定时 SSE 流的场景:
  // 用户若正看着历史报告, 定时触发生成时也要切回主区域显示流式内容。
  useEffect(() => {
    if (phase === 'streaming' && viewing) {
      setViewing(null)
    }
  }, [phase, viewing])

  // 自动归档(生成完成后台静默保存)—— 通过回调注入 store,避免 store 直接依赖 qc/marketQuery
  const onGenerationDone = useCallback(async (fullContent: string, doneMeta: { as_of?: string; summary?: string; emotion_score?: number; emotion_label?: string } | null) => {
    const reportAsOf = doneMeta?.as_of ?? marketQuery.data?.as_of ?? asOf ?? new Date().toISOString().slice(0, 10)
    try {
      await api.reviewReportSave({
        as_of: reportAsOf,
        focus,
        content: fullContent,
        summary: doneMeta?.summary,
        emotion_score: doneMeta?.emotion_score ?? null,
        emotion_label: doneMeta?.emotion_label ?? '',
      })
      qc.invalidateQueries({ queryKey: QK.reviewReports })
    } catch { /* 静默 */ }
  }, [focus, asOf, marketQuery.data, qc])

  // 主流程:生成复盘(委托给全局 store,流在后台独立运行)
  const generate = useCallback(() => {
    if (isReviewGenerating()) return
    setViewing(null)
    resetReview()
    startReviewGeneration(asOf, focus, (full, doneMeta) => {
      onGenerationDone(full, doneMeta).catch(() => { /* 静默 */ })
    })
  }, [asOf, focus, onGenerationDone])

  // 复制全文到剪贴板(viewing 优先,与主区域显示一致)
  const copyContent = useCallback(async () => {
    const text = viewing?.content ?? content
    if (!text) return
    try {
      await navigator.clipboard.writeText(text)
      toast('已复制到剪贴板', 'success')
    } catch {
      toast('复制失败,请手动选择文本', 'error')
    }
  }, [content, viewing])

  // 下载为 .md 文件(viewing 优先)
  const downloadContent = useCallback(() => {
    const text = viewing?.content ?? content
    if (!text) return
    const reportDate = viewing?.as_of ?? meta?.as_of ?? asOf ?? new Date().toISOString().slice(0, 10)
    const blob = new Blob([text], { type: 'text/markdown;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `复盘_${reportDate}.md`
    a.click()
    URL.revokeObjectURL(url)
  }, [content, viewing, meta, asOf])

  // 查看历史报告(不中断后台生成:仅临时把 viewing 覆盖到主区域,
  // 生成中的流仍在 store 里继续跑,点"生成中"项即可切回)
  const viewReport = useCallback((r: AiReviewReport) => {
    setViewing(r)
  }, [])

  const isGenerating = phase === 'loading' || phase === 'streaming'
  const displayDate = viewing?.as_of ?? meta?.as_of ?? marketQuery.data?.as_of ?? asOf ?? '最新'
  const data = marketQuery.data
  // 主区域显示的内容:viewing(查看历史)优先于 store 的生成 content,
  // 这样点历史报告不会覆盖后台生成中的流。
  const displayContent = viewing?.content ?? content

  return (
    <>
      <PageHeader
        title="AI 复盘"
        titleExtra={<Sparkles className="h-4 w-4 text-accent" />}
        subtitle={`${displayDate}${data?.emotion ? ` · 情绪 ${data.emotion.label}` : ''}`}
        right={
          <div className="flex items-center gap-1">
            <button
              onClick={() => { marketQuery.refetch() }}
              disabled={marketQuery.isFetching}
              className="inline-flex items-center gap-1 rounded-btn border border-border bg-elevated px-2 py-1 text-[11px] text-secondary transition-colors hover:text-foreground disabled:opacity-50"
              title="刷新市场数据"
            >
              <RefreshCw className={cn('h-3 w-3', marketQuery.isFetching && 'animate-spin')} />刷新
            </button>
            <button
              onClick={openSchedule}
              className={cn(
                'inline-flex items-center gap-1 rounded-btn border px-2 py-1 text-[11px] transition-colors',
                reviewSched.enabled
                  ? 'border-accent/40 bg-accent/10 text-accent hover:bg-accent/20'
                  : 'border-border bg-elevated text-secondary hover:text-foreground',
              )}
              title={reviewSched.enabled ? `定时复盘已开启 · 每日 ${String(reviewSched.hour).padStart(2,'0')}:${String(reviewSched.minute).padStart(2,'0')}` : '定时复盘'}
            >
              <Clock className="h-3 w-3" />定时
            </button>
            <button
              onClick={generate}
              disabled={isGenerating}
              className={cn(
                'inline-flex items-center gap-1.5 rounded-btn px-3.5 py-1.5 text-xs font-medium transition-all',
                isGenerating
                  ? 'border border-accent/40 bg-accent/10 text-accent cursor-not-allowed'
                  : 'bg-accent text-white shadow-sm shadow-accent/25 hover:bg-accent/90 hover:shadow hover:shadow-accent/30',
              )}
            >
              {isGenerating ? (
                <><RefreshCw className="h-3.5 w-3.5 animate-spin" />生成中…</>
              ) : (
                <><Sparkles className="h-3.5 w-3.5" />生成复盘</>
              )}
            </button>
          </div>
        }
      />

      <div className="min-h-full bg-[radial-gradient(circle_at_15%_-5%,rgba(59,130,246,0.10),transparent_30%),radial-gradient(circle_at_85%_5%,rgba(139,92,246,0.08),transparent_30%)] px-4 py-4 sm:px-6">
        <div className="mx-auto max-w-[1280px] space-y-3">

          {marketQuery.isLoading && !data ? (
            <div className="flex h-40 items-center justify-center">
              <div className="flex items-center gap-2 text-sm text-muted">
                <RefreshCw className="h-4 w-4 animate-spin" /> 加载市场数据…
              </div>
            </div>
          ) : !data || !data.as_of ? (
            <div className="flex flex-col items-center justify-center gap-4 rounded-card border border-border bg-surface/80 px-6 py-16">
              <div className="relative">
                <div className="grid h-14 w-14 place-items-center rounded-2xl bg-gradient-to-br from-accent/20 to-purple-500/15 border border-accent/30">
                  <Database className="h-6 w-6 text-accent" strokeWidth={1.8} />
                </div>
              </div>
              <div className="text-center">
                <div className="text-sm font-medium text-foreground">暂无市场数据</div>
                <p className="mt-1 text-xs text-muted">复盘需要日 K 与指数,请先前往「数据」页同步</p>
              </div>
              <Link
                to="/data"
                className="inline-flex items-center gap-1.5 rounded-btn bg-accent px-4 py-2 text-xs font-medium text-white shadow-sm transition-all hover:bg-accent/90 hover:shadow"
              >
                <Database className="h-3.5 w-3.5" />前往数据页同步
                <ChevronRight className="h-3.5 w-3.5" />
              </Link>
            </div>
          ) : (
            <>
              {/* ===== 市场摘要条(轻量上下文,非重复看板)===== */}
              <MarketSummaryBar data={data} />

              {/* ===== 关注点输入 ===== */}
              <div className="flex items-center gap-2 rounded-card border border-border bg-surface/80 px-3.5 py-2.5 transition-colors focus-within:border-accent/40">
                <Wand2 className="h-3.5 w-3.5 shrink-0 text-accent" />
                <input
                  value={focus}
                  onChange={(e) => setFocus(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter' && !isGenerating) generate() }}
                  placeholder="可选:补充复盘关注点,如「明日是否加仓半导体」「量能是否持续」"
                  className="flex-1 bg-transparent text-sm text-foreground outline-none placeholder:text-muted/60"
                />
                {focus && (
                  <button onClick={() => setFocus('')} className="text-xs text-muted transition-colors hover:text-foreground">清除</button>
                )}
              </div>

              {/* ===== 报告 + 历史 双栏(报告为主体)===== */}
              <div className="grid grid-cols-1 gap-3 lg:grid-cols-[1fr_18rem]">
                <ReportPanel
                  phase={phase}
                  content={displayContent}
                  error={error}
                  isGenerating={isGenerating}
                  viewing={viewing}
                  onCopy={copyContent}
                  onDownload={downloadContent}
                  onRegenerate={generate}
                  reportEndRef={reportEndRef}
                />
                <HistoryPanel
                  reports={historyQuery.data?.reports ?? []}
                  loading={historyQuery.isLoading}
                  viewingId={viewing?.id ?? null}
                  generating={isGenerating}
                  onView={viewReport}
                  onBackToGenerating={() => setViewing(null)}
                  onDelete={(id) => deleteMut.mutate(id)}
                />
              </div>
            </>
          )}
        </div>
      </div>

      {/* ===== 定时复盘设置弹窗 ===== */}
      <AnimatePresence>
        {showSchedule && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
            onClick={() => setShowSchedule(false)}
          >
            <motion.div
              initial={{ scale: 0.96, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              exit={{ scale: 0.96, opacity: 0 }}
              className="w-full max-w-md rounded-card border border-border bg-surface p-5 shadow-2xl"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="mb-4 flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Clock className="h-4 w-4 text-accent" />
                  <h3 className="text-sm font-medium text-foreground">定时复盘</h3>
                </div>
                <button
                  onClick={() => setShowSchedule(false)}
                  className="rounded p-1 text-muted transition-colors hover:bg-elevated hover:text-foreground"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>

              <p className="mb-4 text-[11px] leading-relaxed text-muted">
                开启后,每个交易日到点自动生成大盘复盘报告并归档,静默执行。
                下次打开本页即可在历史列表看到新报告;也可选推送到飞书。
              </p>

              {/* 开关(只改本地草稿, 不提交) */}
              <label className="flex items-center justify-between rounded-btn bg-elevated/40 px-3 py-2.5">
                <span className="text-xs text-foreground">启用定时复盘</span>
                <button
                  type="button"
                  role="switch"
                  aria-checked={draft.enabled}
                  onClick={() => setDraft(d => ({ ...d, enabled: !d.enabled }))}
                  className={cn(
                    'relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors',
                    draft.enabled ? 'bg-accent' : 'bg-border',
                  )}
                >
                  <span className={cn('inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform', draft.enabled ? 'translate-x-[18px]' : 'translate-x-1')} />
                </button>
              </label>

              {/* 时间设置(仅开启时可编辑, 本地草稿) */}
              {draft.enabled && (
                <div className="mt-3 flex items-center gap-2 rounded-btn bg-elevated/40 px-3 py-2.5">
                  <span className="text-[11px] text-muted">每日</span>
                  <input
                    type="number" min={0} max={23} value={draft.hour}
                    onChange={e => setDraft(d => ({ ...d, hour: Math.max(0, Math.min(23, Number(e.target.value))) }))}
                    className="w-12 px-1.5 py-1 rounded-btn bg-base border border-border text-xs font-mono text-foreground text-center focus:outline-none focus:border-accent/50"
                  />
                  <span className="text-xs text-muted">:</span>
                  <input
                    type="number" min={0} max={59} value={draft.minute}
                    onChange={e => setDraft(d => ({ ...d, minute: Math.max(0, Math.min(59, Number(e.target.value))) }))}
                    className="w-12 px-1.5 py-1 rounded-btn bg-base border border-border text-xs font-mono text-foreground text-center focus:outline-none focus:border-accent/50"
                  />
                  <span className="text-[10px] text-muted/70">不早于 15:00 · 工作日执行</span>
                </div>
              )}

              {/* 推送渠道(多选, 独立常驻, 与定时无关, 即时生效) */}
              <div className="mt-3 rounded-btn bg-elevated/40 px-3 py-2.5">
                <div className="flex items-center justify-between">
                  <span className="text-xs text-foreground">生成后推送完整报告</span>
                  <span className="text-[10px] text-muted/70">{reviewPushChannels.length === 0 ? '未开启' : `${reviewPushChannels.length} 个渠道`}</span>
                </div>
                <div className="mt-2 space-y-1.5">
                  {pushOptions.map(ch => (
                    <button
                      key={ch.id}
                      type="button"
                      disabled={pushMut.isPending}
                      onClick={() => togglePushChannel(ch.id)}
                      className={cn(
                        'flex w-full items-center gap-2 rounded-btn border px-2.5 py-1.5 text-left transition-colors disabled:opacity-50',
                        reviewPushChannels.includes(ch.id)
                          ? 'border-accent/40 bg-accent/10'
                          : 'border-border/60 bg-base/40 hover:bg-base/60',
                      )}
                    >
                      <span className={cn('flex h-3 w-3 shrink-0 items-center justify-center rounded border', reviewPushChannels.includes(ch.id) ? 'border-accent bg-accent text-white' : 'border-border')}>
                        {reviewPushChannels.includes(ch.id) && <Check className="h-2.5 w-2.5" />}
                      </span>
                      <span className="text-[11px] text-foreground">{ch.name}</span>
                      <span className="text-[9px] text-muted">{ch.hint}</span>
                      <span className={cn('ml-auto text-[9px]', ch.configured ? 'text-emerald-500' : 'text-warning')}>
                        {ch.configured ? '已配置' : '未配置'}
                      </span>
                    </button>
                  ))}
                </div>
                <p className="mt-1.5 text-[10px] leading-relaxed text-muted/70">
                  手动或定时生成的复盘都会推送完整报告。复用「设置 → 实时监控」的 Webhook。
                  {reviewPushChannels.some(ch => !pushOptions.find(opt => opt.id === ch)?.configured) && (
                    <Link to="/settings?tab=monitoring" className="ml-1 text-accent hover:underline" onClick={() => setShowSchedule(false)}>
                      前往配置 →
                    </Link>
                  )}
                </p>
              </div>

              {!draft.enabled && (
                <p className="mt-3 text-[10px] text-muted/70">
                  当前: 已关闭。开启后将按设定时间自动复盘。
                </p>
              )}

              {/* 操作区: 取消 + 保存(统一提交开关+时间) */}
              <div className="mt-5 flex justify-end gap-2">
                <button
                  onClick={() => setShowSchedule(false)}
                  className="rounded-btn bg-elevated px-4 py-1.5 text-xs text-secondary transition-colors hover:text-foreground"
                >
                  取消
                </button>
                <button
                  onClick={() => reviewMut.mutate({ enabled: draft.enabled, hour: draft.hour, minute: draft.minute })}
                  disabled={reviewMut.isPending}
                  className="inline-flex items-center gap-1.5 rounded-btn bg-accent px-4 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent/90 disabled:opacity-50"
                >
                  {reviewMut.isPending ? '保存中…' : '保存'}
                </button>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  )
}

// ================================================================
// 市场摘要条 —— 复盘页的轻量上下文(非重复看板)
// 仅一行:三大指数涨跌 · 情绪分 · 涨停结构 · 成交额
// 详细数据请去 Dashboard 看,这里只给 AI 报告提供背景参照
// ================================================================
// 指数简称映射:全称太长(上证指数/深证成指/创业板指/科创综指)摘要条放不下,统一缩成单字
const INDEX_SHORT: Record<string, string> = {
  '上证指数': '上', '深证成指': '深', '创业板指': '创', '科创综指': '科', '科创50': '科',
}
function indexShort(name?: string | null, symbol?: string): string {
  if (!name) return symbol ?? '—'
  return INDEX_SHORT[name] ?? (name.replace(/指数|成指|A股|综指|50/g, '').slice(0, 2) || name.slice(0, 1))
}

// 批量替换文本中的指数全称为简称(用于历史列表 summary 显示,
// 兼容存量旧报告 —— 它们存盘时 summary 还是全称)。
const _INDEX_FULL_RE = /上证指数|深证成指|创业板指|科创综指|科创50/g
function shortenIndexNames(text: string): string {
  return text.replace(_INDEX_FULL_RE, (m) => INDEX_SHORT[m] ?? m)
}

// 从 summary 的指数段(如「上-2.26%、深-3.44%、创-4.07%、科-2.02%」)
// 解析出 [{name, pctStr, pctNum}],供列表项按涨跌染色渲染。
const _INDEX_PCT_RE = /([上深创科])([+-]?\d+\.\d+%)/g
function parseIndexPcts(indexSegment: string): { name: string; pctStr: string; pctNum: number }[] {
  const out: { name: string; pctStr: string; pctNum: number }[] = []
  for (const m of indexSegment.matchAll(_INDEX_PCT_RE)) {
    out.push({ name: m[1], pctStr: m[2], pctNum: parseFloat(m[2]) })
  }
  return out
}

function MarketSummaryBar({ data }: { data: OverviewMarket }) {
  const score = data.emotion?.score ?? null
  const emoColor = scoreColor(score)
  const indices = (data.indices ?? []).slice(0, 4)

  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded-card border border-border bg-surface/80 px-4 py-2.5">
      {/* 情绪分(带色徽章)—— 复盘的核心定调 */}
      <div className="flex items-center gap-2">
        <span
          className="grid h-8 w-8 shrink-0 place-items-center rounded font-mono text-xs font-bold tabular-nums"
          style={{ color: emoColor, backgroundColor: `${emoColor}1a` }}
        >
          {score ?? '—'}
        </span>
        <div className="leading-tight">
          <div className="text-[11px] font-medium text-foreground">{data.emotion?.label ?? '情绪'}</div>
          <div className="text-[9px] text-secondary">情绪温度</div>
        </div>
      </div>

      <div className="hidden h-7 w-px bg-border sm:block" />

      {/* 四大指数(简称:上深创科)*/}
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
        {indices.map(idx => (
          <div key={idx.symbol} className="flex items-center gap-1">
            <span className="text-[11px] text-secondary">{indexShort(idx.name, idx.symbol)}</span>
            <span className={cn('font-mono text-[11px] font-semibold tabular-nums', pctClass(idx.change_pct))}>
              {fmtPctAlready(idx.change_pct, 2, true)}
            </span>
          </div>
        ))}
      </div>

      <div className="hidden h-7 w-px bg-border sm:block" />

      {/* 涨跌结构 */}
      <div className="flex items-center gap-1.5 text-[11px]">
        <span className="text-secondary">涨跌</span>
        <span className="font-mono font-semibold text-bull">{data.breadth?.up ?? 0}</span>
        <span className="text-muted">/</span>
        <span className="font-mono font-semibold text-bear">{data.breadth?.down ?? 0}</span>
      </div>

      {/* 涨停结构 */}
      <div className="flex items-center gap-1.5 text-[11px]">
        <span className="text-secondary">涨停</span>
        <span className="font-mono font-semibold text-bull">{data.limit?.limit_up ?? 0}</span>
        <span className="text-secondary">封板 {(data.limit?.seal_rate ?? 0).toFixed(0)}%</span>
      </div>

      {/* 成交额 */}
      <div className="flex items-center gap-1.5 text-[11px]">
        <span className="text-secondary">成交</span>
        <span className="font-mono font-semibold text-foreground">{fmtBigNum(data.amount?.total)}</span>
      </div>
    </div>
  )
}

// ================================================================
// 报告面板(流式 + 错误 + 历史/完成态)
// ================================================================
function ReportPanel({
  phase, content, error, isGenerating, viewing, onCopy, onDownload, onRegenerate, reportEndRef,
}: {
  phase: ReviewPhase
  content: string
  error: string
  isGenerating: boolean
  viewing: AiReviewReport | null
  onCopy: () => void
  onDownload: () => void
  onRegenerate: () => void
  reportEndRef: React.RefObject<HTMLDivElement>
}) {
  if (phase === 'error') {
    return (
      <div className="flex flex-col items-center justify-center gap-3 rounded-card border border-border bg-surface/80 px-6 py-14">
        <div className="grid h-12 w-12 place-items-center rounded-full bg-danger/10">
          <AlertTriangle className="h-5 w-5 text-danger" />
        </div>
        <div className="text-sm font-medium text-foreground">复盘失败</div>
        <div className="max-w-md text-center text-xs text-secondary">{error || '请检查 AI 配置后重试'}</div>
        <button
          onClick={onRegenerate}
          className="mt-1 inline-flex items-center gap-1.5 rounded-btn bg-accent/15 px-3 py-1.5 text-xs text-accent transition-colors hover:bg-accent/20"
        >
          <RefreshCw className="h-3.5 w-3.5" />重新生成
        </button>
      </div>
    )
  }

  if (phase === 'idle' && !content) {
    return (
      <div className="flex min-h-[28rem] flex-col items-center justify-center gap-5 rounded-card border border-border bg-surface/80 px-6 py-16">
        <div className="relative">
          <div className="grid h-20 w-20 place-items-center rounded-2xl bg-gradient-to-br from-accent/20 to-purple-500/15 border border-accent/30">
            <BookOpenCheck className="h-9 w-9 text-accent" strokeWidth={1.8} />
          </div>
          <Sparkles className="absolute -right-1 -top-1 h-5 w-5 text-accent" />
        </div>
        <div className="text-center">
          <div className="text-base font-semibold text-foreground">AI 大盘复盘</div>
          <p className="mx-auto mt-2 max-w-sm text-xs leading-relaxed text-secondary">
            一键生成今日盘后复盘报告 —— 从一句话定调到明日交易计划,
            结构化输出可直接指导次日仓位与节奏。
          </p>
        </div>
        {/* 报告七节预览 —— 空状态也有内容感,暗示报告结构 */}
        <div className="mt-2 grid w-full max-w-md grid-cols-2 gap-2 sm:grid-cols-4">
          {[
            { icon: '🎯', label: '一句话定调' },
            { icon: '📊', label: '盘面总览' },
            { icon: '🔥', label: '板块主线' },
            { icon: '💰', label: '资金情绪' },
            { icon: '📰', label: '消息催化' },
            { icon: '🎯', label: '明日计划' },
            { icon: '⚠️', label: '风险提示' },
          ].map((s) => (
            <div key={s.label} className="flex flex-col items-center gap-1 rounded-btn bg-elevated/40 px-2 py-2">
              <span className="text-base">{s.icon}</span>
              <span className="text-[10px] text-secondary">{s.label}</span>
            </div>
          ))}
        </div>
        <div className="mt-2 flex items-center gap-1.5 text-[11px] text-muted">
          <Sparkles className="h-3 w-3 text-accent" />
          点击右上角「生成复盘」开始
        </div>
      </div>
    )
  }

  // 仅当显示生成内容(非查看历史)且正在生成时,才显示流式光标
  const showCursor = isGenerating && !viewing
  // 查看历史时(即使后台在生成)也能复制/下载该历史报告
  const showActions = !!content && (!isGenerating || !!viewing)
  const showViewingTag = !!viewing
  const isLoading = phase === 'loading' && !content

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className="overflow-hidden rounded-card border border-border bg-surface/80"
    >
      <div className="flex items-center justify-between border-b border-border bg-gradient-to-r from-accent/5 to-transparent px-4 py-2.5">
        <div className="flex items-center gap-1.5">
          {isGenerating ? <RefreshCw className="h-3.5 w-3.5 animate-spin text-accent" /> : <BookOpenCheck className="h-3.5 w-3.5 text-accent" />}
          <span className="text-xs font-medium text-foreground">
            {showViewingTag ? `历史复盘 · ${viewing!.as_of}` : isGenerating ? 'AI 正在复盘…' : '复盘报告'}
          </span>
        </div>
        {showActions && (
          <div className="flex items-center gap-1">
            <button onClick={onCopy} className="inline-flex items-center gap-1 rounded-btn bg-elevated px-2 py-1 text-[11px] text-secondary transition-colors hover:text-foreground hover:bg-elevated/70" title="复制全文">
              <Copy className="h-3 w-3" />复制
            </button>
            <button onClick={onDownload} className="inline-flex items-center gap-1 rounded-btn bg-elevated px-2 py-1 text-[11px] text-secondary transition-colors hover:text-foreground hover:bg-elevated/70" title="下载为 Markdown">
              <Download className="h-3 w-3" />下载
            </button>
          </div>
        )}
      </div>
      <div className="max-h-[calc(100vh-22rem)] overflow-y-auto px-5 py-4">
        {isLoading ? (
          <div className="flex flex-col items-center justify-center gap-3 py-16">
            <div className="relative">
              <div className="grid h-11 w-11 place-items-center rounded-full bg-gradient-to-br from-accent/20 to-purple-500/15 border border-accent/30">
                <Sparkles className="h-5 w-5 animate-pulse text-accent" />
              </div>
              <RefreshCw className="absolute -inset-1 h-13 w-13 animate-spin text-accent/30" style={{ animationDuration: '3s' }} />
            </div>
            <div className="text-sm text-foreground">AI 正在复盘今日盘面…</div>
            <div className="text-xs text-secondary">分析指数结构 · 连板梯队 · 板块轮动 · 资金情绪</div>
          </div>
        ) : (
          <div className="prose prose-invert max-w-none">
            <MarkdownRenderer content={content} />
            {showCursor && (
              <span className="ml-0.5 inline-block h-4 w-1.5 animate-pulse rounded-sm bg-accent align-middle" />
            )}
          </div>
        )}
        <div ref={reportEndRef} />
      </div>
    </motion.div>
  )
}

// ================================================================
// 历史面板
// ================================================================
function HistoryPanel({
  reports, loading, viewingId, generating, onView, onBackToGenerating, onDelete,
}: {
  reports: AiReviewReport[]
  loading: boolean
  viewingId: string | null
  generating: boolean
  onView: (r: AiReviewReport) => void
  onBackToGenerating: () => void
  onDelete: (id: string) => void
}) {
  const empty = !generating && reports.length === 0
  return (
    <div className="overflow-hidden rounded-card border border-border bg-surface/80">
      <div className="flex items-center gap-1.5 border-b border-border bg-gradient-to-r from-accent/5 to-transparent px-3 py-2.5">
        <History className="h-3.5 w-3.5 text-accent" />
        <span className="text-xs font-medium text-foreground">历史复盘</span>
        <span className="font-mono text-[10px] text-muted">({reports.length})</span>
      </div>
      <div className="max-h-[calc(100vh-26rem)] overflow-y-auto p-2">
        {loading ? (
          <div className="grid h-20 place-items-center"><RefreshCw className="h-4 w-4 animate-spin text-muted" /></div>
        ) : empty ? (
          <div className="flex flex-col items-center justify-center gap-2 px-3 py-10 text-center">
            <History className="h-7 w-7 text-muted/40" strokeWidth={1.5} />
            <div className="text-[11px] text-muted">暂无历史复盘</div>
            <div className="text-[10px] text-muted/60">生成完成后自动归档</div>
          </div>
        ) : (
          <div className="space-y-1">
            {/* 生成中占位项:列表顶部,点击回到正在生成的流式内容 */}
            {generating && (
              <div
                className={cn(
                  'flex items-center gap-2 rounded px-2 py-2 cursor-pointer transition-colors',
                  viewingId === null ? 'bg-accent/10 ring-1 ring-accent/20' : 'hover:bg-elevated/60',
                )}
                onClick={onBackToGenerating}
              >
                <div className="grid h-8 w-8 shrink-0 place-items-center rounded bg-accent/15">
                  <RefreshCw className="h-3.5 w-3.5 animate-spin text-accent" />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-[11px] font-medium text-accent">生成中…</div>
                  <div className="mt-0.5 truncate text-[10px] text-secondary">AI 正在复盘今日盘面</div>
                </div>
              </div>
            )}
            {reports.map((r) => {
              const color = scoreColor(r.emotion_score)
              return (
                <div
                  key={r.id}
                  className={cn(
                    'group flex items-center gap-2 rounded px-2 py-2 cursor-pointer transition-colors',
                    viewingId === r.id ? 'bg-accent/10 ring-1 ring-accent/20' : 'hover:bg-elevated/60',
                  )}
                  onClick={() => onView(r)}
                >
                  <div
                    className="grid h-8 w-8 shrink-0 place-items-center rounded font-mono text-[10px] font-bold tabular-nums"
                    style={{ color, backgroundColor: `${color}1a` }}
                  >
                    {r.emotion_score ?? '—'}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <span className="truncate text-[11px] font-medium text-foreground">{r.emotion_label ?? '—'}</span>
                      <span className="font-mono text-[10px] text-secondary">{r.as_of}</span>
                    </div>
                    <div className="mt-0.5 flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
                      {r.summary
                        ? (() => {
                            const pcts = parseIndexPcts(shortenIndexNames(r.summary).split('|')[0])
                            if (pcts.length === 0) {
                              return <span className="truncate text-[10px] text-secondary">{r.content.slice(0, 40)}</span>
                            }
                            return pcts.map((p) => (
                              <span key={p.name} className="inline-flex items-center gap-0.5 text-[10px]">
                                <span className="text-secondary">{p.name}</span>
                                <span className={cn('font-mono font-medium tabular-nums', pctClass(p.pctNum))}>{p.pctStr}</span>
                              </span>
                            ))
                          })()
                        : <span className="truncate text-[10px] text-secondary">{r.content.slice(0, 40)}</span>}
                    </div>
                    {r.created_at && (
                      <div className="mt-0.5 font-mono text-[9px] text-muted">{fmtArchivedAt(r.created_at)}</div>
                    )}
                  </div>
                  <button
                    onClick={(e) => { e.stopPropagation(); onDelete(r.id) }}
                    className="shrink-0 p-1 text-muted opacity-0 transition-all hover:text-bear group-hover:opacity-100"
                    title="删除"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}
