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

const EXTRA_WEBHOOK_CHANNELS = [
  { id: 'dingtalk', name: '钉钉', hint: '群机器人' },
  { id: 'wecom', name: '企微', hint: '群机器人' },
  { id: 'meow', name: 'MeoW', hint: '个人推送' },
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
  const quoteMode = quoteStatus?.mode ?? 'none'
  const realtimeAllowed = prefs?.realtime_allowed ?? quoteStatus?.realtime_allowed ?? quoteMode !== 'none'
  const isNoneTier = !realtimeAllowed
  const isFreeTier = quoteMode === 'watchlist'
  const realtimeEnabled = prefs?.realtime_quotes_enabled ?? false
  const refreshPages = prefs?.sse_refresh_pages ?? {}
  const limitLadderMonitor = prefs?.limit_ladder_monitor_enabled ?? false
  const hasDepth = !!caps?.capabilities?.['depth5.batch']
  // 新建监控规则时是否默认勾选飞书推送 (全局默认值, 单条规则可独立修改)
  const webhookDefault = prefs?.webhook_enabled_default ?? false
  const sidebarIndexSymbols = prefs?.sidebar_index_symbols ?? SIDEBAR_INDEX_OPTIONS.map(i => i.symbol)
  const indicesPinned = prefs?.indices_nav_pinned ?? true
  const isRunning = quoteStatus?.running ?? false
  const isTrading = quoteStatus?.is_trading_hours ?? false
  const interval = intervalData?.interval ?? 10
  const minInterval = intervalData?.min_interval ?? 5
  const maxInterval = intervalData?.max_interval ?? 60
  const [intervalDraft, setIntervalDraft] = useState(interval)
  const feishuWebhookUrl = prefs?.feishu_webhook_url ?? ''
  const feishuWebhookSecret = prefs?.feishu_webhook_secret ?? ''
  const [feishuDraft, setFeishuDraft] = useState(feishuWebhookUrl)
  const [feishuSecretDraft, setFeishuSecretDraft] = useState(feishuWebhookSecret)
  const [feishuError, setFeishuError] = useState('')
  const [extraOpen, setExtraOpen] = useState('')
  const [extraDrafts, setExtraDrafts] = useState<Record<string, { url: string; secret: string; nickname: string }>>({})
  const [extraErrors, setExtraErrors] = useState<Record<string, string>>({})
  // 飞书渠道配置区展开态 (推送通知卡片内)
  const [channelOpen, setChannelOpen] = useState(false)
  useEffect(() => {
    setFeishuDraft(feishuWebhookUrl)
    setFeishuSecretDraft(feishuWebhookSecret)
  }, [feishuWebhookUrl, feishuWebhookSecret])
  useEffect(() => {
    const channels = prefs?.webhook_channels ?? {}
    setExtraDrafts(Object.fromEntries(EXTRA_WEBHOOK_CHANNELS.map(ch => {
      const cfg = channels[ch.id] ?? {}
      return [ch.id, {
        url: cfg.url ?? '',
        secret: cfg.secret ?? '',
        nickname: cfg.nickname ?? '',
      }]
    })))
  }, [prefs?.webhook_channels])
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

  const toggleWebhookDefault = useCallback(async (enabled: boolean) => {
    await api.updateWebhookDefault(enabled)
    qc.invalidateQueries({ queryKey: QK.preferences })
  }, [qc])

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

  const saveWebhookChannel = useMutation({
    mutationFn: ({ channel, config }: { channel: string; config: { url?: string; secret?: string; nickname?: string } }) =>
      api.updateWebhookChannel(channel, config),
    onSuccess: (_data, vars) => {
      setExtraErrors(e => ({ ...e, [vars.channel]: '' }))
      toast('Webhook 通道已保存', 'success')
      qc.invalidateQueries({ queryKey: QK.preferences })
    },
    onError: (err: any, vars) => setExtraErrors(e => ({ ...e, [vars.channel]: String(err?.message ?? '保存失败') })),
  })

  const updateExtraDraft = useCallback((channel: string, patch: Partial<{ url: string; secret: string; nickname: string }>) => {
    setExtraDrafts(d => ({ ...d, [channel]: { ...(d[channel] ?? { url: '', secret: '', nickname: '' }), ...patch } }))
  }, [])

  const submitExtra = useCallback((channel: string) => {
    const draft = extraDrafts[channel] ?? { url: '', secret: '', nickname: '' }
    if (channel === 'meow' && !draft.nickname.trim()) {
      setExtraErrors(e => ({ ...e, [channel]: 'MeoW 需填写昵称' }))
      return
    }
    if (channel !== 'meow' && draft.url.trim() && !draft.url.trim().startsWith('http')) {
      setExtraErrors(e => ({ ...e, [channel]: 'Webhook 地址需以 http/https 开头' }))
      return
    }
    saveWebhookChannel.mutate({ channel, config: draft })
  }, [extraDrafts, saveWebhookChannel])

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
          当前数据源未提供实时行情能力。TickFlow None 档仅可使用 free-api 获取历史日K（当日数据需盘后1-2小时），不能调用付费服务器实时接口。
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
            desc={isRunning && isTrading ? '运行中' : isRunning ? '运行中 (非交易时段)' : '已关闭'}
            checked={realtimeEnabled}
            onChange={handleToggleQuote}
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
          badge={!hasDepth ? '无五档' : undefined}
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
            飞书已实现; 微信开发中, QMT/ptrade 待定。
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
                  checked={webhookDefault}
                  onChange={e => { e.stopPropagation(); toggleWebhookDefault(e.target.checked) }}
                  onClick={e => e.stopPropagation()}
                  title="作为新建规则的默认推送渠道"
                  className="h-3 w-3 accent-accent cursor-pointer"
                />
                <span className="text-[11px] font-medium text-foreground">飞书</span>
                <span className="text-[9px] text-muted">群机器人</span>
                {webhookDefault && (
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
                      <li>打开飞书,进入目标群聊 → 群设置 → <b>群机器人</b></li>
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

            {EXTRA_WEBHOOK_CHANNELS.map(ch => {
              const draft = extraDrafts[ch.id] ?? { url: '', secret: '', nickname: '' }
              const configured = ch.id === 'meow' ? !!draft.nickname : !!draft.url
              const opened = extraOpen === ch.id
              const extraError = extraErrors[ch.id] ?? ''
              return (
              <div
                key={ch.name}
                className="rounded-btn border border-border/60 bg-base/40 overflow-hidden"
              >
                <div
                  onClick={() => setExtraOpen(opened ? '' : ch.id)}
                  className="flex items-center gap-2 px-2.5 py-2 cursor-pointer transition-colors hover:bg-base/60"
                >
                  <input type="checkbox" checked={webhookDefault} readOnly className="h-3 w-3 accent-accent pointer-events-none" />
                  <span className="text-[11px] font-medium text-foreground">{ch.name}</span>
                  <span className="text-[9px] text-muted">{ch.hint}</span>
                  <span className={`ml-auto text-[9px] ${configured ? 'text-emerald-500' : 'text-warning'}`}>
                    {configured ? '已配置' : '未配置'}
                  </span>
                  <ChevronDown className={`h-3 w-3 text-muted transition-transform ${opened ? 'rotate-180' : ''}`} />
                </div>
                {opened && (
                  <div className="border-t border-border/60 bg-base/30 p-3">
                    {ch.id === 'meow' ? (
                      <label className="block space-y-1.5">
                        <span className="text-[11px] text-muted">昵称</span>
                        <input
                          value={draft.nickname}
                          onChange={e => updateExtraDraft(ch.id, { nickname: e.target.value })}
                          placeholder="MeoW 昵称"
                          className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground focus:outline-none focus:border-accent/50"
                        />
                      </label>
                    ) : (
                      <>
                        <label className="block space-y-1.5">
                          <span className="text-[11px] text-muted">Webhook 地址</span>
                          <input
                            value={draft.url}
                            onChange={e => updateExtraDraft(ch.id, { url: e.target.value })}
                            placeholder="https://..."
                            className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground focus:outline-none focus:border-accent/50"
                          />
                        </label>
                        {ch.id === 'dingtalk' && (
                          <label className="block mt-2 space-y-1.5">
                            <span className="text-[11px] text-muted">签名密钥 (可选)</span>
                            <input
                              type="password"
                              value={draft.secret}
                              onChange={e => updateExtraDraft(ch.id, { secret: e.target.value })}
                              placeholder="未启用签名则留空"
                              className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground focus:outline-none focus:border-accent/50"
                            />
                          </label>
                        )}
                      </>
                    )}
                    {extraError && opened && (
                      <div className="mt-2 text-[11px] text-danger">{extraError}</div>
                    )}
                    <div className="mt-2 flex items-center gap-2">
                      <button
                        onClick={() => submitExtra(ch.id)}
                        disabled={saveWebhookChannel.isPending}
                        className="px-3 py-1.5 rounded-btn bg-accent text-base text-xs font-medium disabled:opacity-50 cursor-pointer hover:bg-accent/90 transition-colors"
                      >
                        {saveWebhookChannel.isPending ? '保存中…' : '保存'}
                      </button>
                    </div>
                  </div>
                )}
              </div>
            )})}
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
}: {
  label: string
  desc: string
  checked: boolean
  onChange: (v: boolean) => void
  icon?: React.ComponentType<{ className?: string }>
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
        onClick={() => onChange(!checked)}
        className={`relative inline-flex h-5 w-9 items-center rounded-full shrink-0 transition-colors duration-200 ${
          checked ? 'bg-accent' : 'bg-elevated'
        }`}
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
