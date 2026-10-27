/**
 * 仓位策略管理面板 — 设置 → 定义 → 仓位策略 (自定义仓位规则的创建入口)。
 *
 * 用户在这里「从模板新建」或编辑策略: 名称/说明/参数走表单, 规则只写一个
 * def compute(ctx) 函数; 保存即过三关 (AST 安全校验 → 结构解析 → 合成冒烟),
 * 全过才落盘并立即出现在回测设置的「自定义策略」下拉里。
 * 坏文件只标红禁用自身, 不影响其余条目 (插件隔离)。
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { FlaskConical, Pencil, Plus, Scale, ShieldAlert, Trash2, X } from 'lucide-react'
import { api, type StrategyParamDef } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'
import { Modal } from '@/components/Modal'

const INPUT_CLS =
  'w-full rounded-input border border-border bg-surface px-2.5 py-1.5 text-xs text-foreground outline-none transition-colors focus:border-accent'

interface EditorState {
  id: string
  label: string
  description: string
  params: StrategyParamDef[]
  code: string
}

const BLANK_EDITOR: EditorState = { id: '', label: '我的仓位策略', description: '', params: [], code: '' }

function SectionTitle({ children, hint }: { children: React.ReactNode; hint?: string }) {
  return (
    <div className="flex items-baseline justify-between">
      <div className="text-xs font-medium text-foreground">{children}</div>
      {hint && <div className="text-[10px] text-muted">{hint}</div>}
    </div>
  )
}

export function PositionPoliciesPanel() {
  const queryClient = useQueryClient()
  const [editor, setEditor] = useState<EditorState | null>(null)
  const [tplKey, setTplKey] = useState('')
  const [issues, setIssues] = useState<string[]>([])
  const [smoke, setSmoke] = useState<string | null>(null)

  const listQ = useQuery({ queryKey: QK.positionPolicies, queryFn: api.positionPolicyList })
  const templatesQ = useQuery({
    queryKey: ['position-policy-templates'],
    queryFn: api.positionPolicyTemplates,
    staleTime: 600_000,
  })

  const invalidate = () => queryClient.invalidateQueries({ queryKey: QK.positionPolicies })

  const saveMut = useMutation({
    mutationFn: (payload: Parameters<typeof api.positionPolicySave>[0]) => api.positionPolicySave(payload),
    onSuccess: r => {
      toast(`仓位策略已保存: ${r.id}`, 'success')
      setEditor(null)
      setIssues([])
      setSmoke(null)
      invalidate()
    },
    onError: (e: any) => {
      const detail = e?.payload?.detail
      if (detail?.issues) setIssues(detail.issues as string[])
      else toast(String(e?.message ?? e))
    },
  })
  const validateMut = useMutation({
    mutationFn: (payload: Parameters<typeof api.positionPolicyValidate>[0]) => api.positionPolicyValidate(payload),
    onSuccess: r => {
      setIssues(r.issues)
      setSmoke(
        r.ok
          ? `冒烟通过: ${r.smoke?.weights.length ?? 0} 个候选 → 权重和 ${((r.smoke?.weights ?? []).reduce((a, b) => a + b, 0)).toFixed(3)}, 预算缩放 ${r.smoke?.budget_scale}`
          : null,
      )
    },
  })
  const deleteMut = useMutation({
    mutationFn: (id: string) => api.positionPolicyDelete(id),
    onSuccess: () => {
      toast('已删除', 'success')
      invalidate()
    },
    onError: (e: any) => toast(String(e?.message ?? e)),
  })

  const openNew = () => {
    const tpl = templatesQ.data?.templates?.[0]
    setEditor({
      ...BLANK_EDITOR,
      label: tpl?.meta.label ?? BLANK_EDITOR.label,
      params: (tpl?.meta.params ?? []) as StrategyParamDef[],
      code: tpl?.code ?? '',
    })
    setTplKey(tpl?.key ?? '')
    setIssues([])
    setSmoke(null)
  }

  const openEdit = async (id: string) => {
    try {
      const got = await api.positionPolicyGet(id)
      setEditor({
        id,
        label: got.meta.label,
        description: got.meta.description,
        params: got.meta.params ?? [],
        code: got.code,
      })
      setTplKey('')
      setIssues([])
      setSmoke(null)
    } catch (e: any) {
      toast(String(e?.message ?? e))
    }
  }

  const applyTemplate = (key: string) => {
    const tpl = templatesQ.data?.templates.find(t => t.key === key)
    if (!tpl) return
    setEditor(prev =>
      prev
        ? { ...prev, label: tpl.meta.label, params: (tpl.meta.params ?? []) as StrategyParamDef[], code: tpl.code }
        : prev,
    )
    setTplKey(key)
    setIssues([])
    setSmoke(null)
  }

  const setParam = (idx: number, patch: Partial<StrategyParamDef>) => {
    setEditor(prev => {
      if (!prev) return prev
      const params = prev.params.map((p, i) => (i === idx ? { ...p, ...patch } : p))
      return { ...prev, params }
    })
  }

  const policies = listQ.data?.policies ?? []
  const errors = listQ.data?.errors ?? {}

  return (
    <div className="rounded-card border border-border bg-surface p-4">
      <div className="flex items-center justify-between gap-2">
        <div>
          <div className="text-sm font-medium text-foreground">仓位策略</div>
          <div className="mt-0.5 text-[11px] text-muted">
            自定义动态仓位规则 (波动率目标 / 回撤自适应 / 自己写): 建仓日按当日候选与历史表现分配资金,
            在回测设置的「买入权重 → 自定义策略」选用。
          </div>
        </div>
        <button
          onClick={openNew}
          className="flex shrink-0 items-center gap-1 rounded-btn bg-accent px-2.5 py-1.5 text-xs text-white transition-opacity hover:opacity-90"
        >
          <Plus className="h-3.5 w-3.5" /> 新建仓位策略
        </button>
      </div>

      <div className="mt-3 space-y-1.5">
        {policies.length === 0 && Object.keys(errors).length === 0 && (
          <div className="rounded-btn border border-dashed border-border px-3 py-4 text-center text-[11px] text-muted">
            还没有仓位策略 — 点「新建」从模板开始, 只需写一个 compute 函数。
          </div>
        )}
        {policies.map(p => (
          <div key={p.id} className="flex items-center gap-2 rounded-btn border border-border px-3 py-2">
            <div className="min-w-0 flex-1">
              <div className="truncate text-xs font-medium text-foreground">{p.label}</div>
              <div className="truncate text-[10px] text-muted">
                {p.id}
                {p.params.length > 0 ? ` · ${p.params.length} 个参数` : ''}
                {p.description ? ` · ${p.description}` : ''}
              </div>
            </div>
            <button
              onClick={() => openEdit(p.id)}
              className="rounded-btn border border-border p-1.5 text-secondary transition-colors hover:text-accent"
              title="编辑"
            >
              <Pencil className="h-3.5 w-3.5" />
            </button>
            <button
              onClick={() => {
                if (window.confirm(`删除仓位策略「${p.label}」? 引用它的回测配置将报错。`)) deleteMut.mutate(p.id)
              }}
              className="rounded-btn border border-border p-1.5 text-secondary transition-colors hover:text-danger"
              title="删除"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          </div>
        ))}
        {Object.entries(errors).map(([file, msg]) => (
          <div key={file} className="flex items-start gap-2 rounded-btn border border-danger/30 bg-danger/5 px-3 py-2">
            <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-danger" />
            <div className="min-w-0 text-[11px] text-danger">
              <span className="font-medium">{file}</span> — 加载失败: {msg}
            </div>
          </div>
        ))}
      </div>

      {editor && (
        <Modal
          onClose={() => setEditor(null)}
          ariaLabel="编辑仓位策略"
          panelClassName="flex w-[92vw] max-w-2xl flex-col overflow-hidden p-0"
        >
          {/* 头部 */}
          <div className="flex shrink-0 items-center justify-between border-b border-border px-5 py-3.5">
            <div className="flex items-center gap-2.5">
              <div className="flex h-8 w-8 items-center justify-center rounded-btn bg-accent/10 text-accent">
                <Scale className="h-4 w-4" />
              </div>
              <div>
                <div className="text-sm font-medium text-foreground">
                  {editor.id ? '编辑仓位策略' : '新建仓位策略'}
                </div>
                <div className="text-[11px] text-muted">{editor.id || '保存后自动分配 id'}</div>
              </div>
            </div>
            <button
              onClick={() => setEditor(null)}
              className="rounded-btn p-1.5 text-secondary transition-colors hover:bg-elevated hover:text-foreground"
              title="关闭"
            >
              <X className="h-4 w-4" />
            </button>
          </div>

          {/* 主体 (可滚动) */}
          <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-5 py-4">
            <div className="space-y-2">
              <SectionTitle hint="模板会覆盖名称、参数与代码">基本信息</SectionTitle>
              <div className="grid grid-cols-2 gap-2">
                <div>
                  <label className="mb-1 block text-[11px] text-secondary">名称</label>
                  <input className={INPUT_CLS} value={editor.label} onChange={e => setEditor({ ...editor, label: e.target.value })} />
                </div>
                <div>
                  <label className="mb-1 block text-[11px] text-secondary">从模板开始</label>
                  <select className={INPUT_CLS} value={tplKey} onChange={e => applyTemplate(e.target.value)}>
                    <option value="">选择模板…</option>
                    {(templatesQ.data?.templates ?? []).map(t => (
                      <option key={t.key} value={t.key}>
                        {t.label} — {t.description}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              <div>
                <label className="mb-1 block text-[11px] text-secondary">说明</label>
                <input
                  className={INPUT_CLS}
                  value={editor.description}
                  placeholder="这个策略用什么思路分配仓位"
                  onChange={e => setEditor({ ...editor, description: e.target.value })}
                />
              </div>
            </div>

            <div className="space-y-2">
              <SectionTitle hint="回测设置里可调">参数</SectionTitle>
              {editor.params.length === 0 ? (
                <div className="rounded-btn border border-dashed border-border px-3 py-2.5 text-[11px] text-muted">
                  暂无参数 — 规则里的固定数字建议提成参数, 方便回测时调优。
                </div>
              ) : (
                <div className="space-y-1.5">
                  <div className="grid grid-cols-[1fr_1.2fr_4.5rem_5rem_1.75rem] gap-1.5 px-0.5 text-[10px] text-muted">
                    <span>标识 (代码里引用)</span>
                    <span>显示名称</span>
                    <span>类型</span>
                    <span>默认值</span>
                    <span />
                  </div>
                  {editor.params.map((pd, i) => (
                    <div key={i} className="grid grid-cols-[1fr_1.2fr_4.5rem_5rem_1.75rem] items-center gap-1.5">
                      <input
                        className={INPUT_CLS}
                        placeholder="window"
                        value={pd.id}
                        onChange={e => setParam(i, { id: e.target.value })}
                      />
                      <input
                        className={INPUT_CLS}
                        placeholder="波动率窗口"
                        value={pd.label ?? ''}
                        onChange={e => setParam(i, { label: e.target.value })}
                      />
                      <select
                        className={INPUT_CLS}
                        value={pd.type}
                        onChange={e => setParam(i, { type: e.target.value as StrategyParamDef['type'] })}
                      >
                        <option value="float">小数</option>
                        <option value="int">整数</option>
                        <option value="bool">开关</option>
                      </select>
                      <input
                        className={INPUT_CLS}
                        placeholder="20"
                        value={String(pd.default ?? '')}
                        onChange={e =>
                          setParam(i, {
                            default: pd.type === 'bool' ? e.target.value === 'true' || e.target.value === '1' : e.target.value,
                          })
                        }
                      />
                      <button
                        className="justify-self-center rounded-btn p-1 text-secondary transition-colors hover:text-danger"
                        title="删除参数"
                        onClick={() => setEditor({ ...editor, params: editor.params.filter((_, j) => j !== i) })}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  ))}
                </div>
              )}
              <button
                className="text-[11px] text-accent transition-opacity hover:underline"
                onClick={() =>
                  setEditor({
                    ...editor,
                    params: [
                      ...editor.params,
                      { id: `p${editor.params.length + 1}`, label: '新参数', type: 'float', default: 1 },
                    ],
                  })
                }
              >
                + 添加参数
              </button>
            </div>

            <div className="space-y-2">
              <SectionTitle hint="只需写这一个函数">规则代码 · def compute(ctx)</SectionTitle>
              <textarea
                className="h-44 w-full rounded-input border border-border bg-surface px-3 py-2 font-mono text-[11px] leading-relaxed text-foreground outline-none transition-colors focus:border-accent"
                value={editor.code}
                onChange={e => setEditor({ ...editor, code: e.target.value })}
                spellCheck={false}
              />
              <div className="rounded-btn bg-accent/5 px-3 py-2 font-mono text-[10px] leading-relaxed text-muted">
                ctx.selected 当日候选(按信号降序) · ctx.recent_returns 近60日收益 · ctx.closed_trades 已平仓快照 ·
                ctx.portfolio 现金/权益/回撤 · ctx.params 上面的参数
                <br />
                return PositionSizingResult(weights=[...], budget_scale=1.0) — 引擎保证单仓上限, 写错不会爆仓
              </div>
            </div>

            {(issues.length > 0 || smoke) && (
              <div
                className={`rounded-btn border px-3 py-2 text-[11px] leading-relaxed ${
                  issues.length > 0
                    ? 'border-danger/30 bg-danger/5 text-danger'
                    : 'border-emerald-500/30 bg-emerald-500/5 text-emerald-500'
                }`}
              >
                {issues.length > 0 ? (
                  issues.map((it, i) => (
                    <div key={i}>
                      ✗ {it}
                    </div>
                  ))
                ) : (
                  <div className="flex items-center gap-1.5">
                    <FlaskConical className="h-3.5 w-3.5 shrink-0" />
                    {smoke}
                  </div>
                )}
              </div>
            )}
          </div>

          {/* 底部操作栏 */}
          <div className="flex shrink-0 items-center justify-between border-t border-border px-5 py-3">
            <div className="text-[10px] text-muted">保存时自动执行安全校验 + 冒烟</div>
            <div className="flex items-center gap-2">
              <button
                className="rounded-btn border border-border px-3 py-1.5 text-xs text-secondary transition-colors hover:text-foreground disabled:opacity-50"
                disabled={validateMut.isPending || !editor.code.trim()}
                onClick={() =>
                  validateMut.mutate({
                    id: editor.id || undefined,
                    label: editor.label,
                    description: editor.description,
                    params: editor.params,
                    code: editor.code,
                  })
                }
              >
                校验并冒烟
              </button>
              <button
                className="rounded-btn bg-accent px-4 py-1.5 text-xs text-white transition-opacity hover:opacity-90 disabled:opacity-50"
                disabled={saveMut.isPending || !editor.code.trim() || !editor.label.trim()}
                onClick={() =>
                  saveMut.mutate({
                    id: editor.id || undefined,
                    label: editor.label,
                    description: editor.description,
                    params: editor.params,
                    code: editor.code,
                  })
                }
              >
                保存
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  )
}
