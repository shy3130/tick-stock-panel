import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import { Wifi, Play, Loader2, X, Check, Crown } from 'lucide-react'
import { api, type EndpointItem } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { EXPERT_RANK, tierRank } from '@/lib/capability-labels'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'

interface EpResult {
  ok: boolean
  median_ms?: number | null
  min_ms?: number | null
  max_ms?: number | null
  rounds?: number
  success?: number
  error?: string
}

export function EndpointTestDialog({ hasKey, tierLabel, currentEndpoint, onClose }: { hasKey: boolean; tierLabel: string; currentEndpoint: string; onClose: () => void }) {
  const qc = useQueryClient()
  const backdrop = useDialogBackdrop(onClose)
  const [results, setResults] = useState<Record<string, EpResult | null>>({})
  const [testing, setTesting] = useState<Record<string, boolean>>({})
  const [switching, setSwitching] = useState<string | null>(null)

  // 动态加载端点清单 —— 前端无法跨域直连 tickflow.org,走后端代理
  const { data, isLoading } = useQuery({
    queryKey: QK.endpoints,
    queryFn: api.listEndpoints,
    staleTime: 5 * 60 * 1000,
  })

  const endpoints = data?.endpoints ?? []
  const isFallback = data?.source === 'fallback'
  const testRounds = data?.testRounds

  async function testOne(url: string) {
    setTesting(prev => ({ ...prev, [url]: true }))
    setResults(prev => ({ ...prev, [url]: null }))
    try {
      const res = await api.testEndpoint(url, testRounds)
      setResults(prev => ({ ...prev, [url]: res }))
    } catch (e: any) {
      setResults(prev => ({ ...prev, [url]: { ok: false, error: e?.message ?? '请求失败' } }))
    } finally {
      setTesting(prev => ({ ...prev, [url]: false }))
    }
  }

  async function testAll() {
    setResults({})
    await Promise.all(endpoints.map(ep => testOne(ep.url)))
  }

  const anyTesting = Object.values(testing).some(Boolean)
  const isFree = !hasKey
  // 专线端点需 Expert 及以上套餐;Free 模式必然不可用
  const canUsePremium = !isFree && tierRank(tierLabel) >= EXPERT_RANK
  const currentLabel = endpoints.find(ep => ep.url === currentEndpoint)?.label ?? currentEndpoint

  async function applyEndpoint(url: string) {
    setSwitching(url)
    try {
      await api.switchEndpoint(url)
      await qc.invalidateQueries({ queryKey: QK.settings })
      onClose()
    } catch {
      // 错误由 query 处理
    } finally {
      setSwitching(null)
    }
  }

  return (
    <AnimatePresence>
      <div className="fixed inset-0 z-50 flex items-center justify-center">
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="absolute inset-0 bg-black/60 backdrop-blur-sm"
          {...backdrop}
        />
        <motion.div
          initial={{ opacity: 0, scale: 0.95, y: 12 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.97, y: 8 }}
          transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
          className="relative w-[480px] max-h-[90vh] flex flex-col rounded-card border border-border bg-base shadow-2xl overflow-hidden"
        >
          {/* 顶栏 */}
          <div className="flex items-center justify-between px-5 py-3 border-b border-border shrink-0">
            <div className="flex items-center gap-2">
              <Wifi className="h-4 w-4 text-accent" />
              <span className="text-sm font-medium text-foreground">端点测速</span>
            </div>
            <div className="flex items-center gap-3">
              {isFree && (
                <span className="text-[10px] px-2 py-0.5 rounded bg-warning/10 text-warning/80">Free 模式</span>
              )}
              <button
                onClick={testAll}
                disabled={isFree || anyTesting || endpoints.length === 0}
                title={isFree ? 'Free 模式不可使用付费端点，请先配置 API Key' : undefined}
                className="inline-flex items-center gap-1.5 px-3 py-1 rounded-btn bg-accent/15 text-accent text-xs font-medium hover:bg-accent/25 disabled:opacity-50 transition-colors"
              >
                {anyTesting ? <Loader2 className="h-3 w-3 animate-spin" /> : <Play className="h-3 w-3" />}
                全部测速
              </button>
              <button onClick={onClose} className="p-1 rounded text-secondary hover:text-foreground transition-colors">
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>

          {/* 当前使用 */}
          <div className="mx-4 mt-3 px-3 py-2 rounded-btn bg-accent/8 border border-accent/20 flex items-center gap-2 shrink-0">
            <span className="inline-block h-1.5 w-1.5 rounded-full bg-accent shadow-[0_0_4px_rgba(61,214,140,0.5)]" />
            <span className="text-[11px] text-secondary">当前使用</span>
            <span className="text-[11px] font-medium text-foreground">{currentLabel}</span>
            <span className="text-[10px] text-muted font-mono ml-auto">{currentEndpoint.replace('https://', '')}</span>
          </div>

          {/* Free 模式提示 —— 以下均为 Starter+ 付费端点 */}
          {isFree && (
            <div className="mx-4 mt-2 px-3 py-1.5 rounded-btn bg-warning/8 border border-warning/20 shrink-0">
              <span className="text-[10px] text-warning/80 leading-snug">
                以下均为 Starter+ 付费端点，Free 模式不可使用。配置 API Key 后可自动切换并测速选优。
              </span>
            </div>
          )}

          {/* 端点列表 —— 可滚动区,顶栏/当前使用/底栏始终可见 */}
          <div className="p-4 space-y-2 overflow-y-auto min-h-0">
            {isLoading ? (
              <div className="py-10 flex items-center justify-center gap-2 text-muted">
                <Loader2 className="h-4 w-4 animate-spin" />
                <span className="text-xs">加载端点列表…</span>
              </div>
            ) : endpoints.length === 0 ? (
              <div className="py-10 text-center text-xs text-muted">未能加载端点列表</div>
            ) : (
              endpoints.map(ep => (
                <EpRow key={ep.url} ep={ep} result={results[ep.url]} testing={testing[ep.url]} isCurrent={ep.url === currentEndpoint} isFree={isFree} canUsePremium={canUsePremium} switching={switching} onApply={applyEndpoint} />
              ))
            )}
          </div>

          {isFallback ? (
            <span className="text-[10px] text-warning/70">远程获取失败，显示内置列表</span>
          ) : null}
        </motion.div>
      </div>
    </AnimatePresence>
  )
}

function EpRow({ ep, result, testing, isCurrent, isFree, canUsePremium, switching, onApply }: {
  ep: EndpointItem
  result: EpResult | null
  testing?: boolean
  isCurrent?: boolean
  isFree?: boolean
  canUsePremium?: boolean
  switching: string | null
  onApply: (url: string) => void
}) {
  const isError = result && !result.ok
  const canApply = result?.ok && !testing && !isCurrent
  const isPremium = ep.premium === true
  const median = result?.median_ms

  return (
    <div
      className={`flex items-start gap-3 px-3 py-2.5 rounded-btn border transition-colors ${
        isError
          ? 'border-danger/30 bg-danger/5'
          : result?.ok
            ? 'border-bear/20 bg-bear/5'
            : isCurrent
              ? 'border-accent/20 bg-accent/5'
              : 'border-border bg-surface'
      }`}
    >
      <div className="flex-1 min-w-0">
        {/* 第1行:label + 徽章(左) / 中位延迟(右) */}
        <div className="flex items-center gap-1.5">
          <div className="flex items-center gap-1.5 flex-wrap min-w-0">
            <span className="text-xs font-medium text-foreground">{ep.label}</span>
            {isPremium && (
              <span className="inline-flex items-center gap-0.5 text-[9px] px-1.5 py-px rounded-sm bg-warning/15 text-warning font-medium" title={ep.description ?? '需专线加速权限'}>
                <Crown className="h-2.5 w-2.5" />
                专线
              </span>
            )}
            {isCurrent && (
              <span className="text-[9px] px-1.5 py-px rounded-sm bg-accent/15 text-accent font-medium">使用中</span>
            )}
          </div>
          {/* 中位延迟 —— 测试中/前/后都占位,避免高度跳动 */}
          <span className="ml-auto shrink-0 text-sm font-mono font-medium tabular-nums leading-none">
            {isFree ? (
              // Free 模式:普通付费端点需 Starter+,premium 端点需 Expert+
              <span
                className={`text-[10px] px-1.5 py-0.5 rounded-sm font-medium font-sans ${
                  isPremium
                    ? 'bg-warning/15 text-warning'
                    : 'bg-muted/10 text-muted/70'
                }`}
                title={isPremium ? '需 Expert 及以上套餐' : '需 Starter+ 套餐'}
              >
                {isPremium ? 'Expert+' : 'Starter+'}
              </span>
            ) : testing ? (
              <span className="text-[11px] text-muted animate-pulse">测试中…</span>
            ) : result && result.ok && median != null ? (
              <span className={median < 500 ? 'text-bear' : median < 1000 ? 'text-warning' : 'text-danger'}>
                {median} ms
              </span>
            ) : result && !result.ok ? (
              <span className="text-[11px] text-danger">{result.error ?? '不可达'}</span>
            ) : (
              <span className="text-[11px] text-muted/40">—</span>
            )}
          </span>
        </div>

        {/* 第2行:description */}
        <span className="block text-[10px] text-muted/70 leading-snug mt-0.5 break-words">{ep.description}</span>

        {/* 第3行:URL(左) / min~max·成功率(右) —— 副信息始终占位,行数不变 */}
        <div className="flex items-center gap-2 mt-0.5">
          <span className="text-[10px] text-muted/50 font-mono truncate min-w-0" title={ep.url}>{ep.url.replace('https://', '')}</span>
          <span className="ml-auto shrink-0 text-[9px] text-muted/50 font-mono whitespace-nowrap">
            {result && result.ok && result.min_ms != null
              ? `${result.min_ms}~${result.max_ms} · ${result.success}/${result.rounds}`
              : '\u00A0'}
          </span>
        </div>
      </div>

      {/* 应用按钮区域 —— Free 模式不可用任何付费端点；专线端点需 Expert+ */}
      {isFree ? null : (isPremium && !canUsePremium) ? (
        // 专线端点:需 Expert 及以上套餐权限,当前套餐不足,不可应用
        <span
          className="shrink-0 inline-flex items-center gap-1 px-2 py-1 rounded-btn text-[11px] font-medium bg-warning/10 text-warning/70 cursor-not-allowed select-none mt-0.5"
          title="需要 Expert 及以上套餐的专线加速权限"
        >
          <Crown className="h-3 w-3" />
          Expert+
        </span>
      ) : canApply ? (
        <button
          onClick={() => onApply(ep.url)}
          disabled={switching !== null}
          className="shrink-0 inline-flex items-center gap-1 px-2 py-1 rounded-btn text-[11px] font-medium bg-accent/15 text-accent hover:bg-accent/25 disabled:opacity-50 transition-colors mt-0.5"
        >
          {switching === ep.url ? <Loader2 className="h-3 w-3 animate-spin" /> : <Check className="h-3 w-3" />}
          应用
        </button>
      ) : null}
    </div>
  )
}
