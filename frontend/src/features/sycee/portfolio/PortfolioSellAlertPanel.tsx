import { useEffect, useMemo, useRef } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  BellOff,
  BellRing,
  CheckCircle2,
  Loader2,
  RefreshCw,
} from 'lucide-react'

import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'
import {
  PORTFOLIO_SELL_ALERT_QUERY_KEY,
  portfolioSellAlertApi,
  type Portfolio,
  type PortfolioAlertChannel,
  type PortfolioSellAlertUpdate,
} from './api'
import {
  planSellAlertReconciliation,
  sellAlertActionKey,
  type SellAlertReconcileAction,
} from './sellAlertRules'

const MONITOR_RULES_QUERY_KEY = ['monitor-rules'] as const
const STRATEGIES_QUERY_KEY = ['sycee', 'portfolio-sell-alert-strategies'] as const

function channelLabel(channel: PortfolioAlertChannel): string {
  return channel === 'feishu' ? '飞书' : '企业微信'
}

export function PortfolioSellAlertPanel({ portfolio }: { portfolio: Portfolio }) {
  const queryClient = useQueryClient()
  const attemptedAction = useRef('')
  const symbolKey = useMemo(
    () => portfolio.positions.map(position => position.symbol).sort().join(','),
    [portfolio.positions],
  )
  const statusQuery = useQuery({
    queryKey: [...PORTFOLIO_SELL_ALERT_QUERY_KEY, symbolKey],
    queryFn: portfolioSellAlertApi.get,
  })
  const rulesQuery = useQuery({
    queryKey: MONITOR_RULES_QUERY_KEY,
    queryFn: portfolioSellAlertApi.rules,
  })
  const strategiesQuery = useQuery({
    queryKey: STRATEGIES_QUERY_KEY,
    queryFn: portfolioSellAlertApi.strategies,
    staleTime: 60_000,
  })
  const strategies = useMemo(
    () => (strategiesQuery.data?.strategies ?? []).filter(strategy => strategy.exit_signals.length > 0),
    [strategiesQuery.data],
  )
  const status = statusQuery.data
  const action = useMemo(
    () => status && rulesQuery.data
      ? planSellAlertReconciliation(status, rulesQuery.data.rules)
      : { type: 'none' } as SellAlertReconcileAction,
    [rulesQuery.data, status],
  )

  const reconcile = useMutation({
    mutationFn: async (next: SellAlertReconcileAction) => {
      if (next.type === 'upsert') return portfolioSellAlertApi.saveRule(next.rule)
      if (next.type === 'delete') return portfolioSellAlertApi.deleteRule(next.ruleId)
      return { ok: true }
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: MONITOR_RULES_QUERY_KEY })
    },
  })

  useEffect(() => {
    if (action.type === 'none' || reconcile.isPending) {
      if (action.type === 'none') attemptedAction.current = ''
      return
    }
    const key = sellAlertActionKey(action)
    if (attemptedAction.current === key) return
    attemptedAction.current = key
    reconcile.mutate(action)
  }, [action, reconcile])

  const saveConfig = useMutation({
    mutationFn: portfolioSellAlertApi.update,
    onSuccess: async next => {
      attemptedAction.current = ''
      queryClient.setQueryData([...PORTFOLIO_SELL_ALERT_QUERY_KEY, symbolKey], next)
      await queryClient.invalidateQueries({ queryKey: PORTFOLIO_SELL_ALERT_QUERY_KEY })
    },
  })

  const updateConfig = (changes: Partial<PortfolioSellAlertUpdate>) => {
    if (!status) return
    saveConfig.mutate({
      enabled: status.config.enabled,
      strategy_id: status.config.strategy_id,
      webhook_channels: status.config.webhook_channels,
      ...changes,
    })
  }

  const retrySync = () => {
    attemptedAction.current = ''
    if (action.type !== 'none') reconcile.mutate(action)
  }

  const toggleEnabled = () => {
    if (!status) return
    if (!status.config.enabled && !status.config.strategy_id) {
      toast('请先选择一个包含卖出信号的策略', 'error')
      return
    }
    updateConfig({ enabled: !status.config.enabled })
  }

  const toggleChannel = (channel: PortfolioAlertChannel) => {
    if (!status) return
    const selected = status.config.webhook_channels.includes(channel)
    updateConfig({
      webhook_channels: selected
        ? status.config.webhook_channels.filter(item => item !== channel)
        : [...status.config.webhook_channels, channel],
    })
  }

  const loading = statusQuery.isLoading || rulesQuery.isLoading || strategiesQuery.isLoading
  const loadError = statusQuery.isError || rulesQuery.isError || strategiesQuery.isError
  const syncError = reconcile.isError || saveConfig.isError
  const syncing = reconcile.isPending || saveConfig.isPending || (!syncError && action.type !== 'none')
  const enabled = status?.config.enabled === true
  const waiting = enabled && status?.state === 'waiting_for_positions'

  return (
    <section aria-labelledby="portfolio-sell-alert-title" className="overflow-hidden rounded-card border border-border bg-surface">
      <div className="flex min-h-12 items-center justify-between gap-3 border-b border-border px-4 py-2.5">
        <div className="flex min-w-0 items-center gap-2.5">
          <BellRing className="h-4 w-4 shrink-0 text-accent" />
          <div className="min-w-0">
            <h2 id="portfolio-sell-alert-title" className="text-sm font-semibold text-foreground">持仓卖出提醒</h2>
            <p className="mt-0.5 truncate text-[11px] text-muted">本规则仅扫描当前持仓，买入与选股池事件不会推送。</p>
          </div>
        </div>
        <div className={cn(
          'flex shrink-0 items-center gap-1.5 text-[11px]',
          syncError || loadError ? 'text-danger' : enabled ? 'text-bull' : 'text-muted',
        )}>
          {loading || syncing ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
            : syncError || loadError ? <AlertTriangle className="h-3.5 w-3.5" />
              : enabled ? <CheckCircle2 className="h-3.5 w-3.5" /> : <BellOff className="h-3.5 w-3.5" />}
          <span>{loading ? '读取中' : syncError || loadError ? '需要处理' : syncing ? '同步中' : waiting ? '等待持仓' : enabled ? '已启用' : '未启用'}</span>
        </div>
      </div>

      <div className="grid gap-4 p-4 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-end">
        {loadError ? (
          <div className="flex min-h-11 items-center gap-3 text-sm text-danger">
            <AlertTriangle className="h-4 w-4 shrink-0" />提醒配置读取失败
          </div>
        ) : (
          <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end">
            <label className="block min-w-0">
              <span className="mb-1.5 block text-xs font-medium text-secondary">卖出策略</span>
              <select
                value={status?.config.strategy_id ?? ''}
                onChange={event => updateConfig({ strategy_id: event.target.value })}
                disabled={loading || saveConfig.isPending}
                className="min-h-11 w-full rounded-input border border-border bg-base px-3 text-sm text-foreground outline-none focus:border-accent focus:ring-2 focus:ring-accent/20 disabled:opacity-50 lg:min-h-9"
              >
                <option value="">选择策略</option>
                {status?.config.strategy_id && !strategies.some(strategy => strategy.id === status.config.strategy_id) && (
                  <option value={status.config.strategy_id}>{status.config.strategy_id}（当前不可用）</option>
                )}
                {strategies.map(strategy => (
                  <option key={strategy.id} value={strategy.id}>{strategy.name}</option>
                ))}
              </select>
            </label>

            <fieldset disabled={loading || saveConfig.isPending} className="min-w-0">
              <legend className="mb-1.5 text-xs font-medium text-secondary">外部推送</legend>
              <div className="flex min-h-11 items-center gap-4 rounded-input border border-border bg-base px-3 lg:min-h-9">
                {(['feishu', 'wecom'] as const).map(channel => (
                  <label key={channel} className="flex cursor-pointer items-center gap-2 whitespace-nowrap text-xs text-secondary">
                    <input
                      type="checkbox"
                      checked={status?.config.webhook_channels.includes(channel) ?? false}
                      onChange={() => toggleChannel(channel)}
                      className="h-4 w-4 rounded border-border accent-[var(--accent)]"
                    />
                    {channelLabel(channel)}
                  </label>
                ))}
              </div>
            </fieldset>
          </div>
        )}

        <div className="flex items-center justify-end gap-2">
          {(syncError || loadError) && (
            <button
              type="button"
              onClick={loadError ? () => { statusQuery.refetch(); rulesQuery.refetch(); strategiesQuery.refetch() } : retrySync}
              className="flex h-11 w-11 items-center justify-center rounded-btn border border-border text-muted hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent lg:h-9 lg:w-9"
              aria-label="重试同步持仓卖出提醒"
              title="重试同步"
            >
              <RefreshCw className="h-4 w-4" />
            </button>
          )}
          <button
            type="button"
            onClick={toggleEnabled}
            disabled={loading || saveConfig.isPending || loadError}
            className={cn(
              'inline-flex min-h-11 items-center justify-center gap-2 rounded-btn px-4 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-50 lg:min-h-9',
              enabled
                ? 'border border-border text-secondary hover:bg-elevated'
                : 'bg-accent text-white hover:bg-accent/90',
            )}
          >
            {enabled ? <BellOff className="h-4 w-4" /> : <BellRing className="h-4 w-4" />}
            {enabled ? '停用提醒' : '启用提醒'}
          </button>
        </div>
      </div>

      {status && !loadError && (
        <div className="border-t border-border bg-base/50 px-4 py-2 text-[11px] text-muted">
          {waiting
            ? '配置已保留，新持仓录入后自动建立监控规则。'
            : enabled
              ? `当前覆盖 ${status.position_count} 只持仓 · 仅卖出信号 · 冷却 1 小时`
              : '监控中心中不会保留此功能创建的规则。'}
        </div>
      )}
    </section>
  )
}
