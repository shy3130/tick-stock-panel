import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Check, Globe, Loader2 } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { usePreferences } from '@/lib/useSharedQueries'
import { Skeleton } from './Skeleton'

/** 受控外部 fallback 白名单 scope（与 preferences 契约一致）。 */
const SCOPE_REALTIME = 'realtime'
const SCOPE_DEPTH = 'depth'
type FallbackScope = typeof SCOPE_REALTIME | typeof SCOPE_DEPTH

const SCOPE_ITEMS: { key: FallbackScope; label: string; desc: string }[] = [
  {
    key: SCOPE_REALTIME,
    label: '实时行情展示',
    desc: '本地实时快照缺失或早于当前交易日时补读；页面保留「外部源·降级数据」标记',
  },
  {
    key: SCOPE_DEPTH,
    label: '五档盘口实时展示·仅盘中',
    desc: '本地无五档能力时补读腾讯公共行情；仅进程内展示，不写 sealed / 选股 / 回测 / 监控',
  },
]

function sanitizeScopes(raw: string[] | undefined | null): FallbackScope[] {
  const seen: Partial<Record<FallbackScope, true>> = {}
  const out: FallbackScope[] = []
  for (const s of raw ?? []) {
    const key = String(s).trim().toLowerCase()
    if (key !== SCOPE_REALTIME && key !== SCOPE_DEPTH) continue
    if (seen[key]) continue
    seen[key] = true
    out.push(key)
  }
  return out
}

/**
 * 受控外部行情降级（默认关闭）。
 *
 * 总开关 + realtime / depth 两个显式 scope 复选。
 * - 开启且 scopes 为空 → 默认 realtime
 * - 关闭 → scopes 清空
 * - 取消最后一个 scope → 保持总开关，显示「未选择」（enabled + 空 scopes = 零网络）
 *
 * 外部数据仅只读展示并带 provenance，不写 sealed / 本地行情库，不参与选股、监控与回测。
 */
export function ExternalFallbackCard() {
  const qc = useQueryClient()
  const prefs = usePreferences()
  const enabled = prefs.data?.external_fallback_enabled ?? false
  const scopes = sanitizeScopes(prefs.data?.external_fallback_scopes)
  const hasRealtime = scopes.includes(SCOPE_REALTIME)
  const hasDepth = scopes.includes(SCOPE_DEPTH)

  const update = useMutation({
    mutationFn: ({ on, nextScopes }: { on: boolean; nextScopes: FallbackScope[] }) =>
      api.updateExternalFallback(on, nextScopes),
    onSuccess: () => qc.invalidateQueries({ queryKey: QK.preferences }),
  })

  const busy = update.isPending || prefs.isLoading

  const setMaster = (next: boolean) => {
    if (busy) return
    if (!next) {
      update.mutate({ on: false, nextScopes: [] })
      return
    }
    // 开启：保留已选 scope；若空则默认 realtime
    update.mutate({
      on: true,
      nextScopes: scopes.length > 0 ? scopes : [SCOPE_REALTIME],
    })
  }

  // 勾选 scope 时若总开关仍关则一并打开；取消最后一个时保持总开 + 空 scopes
  const onScopeClick = (key: FallbackScope) => {
    if (busy) return
    const next = scopes.includes(key)
      ? scopes.filter((s) => s !== key)
      : [...scopes, key]
    if (next.length === 0) {
      // 最清晰保守：保持当前总开关状态，scopes 置空 → UI「未选择」
      update.mutate({ on: enabled, nextScopes: [] })
      return
    }
    update.mutate({ on: true, nextScopes: next })
  }

  const statusLabel = !enabled ? '已关闭' : scopes.length === 0 ? '未选择' : '已开启'
  const statusClass = !enabled
    ? 'text-muted'
    : scopes.length === 0
      ? 'text-warning'
      : 'text-accent'

  const scopeSummary =
    !enabled || scopes.length === 0
      ? '—'
      : [
          hasRealtime ? '实时行情' : null,
          hasDepth ? '五档盘口' : null,
        ]
          .filter(Boolean)
          .join(' · ')

  return (
    <div className="rounded-card border border-border bg-surface p-4 relative">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <Globe className="h-4 w-4 text-secondary" />
          <h3 className="text-sm font-medium text-foreground">外部行情降级</h3>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={enabled}
          aria-label="外部行情降级总开关"
          onClick={() => setMaster(!enabled)}
          disabled={busy}
          className={`relative inline-flex h-4 w-7 items-center rounded-full shrink-0 transition-colors duration-200 ${
            enabled
              ? 'bg-accent shadow-[0_0_6px_rgba(59,130,246,0.3)]'
              : 'bg-elevated'
          } ${busy ? 'opacity-50' : 'cursor-pointer'}`}
        >
          <span
            className={`inline-block h-3 w-3 rounded-full bg-white shadow-sm transition-transform duration-200 ${
              enabled ? 'translate-x-[14px]' : 'translate-x-0.5'
            }`}
          />
        </button>
      </div>

      {prefs.isLoading ? (
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <Skeleton w="w-8" />
            <Skeleton w="w-16" />
          </div>
          <div className="flex items-center justify-between">
            <Skeleton w="w-12" />
            <Skeleton w="w-20" />
          </div>
          <div className="flex items-center justify-between">
            <Skeleton w="w-10" />
            <Skeleton w="w-14" />
          </div>
        </div>
      ) : (
        <div className="space-y-2">
          <div className="flex items-center justify-between text-[11px]">
            <span className="text-muted">状态</span>
            <span className={`font-mono ${statusClass}`}>{statusLabel}</span>
          </div>
          <div className="flex items-center justify-between text-[11px]">
            <span className="text-muted">降级来源</span>
            <span className="font-mono text-secondary">腾讯公共行情</span>
          </div>
          <div className="flex items-center justify-between text-[11px]">
            <span className="text-muted">已选范围</span>
            <span className="font-mono text-secondary">{scopeSummary}</span>
          </div>

          <div className="space-y-1.5 pt-1">
            {SCOPE_ITEMS.map((item) => {
              // 总开关关闭时视觉一律视为未启用（即便 prefs 残留 scopes）
              const active = enabled && scopes.includes(item.key)
              return (
                <label
                  key={item.key}
                  className={`flex items-start gap-2.5 rounded-card border px-2.5 py-2 transition-colors cursor-pointer ${
                    active
                      ? 'border-accent/40 bg-accent/[0.05]'
                      : 'border-border bg-base/30 hover:border-border/70'
                  } ${busy ? 'opacity-60' : ''}`}
                >
                  <button
                    type="button"
                    role="checkbox"
                    aria-checked={active}
                    aria-label={item.label}
                    disabled={busy}
                    onClick={(e) => {
                      e.preventDefault()
                      onScopeClick(item.key)
                    }}
                    className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded border transition-colors ${
                      active ? 'bg-accent border-accent' : 'bg-base border-border'
                    }`}
                  >
                    {active && <Check className="h-3 w-3 text-white" strokeWidth={3} />}
                  </button>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <span className="text-xs font-medium text-foreground">{item.label}</span>
                      <span className={`text-[9px] font-mono ${active ? 'text-accent' : 'text-muted'}`}>
                        {active ? '启用' : '关闭'}
                      </span>
                    </div>
                    <div className="text-[10px] text-muted leading-snug mt-0.5">{item.desc}</div>
                  </div>
                </label>
              )
            })}
          </div>

          {update.isPending && (
            <div className="flex items-center gap-1.5 text-[10px] text-muted">
              <Loader2 className="h-3 w-3 animate-spin" />
              保存中…
            </div>
          )}

          {update.isError && (
            <p className="text-[10px] leading-snug text-danger">
              保存失败：{(update.error as Error)?.message || '请稍后重试'}
            </p>
          )}

          <p className="text-[10px] leading-snug text-muted pt-1 border-t border-border/50">
            默认关闭。外部数据仅只读展示并保留 provenance 标记；不写入 sealed / 本地行情库，
            也不参与选股、监控与回测。depth 仅盘中补读五档，realtime 仅在本地快照缺失或陈旧时触发。
          </p>
        </div>
      )}
    </div>
  )
}
