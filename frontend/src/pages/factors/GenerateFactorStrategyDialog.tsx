import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Sparkles } from 'lucide-react'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'
import { api } from '@/lib/api'

const INPUT_CLS = 'h-8 w-full rounded-input border border-border bg-surface px-2.5 text-xs text-foreground focus:border-accent focus:outline-none'

/** 渲染与挖掘发布同构的单因子排名策略源码 (FactorRankResearchMatrixStrategy)。 */
function renderStrategyCode(strategyId: string, name: string, factorId: string, direction: 'high' | 'low', description: string): string {
  const assetTypes = '["stock"]'
  return `"""${name}: 由因子库一键生成的单因子排名策略。"""
from app.strategy.research.factor_rank_research import FactorRankResearchMatrixStrategy

META = {
    "id": ${JSON.stringify(strategyId)},
    "name": ${JSON.stringify(name)},
    "description": ${JSON.stringify(description)},
    "tags": ["factor", "auto-generated"],
    "asset_types": ${assetTypes},
    "timeframes": ["1d"],
    "params": [
        {"id": "entry_score", "label": "入选评分下限", "type": "float", "default": 70.0, "min": 0.0, "max": 100.0, "step": 5.0},
        {"id": "exit_score", "label": "离场评分上限", "type": "float", "default": 40.0, "min": 0.0, "max": 100.0, "step": 5.0},
        {"id": "top_rank", "label": "每日入选上限", "type": "int", "default": 20, "min": 1, "max": 100, "step": 1},
    ],
    "scoring": {},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_factor_rank_entry"]
EXIT_SIGNALS = ["signal_factor_rank_exit"]
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 30

SCORING = {${JSON.stringify(factorId)}: 1.0}
DIRECTIONS = {${JSON.stringify(factorId)}: ${JSON.stringify(direction)}}
MATRIX_STRATEGY = FactorRankResearchMatrixStrategy(SCORING, DIRECTIONS)
`
}

/** 因子库 → 一键生成完整可回测策略 (单因子排名, 参数化入场/离场评分与每日上限)。 */
export function GenerateFactorStrategyDialog({
  item, onClose,
}: {
  item: { id: string; label: string; group: string; direction?: 'high' | 'low' | 'none' }
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [name, setName] = useState(`${item.label}排名策略`)
  const [direction, setDirection] = useState<'high' | 'low'>(item.direction === 'low' ? 'low' : 'high')

  const generate = useMutation({
    mutationFn: async () => {
      const baseId = `custom_factor_${item.id}`.replace(/[^a-z0-9_]/g, '').slice(0, 60)
      const description = `Generated from factor ${item.id} (${item.label})`
      const attempts = [baseId, `${baseId}_2`, `${baseId}_3`]
      let lastError: Error | null = null
      for (const strategyId of attempts) {
        try {
          return await api.strategySaveCodeV2({
            strategy_id: strategyId,
            code: renderStrategyCode(strategyId, name.trim(), item.id, direction, description),
            target_source: 'custom',
            mode: 'create',
            name: name.trim(),
            description,
          })
        } catch (error) {
          lastError = error as Error
          // ID 冲突时换后缀重试, 其余错误直接抛出
          if (!/已存在|already exists/i.test((error as Error).message)) throw error
        }
      }
      throw lastError ?? new Error('生成失败')
    },
    onSuccess: result => {
      toast(`已生成策略 ${result.strategy_id}，可到回测页运行`, 'success')
      void queryClient.invalidateQueries()
      onClose()
    },
    onError: (error: Error) => toast(`生成失败 · ${error.message}`, 'error'),
  })

  return (
    <Modal
      onClose={onClose}
      labelledBy="generate-factor-strategy-title"
      panelClassName="flex max-h-[86vh] w-[92vw] max-w-md flex-col overflow-hidden border border-border bg-surface shadow-xl rounded-card"
    >
      <div className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Sparkles className="h-4 w-4 shrink-0 text-accent" />
            <span id="generate-factor-strategy-title" className="text-sm font-semibold text-foreground">因子生成策略</span>
          </div>
          <p className="mt-1 font-mono text-[11px] text-muted">{item.id}</p>
        </div>
        <span className="shrink-0 rounded-btn bg-elevated px-1.5 py-0.5 text-[10px] text-secondary">{item.label}</span>
      </div>

      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-4 py-3">
        <div className="rounded-btn border border-border bg-base/40 px-3 py-2 text-[11px] leading-relaxed text-secondary">
          生成一个单因子排名策略：按因子值全市场排名打分，评分高于「入选评分下限」入场、跌破「离场评分上限」出场。
          入场/离场评分、每日入选上限都是策略参数，回测时可直接调。
        </div>
        <label className="block">
          <span className="mb-1 block text-[10px] text-muted">策略名称</span>
          <input type="text" value={name} maxLength={24} onChange={event => setName(event.target.value)} className={INPUT_CLS} />
        </label>
        <div>
          <span className="mb-1 block text-[10px] text-muted">因子方向（打分方向）</span>
          <div className="flex gap-1.5">
            {([['high', '值大加分'], ['low', '值小加分']] as const).map(([value, label]) => (
              <button
                key={value}
                type="button"
                onClick={() => setDirection(value)}
                className={`rounded-btn border px-2.5 py-1.5 text-[11px] transition-colors ${direction === value
                  ? 'border-accent/50 bg-accent/10 text-accent'
                  : 'border-border bg-base text-muted hover:border-accent/40'}`}
              >
                {label}
              </button>
            ))}
          </div>
          <p className="mt-1 text-[10px] text-muted">拿不准就先按默认生成，回测对比两个方向再定。</p>
        </div>
        <p className="text-[10px] leading-relaxed text-muted">
          生成后：默认止损 -8%、最大持仓 30 个交易日、评分口径与检验/挖掘同一条计算管线（matrix_native）。策略文件保存在自定义策略目录，可在策略页查看。
        </p>
      </div>

      <div className="flex items-center justify-end gap-2 border-t border-border px-4 py-3">
        <button type="button" onClick={onClose} className="rounded-btn bg-elevated px-3 py-1.5 text-xs text-secondary transition-colors hover:text-foreground">取消</button>
        <button
          type="button"
          onClick={() => generate.mutate()}
          disabled={generate.isPending || !name.trim()}
          className="inline-flex items-center gap-1.5 rounded-btn bg-accent px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Sparkles className="h-3.5 w-3.5" />
          {generate.isPending ? '生成中…' : '生成策略'}
        </button>
      </div>
    </Modal>
  )
}
