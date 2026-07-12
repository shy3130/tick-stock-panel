import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Loader2 } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

type PullKey = 'pipeline_pull_a_share' | 'pipeline_pull_etf' | 'pipeline_pull_index' | 'pipeline_pull_hk'

interface ScopeItem {
  key: PullKey
  label: string
  desc: string
  defaultOn: boolean
}

const ITEMS: ScopeItem[] = [
  { key: 'pipeline_pull_a_share', label: 'A股', desc: '沪深京 A 股日K(约 5500 只)', defaultOn: true },
  { key: 'pipeline_pull_index', label: '指数', desc: '主要市场指数(默认全量约 600 只)', defaultOn: true },
  { key: 'pipeline_pull_etf', label: 'ETF', desc: '场内交易基金(约 1500 只,首次较慢)', defaultOn: false },
  // 不复权是数据现实,不是可调选项:本地无港股除权数据源(chuquan_chuxi 只有 A 股,
  // tdx-hk 无 xdxr 表)。除权日附近的均线/新高新低会有假信号,故文案里必须点出来,
  // 不能让用户以为这是和 ETF 一样"默认关闭、开了就完整"的开关。
  { key: 'pipeline_pull_hk', label: '港股', desc: '港股日K(约 2900 只,不复权 —— 除权日附近指标可能失真)', defaultOn: false },
]

export function PipelineScopeConfig() {
  const qc = useQueryClient()
  const prefs = useQuery({ queryKey: QK.preferences, queryFn: api.preferences })

  const updateToggle = useMutation({
    mutationFn: (cfg: Partial<Record<PullKey, boolean>>) => api.updatePipelinePullTypes(cfg),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.preferences })
      qc.invalidateQueries({ queryKey: QK.dataStatus })
    },
  })

  const getValue = (key: PullKey, def: boolean) => prefs.data?.[key] ?? def

  return (
    <div className="space-y-2.5">
      <p className="text-xs text-secondary leading-relaxed">
        勾选盘后管道每次自动拉取的数据类型。仅影响后续同步,已存储的历史数据不受影响。
      </p>
      <div className="space-y-1.5">
        {ITEMS.map((item) => {
          const locked = item.key === 'pipeline_pull_a_share'
          const on = locked || getValue(item.key, item.defaultOn)
          return (
            <div key={item.key}>
              <label
                className={`flex items-start gap-2.5 rounded-card border px-3 py-2.5 transition-colors ${
                  locked ? 'cursor-default' : 'cursor-pointer'
                } ${on ? 'border-accent/40 bg-accent/[0.05]' : 'border-border bg-base/30 hover:border-border/70'}`}
              >
                <button
                  type="button"
                  onClick={() => {
                    if (!locked) updateToggle.mutate({ [item.key]: !on } as never)
                  }}
                  disabled={locked || updateToggle.isPending}
                  className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded border transition-colors ${
                    on ? 'bg-accent border-accent' : 'bg-base border-border'
                  } ${locked ? 'opacity-80' : ''}`}
                  role="checkbox"
                  aria-checked={on}
                >
                  {on && <Check className="h-3 w-3 text-white" strokeWidth={3} />}
                </button>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <span className="text-xs font-medium text-foreground">{item.label}</span>
                  </div>
                  <div className="text-[10px] text-muted leading-snug mt-0.5">{item.desc}</div>
                </div>
              </label>
            </div>
          )
        })}
      </div>
      {updateToggle.isPending && (
        <div className="flex items-center gap-1.5 text-[10px] text-muted">
          <Loader2 className="h-3 w-3 animate-spin" />保存中…
        </div>
      )}
      <div className="text-[10px] text-muted leading-relaxed pt-1">
        数据通道基于免费接口,所有档位均可拉取。
      </div>
    </div>
  )
}
