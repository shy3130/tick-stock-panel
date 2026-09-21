// 新建策略「默认基础参数」设置: 策略页右上角齿轮入口。
// 保存后, 之后新建的策略 (自定义模板 + AI 生成) 的 META.basic_filter 默认使用这套值。
// 「一键应用到全部策略」: 二次确认后把当前参数 PATCH 到所有股票策略的 override
// (只动 basic_filter, 不碰各策略已调的 params/评分), 并同步存为默认配置。
import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { SlidersHorizontal, RotateCcw, Save, Zap } from 'lucide-react'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'
import { storage, type DefaultStrategyBasicFilter } from '@/lib/storage'
import { color } from '@/lib/colors'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { RangeField, ALL_BOARDS } from './StrategySettingsDialog'

/** 与自定义策略模板内置默认一致 (CUSTOM_TEMPLATE 的 basic_filter 段) */
export const BUILTIN_DEFAULT_BASIC_FILTER: DefaultStrategyBasicFilter = {
  price_min: 5, price_max: 200,
  float_cap_min: 30e8, float_cap_max: 1500e8,
  amount_min: null, amount_max: null,
  turnover_min: 1, turnover_max: null,
  exclude_st: true,
  boards: ['沪主板', '深主板', '创业板', '科创板'],
}

export function loadDefaultBasicFilter(): DefaultStrategyBasicFilter {
  const saved = storage.defaultStrategyBasicFilter.get(null)
  return saved ? { ...BUILTIN_DEFAULT_BASIC_FILTER, ...saved } : { ...BUILTIN_DEFAULT_BASIC_FILTER }
}

interface Props {
  show: boolean
  onClose: () => void
}

export function DefaultStrategyParamsDialog({ show, onClose }: Props) {
  const [bf, setBf] = useState<DefaultStrategyBasicFilter>(() => loadDefaultBasicFilter())
  // 一键应用: idle → confirm (二次确认) → applying (分批 PATCH) → idle
  const [applyPhase, setApplyPhase] = useState<'idle' | 'confirm' | 'applying'>('idle')
  const [applyProgress, setApplyProgress] = useState({ done: 0, total: 0, failed: 0 })
  const qc = useQueryClient()

  // 全部股票策略 (与策略页同一 query key, 复用缓存); 仅弹窗打开时启用
  const strategiesQuery = useQuery({
    queryKey: [...QK.screenerStrategies('all'), 'all'],
    queryFn: () => api.screenerStrategies(undefined, 'all'),
    enabled: show,
  })
  const strategyCount = strategiesQuery.data?.presets?.length ?? 0

  // 重新打开时重置表单与一键应用状态
  useEffect(() => {
    if (show) {
      setBf(loadDefaultBasicFilter())
      setApplyPhase('idle')
      setApplyProgress({ done: 0, total: 0, failed: 0 })
    }
  }, [show])

  if (!show) return null

  const set = <K extends keyof DefaultStrategyBasicFilter>(key: K, value: DefaultStrategyBasicFilter[K]) =>
    setBf(prev => ({ ...prev, [key]: value }))

  const save = () => {
    storage.defaultStrategyBasicFilter.set(bf)
    toast('默认基础参数已保存，之后新建的策略将默认使用', 'success')
    onClose()
  }

  const applyToAll = async () => {
    const ids = (strategiesQuery.data?.presets ?? []).map(p => p.id)
    if (!ids.length) return
    setApplyPhase('applying')
    setApplyProgress({ done: 0, total: ids.length, failed: 0 })
    // 小并发分批 PATCH, 只写 basic_filter; 后端 _strip_defaults 会剥掉与
    // 策略 META 默认相同的键, 各策略已调的 params/评分不受影响
    let failed = 0
    const CHUNK = 8
    for (let i = 0; i < ids.length; i += CHUNK) {
      const results = await Promise.allSettled(
        ids.slice(i, i + CHUNK).map(id => api.strategyPatchConfig(id, { basic_filter: { ...bf } })),
      )
      failed += results.filter(r => r.status === 'rejected').length
      setApplyProgress({ done: Math.min(i + CHUNK, ids.length), total: ids.length, failed })
    }
    // 应用即所见: 当前参数同时存为默认配置, 新建策略与存量策略保持一致
    storage.defaultStrategyBasicFilter.set(bf)
    qc.invalidateQueries({ queryKey: ['screener-strategies'] })
    setApplyPhase('idle')
    if (failed === 0) {
      toast(`已应用到全部 ${ids.length} 个策略，并保存为默认配置；重跑后生效`, 'success')
    } else {
      toast(`已应用 ${ids.length - failed}/${ids.length} 个策略（${failed} 个失败），并保存为默认配置`, 'error')
    }
  }

  return (
    <Modal
      onClose={onClose}
      labelledBy="default-strategy-params-title"
      overlayClassName="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm"
      panelClassName="w-[460px] max-w-[92vw] max-h-[86vh] bg-surface/95 backdrop-blur-xl border border-border/50 rounded-2xl shadow-2xl flex flex-col overflow-hidden"
    >
      {/* 标题 */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-border/50">
        <div className="flex items-center gap-2">
          <SlidersHorizontal className="h-4 w-4 text-accent" />
          <span id="default-strategy-params-title" className="text-sm font-semibold text-foreground">默认基础参数</span>
        </div>
        <button onClick={onClose} className="p-1 rounded hover:bg-elevated text-muted hover:text-foreground transition-colors cursor-pointer" title="关闭">
          <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M18 6 6 18M6 6l12 12" /></svg>
        </button>
      </div>

      {/* 内容 */}
      <div className="flex-1 overflow-y-auto px-4 py-3 space-y-2">
        <p className="text-[11px] text-muted leading-relaxed">
          之后新建的策略（含 AI 生成）将默认使用以下基础过滤参数；已存在的策略不受影响，可用下方「一键应用」批量同步，或逐策略修改。
        </p>
        <div className="space-y-2">
          <RangeField label="价格" minVal={bf.price_min} maxVal={bf.price_max} onMinChange={v => set('price_min', v)} onMaxChange={v => set('price_max', v)} unit="元" step="1" />
          <RangeField label="流通市值" minVal={bf.float_cap_min != null ? bf.float_cap_min / 1e8 : null} maxVal={bf.float_cap_max != null ? bf.float_cap_max / 1e8 : null} onMinChange={v => set('float_cap_min', v != null ? v * 1e8 : null)} onMaxChange={v => set('float_cap_max', v != null ? v * 1e8 : null)} unit="亿" step="5" />
          <RangeField label="成交额" minVal={bf.amount_min != null ? bf.amount_min / 1e8 : null} maxVal={bf.amount_max != null ? bf.amount_max / 1e8 : null} onMinChange={v => set('amount_min', v != null ? v * 1e8 : null)} onMaxChange={v => set('amount_max', v != null ? v * 1e8 : null)} unit="亿" step="0.5" />
          <RangeField label="换手率" minVal={bf.turnover_min} maxVal={bf.turnover_max} onMinChange={v => set('turnover_min', v)} onMaxChange={v => set('turnover_max', v)} unit="%" step="0.5" />
          <div className="flex items-start gap-1.5">
            <span className="text-[11px] text-secondary w-16 shrink-0 text-right pt-0.5">板块</span>
            <div className="flex flex-wrap gap-0.5">
              {ALL_BOARDS.map(b => {
                const active = bf.boards.includes(b)
                return (
                  <button key={b} onClick={() => { const next = active ? bf.boards.filter(x => x !== b) : [...bf.boards, b]; set('boards', next.length === 0 ? [...ALL_BOARDS] : next) }}
                    className={`px-1.5 py-0.5 rounded text-[10px] font-medium border transition-colors cursor-pointer ${active ? `${color.select.border} ${color.select.bgLight} ${color.select.text}` : `border-border bg-base text-muted ${color.select.borderHover}`}`}>{b}</button>
                )
              })}
            </div>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="text-[11px] text-secondary w-16 shrink-0 text-right">ST</span>
            <button onClick={() => set('exclude_st', !bf.exclude_st)}
              className={`px-1.5 py-0.5 rounded text-[10px] font-medium border transition-colors cursor-pointer ${bf.exclude_st ? 'border-danger/40 bg-danger/10 text-danger' : 'border-border bg-base text-muted hover:border-danger/30'}`}>{bf.exclude_st ? '排除' : '包含'}</button>
          </div>
        </div>
      </div>

      {/* 一键应用到全部存量策略 */}
      <div className="px-4 py-3 border-t border-border/50">
        {applyPhase === 'idle' && (
          <button onClick={() => setApplyPhase('confirm')} disabled={!strategyCount}
            title="把当前参数写入全部股票策略的基础参数（不影响各策略已调的其他配置）"
            className="w-full inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-btn text-xs font-medium
              border border-amber-500/40 bg-amber-500/10 text-amber-500 hover:bg-amber-500/20
              transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed">
            <Zap className="h-3 w-3" />
            一键应用到全部策略{strategyCount ? `（${strategyCount} 个）` : ''}
          </button>
        )}
        {applyPhase === 'confirm' && (
          <div className="flex items-center gap-2">
            <span className="flex-1 text-[11px] text-muted leading-tight">
              将覆盖全部 <span className="text-amber-500 font-medium">{strategyCount}</span> 个策略的基础参数（只动基础过滤，不碰各策略的参数/评分），重跑后生效。
            </span>
            <button onClick={() => setApplyPhase('idle')}
              className="px-2.5 py-1.5 rounded-btn text-xs text-secondary border border-border hover:text-foreground transition-colors cursor-pointer shrink-0">取消</button>
            <button onClick={applyToAll}
              className="px-3 py-1.5 rounded-btn text-xs font-medium text-white bg-amber-500 hover:bg-amber-500/90 transition-colors cursor-pointer shrink-0">确认覆盖</button>
          </div>
        )}
        {applyPhase === 'applying' && (
          <div className="flex items-center justify-center gap-2 px-3 py-1.5 rounded-btn text-xs text-muted border border-border/50 bg-elevated/50">
            <span className="h-3 w-3 rounded-full border-2 border-amber-500/30 border-t-amber-500 animate-spin" />
            应用中 {applyProgress.done}/{applyProgress.total}
            {applyProgress.failed > 0 && <span className="text-danger">（{applyProgress.failed} 失败）</span>}
          </div>
        )}
      </div>

      {/* 底部按钮 */}
      <div className="flex items-center justify-between gap-2 px-4 py-3 border-t border-border/50">
        <button onClick={() => setBf({ ...BUILTIN_DEFAULT_BASIC_FILTER })}
          className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-btn text-xs text-muted border border-border hover:text-foreground hover:border-accent/40 transition-colors cursor-pointer">
          <RotateCcw className="h-3 w-3" />
          恢复默认
        </button>
        <div className="flex items-center gap-2">
          <button onClick={onClose} className="px-3 py-1.5 rounded-btn text-xs text-secondary border border-border hover:text-foreground transition-colors cursor-pointer">取消</button>
          <button onClick={save}
            className="inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-btn text-xs font-medium text-white bg-accent hover:bg-accent/90 transition-colors cursor-pointer">
            <Save className="h-3 w-3" />
            保存
          </button>
        </div>
      </div>
    </Modal>
  )
}
