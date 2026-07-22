import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowDown, ArrowUp, Bell, Check, ExternalLink, Loader2, Trash2, X } from 'lucide-react'
import { toast } from '@/components/Toast'
import { LEVEL_GROUPS } from './AnalysisKChart'
import { api, genRuleId, type MonitorRule, type PriceLevel } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { usePreferences } from '@/lib/useSharedQueries'

interface Props {
  symbol: string
  name: string
  onClose: () => void
}

type AlertDirection = 'up' | 'down'

const COOLDOWNS = [
  { value: 600, label: '10 分钟' },
  { value: 1800, label: '30 分钟' },
  { value: 3600, label: '1 小时' },
  { value: 86400, label: '当日一次' },
]

function pointCondition(rule: MonitorRule) {
  if (rule.type !== 'price' || rule.conditions.length !== 1) return null
  const condition = rule.conditions[0]
  if (condition.field !== 'close' || !['>=', '<='].includes(condition.op)) return null
  if (typeof condition.value !== 'number') return null
  return condition
}

function levelGroupLabel(level: PriceLevel) {
  return LEVEL_GROUPS.find(group => group.key === level.type)?.label ?? level.type
}

export function PriceAlertDialog({ symbol, name, onClose }: Props) {
  const qc = useQueryClient()
  const { data: prefs } = usePreferences()
  const levelsQuery = useQuery({
    queryKey: QK.stockLevels(symbol),
    queryFn: () => api.stockAnalysisLevels(symbol, 250),
    staleTime: 60_000,
  })
  const rulesQuery = useQuery({ queryKey: QK.monitorRules, queryFn: api.monitorRulesList })
  const [tab, setTab] = useState<'create' | 'existing'>('create')
  const [direction, setDirection] = useState<AlertDirection>('up')
  const [target, setTarget] = useState('')
  const [selectedLabel, setSelectedLabel] = useState('')
  const [cooldown, setCooldown] = useState(3600)
  const [message, setMessage] = useState('')
  const [channels, setChannels] = useState<string[]>([])
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const channelsInitialized = useRef(false)

  const currentPrice = levelsQuery.data?.close ?? null
  const recommended = useMemo(() => {
    if (currentPrice == null) return { above: [] as PriceLevel[], below: [] as PriceLevel[] }
    const seen = new Set<string>()
    const all = Object.values(levelsQuery.data?.levels ?? {})
      .flat()
      .filter(level => Number.isFinite(level.value) && level.value > 0)
      .sort((a, b) => Math.abs(a.value - currentPrice) - Math.abs(b.value - currentPrice))
      .filter(level => {
        const key = level.value.toFixed(2)
        if (seen.has(key)) return false
        seen.add(key)
        return true
      })
    return {
      above: all.filter(level => level.value > currentPrice).slice(0, 6),
      below: all.filter(level => level.value < currentPrice).slice(0, 6),
    }
  }, [currentPrice, levelsQuery.data?.levels])

  useEffect(() => {
    if (target || currentPrice == null) return
    const initial = recommended.above[0] ?? recommended.below[0]
    if (!initial) return
    setTarget(initial.value.toFixed(2))
    setDirection(initial.value > currentPrice ? 'up' : 'down')
    setSelectedLabel(initial.label)
  }, [currentPrice, recommended, target])

  useEffect(() => {
    if (channelsInitialized.current || !prefs) return
    channelsInitialized.current = true
    const configured = new Set<string>()
    if (prefs.feishu_webhook_url) configured.add('feishu')
    if (prefs.wecom_webhook_url) configured.add('wecom')
    setChannels((prefs.webhook_default_channels ?? []).filter(channel => configured.has(channel)))
  }, [prefs])

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [onClose])

  const pointRules = useMemo(
    () => (rulesQuery.data?.rules ?? []).filter(rule =>
      rule.scope === 'symbols'
      && rule.symbols.length === 1
      && rule.symbols[0] === symbol
      && pointCondition(rule),
    ),
    [rulesQuery.data?.rules, symbol],
  )

  const targetValue = Number(target)
  const targetValid = Number.isFinite(targetValue) && targetValue > 0
  const alreadyReached = targetValid && currentPrice != null && (
    direction === 'up' ? currentPrice >= targetValue : currentPrice <= targetValue
  )
  const duplicate = targetValid && pointRules.some(rule => {
    const condition = pointCondition(rule)!
    return condition.op === (direction === 'up' ? '>=' : '<=')
      && Math.abs((condition.value ?? 0) - targetValue) < 0.005
  })

  const save = useMutation({
    mutationFn: () => api.monitorRuleSave({
      id: genRuleId(),
      name: `点位提醒 · ${name || symbol} · ${direction === 'up' ? '涨至' : '跌至'}${selectedLabel || targetValue.toFixed(2)}`,
      enabled: true,
      type: 'price',
      asset_type: 'stock',
      scope: 'symbols',
      symbols: [symbol],
      sector: null,
      strategy_id: null,
      direction: 'entry',
      conditions: [{ field: 'close', op: direction === 'up' ? '>=' : '<=', value: targetValue }],
      logic: 'and',
      cooldown_seconds: cooldown,
      severity: 'warn',
      message: message.trim(),
      webhook_channels: channels,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.monitorRules })
      toast('点位提醒已创建', 'success')
      onClose()
    },
    onError: error => toast(String((error as Error)?.message || '创建失败'), 'error'),
  })

  const toggle = useMutation({
    mutationFn: (rule: MonitorRule) => {
      const { runtime_warning: _runtimeWarning, ...persisted } = rule
      return api.monitorRuleSave({ ...persisted, enabled: !rule.enabled })
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: QK.monitorRules }),
    onError: error => toast(String((error as Error)?.message || '更新失败'), 'error'),
  })

  const remove = useMutation({
    mutationFn: api.monitorRuleDelete,
    onSuccess: () => {
      setConfirmDelete(null)
      qc.invalidateQueries({ queryKey: QK.monitorRules })
      toast('点位提醒已删除', 'success')
    },
    onError: error => toast(String((error as Error)?.message || '删除失败'), 'error'),
  })

  const selectLevel = (level: PriceLevel) => {
    setTarget(level.value.toFixed(2))
    setDirection(currentPrice != null && level.value < currentPrice ? 'down' : 'up')
    setSelectedLabel(level.label)
  }

  const updateTarget = (value: string) => {
    setTarget(value)
    setSelectedLabel('')
    const parsed = Number(value)
    if (currentPrice != null && Number.isFinite(parsed)) {
      setDirection(parsed < currentPrice ? 'down' : 'up')
    }
  }

  const toggleChannel = (channel: string) => {
    setChannels(current => current.includes(channel)
      ? current.filter(item => item !== channel)
      : [...current, channel])
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 p-3 backdrop-blur-sm sm:p-4" onClick={onClose}>
      <div role="dialog" aria-modal="true" aria-labelledby="price-alert-title" className="flex max-h-[88vh] w-full max-w-2xl flex-col overflow-hidden rounded-lg border border-border bg-surface shadow-2xl" onClick={event => event.stopPropagation()}>
        <header className="flex items-center gap-3 border-b border-border/60 px-5 py-3.5">
          <span className="grid h-8 w-8 shrink-0 place-items-center rounded-md border border-sky-400/25 bg-sky-400/10 text-sky-400">
            <Bell className="h-4 w-4" />
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <h2 id="price-alert-title" className="text-sm font-semibold text-foreground">点位提醒</h2>
              <span className="truncate text-xs text-secondary">{name || symbol}</span>
              <span className="shrink-0 font-mono text-[10px] text-muted">{symbol}</span>
            </div>
            <div className="mt-0.5 text-[10px] text-muted">
              当前价 <span className="font-mono text-foreground">{currentPrice?.toFixed(2) ?? '—'}</span>
            </div>
          </div>
          <button onClick={onClose} className="rounded-md p-1.5 text-muted transition-colors hover:bg-elevated hover:text-foreground" title="关闭">
            <X className="h-4 w-4" />
          </button>
        </header>

        <div className="flex h-10 shrink-0 border-b border-border/60 px-5">
          {([
            { key: 'create', label: '新建提醒' },
            { key: 'existing', label: `已有提醒 ${pointRules.length}` },
          ] as const).map(item => (
            <button key={item.key} onClick={() => setTab(item.key)} className={`relative px-4 text-xs font-medium transition-colors ${tab === item.key ? 'text-sky-400' : 'text-muted hover:text-foreground'}`}>
              {item.label}
              {tab === item.key && <span className="absolute inset-x-2 bottom-0 h-0.5 bg-sky-400" />}
            </button>
          ))}
        </div>

        {tab === 'create' ? (
          <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-5">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-[180px_1fr]">
              <div className="space-y-1.5">
                <span className="text-[11px] text-muted">触发方向</span>
                <div className="grid h-9 grid-cols-2 overflow-hidden rounded-md border border-border bg-base">
                  <button onClick={() => setDirection('up')} className={`inline-flex items-center justify-center gap-1 text-xs font-medium transition-colors ${direction === 'up' ? 'bg-bull/10 text-bull' : 'text-muted hover:text-foreground'}`}>
                    <ArrowUp className="h-3.5 w-3.5" />涨至
                  </button>
                  <button onClick={() => setDirection('down')} className={`inline-flex items-center justify-center gap-1 border-l border-border text-xs font-medium transition-colors ${direction === 'down' ? 'bg-bear/10 text-bear' : 'text-muted hover:text-foreground'}`}>
                    <ArrowDown className="h-3.5 w-3.5" />跌至
                  </button>
                </div>
              </div>
              <label className="space-y-1.5">
                <span className="text-[11px] text-muted">目标价格</span>
                <div className="relative">
                  <input type="number" min="0" step="0.01" value={target} onChange={event => updateTarget(event.target.value)} className="h-9 w-full rounded-md border border-border bg-base px-3 pr-14 font-mono text-sm text-foreground focus:border-sky-400/50 focus:outline-none" />
                  {targetValid && currentPrice != null && (
                    <span className={`absolute right-3 top-2.5 font-mono text-[10px] ${targetValue >= currentPrice ? 'text-bull' : 'text-bear'}`}>
                      {((targetValue / currentPrice - 1) * 100).toFixed(2)}%
                    </span>
                  )}
                </div>
              </label>
            </div>

            <section className="mt-5">
              <div className="mb-2 flex items-center justify-between">
                <span className="text-[11px] font-medium text-secondary">关键价位</span>
                {levelsQuery.isLoading && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted" />}
              </div>
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 sm:gap-x-4">
                {([
                  { key: 'above', label: '上方', icon: ArrowUp, levels: recommended.above, color: 'text-bull' },
                  { key: 'below', label: '下方', icon: ArrowDown, levels: recommended.below, color: 'text-bear' },
                ] as const).map(group => (
                  <div key={group.key} className="min-w-0">
                    <div className={`mb-1.5 flex items-center gap-1 text-[10px] ${group.color}`}>
                      <group.icon className="h-3 w-3" />{group.label}
                    </div>
                    <div className="divide-y divide-border/50 border-y border-border/50">
                      {group.levels.length === 0 ? (
                        <div className="py-5 text-center text-[10px] text-muted">暂无价位</div>
                      ) : group.levels.map(level => {
                        const selected = Math.abs(Number(target) - level.value) < 0.005
                        return (
                          <button key={`${level.type}-${level.value}`} onClick={() => selectLevel(level)} className={`flex h-10 w-full items-center gap-2 px-1.5 text-left transition-colors hover:bg-elevated/60 ${selected ? 'bg-sky-400/[0.08]' : ''}`}>
                            <span className="min-w-0 flex-1">
                              <span className={`block truncate text-[11px] ${selected ? 'text-sky-300' : 'text-foreground'}`}>{level.label}</span>
                              <span className="block truncate text-[9px] text-muted">{levelGroupLabel(level)}</span>
                            </span>
                            <span className="shrink-0 font-mono text-xs text-secondary">{level.value.toFixed(2)}</span>
                            <span className={`grid h-4 w-4 shrink-0 place-items-center rounded-full border ${selected ? 'border-sky-400 bg-sky-400 text-white' : 'border-border text-transparent'}`}>
                              <Check className="h-2.5 w-2.5" />
                            </span>
                          </button>
                        )
                      })}
                    </div>
                  </div>
                ))}
              </div>
            </section>

            <div className="mt-5 grid grid-cols-1 gap-4 border-t border-border/60 pt-4 sm:grid-cols-2">
              <label className="space-y-1.5">
                <span className="text-[11px] text-muted">重复提醒</span>
                <select value={cooldown} onChange={event => setCooldown(Number(event.target.value))} className="h-9 w-full rounded-md border border-border bg-base px-3 text-xs text-foreground focus:outline-none">
                  {COOLDOWNS.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
                </select>
              </label>
              <label className="space-y-1.5">
                <span className="text-[11px] text-muted">自定义提示</span>
                <input value={message} onChange={event => setMessage(event.target.value)} placeholder="留空使用默认内容" className="h-9 w-full rounded-md border border-border bg-base px-3 text-xs text-foreground placeholder:text-muted/50 focus:outline-none" />
              </label>
            </div>

            <div className="mt-4">
              <span className="text-[11px] text-muted">通知渠道</span>
              <div className="mt-2 flex flex-wrap gap-4">
                <label className="inline-flex items-center gap-2 text-xs text-foreground">
                  <input type="checkbox" checked disabled className="h-3.5 w-3.5 accent-sky-500" />站内
                </label>
                {([
                  { key: 'feishu', label: '飞书', configured: !!prefs?.feishu_webhook_url },
                  { key: 'wecom', label: '企业微信', configured: !!prefs?.wecom_webhook_url },
                ]).map(channel => (
                  <label key={channel.key} className={`inline-flex items-center gap-2 text-xs ${channel.configured ? 'text-foreground' : 'text-muted/60'}`}>
                    <input type="checkbox" checked={channels.includes(channel.key)} disabled={!channel.configured} onChange={() => toggleChannel(channel.key)} className="h-3.5 w-3.5 accent-sky-500" />
                    {channel.label}
                    {!channel.configured && <span className="text-[9px]">未配置</span>}
                  </label>
                ))}
              </div>
            </div>

            {(alreadyReached || duplicate) && (
              <div className="mt-4 rounded-md border border-warning/30 bg-warning/5 px-3 py-2 text-[11px] text-warning">
                {duplicate ? '相同方向和价格的提醒已存在。' : '当前价格已处于触发区间,请调整目标价格或触发方向。'}
              </div>
            )}
          </div>
        ) : (
          <div className="min-h-[280px] flex-1 overflow-y-auto px-5 py-4">
            {rulesQuery.isLoading ? (
              <div className="flex justify-center py-16"><Loader2 className="h-5 w-5 animate-spin text-muted" /></div>
            ) : pointRules.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-16 text-center">
                <Bell className="h-6 w-6 text-muted/50" />
                <span className="mt-2 text-xs text-muted">暂无点位提醒</span>
                <button onClick={() => setTab('create')} className="mt-3 text-xs text-sky-400 hover:text-sky-300">新建提醒</button>
              </div>
            ) : (
              <div className="divide-y divide-border/50 border-y border-border/50">
                {pointRules.map(rule => {
                  const condition = pointCondition(rule)!
                  const isUp = condition.op === '>='
                  return (
                    <div key={rule.id} className={`flex items-center gap-3 px-2 py-3 ${rule.enabled ? '' : 'opacity-55'}`}>
                      <span className={`grid h-7 w-7 shrink-0 place-items-center rounded-md ${isUp ? 'bg-bull/10 text-bull' : 'bg-bear/10 text-bear'}`}>
                        {isUp ? <ArrowUp className="h-3.5 w-3.5" /> : <ArrowDown className="h-3.5 w-3.5" />}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-xs text-foreground">{rule.name}</span>
                        <span className="mt-0.5 block font-mono text-[10px] text-muted">{isUp ? '涨至' : '跌至'} {condition.value!.toFixed(2)} · {COOLDOWNS.find(item => item.value === rule.cooldown_seconds)?.label ?? `${rule.cooldown_seconds} 秒`}</span>
                      </span>
                      <button role="switch" aria-checked={rule.enabled} onClick={() => toggle.mutate(rule)} disabled={toggle.isPending} className={`relative h-5 w-9 shrink-0 rounded-full transition-colors ${rule.enabled ? 'bg-sky-500' : 'bg-elevated'}`} title={rule.enabled ? '停用' : '启用'}>
                        <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-transform ${rule.enabled ? 'translate-x-[18px]' : 'translate-x-0.5'}`} />
                      </button>
                      {confirmDelete === rule.id ? (
                        <button onClick={() => remove.mutate(rule.id)} disabled={remove.isPending} className="h-7 rounded-md border border-danger/30 bg-danger/10 px-2 text-[10px] text-danger">确认</button>
                      ) : (
                        <button onClick={() => setConfirmDelete(rule.id)} className="rounded-md p-1.5 text-muted hover:bg-danger/10 hover:text-danger" title="删除">
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        )}

        <footer className="flex min-h-14 items-center justify-between gap-3 border-t border-border/60 bg-base/30 px-5 py-2.5">
          <Link to="/monitor" onClick={onClose} className="inline-flex items-center gap-1 text-[11px] text-muted hover:text-sky-400">
            监控中心<ExternalLink className="h-3 w-3" />
          </Link>
          <div className="flex items-center gap-2">
            <button onClick={onClose} className="h-8 rounded-md border border-border px-3 text-xs text-secondary hover:text-foreground">取消</button>
            {tab === 'create' && (
              <button onClick={() => save.mutate()} disabled={!targetValid || alreadyReached || duplicate || save.isPending} className="inline-flex h-8 items-center gap-1.5 rounded-md bg-sky-500 px-4 text-xs font-medium text-white transition-colors hover:bg-sky-400 disabled:cursor-not-allowed disabled:opacity-40">
                {save.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Bell className="h-3.5 w-3.5" />}
                创建提醒
              </button>
            )}
          </div>
        </footer>
      </div>
    </div>
  )
}
