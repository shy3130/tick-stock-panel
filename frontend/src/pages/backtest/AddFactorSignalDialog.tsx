import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Zap } from 'lucide-react'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'
import { api, type FactorBatchItem } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

const INPUT_CLS = 'h-8 w-full rounded-input border border-border bg-surface px-2.5 text-xs text-foreground focus:border-accent focus:outline-none'

/** 按因子族给默认阈值 (RSI/KDJ 经典区间, z 分 ±2, 有界位置 0.2/0.8, 乖离 ±5%)。 */
function suggestThreshold(factorId: string, op: '>' | '<'): number {
  if (/^rsi_/.test(factorId)) return op === '<' ? 30 : 70
  if (/^kdj_/.test(factorId)) return op === '<' ? 20 : 80
  if (factorId.includes('_z_')) return op === '<' ? -2 : 2
  if (/(boll_position|close_position|position_240d)/.test(factorId)) return op === '<' ? 0.2 : 0.8
  if (/_bias/.test(factorId)) return op === '<' ? -0.05 : 0.05
  return 0
}

interface CreatedSignal {
  id: string
  kind: 'entry' | 'exit' | 'both'
  name: string
}

/**
 * 因子 → 条件信号快捷创建。
 * - 检验页传入 item: 固定因子, 方向按 IC 符号预填。
 * - 触发器场景不传 item: 显示因子选择器 (注册表全量), 阈值按因子族给建议值;
 *   onCreated 回调让调用方把新信号立即启用为触发器。
 */
export function AddFactorSignalDialog({
  item, defaultKind = 'entry', onCreated, onClose,
}: {
  item?: FactorBatchItem
  defaultKind?: 'entry' | 'exit'
  onCreated?: (signal: CreatedSignal) => void
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [selectedId, setSelectedId] = useState('')
  const library = useQuery({
    queryKey: QK.factorLibrary('all'),
    queryFn: () => api.factorLibrary(),
    enabled: !item,
    staleTime: 60_000,
  })
  const libItem = !item ? library.data?.factors.find(f => f.id === selectedId) : undefined
  const selected = item
    ? { factor_name: item.factor_name, label: item.label, group: item.group }
    : libItem
      ? { factor_name: libItem.id, label: libItem.label, group: libItem.group }
      : undefined

  const icPositive = (item?.ic_mean ?? 0) >= 0
  const [op, setOp] = useState<'>' | '<'>(item ? (icPositive ? '>' : '<') : '>')
  const [threshold, setThreshold] = useState(
    item ? String(suggestThreshold(item.factor_name, icPositive ? '>' : '<')) : '',
  )
  const [name, setName] = useState(item ? `${item.label}·${icPositive ? '高值利多' : '低值利多'}` : '')
  const [kind, setKind] = useState<'entry' | 'exit' | 'both'>(defaultKind)
  const thresholdValid = threshold.trim() !== '' && Number.isFinite(Number(threshold))

  const pickFactor = (factorId: string) => {
    setSelectedId(factorId)
    const factor = library.data?.factors.find(f => f.id === factorId)
    if (!factor) return
    setThreshold(String(suggestThreshold(factorId, op)))
    setName(`${factor.label}·${op === '>' ? '高值' : '低值'}`)
  }
  const pickOp = (nextOp: '>' | '<') => {
    setOp(nextOp)
    if (!item && selected) setThreshold(String(suggestThreshold(selected.factor_name, nextOp)))
  }

  const signalId = selected
    ? `f_${selected.factor_name}`.replace(/[^a-z0-9_]/g, '').slice(0, 40)
    : ''

  const save = useMutation({
    mutationFn: () => api.customSignalSave({
      id: signalId,
      name: name.trim() || selected!.label,
      kind,
      enabled: true,
      conditions: [{ left: selected!.factor_name, op, right: threshold.trim(), leftDays: 0, rightDays: 0 }],
    }),
    onSuccess: () => {
      onCreated?.({ id: signalId, kind, name: name.trim() || selected!.label })
      toast('已保存为自定义信号（信号库可查看编辑）', 'success')
      void queryClient.invalidateQueries({ queryKey: QK.customSignals })
      onClose()
    },
    onError: (error: Error) => toast(`保存失败 · ${error.message}`, 'error'),
  })

  const grouped = !item && library.data
    ? Object.entries(library.data.factors.reduce<Record<string, typeof library.data.factors>>((acc, f) => {
        ;(acc[f.group] ??= []).push(f)
        return acc
      }, {})).sort(([a], [b]) => a.localeCompare(b, 'zh'))
    : []

  return (
    <Modal
      onClose={onClose}
      labelledBy="add-factor-signal-title"
      panelClassName="flex max-h-[86vh] w-[92vw] max-w-md flex-col overflow-hidden border border-border bg-surface shadow-xl rounded-card"
    >
      <div className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Zap className="h-4 w-4 shrink-0 text-amber-400" />
            <span id="add-factor-signal-title" className="text-sm font-semibold text-foreground">因子加入信号条件</span>
          </div>
          {selected && <p className="mt-1 font-mono text-[11px] text-muted">{selected.factor_name}</p>}
        </div>
        {selected && <span className="shrink-0 rounded-btn bg-elevated px-1.5 py-0.5 text-[10px] text-secondary">{selected.label}</span>}
      </div>

      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-4 py-3">
        {!item && (
          <label className="block">
            <span className="mb-1 block text-[10px] text-muted">选择因子（内置 / 自定义 / 复合）</span>
            <select value={selectedId} onChange={event => pickFactor(event.target.value)} className={INPUT_CLS}>
              <option value="">{library.isPending ? '加载因子中…' : '请选择因子'}</option>
              {grouped.map(([group, factors]) => (
                <optgroup key={group} label={group}>
                  {factors.map(f => (
                    <option key={f.id} value={f.id}>{f.label}（{f.id}）</option>
                  ))}
                </optgroup>
              ))}
            </select>
          </label>
        )}
        {item ? (
          <div className="rounded-btn border border-border bg-base/40 px-3 py-2 text-[11px] leading-relaxed text-secondary">
            检验方向：IC {item.ic_mean == null ? '—' : (item.ic_mean * 100).toFixed(2)}%
            {icPositive ? '（值大看多，条件取高值端）' : '（值小看多，条件取低值端）'}。方向与阈值只是预填建议，可按业务调整。
          </div>
        ) : (
          <div className="rounded-btn border border-border bg-base/40 px-3 py-2 text-[11px] leading-relaxed text-secondary">
            阈值按因子族惯例给建议值（RSI/KDJ 区间、z 分 ±2、有界位置 0.2/0.8、乖离 ±5%），请结合检验结果调整。
          </div>
        )}
        <div className="grid grid-cols-[5rem_1fr] gap-2">
          <label className="block">
            <span className="mb-1 block text-[10px] text-muted">运算符</span>
            <select value={op} onChange={event => pickOp(event.target.value as '>' | '<')} className={INPUT_CLS}>
              <option value=">">&gt; 超过阈值</option>
              <option value="<">&lt; 低于阈值</option>
            </select>
          </label>
          <label className="block">
            <span className="mb-1 block text-[10px] text-muted">阈值（按因子族惯例建议）</span>
            <input
              type="number" step="any" value={threshold}
              onChange={event => setThreshold(event.target.value)}
              className={`${INPUT_CLS} font-mono`}
            />
          </label>
        </div>
        <label className="block">
          <span className="mb-1 block text-[10px] text-muted">信号名称</span>
          <input type="text" value={name} maxLength={24} onChange={event => setName(event.target.value)} className={INPUT_CLS} />
        </label>
        <label className="block">
          <span className="mb-1 block text-[10px] text-muted">类型</span>
          <select value={kind} onChange={event => setKind(event.target.value as 'entry' | 'exit' | 'both')} className={INPUT_CLS}>
            <option value="entry">入场</option>
            <option value="exit">出场</option>
            <option value="both">出入通用</option>
          </select>
        </label>
        <p className="text-[10px] leading-relaxed text-muted">
          保存后成为 csg_ 信号列：选股、回测、盘后监控可用；因子列由历史路径自动补算（与检验同一条计算管线），盘中实时快照无滚动窗口、该信号盘中不触发。
        </p>
      </div>

      <div className="flex items-center justify-end gap-2 border-t border-border px-4 py-3">
        <button type="button" onClick={onClose} className="rounded-btn bg-elevated px-3 py-1.5 text-xs text-secondary transition-colors hover:text-foreground">取消</button>
        <button
          type="button"
          onClick={() => save.mutate()}
          disabled={save.isPending || !selected || !thresholdValid || !name.trim()}
          className="inline-flex items-center gap-1.5 rounded-btn bg-amber-500/90 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-amber-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Zap className="h-3.5 w-3.5" />
          {save.isPending ? '保存中…' : '保存信号'}
        </button>
      </div>
    </Modal>
  )
}
