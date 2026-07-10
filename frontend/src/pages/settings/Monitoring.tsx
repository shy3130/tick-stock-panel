import { useState, useCallback, useEffect, useRef } from 'react'
import { Link } from 'react-router-dom'
import { useQueryClient, useMutation, useQuery } from '@tanstack/react-query'
import {
  Activity,
  Wifi,
  BarChart3,
  Flame,
  Zap,
  Webhook,
  ChevronDown,
} from 'lucide-react'
import {
  usePreferences,
  useQuoteStatus,
  useQuoteInterval,
  useCapabilities,
} from '@/lib/useSharedQueries'
import { useUpdateQuoteInterval, useToggleRealtimeQuotes } from '@/lib/useSharedMutations'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { tierRank } from '@/lib/capability-labels'
import { toast } from '@/components/Toast'
import { DepthConfigContent } from '@/components/data/DepthConfigCard'

// 页面 → 显示名
const PAGE_LABELS: Record<string, string> = {
  'overview-market': '看板',
  watchlist: '自选页',
  'limit-ladder': '连板梯队',
}

const SIDEBAR_INDEX_OPTIONS = [
  { symbol: '000001.SH', name: '上证指数' },
  { symbol: '399001.SZ', name: '深证成指' },
  { symbol: '399006.SZ', name: '创业板指' },
  { symbol: '000680.SH', name: '科创综指' },
]

// ===== 导出为 Panel 组件 (由 Settings.tsx 嵌入) =====

export function SettingsMonitoringPanel({ highlight }: { highlight?: string } = {}) {
  const qc = useQueryClient()
  const { data: prefs } = usePreferences()
  const { data: caps } = useCapabilities()
  const { data: quoteStatus } = useQuoteStatus()
  const { data: intervalData } = useQuoteInterval()
  const updateInterval = useUpdateQuoteInterval()
  const toggleQuote = useToggleRealtimeQuotes()
  const tier = tierRank(caps?.label ?? '')
  const isNoneTier = tier < 0
  const isFreeTier = tier === 0
  const realtimeEnabled = prefs?.realtime_quotes_enabled ?? false
  const refreshPages = prefs?.sse_refresh_pages ?? {}
  const limitLadderMonitor = prefs?.limit_ladder_monitor_enabled ?? false
  const hasDepth = !!caps?.capabilities?.['depth5.batch']
  // 新建监控规则时默认勾选的推送渠道 (全局默认值数组, 单条规则可独立修改)
  const webhookDefaultChannels = prefs?.webhook_default_channels ?? []
  const sidebarIndexSymbols = prefs?.sidebar_index_symbols ?? SIDEBAR_INDEX_OPTIONS.map(i => i.symbol)
  const indicesPinned = prefs?.indices_nav_pinned ?? true
  const isRunning = quoteStatus?.running ?? false
  const isTrading = quoteStatus?.is_trading_hours ?? false
  // 管道/数据修正运行期间实时行情被临时暂停 — 此时禁止开启
  const isPaused = quoteStatus?.paused ?? false
  const interval = intervalData?.interval ?? 10
  const minInterval = intervalData?.min_interval ?? 5
  const maxInterval = intervalData?.max_interval ?? 60
  const [intervalDraft, setIntervalDraft] = useState(interval)
  const feishuWebhookUrl = prefs?.feishu_webhook_url ?? ''
  const feishuWebhookSecret = prefs?.feishu_webhook_secret ?? ''
  const [feishuDraft, setFeishuDraft] = useState(feishuWebhookUrl)
  const [feishuSecretDraft, setFeishuSecretDraft] = useState(feishuWebhookSecret)
  const [feishuError, setFeishuError] = useState('')
  // 企业微信 webhook
  const wecomWebhookUrl = prefs?.wecom_webhook_url ?? ''
  const [wecomDraft, setWecomDraft] = useState(wecomWebhookUrl)
  const [wecomError, setWecomError] = useState('')
  // 企业微信智能机器人 (BotID + Secret, 长连接通道)
  const wecomBotId = prefs?.wecom_bot_id ?? ''
  const wecomBotSecret = prefs?.wecom_bot_secret ?? ''
  const wecomBotEnabled = prefs?.wecom_bot_enabled ?? false
  const [botIdDraft, setBotIdDraft] = useState(wecomBotId)
  const [botSecretDraft, setBotSecretDraft] = useState(wecomBotSecret)
  const [botError, setBotError] = useState('')
  const [botStatus, setBotStatus] = useState<{connected: boolean; last_error: string} | null>(null)
  // 飞书渠道配置区展开态 (推送通知卡片内)
  const [channelOpen, setChannelOpen] = useState(false)
  // 企业微信渠道配置区展开态
  const [wecomOpen, setWecomOpen] = useState(false)
  // 智能机器人配置区展开态
  const [botOpen, setBotOpen] = useState(false)
  useEffect(() => {
    setFeishuDraft(feishuWebhookUrl)
    setFeishuSecretDraft(feishuWebhookSecret)
  }, [feishuWebhookUrl, feishuWebhookSecret])
  useEffect(() => {
    setWecomDraft(wecomWebhookUrl)
  }, [wecomWebhookUrl])
  useEffect(() => {
    setBotIdDraft(wecomBotId)
    setBotSecretDraft(wecomBotSecret)
  }, [wecomBotId, wecomBotSecret])
  const watchlistSymbols = prefs?.realtime_watchlist_symbols ?? []
  const watchlist = useQuery({
    queryKey: QK.watchlist,
    queryFn: () => api.watchlistList(),
    enabled: isFreeTier && watchlistSymbols.length > 0,
  })
  const watchlistNameBySymbol = new Map(
    (watchlist.data?.symbols ?? []).map(row => [row.symbol, row.name] as const),
  )

  const save = useCallback(async (cfg: Record<string, unknown>) => {
    try {
      await api.updateRealtimeMonitorConfig(cfg)
      qc.invalidateQueries({ queryKey: QK.preferences })
    } catch (e) {
      // 忽略 — Toast 已在 request 层处理
    }
  }, [qc])

  const handleToggleQuote = useCallback(async (enabled: boolean) => {
    await toggleQuote.mutateAsync(enabled)
    qc.invalidateQueries({ queryKey: QK.preferences })
    qc.invalidateQueries({ queryKey: QK.quoteStatus })
  }, [toggleQuote, qc])

  const toggleSidebarIndex = useCallback((symbol: string, visible: boolean) => {
    const selected = new Set(sidebarIndexSymbols)
    if (visible) selected.add(symbol)
    else selected.delete(symbol)
    const next = SIDEBAR_INDEX_OPTIONS
      .map(item => item.symbol)
      .filter(s => selected.has(s))
    save({ sidebar_index_symbols: next })
  }, [save, sidebarIndexSymbols])

  const toggleIndicesPin = useCallback((pinned: boolean) => {
    api.updateIndicesNavPinned(pinned).then(() => qc.invalidateQueries({ queryKey: QK.preferences }))
  }, [qc])

  const toggleLimitLadderMonitor = useCallback(async (enabled: boolean) => {
    await api.updateLimitLadderMonitor(enabled)
    qc.invalidateQueries({ queryKey: QK.preferences })
  }, [qc])

  // 勾选/取消勾选某个默认推送渠道 (飞书 / 企业微信 各自独立)
  const toggleDefaultChannel = useCallback(async (ch: string, enabled: boolean) => {
    const cur = prefs?.webhook_default_channels ?? []
    const next = enabled ? [...cur, ch] : cur.filter(c => c !== ch)
    await api.updateWebhookDefaultChannels(next)
    qc.invalidateQueries({ queryKey: QK.preferences })
  }, [qc, prefs])

  const saveFeishuWebhook = useMutation({
    mutationFn: ({ url, secret }: { url: string; secret: string }) => api.updateFeishuWebhook(url, secret),
    onSuccess: () => {
      setFeishuError('')
      toast('飞书 Webhook 已保存', 'success')
      qc.invalidateQueries({ queryKey: QK.preferences })
    },
    onError: (err: any) => setFeishuError(String(err?.message ?? '保存失败')),
  })
  const FEISHU_PREFIX = 'https://open.feishu.cn/open-apis/bot/v2/hook/'
  const submitFeishu = useCallback(() => {
    const url = feishuDraft.trim()
    const secret = feishuSecretDraft.trim()
    if (url && !url.startsWith(FEISHU_PREFIX)) {
      setFeishuError('地址需以 ' + FEISHU_PREFIX + ' 开头')
      return
    }
    saveFeishuWebhook.mutate({ url, secret })
  }, [feishuDraft, feishuSecretDraft, saveFeishuWebhook])

  const saveWecomWebhook = useMutation({
    mutationFn: (url: string) => api.updateWecomWebhook(url),
    onSuccess: () => {
      setWecomError('')
      toast('企业微信 Webhook 已保存', 'success')
      qc.invalidateQueries({ queryKey: QK.preferences })
    },
    onError: (err: any) => setWecomError(String(err?.message ?? '保存失败')),
  })
  const WECOM_PREFIX = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send'
  const submitWecom = useCallback(() => {
    const url = wecomDraft.trim()
    // 允许完整 URL 或纯 key (36位UUID样式)
    if (url && !url.startsWith(WECOM_PREFIX) && url.length < 20) {
      setWecomError('请输入完整 Webhook 地址或纯 key (至少 20 位)')
      return
    }
    saveWecomWebhook.mutate(url)
  }, [wecomDraft, saveWecomWebhook])

  // 智能机器人 (BotID + Secret) 保存 → 后端立即重建连接
  const saveWecomBot = useMutation({
    mutationFn: ({ botId, secret }: { botId: string; secret: string }) =>
      api.updateWecomBot(botId, secret, true),
    onSuccess: (data) => {
      setBotError('')
      toast('智能机器人凭证已保存, 正在连接…', 'success')
      setBotStatus({
        connected: data.wecom_bot_status?.connected ?? false,
        last_error: data.wecom_bot_status?.last_error ?? '',
      })
      qc.invalidateQueries({ queryKey: QK.preferences })
    },
    onError: (err: any) => setBotError(String(err?.message ?? '保存失败')),
  })
  const submitBot = useCallback(() => {
    saveWecomBot.mutate({ botId: botIdDraft.trim(), secret: botSecretDraft.trim() })
  }, [botIdDraft, botSecretDraft, saveWecomBot])

  // 智能机器人长连接开关(不改动凭证): 开启→连接, 关闭→断开
  const toggleBotConnection = useMutation({
    mutationFn: (enabled: boolean) => api.toggleWecomBot(enabled),
    onSuccess: (data) => {
      setBotStatus({
        connected: data.wecom_bot_status?.connected ?? false,
        last_error: data.wecom_bot_status?.last_error ?? '',
      })
      qc.invalidateQueries({ queryKey: QK.preferences })
    },
  })

  const runFix = useMutation({
    mutationFn: () => api.runLimitLadderFix(),
    onSuccess: (data) => {
      toast(data.msg, data.ok ? 'success' : 'error')
      // 修正后连板梯队数据变了, 刷新相关缓存
      qc.invalidateQueries({ queryKey: ['limit-ladder'] })
    },
    onError: () => toast('修正请求失败', 'error'),
  })

  useEffect(() => {
    setIntervalDraft(interval)
  }, [interval])

  useEffect(() => {
    if (intervalDraft === interval) return
    const t = window.setTimeout(() => {
      updateInterval.mutate(intervalDraft)
    }, 2000)
    return () => window.clearTimeout(t)
  }, [intervalDraft, interval, updateInterval])

  // highlight=depth-fix 时闪烁高亮连板梯队修正卡片
  const [flash, setFlash] = useState(false)
  const flashedRef = useRef(false)
  useEffect(() => {
    if (highlight === 'depth-fix' && !flashedRef.current) {
      flashedRef.current = true
      // 延迟一帧确保 DOM 已渲染, 再触发闪烁
      requestAnimationFrame(() => {
        setFlash(true)
        const t = setTimeout(() => setFlash(false), 2000)
        return () => clearTimeout(t)
      })
    }
  }, [highlight])

  if (isNoneTier) {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-center">
        <div className="inline-flex items-center justify-center w-14 h-14 rounded-2xl
                        bg-gradient-to-br from-purple-500/20 to-blue-500/20 mb-5">
          <Activity className="h-7 w-7 text-purple-400" />
        </div>
        <h2 className="text-lg font-medium text-foreground mb-2">实时监控</h2>
        <p className="text-sm text-secondary max-w-md mb-6">
          实时行情需要 Free 及以上档位。None 档可使用 free-api 获取历史日K（当日数据需盘后1-2小时），但不能调用付费服务器实时接口。
        </p>
        <a
          href="/settings?tab=account"
          className="inline-flex items-center gap-2 px-5 py-2.5 rounded-btn
                     bg-accent text-white text-sm font-medium
                     hover:bg-accent/90 transition-colors"
        >
          配置 API Key 升级
        </a>
      </div>
    )
  }

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[1fr_1fr] gap-6 max-w-5xl">
      {/* ========== 左列 ========== */}
      <div className="space-y-6">
        {/* 行情状态 — 开关 + 间隔 */}
        <Card icon={Activity} title="行情轮询">
          <ToggleRow
            label="实时行情"
            desc={
              isPaused ? '数据同步运行中，已临时暂停'
              : isRunning && isTrading ? '运行中'
              : isRunning ? '运行中 (非交易时段)'
              : '已关闭'
            }
            checked={realtimeEnabled}
            onChange={handleToggleQuote}
            disabled={isPaused}
          />

          <div className="mt-3 pt-3 border-t border-border">
            <div className="flex items-center justify-between gap-4 py-1">
              <div className="min-w-0">
                <div className="text-sm text-foreground">轮询间隔</div>
                <div className="text-[11px] text-muted">
                  {isFreeTier ? '每轮拉取自选股实时行情的时间间隔' : '每轮拉取全市场行情的时间间隔'}
                </div>
              </div>
              <span className="text-[11px] font-mono text-foreground shrink-0 tabular-nums">
                {intervalDraft < 1 ? intervalDraft.toFixed(1) : intervalDraft.toFixed(0)}s
              </span>
            </div>
            <div className="flex items-center gap-3 mt-2">
              <input
                type="range"
                min={minInterval}
                max={maxInterval}
                step={minInterval < 1 ? 0.1 : minInterval < 3 ? 0.5 : 1}
                value={intervalDraft}
                onChange={(e) => setIntervalDraft(parseFloat(e.target.value))}
                className="flex-1 h-1 accent-accent cursor-pointer"
              />
              <span className="text-[10px] text-muted shrink-0">
                {intervalDraft !== interval ? '2秒后保存' : `${minInterval}s — ${maxInterval}s`}
              </span>
            </div>
          </div>
        </Card>

        {isFreeTier && (
        <Card icon={Activity} title="自选股实时">
          <div className="mb-3 rounded-btn border border-accent/25 bg-accent/10 px-3 py-2 text-xs font-medium leading-snug text-accent">
            Free 档开启实时行情时自动监控「自选」页面前 5 个标的，最低 6 秒刷新。
          </div>
          {watchlistSymbols.length > 0 ? (
            <div className="space-y-1.5">
              {watchlistSymbols.map(symbol => {
                const name = watchlistNameBySymbol.get(symbol)
                return (
                  <div key={symbol} className="flex items-center justify-between rounded-btn bg-base/50 border border-border px-2 py-1.5">
                    <div className="min-w-0 flex items-baseline gap-1.5">
                      <span className="text-xs font-mono text-foreground">{symbol}</span>
                      {name && <span className="truncate text-[11px] text-secondary">{name}</span>}
                    </div>
                    <span className="text-[10px] text-muted shrink-0">自选页</span>
                  </div>
                )
              })}
            </div>
          ) : (
            <div className="rounded-btn border border-border bg-base/40 px-3 py-3 text-xs text-muted">
              自选列表为空，Free 实时行情开启前请先添加自选股。
            </div>
          )}
          <div className="mt-2 flex items-center justify-between gap-3">
            <span className="text-[10px] text-muted">当前 {watchlistSymbols.length}/5 只</span>
            <Link
              to="/watchlist"
              className="px-3 py-1 rounded-btn bg-elevated text-secondary text-xs font-medium hover:text-foreground transition-colors"
            >
              管理自选
            </Link>
          </div>
        </Card>
        )}
        {!isFreeTier && (
        <Card icon={Wifi} title="页面实时刷新">
          <p className="text-xs text-secondary mb-4">
            选择哪些页面跟随 SSE 实时刷新数据。关闭的页面不会被推送，
            但行情轮询和策略监控不受影响。
          </p>
          <div className="space-y-2">
            {Object.entries(PAGE_LABELS).map(([key, label]) => (
              <ToggleRow
                key={key}
                label={label}
                desc={`SSE 推送时刷新 ${label} 数据`}
                checked={refreshPages[key] !== false}
                onChange={(v) => save({ sse_refresh_pages: { ...refreshPages, [key]: v } })}
              />
            ))}
          </div>
        </Card>
        )}

        {/* 自选列表分时图实时刷新 (默认关闭, 开启后盘中 15s 轮询刷新分时数据) */}
        <Card icon={Activity} title="分时图刷新">
          <ToggleRow
            label="自选分时图实时刷新"
            desc="开启后自选列表的分时图盘中每 15 秒自动刷新（需 Pro+ 权限）。关闭时仅打开页面时拉取一次。"
            checked={prefs?.minute_intraday_refresh ?? false}
            onChange={(v) => save({ minute_intraday_refresh: v })}
          />
        </Card>

        {!isFreeTier && (
        <Card icon={BarChart3} title="左侧菜单指数">
          <p className="text-xs text-secondary mb-4">
            选择实时行情开启时，左侧菜单底部显示哪些指数点位和涨跌幅。
          </p>
          <div className="space-y-2">
            {SIDEBAR_INDEX_OPTIONS.map(item => (
              <ToggleRow
                key={item.symbol}
                label={item.name}
                desc={item.symbol}
                checked={sidebarIndexSymbols.includes(item.symbol)}
                onChange={(v) => toggleSidebarIndex(item.symbol, v)}
              />
            ))}
          </div>
          <div className="mt-3 pt-3 border-t border-border">
            <ToggleRow
              label="固定显示"
              desc={indicesPinned ? '指数卡片常驻显示（即使实时行情关闭）' : '跟随实时行情开关（仅实时开时显示）'}
              checked={indicesPinned}
              onChange={toggleIndicesPin}
            />
          </div>
        </Card>
        )}
      </div>

      {/* ========== 右列 ========== */}
      <div className="space-y-6">
        {/* 连板梯队降级修正 (移至右列顶部) */}
        <div
          id="depth-fix"
          className={`rounded-card transition-all duration-500 ${flash ? 'ring-2 ring-accent/60 ring-offset-2 ring-offset-base scale-[1.01]' : 'ring-0 ring-transparent'}`}
        >
        <Card
          icon={Flame}
          title="连板梯队降级修正"
          badge={!hasDepth ? '需 Pro+' : undefined}
          right={hasDepth ? (
            <button
              onClick={() => runFix.mutate()}
              disabled={runFix.isPending}
              className="inline-flex items-center gap-1 px-2 py-1 rounded text-[11px]
                         bg-accent/15 text-accent hover:bg-accent/25 transition-colors
                         disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <Zap className="h-3 w-3" />
              {runFix.isPending ? '修正中…' : '立即修正'}
            </button>
          ) : undefined}
        >
          {hasDepth ? (
            <>
              <p className="text-xs text-secondary mb-4">
                通过五档盘口实时修正真假涨停/跌停。真封板显示封单量,假涨停(收盘价=涨停价但卖一有量)归入炸板。
                盘中按设定间隔轮询,收盘后自动定版。
              </p>
              <ToggleRow
                label="启用真假板修正"
                desc="开启后盘中自动拉取五档盘口修正真假板"
                checked={limitLadderMonitor}
                onChange={toggleLimitLadderMonitor}
              />
              <div className="mt-4 pt-3 border-t border-border">
                <div className="text-[10px] uppercase tracking-widest text-muted mb-3">
                  五档盘口配置
                </div>
                <DepthConfigContent disabled={!limitLadderMonitor} />
              </div>
            </>
          ) : (
            <DepthConfigContent disabled />
          )}
        </Card>
        </div>

        {/* 推送通知 — 监控告警的外部推送渠道 (全局配置)。
            飞书 / 企业微信已实现; QMT/ptrade 待定。
            每个渠道合并成一行: 勾选=新建规则默认推送, 点行展开地址配置。 */}
        <Card icon={Webhook} title="推送通知">
          <p className="text-xs text-secondary mb-3">
            监控规则命中后,可把告警推送到外部。勾选渠道作为<b className="text-foreground/80">新建规则的默认推送</b>,
            单条规则仍可在编辑页独立修改。
          </p>

          {/* 渠道列表 — 每行一个渠道, 勾选默认 + 点行展开地址配置 */}
          <div className="space-y-2">
            {/* 飞书 (可用): 勾选默认 + 展开地址配置 */}
            <div className="rounded-btn border border-border/60 bg-base/40 overflow-hidden">
              <div
                onClick={() => setChannelOpen(o => !o)}
                className="flex items-center gap-2 px-2.5 py-2 cursor-pointer transition-colors hover:bg-base/60"
              >
                <input
                  type="checkbox"
                  checked={webhookDefaultChannels.includes('feishu')}
                  onChange={e => { e.stopPropagation(); toggleDefaultChannel('feishu', e.target.checked) }}
                  onClick={e => e.stopPropagation()}
                  title="作为新建规则的默认推送渠道"
                  className="h-3 w-3 accent-accent cursor-pointer"
                />
                <span className="text-[11px] font-medium text-foreground">飞书</span>
                <span className="text-[9px] text-muted">群推送 Webhook</span>
                {webhookDefaultChannels.includes('feishu') && (
                  <span className="rounded bg-accent/15 px-1 py-px text-[9px] text-accent">默认</span>
                )}
                <span className={`ml-auto text-[9px] ${feishuWebhookUrl ? 'text-emerald-500' : 'text-warning'}`}>
                  {feishuWebhookUrl ? '已配置' : '未配置'}
                </span>
                <ChevronDown className={`h-3 w-3 text-muted transition-transform ${channelOpen ? 'rotate-180' : ''}`} />
              </div>

              {/* 飞书地址配置 — 行内展开 */}
              {channelOpen && (
                <div className="border-t border-border/60 bg-base/30 p-3">
                  <label className="block space-y-1.5">
                    <span className="text-[11px] text-muted">Webhook 地址</span>
                    <input
                      value={feishuDraft}
                      onChange={e => setFeishuDraft(e.target.value)}
                      placeholder={FEISHU_PREFIX + 'xxxxxxxx'}
                      className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground focus:outline-none focus:border-accent/50"
                    />
                  </label>

                  <label className="block mt-2 space-y-1.5">
                    <span className="text-[11px] text-muted">签名密钥 (可选 · 启用签名校验时填)</span>
                    <input
                      type="password"
                      value={feishuSecretDraft}
                      onChange={e => setFeishuSecretDraft(e.target.value)}
                      placeholder="机器人未启用签名校验则留空"
                      className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground focus:outline-none focus:border-accent/50"
                    />
                  </label>

                  {feishuError && (
                    <div className="mt-2 text-[11px] text-danger">{feishuError}</div>
                  )}

                  <div className="mt-2 flex items-center gap-2">
                    <button
                      onClick={submitFeishu}
                      disabled={saveFeishuWebhook.isPending || (feishuDraft.trim() === feishuWebhookUrl && feishuSecretDraft.trim() === feishuWebhookSecret)}
                      className="px-3 py-1.5 rounded-btn bg-accent text-base text-xs font-medium disabled:opacity-50 cursor-pointer hover:bg-accent/90 transition-colors"
                    >
                      {saveFeishuWebhook.isPending ? '保存中…' : '保存'}
                    </button>
                    {feishuWebhookUrl && (
                      <span className="text-[10px] text-emerald-500">● 已配置</span>
                    )}
                  </div>

                  <details className="mt-3 text-[10px] text-muted">
                    <summary className="cursor-pointer hover:text-secondary">如何获取飞书 Webhook 地址?</summary>
                    <ol className="mt-1.5 space-y-1 pl-4 list-decimal leading-relaxed">
                      <li>打开飞书,进入目标群聊 → 群设置 → <b>群推送 Webhook</b></li>
                      <li>点击「添加机器人」→ 选择「<b>自定义机器人</b>」</li>
                      <li>填写机器人名称后添加,复制生成的 Webhook 地址</li>
                      <li>安全设置若启用了「<b>签名校验</b>」,把密钥一并复制填到「签名密钥」框</li>
                      <li>粘贴到上方输入框并保存</li>
                    </ol>
                    <p className="mt-1.5 pl-4 text-muted/70">
                      📖 官方文档:
                      <a href="https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot?lang=zh-CN" target="_blank" rel="noreferrer" className="text-accent hover:text-accent/80">
                        自定义机器人使用指南 ↗
                      </a>
                    </p>
                  </details>
                </div>
              )}
            </div>

            {/* 企业微信群推送 Webhook (可用): 与飞书并列, 勾选默认 + 展开地址配置 */}
            <div className="rounded-btn border border-border/60 bg-base/40 overflow-hidden">
              <div
                onClick={() => setWecomOpen(o => !o)}
                className="flex items-center gap-2 px-2.5 py-2 cursor-pointer transition-colors hover:bg-base/60"
              >
                <input
                  type="checkbox"
                  checked={webhookDefaultChannels.includes('wecom')}
                  onChange={e => { e.stopPropagation(); toggleDefaultChannel('wecom', e.target.checked) }}
                  onClick={e => e.stopPropagation()}
                  title="作为新建规则的默认推送渠道"
                  className="h-3 w-3 accent-accent cursor-pointer"
                />
                <span className="text-[11px] font-medium text-foreground">企业微信</span>
                <span className="text-[9px] text-muted">群推送 Webhook</span>
                {webhookDefaultChannels.includes('wecom') && (
                  <span className="rounded bg-accent/15 px-1 py-px text-[9px] text-accent">默认</span>
                )}
                <span className={`ml-auto text-[9px] ${wecomWebhookUrl ? 'text-emerald-500' : 'text-warning'}`}>
                  {wecomWebhookUrl ? '已配置' : '未配置'}
                </span>
                <ChevronDown className={`h-3 w-3 text-muted transition-transform ${wecomOpen ? 'rotate-180' : ''}`} />
              </div>

              {wecomOpen && (
                <div className="border-t border-border/60 bg-base/30 p-3">
                  <label className="block space-y-1.5">
                    <span className="text-[11px] text-muted">Webhook 地址 或 Key</span>
                    <input
                      value={wecomDraft}
                      onChange={e => setWecomDraft(e.target.value)}
                      placeholder={WECOM_PREFIX + '?key=xxxxxxxx' + ' 或直接填 key'}
                      className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground focus:outline-none focus:border-accent/50"
                    />
                  </label>

                  {wecomError && (
                    <div className="mt-2 text-[11px] text-danger">{wecomError}</div>
                  )}

                  <div className="mt-2 flex items-center gap-2">
                    <button
                      onClick={submitWecom}
                      disabled={saveWecomWebhook.isPending || wecomDraft.trim() === wecomWebhookUrl}
                      className="px-3 py-1.5 rounded-btn bg-accent text-base text-xs font-medium disabled:opacity-50 cursor-pointer hover:bg-accent/90 transition-colors"
                    >
                      {saveWecomWebhook.isPending ? '保存中…' : '保存'}
                    </button>
                    {wecomWebhookUrl && (
                      <span className="text-[10px] text-emerald-500">● 已配置</span>
                    )}
                  </div>

                  <details className="mt-3 text-[10px] text-muted">
                    <summary className="cursor-pointer hover:text-secondary">如何获取企业微信 Webhook 地址?</summary>
                    <ol className="mt-1.5 space-y-1 pl-4 list-decimal leading-relaxed">
                      <li>打开企业微信,进入目标群聊 → 右上角「...」→ <b>群推送 Webhook</b></li>
                      <li>点击「添加」→ 选择「<b>消息推送</b>」→ 填写名字</li>
                      <li>复制生成的 <b>Webhook 地址</b>(含 key 参数),粘贴到上方输入框</li>
                      <li>也可只复制 key 参数部分(= 后面的内容)填入</li>
                      <li>企业微信群的消息可同步到绑定的个人微信,实现"微信推送"</li>
                    </ol>
                    <p className="mt-1.5 pl-4 text-muted/70">
                      📖 官方文档:
                      <a href="https://developer.work.weixin.qq.com/document/path/91770" target="_blank" rel="noreferrer" className="text-accent hover:text-accent/80">
                        群推送 Webhook 使用指南 ↗
                      </a>
                    </p>
                  </details>
                </div>
              )}
            </div>

            {/* 企业微信智能机器人 (BotID + Secret): 长连接通道, 与群推送 Webhook 并列 */}
            <div className="rounded-btn border border-border/60 bg-base/40 overflow-hidden">
              <div
                onClick={() => setBotOpen(o => !o)}
                className="flex items-center gap-2 px-2.5 py-2 cursor-pointer transition-colors hover:bg-base/60"
              >
                <input
                  type="checkbox"
                  checked={wecomBotEnabled}
                  onChange={e => { e.stopPropagation(); toggleBotConnection.mutate(e.target.checked) }}
                  onClick={e => e.stopPropagation()}
                  disabled={!wecomBotId || toggleBotConnection.isPending}
                  title="开启后建立长连接保活, 关闭则断开"
                  className="h-3 w-3 accent-accent cursor-pointer disabled:opacity-40"
                />
                <span className="text-[11px] font-medium text-foreground">企业微信</span>
                <span className="text-[9px] text-muted">智能机器人</span>
                <span className={`ml-auto text-[9px] ${wecomBotId ? (botStatus?.connected ? 'text-emerald-500' : 'text-warning') : 'text-muted'}`}>
                  {wecomBotId ? (botStatus?.connected ? '已连接' : (wecomBotEnabled ? '连接中' : '已配置')) : '未配置'}
                </span>
                <ChevronDown className={`h-3 w-3 text-muted transition-transform ${botOpen ? 'rotate-180' : ''}`} />
              </div>

              {botOpen && (
                <div className="border-t border-border/60 bg-base/30 p-3">
                  <p className="mb-2.5 text-[10px] text-muted leading-relaxed">
                    勾选卡片左侧开关可启用长连接保活(开启后后端持续保持与企业微信的
                    WebSocket 连接)。保存凭证后需勾选才会连接, 取消勾选则立即断开。
                  </p>
                  <label className="block space-y-1.5">
                    <span className="text-[11px] text-muted">BotID</span>
                    <input
                      value={botIdDraft}
                      onChange={e => setBotIdDraft(e.target.value)}
                      placeholder="智能机器人的唯一标识"
                      className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground focus:outline-none focus:border-accent/50"
                    />
                  </label>

                  <label className="block mt-2 space-y-1.5">
                    <span className="text-[11px] text-muted">Secret (长连接专用密钥)</span>
                    <input
                      type="password"
                      value={botSecretDraft}
                      onChange={e => setBotSecretDraft(e.target.value)}
                      placeholder="开启长连接 API 模式后获取的密钥"
                      className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground focus:outline-none focus:border-accent/50"
                    />
                  </label>

                  {botError && (
                    <div className="mt-2 text-[11px] text-danger">{botError}</div>
                  )}

                  {botStatus?.last_error && !botError && (
                    <div className="mt-2 text-[11px] text-warning">连接异常: {botStatus.last_error}</div>
                  )}

                  <div className="mt-2 flex items-center gap-2">
                    <button
                      onClick={submitBot}
                      disabled={saveWecomBot.isPending || (botIdDraft.trim() === wecomBotId && botSecretDraft.trim() === wecomBotSecret)}
                      className="px-3 py-1.5 rounded-btn bg-accent text-base text-xs font-medium disabled:opacity-50 cursor-pointer hover:bg-accent/90 transition-colors"
                    >
                      {saveWecomBot.isPending ? '保存中…' : '保存并连接'}
                    </button>
                    {wecomBotId && (
                      <span className="text-[10px] text-emerald-500">● 已配置</span>
                    )}
                  </div>

                  <details className="mt-3 text-[10px] text-muted">
                    <summary className="cursor-pointer hover:text-secondary">如何获取 BotID 和 Secret?</summary>
                    <ol className="mt-1.5 space-y-1 pl-4 list-decimal leading-relaxed">
                      <li>登录<b>企业微信管理后台</b> → 应用管理 → <b>智能机器人</b> → 创建机器人</li>
                      <li>填写名称、头像后进入机器人配置页</li>
                      <li>开启「<b>API 模式</b>」→ 选择「<b>长连接</b>」方式(另一项"回调URL"需公网IP)</li>
                      <li>页面显示 <b>BotID</b> 和 <b>Secret</b>, 复制填到上方输入框</li>
                    </ol>
                    <p className="mt-1.5 pl-4 text-muted/70">
                      💡 智能机器人支持 @交互和流式回复, 与群推送 Webhook(单向推送)互补。
                      保存后后端会自动建立 WebSocket 长连接保活。
                    </p>
                    <p className="mt-1.5 pl-4 text-muted/70">
                      📖 官方文档:
                      <a href="https://developer.work.weixin.qq.com/document/path/101463" target="_blank" rel="noreferrer" className="text-accent hover:text-accent/80">
                        智能机器人长连接 ↗
                      </a>
                    </p>
                  </details>
                </div>
              )}
            </div>

            {/* 占位渠道 — 不可点 */}
            {[
              { name: 'QMT', hint: '量化交易终端', status: '待定' },
              { name: 'ptrade', hint: '量化交易终端', status: '待定' },
            ].map(ch => (
              <div
                key={ch.name}
                className="flex items-center gap-2 rounded-btn border border-border/40 bg-base/20 px-2.5 py-2 opacity-60"
              >
                <input type="checkbox" disabled className="h-3 w-3 accent-accent" />
                <span className="text-[11px] text-secondary">{ch.name}</span>
                <span className="text-[9px] text-muted">{ch.hint}</span>
                <span className="ml-auto rounded bg-muted/10 px-1 py-px text-[9px] text-muted">{ch.status}</span>
              </div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  )
}


// ===== ToggleRow =====

function ToggleRow({
  label,
  desc,
  checked,
  onChange,
  icon: Icon,
  disabled,
}: {
  label: string
  desc: string
  checked: boolean
  onChange: (v: boolean) => void
  icon?: React.ComponentType<{ className?: string }>
  disabled?: boolean
}) {
  return (
    <div className="flex items-center justify-between gap-4 py-2">
      <div className="min-w-0 flex items-start gap-2">
        {Icon && <Icon className="h-3.5 w-3.5 text-secondary shrink-0 mt-0.5" />}
        <div className="min-w-0">
          <div className="text-sm text-foreground">{label}</div>
          <div className="text-[11px] text-muted truncate">{desc}</div>
        </div>
      </div>
      <button
        onClick={() => !disabled && onChange(!checked)}
        disabled={disabled}
        className={`relative inline-flex h-5 w-9 items-center rounded-full shrink-0 transition-colors duration-200 ${
          checked ? 'bg-accent' : 'bg-elevated'
        } ${disabled ? 'opacity-40 cursor-not-allowed' : 'cursor-pointer'}`}
      >
        <span
          className={`inline-block h-3.5 w-3.5 rounded-full bg-white shadow-sm transition-transform duration-200 ${
            checked ? 'translate-x-[18px]' : 'translate-x-[3px]'
          }`}
        />
      </button>
    </div>
  )
}


// ===== 通用卡片 =====

interface CardProps {
  icon: React.ComponentType<{ className?: string }>
  title: string
  badge?: string
  right?: React.ReactNode
  children: React.ReactNode
}

function Card({ icon: Icon, title, badge, right, children }: CardProps) {
  return (
    <section className="rounded-card border border-border bg-surface p-5">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2.5">
          <Icon className="h-4 w-4 text-secondary" />
          <h2 className="text-sm font-medium text-foreground">{title}</h2>
          {badge && (
            <span className="px-1.5 py-0.5 text-[10px] font-mono rounded bg-elevated text-muted">
              {badge}
            </span>
          )}
        </div>
        {right}
      </div>
      {children}
    </section>
  )
}
