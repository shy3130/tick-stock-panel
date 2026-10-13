/**
 * 情绪评分口径配置对话框。
 *
 * 三块内容:
 *   1. 计算方式说明 (公式链路 + 各维度当前实际得分, 让「35 分是怎么来的」可解释)
 *   2. 可编辑项 (维度权重 / 子项权重 / 归一化区间 / 档位阈值)
 *   3. 恢复出厂口径
 *
 * 口径契约在后端 app/services/emotion_score.py: 前端只负责渲染返回值与回写改动,
 * 不硬编码任何权重或区间字面量 —— 出厂默认也从上线的 defaults 里取。
 */
import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { Loader2, X, RotateCcw, Info, Sigma } from 'lucide-react'
import { api, type EmotionScoreConfig, type EmotionScoreConfigPayload } from '@/lib/api'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'
import { toast } from '@/components/Toast'

// 深拷贝: 编辑期间不污染传入对象 (取消失效即丢弃)
const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v))

/**
 * 情绪评分口径配置对话框。
 *
 * 三块内容:
 *   1. 计算方式说明 (公式链路, 让「35 分是怎么来的」可解释)
 *   2. 可编辑项 (维度权重 / 子项权重 / 归一化区间 / 档位阈值)
 *   3. 恢复出厂口径
 *
 * 必须走 portal 挂到 body: 本对话框由看板卡片内部触发, 而 react-grid-layout 给
 * 每张卡片设了 transform (拖拽定位), 会创建新的层叠上下文 —— 卡片内的 fixed/z-50
 * 被限制在卡片内, 遮罩盖不住旁边的卡片。
 */
export function EmotionScoreConfigDialog({ onClose }: { onClose: () => void }) {
  const backdrop = useDialogBackdrop(onClose)
  const [payload, setPayload] = useState<EmotionScoreConfigPayload | null>(null)
  const [draft, setDraft] = useState<EmotionScoreConfig | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    setLoading(true)
    api.emotionScoreConfig()
      .then(res => {
        if (!alive) return
        setPayload(res)
        setDraft(clone(res.config))
      })
      .catch(e => alive && setError(String(e)))
      .finally(() => alive && setLoading(false))
    return () => { alive = false }
  }, [])

  const order = payload?.dimension_order ?? []
  const labels = payload?.labels ?? []

  const setDimWeight = (key: string, v: number) =>
    setDraft(d => d && ({ ...d, dimensions: { ...d.dimensions, [key]: { ...d.dimensions[key], weight: v } } }))

  const setTerm = (dimKey: string, termKey: string, field: 'weight' | 'low' | 'high', v: number) =>
    setDraft(d => {
      if (!d) return d
      const dim = d.dimensions[dimKey]
      return {
        ...d,
        dimensions: {
          ...d.dimensions,
          [dimKey]: {
            ...dim,
            terms: dim.terms.map(t => (t.key === termKey ? { ...t, [field]: v } : t)),
          },
        },
      }
    })

  const setThreshold = (key: string, v: number) =>
    setDraft(d => d && ({ ...d, thresholds: { ...d.thresholds, [key]: v } }))

  const save = async () => {
    if (!draft) return
    setSaving(true); setError('')
    try {
      const res = await api.emotionScoreConfigUpdate(draft)
      setPayload(res)
      setDraft(clone(res.config))
      toast('情绪评分口径已保存', 'success')
      onClose()
    } catch (e) {
      setError(String(e))
    } finally {
      setSaving(false)
    }
  }

  const reset = async () => {
    setSaving(true); setError('')
    try {
      const res = await api.emotionScoreConfigUpdate(null)
      setPayload(res)
      setDraft(clone(res.defaults))
      toast('已恢复出厂口径', 'success')
    } catch (e) {
      setError(String(e))
    } finally {
      setSaving(false)
    }
  }

  return createPortal(
    <div className="fixed inset-0 z-[60] flex items-center justify-center">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" {...backdrop} />
      <div className="relative mx-4 flex max-h-[88vh] w-full max-w-3xl flex-col overflow-hidden rounded-dialog border border-border bg-surface shadow-2xl">
        {/* 头部 */}
        <div className="flex items-center gap-2.5 border-b border-border px-5 py-3.5">
          <Sigma className="h-4 w-4 shrink-0 text-accent" />
          <h3 className="text-sm font-medium text-foreground">情绪评分 · 计算方式</h3>
          {payload?.customized && (
            <span className="rounded-full bg-accent/15 px-2 py-0.5 text-[10px] font-medium leading-none text-accent">
              已自定义
            </span>
          )}
          <button onClick={onClose} className="ml-auto rounded p-0.5 text-secondary transition-colors hover:bg-elevated hover:text-foreground" aria-label="关闭">
            <X className="h-4 w-4" />
          </button>
        </div>

        {loading ? (
          <div className="flex items-center justify-center gap-2 py-20 text-xs text-muted">
            <Loader2 className="h-4 w-4 animate-spin" /> 加载计算方式…
          </div>
        ) : !draft ? (
          <div className="px-5 py-16 text-center text-xs text-danger">{error || '加载失败'}</div>
        ) : (
          <div className="flex-1 overflow-y-auto px-5 py-4">
            {/* 公式说明 */}
            <div className="rounded-lg border border-border/60 bg-elevated/30 px-3.5 py-2.5">
              <div className="flex items-start gap-2 text-[11px] leading-relaxed text-secondary">
                <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent/70" />
                <div>
                  每个维度先由若干指标按 <span className="text-foreground">区间线性归一化</span> 到
                  0–100，再按 <span className="text-foreground">子项权重</span> 加权合成该维度得分；
                  各维度再按 <span className="text-foreground">维度权重</span> 加权平均得到总分。
                  <div className="mt-1 text-muted">
                    子项权重会在维度内自动归一化（权重和不必等于 1）；标记
                    <span className="mx-0.5 rounded bg-elevated/70 px-1 py-px font-mono text-[10px]">逆向</span>
                    的维度表示「越强得分越低」（如抗跌：下跌越多，得分越少）。
                  </div>
                </div>
              </div>
            </div>

            {/* 维度与子项 */}
            <div className="mt-4 space-y-3">
              {order.map(dimKey => {
                const dim = draft.dimensions[dimKey]
                if (!dim) return null
                return (
                  <div key={dimKey} className="rounded-lg border border-border/60 bg-base/30">
                    <div className="flex items-center gap-2 border-b border-border/40 px-3 py-2">
                      <span className="text-xs font-medium text-foreground">{dim.label}</span>
                      {dim.invert && (
                        <span className="rounded bg-warning/15 px-1.5 py-px text-[9px] font-medium leading-none text-warning">逆向</span>
                      )}
                      <span className="ml-auto flex items-center gap-1.5">
                        <span className="text-[10px] text-muted">维度权重</span>
                        <input
                          type="number" min={0} max={10} step={0.1}
                          value={dim.weight}
                          onChange={e => setDimWeight(dimKey, Number(e.target.value))}
                          className="h-6 w-16 rounded border border-border bg-base px-1.5 text-right font-mono text-[11px] text-foreground focus:outline-none focus:ring-1 focus:ring-accent/50"
                        />
                      </span>
                    </div>
                    <div className="px-3 py-2">
                      <div className="grid grid-cols-[1fr_54px_64px_64px] items-center gap-2 pb-1 text-[10px] text-muted">
                        <span>指标</span>
                        <span className="text-right">权重</span>
                        <span className="text-right">归一化下限</span>
                        <span className="text-right">归一化上限</span>
                      </div>
                      {dim.terms.map(term => (
                        <div key={term.key} className="grid grid-cols-[1fr_54px_64px_64px] items-center gap-2 py-0.5">
                          <span className="truncate text-[11px] text-secondary" title={`${term.label}${term.unit ? ` (${term.unit})` : ''}`}>
                            {term.label}
                            {term.unit && <span className="ml-1 text-[10px] text-muted/70">{term.unit}</span>}
                          </span>
                          {(['weight', 'low', 'high'] as const).map(field => (
                            <input
                              key={field}
                              type="number" step={field === 'weight' ? 0.05 : 'any'}
                              value={term[field]}
                              onChange={e => setTerm(dimKey, term.key, field, Number(e.target.value))}
                              className="h-6 w-full rounded border border-border bg-base px-1.5 text-right font-mono text-[11px] text-foreground focus:outline-none focus:ring-1 focus:ring-accent/50"
                            />
                          ))}
                        </div>
                      ))}
                    </div>
                  </div>
                )
              })}
            </div>

            {/* 档位阈值 */}
            <div className="mt-4 rounded-lg border border-border/60 bg-base/30 px-3 py-2.5">
              <div className="text-xs font-medium text-foreground">档位阈值</div>
              <p className="mt-1 text-[11px] leading-relaxed text-muted">
                总分达到阈值即归入该档位（自上而下匹配第一个满足项，最低档为
                <span className="mx-0.5 text-secondary">{labels.find(l => l.key === 'fallback')?.name ?? '冰点'}</span>）。
              </p>
              <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
                {labels.filter(l => l.key !== 'fallback').map(l => (
                  <div key={l.key} className="flex items-center gap-2">
                    <span className="w-8 shrink-0 text-[11px] text-secondary">{l.name}</span>
                    <input
                      type="number" min={0} max={100} step={1}
                      value={draft.thresholds[l.key] ?? 0}
                      onChange={e => setThreshold(l.key, Number(e.target.value))}
                      className="h-6 w-full rounded border border-border bg-base px-1.5 text-right font-mono text-[11px] text-foreground focus:outline-none focus:ring-1 focus:ring-accent/50"
                    />
                  </div>
                ))}
              </div>
            </div>

            {error && (
              <div className="mt-3 rounded-lg bg-danger/5 px-3 py-2 text-[11px] text-danger">{error}</div>
            )}
          </div>
        )}

        {/* 底部操作 */}
        {!loading && draft && (
          <div className="flex items-center gap-2 border-t border-border px-5 py-3">
            <button
              onClick={reset}
              disabled={saving}
              className="inline-flex items-center gap-1.5 rounded-btn px-3 py-1.5 text-xs text-secondary transition-colors hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 disabled:opacity-50"
            >
              <RotateCcw className="h-3.5 w-3.5" /> 恢复出厂
            </button>
            <div className="ml-auto flex items-center gap-2">
              <button
                onClick={onClose}
                className="rounded-btn px-3.5 py-1.5 text-xs text-secondary transition-colors hover:bg-elevated hover:text-foreground"
              >
                取消
              </button>
              <button
                onClick={save}
                disabled={saving}
                className="inline-flex items-center gap-1.5 rounded-btn bg-accent px-4 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 disabled:opacity-50"
              >
                {saving && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                保存
              </button>
            </div>
          </div>
        )}
      </div>
    </div>,
    document.body,
  )
}
