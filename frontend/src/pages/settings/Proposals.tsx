/**
 * 策略提案 + 策略体检 —— YMOS 防线的前端入口。
 *
 * 提案(/api/trading/proposals):
 *   单笔结果不改内核;提案必带反证条件(falsifier 必填);sampleSize<10 只登记不可批准。
 *   状态机:draft→approved|rejected, approved→trial, trial→verified|rejected。
 *   非法迁移不渲染按钮(后端同样 400)。
 *
 * 策略体检(/api/strategies/{id}/profile):
 *   风险声明(失效信号三要素 + 风险预算 + 复盘节奏)的读写与机械体检。
 *   PUT 前后端强制结构校验,422 时 detail.problems 为问题清单,这里解析后内联展示。
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, ChevronDown, ChevronRight, Lightbulb, Plus, RefreshCw, Save, ShieldCheck,
  Sparkles, Stethoscope, Trash2,
} from 'lucide-react'

import {
  api,
  tradingCreateProposal, tradingListProposals, tradingUpdateProposal,
  strategyGetProfile, strategyPutProfile, strategyValidateProfile,
  type AiExecutionMeta, type StrategyProfile, type StrategyProfileCheck,
} from '@/lib/api'
import { cn } from '@/lib/cn'
import { resolveEntryProfile } from '@/lib/aiProfile'
import { toast } from '@/components/Toast'
import { AiProviderSelector } from '@/components/AiProviderSelector'
import { AiExecutionMetaBadge } from '@/components/AiExecutionMetaBadge'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'

// ===== 提案状态与合法迁移(与 backend services/trading/proposals.py 一致)=====

const STATUS_META: Record<string, { label: string; badge: string }> = {
  draft:    { label: '草稿',   badge: 'bg-muted/10 text-muted' },
  approved: { label: '已批准', badge: 'bg-accent/10 text-accent' },
  rejected: { label: '已驳回', badge: 'bg-danger/10 text-danger' },
  trial:    { label: '试运行', badge: 'bg-warning/10 text-warning' },
  verified: { label: '已验证', badge: 'bg-bear/10 text-bear' },
}

/** 合法状态迁移;needsEvidence = 目标态要求 sampleSize>=10(后端 400 强制) */
const TRANSITIONS: Record<string, { to: string; label: string; primary?: boolean; needsEvidence?: boolean }[]> = {
  draft: [
    { to: 'approved', label: '批准', primary: true, needsEvidence: true },
    { to: 'rejected', label: '驳回' },
  ],
  approved: [{ to: 'trial', label: '试运行', primary: true }],
  trial: [
    { to: 'verified', label: '已验证', primary: true },
    { to: 'rejected', label: '驳回' },
  ],
}

const MIN_SAMPLE_FOR_APPROVAL = 10

// ===== 策略体检徽标(与 backend strategy_validator 的四种 status 一致)=====

const CHECK_META: Record<string, { label: string; badge: string }> = {
  pass:                  { label: '通过',     badge: 'bg-bear/10 text-bear' },
  partial:               { label: '部分',     badge: 'bg-warning/10 text-warning' },
  fail:                  { label: '未过',     badge: 'bg-danger/10 text-danger' },
  insufficient_evidence: { label: '证据不足', badge: 'bg-muted/10 text-muted' },
}

// ===== 策略风险声明草稿(backend schema: invalidation[] + risk + cadence)=====
// 草稿比 StrategyProfile 少 schemaVersion/strategyId(由服务端钉死)

type ProfileDraft = Omit<StrategyProfile, 'schemaVersion' | 'strategyId'>
type InvalidationItem = ProfileDraft['invalidation'][number]

const REVIEW_CADENCES = [
  { value: 'weekly', label: '每周' },
  { value: 'monthly', label: '每月' },
  { value: 'quarterly', label: '每季' },
]

function emptyDraft(): ProfileDraft {
  return {
    invalidation: [{ name: '', observable: '', action: '' }],
    risk: { positionLimitPct: 0, lossBudgetPct: 0, thesisHorizonMonths: 0 },
    cadence: { review: 'weekly' },
    family: '',
    familyMix: emptyMix(),
    playbook: emptyPlaybook(),
  }
}

/** 对象窄化(网络边界:profile 由后端 JSON 而来,逐字段防御) */
function asRecord(v: unknown): Record<string, unknown> {
  return v !== null && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : {}
}

function asNum(v: unknown): number {
  const n = Number(v)
  return Number.isFinite(n) ? n : 0
}

// 策略坐标卡 family 七选项中文标签(与 backend FAMILY_VALUES 一致)
const FAMILY_OPTIONS: { value: string; label: string }[] = [
  { value: 'value', label: '价值' },
  { value: 'growth', label: '成长' },
  { value: 'trend', label: '趋势' },
  { value: 'event', label: '事件驱动' },
  { value: 'short_horizon', label: '短周期' },
  { value: 'relative_value', label: '相对价值套利' },
  { value: 'mixed', label: '混合' },
]

// family=mixed / playbook 的空草稿(被 emptyDraft、toDraft 默认、save、JSX spread 共用)
function emptyMix(): NonNullable<ProfileDraft['familyMix']> {
  return { entryJudge: '', invalidationAuthority: '', sizingHorizon: '', conflictResolution: '' }
}

function emptyPlaybook(): NonNullable<ProfileDraft['playbook']> {
  return { scope: '', entry: '', exit: '' }
}

/** 后端 profile → 表单草稿(容错:缺段补默认,保证表单可控) */
function toDraft(p: unknown): ProfileDraft {
  const obj = asRecord(p)
  const invRaw = Array.isArray(obj.invalidation) ? obj.invalidation : []
  const inv = invRaw.map((it): InvalidationItem => {
    const r = asRecord(it)
    return {
      name: typeof r.name === 'string' ? r.name : '',
      observable: typeof r.observable === 'string' ? r.observable : '',
      action: typeof r.action === 'string' ? r.action : '',
    }
  })
  const risk = asRecord(obj.risk)
  const cadence = asRecord(obj.cadence)
  const mix = asRecord(obj.familyMix)
  const playbook = asRecord(obj.playbook)
  return {
    invalidation: inv.length ? inv : [{ name: '', observable: '', action: '' }],
    risk: {
      positionLimitPct: asNum(risk.positionLimitPct),
      lossBudgetPct: asNum(risk.lossBudgetPct),
      thesisHorizonMonths: asNum(risk.thesisHorizonMonths),
    },
    cadence: { review: typeof cadence.review === 'string' ? cadence.review : 'weekly' },
    family: typeof obj.family === 'string' ? obj.family : '',
    familyMix: {
      entryJudge: typeof mix.entryJudge === 'string' ? mix.entryJudge : '',
      invalidationAuthority: typeof mix.invalidationAuthority === 'string' ? mix.invalidationAuthority : '',
      sizingHorizon: typeof mix.sizingHorizon === 'string' ? mix.sizingHorizon : '',
      conflictResolution: typeof mix.conflictResolution === 'string' ? mix.conflictResolution : '',
    },
    playbook: {
      scope: typeof playbook.scope === 'string' ? playbook.scope : '',
      entry: typeof playbook.entry === 'string' ? playbook.entry : '',
      exit: typeof playbook.exit === 'string' ? playbook.exit : '',
    },
  }
}

function jsonBlock(v: unknown): string {
  if (v == null) return '—'
  if (typeof v === 'object' && !Array.isArray(v) && Object.keys(v as object).length === 0) return '—'
  if (Array.isArray(v) && v.length === 0) return '—'
  return JSON.stringify(v, null, 2)
}

const INPUT = 'rounded-btn border border-border bg-base px-2.5 py-1.5 text-xs text-foreground outline-none transition-colors placeholder:text-muted/60 focus:border-accent/50'

// ================================================================

export function SettingsProposalsPanel() {
  const qc = useQueryClient()
  const listQuery = useQuery({
    queryKey: ['trading-proposals'],
    queryFn: () => tradingListProposals(),
    staleTime: 15_000,
  })

  // ===== 新建提案表单 =====
  const [showForm, setShowForm] = useState(false)
  const [title, setTitle] = useState('')
  const [target, setTarget] = useState('')
  const [evidence, setEvidence] = useState('')
  const [falsifier, setFalsifier] = useState('')
  const [sampleSize, setSampleSize] = useState('0')

  const [expandedId, setExpandedId] = useState<string | null>(null)

  const createMut = useMutation({
    mutationFn: () => tradingCreateProposal({
      title: title.trim(),
      target: target.trim(),
      evidence: evidence.split('\n').map(s => s.trim()).filter(Boolean),
      falsifier: falsifier.trim(),
      sampleSize: Math.max(0, Number.parseInt(sampleSize, 10) || 0),
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['trading-proposals'] })
      toast('已登记提案', 'success')
      setTitle(''); setTarget(''); setEvidence(''); setFalsifier(''); setSampleSize('0')
      setShowForm(false)
    },
    onError: () => { /* request() 已 toast */ },
  })

  const updateMut = useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) =>
      tradingUpdateProposal(id, { status, note: '' }),
    onSuccess: (_d, vars) => {
      qc.invalidateQueries({ queryKey: ['trading-proposals'] })
      toast(`已更新为「${STATUS_META[vars.status]?.label ?? vars.status}」`, 'success')
    },
    onError: () => { /* request() 已 toast(含非法迁移 400) */ },
  })

  const proposals = listQuery.data?.proposals ?? []
  const formSample = Math.max(0, Number.parseInt(sampleSize, 10) || 0)
  const canSubmit = !!title.trim() && !!falsifier.trim() && !createMut.isPending

  return (
    <div className="max-w-4xl space-y-6">
      {/* ===== 策略提案 ===== */}
      <section className="overflow-hidden rounded-card border border-border bg-surface">
        <div className="flex items-center justify-between gap-2 border-b border-border px-5 py-3">
          <div className="flex items-center gap-2">
            <Lightbulb className="h-4 w-4 text-accent" />
            <h2 className="text-sm font-medium text-foreground">策略提案</h2>
            <span className="text-[11px] text-muted">
              单笔结果不改内核 · 提案必带反证条件
            </span>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => listQuery.refetch()}
              disabled={listQuery.isFetching}
              className="inline-flex items-center gap-1 rounded-btn border border-border bg-elevated px-2 py-1 text-[11px] text-secondary transition-colors hover:text-foreground disabled:opacity-50"
              title="刷新提案列表"
            >
              <RefreshCw className={cn('h-3 w-3', listQuery.isFetching && 'animate-spin')} />刷新
            </button>
            <button
              onClick={() => setShowForm(v => !v)}
              className="inline-flex items-center gap-1 rounded-btn bg-accent px-2.5 py-1 text-[11px] font-medium text-white transition-colors hover:bg-accent/90"
            >
              <Plus className="h-3 w-3" />新建提案
            </button>
          </div>
        </div>

        {/* 新建表单 */}
        {showForm && (
          <div className="space-y-3 border-b border-border bg-elevated/30 px-5 py-4">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <label className="block">
                <span className="mb-1 block text-[11px] text-secondary">标题 *</span>
                <input
                  value={title}
                  onChange={e => setTitle(e.target.value)}
                  placeholder="如:放宽半导体策略止损到 8%"
                  className={cn(INPUT, 'w-full')}
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-[11px] text-secondary">变更目标</span>
                <input
                  value={target}
                  onChange={e => setTarget(e.target.value)}
                  placeholder="strategy 配置或 gate_rules"
                  className={cn(INPUT, 'w-full font-mono')}
                />
              </label>
            </div>
            <label className="block">
              <span className="mb-1 block text-[11px] text-secondary">证据(每行一条)</span>
              <textarea
                value={evidence}
                onChange={e => setEvidence(e.target.value)}
                rows={3}
                placeholder={'如:\n2026-07 的 5 笔样本中 4 笔被甩在起飞前\n止损放宽后平均回撤扩大 3%'}
                className={cn(INPUT, 'w-full resize-y')}
              />
            </label>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-[1fr_8rem]">
              <label className="block">
                <span className="mb-1 block text-[11px] text-secondary">反证条件 *(必填)</span>
                <input
                  value={falsifier}
                  onChange={e => setFalsifier(e.target.value)}
                  placeholder="如果改错了,我会在什么情况下看到"
                  className={cn(INPUT, 'w-full')}
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-[11px] text-secondary">样本数</span>
                <input
                  type="number" min={0}
                  value={sampleSize}
                  onChange={e => setSampleSize(e.target.value)}
                  className={cn(INPUT, 'w-full font-mono')}
                />
              </label>
            </div>
            {formSample < MIN_SAMPLE_FOR_APPROVAL && (
              <p className="text-[11px] text-warning">证据不足 10 笔,仅登记不可批准</p>
            )}
            <div className="flex items-center justify-end gap-2">
              <button
                onClick={() => setShowForm(false)}
                className="rounded-btn border border-border bg-elevated px-3 py-1.5 text-xs text-secondary transition-colors hover:text-foreground"
              >
                取消
              </button>
              <button
                onClick={() => createMut.mutate()}
                disabled={!canSubmit}
                className="rounded-btn bg-accent px-3.5 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent/90 disabled:opacity-50"
              >
                {createMut.isPending ? '提交中…' : '登记提案'}
              </button>
            </div>
          </div>
        )}

        {/* 提案列表 */}
        {listQuery.isLoading && !listQuery.data ? (
          <div className="grid h-32 place-items-center">
            <RefreshCw className="h-4 w-4 animate-spin text-muted" />
          </div>
        ) : proposals.length === 0 ? (
          <div className="px-5 py-10 text-center">
            <Lightbulb className="mx-auto h-8 w-8 text-muted" strokeWidth={1.5} />
            <p className="mt-3 text-sm text-secondary">暂无策略提案</p>
            <p className="mt-1 text-[11px] text-muted">想改策略?先登记一份带反证条件的提案,攒够样本再批准</p>
          </div>
        ) : (
          <div className="divide-y divide-border">
            {proposals.map(p => {
              const meta = STATUS_META[p.status] ?? { label: p.status, badge: 'bg-muted/10 text-muted' }
              const actions = TRANSITIONS[p.status] ?? []
              const expanded = expandedId === p.id
              return (
                <div key={p.id}>
                  {/* 行:点击展开详情 */}
                  <div
                    onClick={() => setExpandedId(expanded ? null : p.id)}
                    className="flex cursor-pointer items-center gap-3 px-5 py-3 transition-colors hover:bg-elevated/40"
                  >
                    {expanded
                      ? <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted" />
                      : <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted" />}
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="truncate text-sm text-foreground">{p.title || '(无标题)'}</span>
                        <span className={cn('shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium', meta.badge)}>
                          {meta.label}
                        </span>
                        {p.relaxationAfterLoss && (
                          <span className="inline-flex shrink-0 items-center gap-0.5 rounded bg-warning/10 px-1.5 py-0.5 text-[10px] font-medium text-warning" title="放宽规则且近 30 天有亏损平仓,审批需格外谨慎(对照 12 模式#12)">
                            <AlertTriangle className="h-2.5 w-2.5" />疑似亏损后放宽规则
                          </span>
                        )}
                      </div>
                      <div className="mt-0.5 truncate text-[11px] text-secondary" title={p.falsifier}>
                        反证:{p.falsifier}
                      </div>
                      <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-muted">
                        {p.target && <span className="font-mono">{p.target}</span>}
                        <span className="font-mono tabular-nums">样本 {p.sampleSize}</span>
                        {p.status === 'draft' && p.sampleSize < MIN_SAMPLE_FOR_APPROVAL && (
                          <span className="text-warning">证据不足 10 笔,仅登记</span>
                        )}
                        <span className="font-mono tabular-nums">{p.updatedAt}</span>
                      </div>
                    </div>
                    {/* 状态操作:仅渲染合法迁移 */}
                    {actions.length > 0 && (
                      <div className="flex shrink-0 items-center gap-1.5" onClick={e => e.stopPropagation()}>
                        {actions.map(a => {
                          const blocked = !!a.needsEvidence && p.sampleSize < MIN_SAMPLE_FOR_APPROVAL
                          return (
                            <button
                              key={a.to}
                              onClick={() => updateMut.mutate({ id: p.id, status: a.to })}
                              disabled={updateMut.isPending || blocked}
                              title={blocked ? `样本不足 ${MIN_SAMPLE_FOR_APPROVAL} 笔,不可批准` : undefined}
                              className={cn(
                                'rounded-btn px-2.5 py-1 text-[11px] font-medium transition-colors disabled:opacity-40',
                                a.primary
                                  ? 'bg-accent text-white hover:bg-accent/90'
                                  : 'border border-border bg-elevated text-secondary hover:text-foreground',
                              )}
                            >
                              {a.label}
                            </button>
                          )
                        })}
                      </div>
                    )}
                  </div>

                  {/* 详情:before/after + falsifier + 迁移历史 */}
                  {expanded && (
                    <div className="space-y-3 border-t border-border/60 bg-elevated/20 px-5 py-4">
                      {p.relaxationAfterLoss && (
                        <div className="flex items-start gap-2 rounded-btn border border-warning/30 bg-warning/5 px-3 py-2 text-[11px] leading-relaxed text-warning">
                          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                          <span>疑似亏损后放宽规则:该提案属放宽类变更,且近 30 天存在亏损平仓。审批前请确认这不是为挽救既有亏损而放松纪律(12 模式#12)。</span>
                        </div>
                      )}
                      <div>
                        <div className="mb-1 text-[11px] font-medium text-secondary">反证条件</div>
                        <p className="text-xs leading-relaxed text-foreground">{p.falsifier}</p>
                      </div>
                      {(p.evidence?.length ?? 0) > 0 && (
                        <div>
                          <div className="mb-1 text-[11px] font-medium text-secondary">证据</div>
                          <ul className="list-disc space-y-0.5 pl-4 text-xs text-secondary">
                            {p.evidence.map((ev, i) => <li key={i}>{String(ev)}</li>)}
                          </ul>
                        </div>
                      )}
                      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                        <div>
                          <div className="mb-1 text-[11px] font-medium text-secondary">变更前 before</div>
                          <pre className="max-h-40 overflow-auto rounded-btn border border-border bg-base p-2.5 font-mono text-[10px] leading-relaxed text-secondary">{jsonBlock(p.before)}</pre>
                        </div>
                        <div>
                          <div className="mb-1 text-[11px] font-medium text-secondary">变更后 after</div>
                          <pre className="max-h-40 overflow-auto rounded-btn border border-border bg-base p-2.5 font-mono text-[10px] leading-relaxed text-secondary">{jsonBlock(p.after)}</pre>
                        </div>
                      </div>
                      <div>
                        <div className="mb-1 text-[11px] font-medium text-secondary">迁移记录</div>
                        {(p.history?.length ?? 0) === 0 ? (
                          <p className="text-[11px] text-muted">尚无状态迁移</p>
                        ) : (
                          <ul className="space-y-1">
                            {p.history.map((h, i) => (
                              <li key={i} className="flex flex-wrap items-center gap-1.5 text-[11px]">
                                <span className="font-mono tabular-nums text-muted">{h.ts}</span>
                                <span className="text-secondary">
                                  {STATUS_META[h.from]?.label ?? h.from} → {STATUS_META[h.to]?.label ?? h.to}
                                </span>
                                {h.note && <span className="text-muted">· {h.note}</span>}
                              </li>
                            ))}
                          </ul>
                        )}
                      </div>
                      <div className="font-mono text-[10px] text-muted">
                        {p.id} · 创建于 {p.createdAt}
                      </div>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </section>

      {/* ===== 策略体检(风险声明)===== */}
      <StrategyCheckSection />
    </div>
  )
}

// ================================================================
// 策略体检 —— 风险声明的读写 + 机械体检
// ================================================================

function StrategyCheckSection() {
  const [strategyId, setStrategyId] = useState('')
  const [loadedId, setLoadedId] = useState<string | null>(null)
  const [draft, setDraft] = useState<ProfileDraft | null>(null)
  const [problems, setProblems] = useState<string[]>([])
  const [checks, setChecks] = useState<StrategyProfileCheck[] | null>(null)
  const [busy, setBusy] = useState<'load' | 'save' | 'validate' | 'ai' | null>(null)
  const [aiReport, setAiReport] = useState<string | undefined>(undefined)
  const [aiError, setAiError] = useState('')
  // P3: strategy_profile_deep_review 入口 profile 选择 + 执行元信息(optional,旧响应兼容)
  const [profileId, setProfileId] = useState<string>()
  const [aiMeta, setAiMeta] = useState<AiExecutionMeta | null>(null)
  const aiProfiles = useQuery({ queryKey: ['aiProfiles'], queryFn: api.aiProfiles, retry: false })

  const load = async () => {
    const id = strategyId.trim()
    if (!id || busy) return
    setBusy('load'); setProblems([]); setChecks(null); setAiReport(undefined); setAiError(''); setAiMeta(null)
    try {
      const res = await strategyGetProfile(id)
      setDraft(toDraft(res?.profile))
      setLoadedId(id)
    } catch {
      // 404 = 尚未声明 → 空表单新建(request() 已 toast 后端原文)
      setDraft(emptyDraft())
      setLoadedId(id)
    } finally {
      setBusy(null)
    }
  }

  const save = async () => {
    if (!loadedId || !draft || busy) return
    setBusy('save'); setProblems([])
    try {
      const payload: Record<string, unknown> = {
        invalidation: draft.invalidation.map(it => ({
          name: it.name.trim(), observable: it.observable.trim(), action: it.action.trim(),
        })),
        risk: {
          positionLimitPct: Number(draft.risk.positionLimitPct),
          lossBudgetPct: Number(draft.risk.lossBudgetPct),
          thesisHorizonMonths: Math.trunc(Number(draft.risk.thesisHorizonMonths)),
        },
        cadence: { review: draft.cadence.review },
      }
      // family:仅声明(非空)时发送,保持向后兼容
      const family = (draft.family ?? '').trim()
      if (family) {
        payload.family = family
        if (family === 'mixed') {
          const m = draft.familyMix ?? emptyMix()
          payload.familyMix = {
            entryJudge: m.entryJudge.trim(),
            invalidationAuthority: m.invalidationAuthority.trim(),
            sizingHorizon: m.sizingHorizon.trim(),
            conflictResolution: m.conflictResolution.trim(),
          }
        }
      }
      // playbook:仅当至少一个文本非空时发送(且只发送非空键)
      const pb = draft.playbook ?? emptyPlaybook()
      const playbook: Record<string, string> = {}
      if (pb.scope?.trim()) playbook.scope = pb.scope.trim()
      if (pb.entry?.trim()) playbook.entry = pb.entry.trim()
      if (pb.exit?.trim()) playbook.exit = pb.exit.trim()
      if (Object.keys(playbook).length) payload.playbook = playbook
      await strategyPutProfile(loadedId, payload)
      toast('已保存策略风险声明', 'success')
    } catch (e) {
      // 422: request() 把 detail 对象 JSON.stringify 进 Error.message → 解析回问题清单
      const msg = e instanceof Error ? e.message : ''
      try {
        const parsed = JSON.parse(msg)
        if (Array.isArray(parsed?.problems)) {
          setProblems(parsed.problems.map(String))
        }
      } catch { /* 非 422,request() 已 toast */ }
    } finally {
      setBusy(null)
    }
  }

  const validate = async () => {
    const id = loadedId ?? strategyId.trim()
    if (!id || busy) return
    setBusy('validate'); setAiReport(undefined); setAiError(''); setAiMeta(null)
    try {
      const res = await strategyValidateProfile(id)
      setChecks(res.checks)
    } catch { /* request() 已 toast */ } finally {
      setBusy(null)
    }
  }

  const validateAi = async () => {
    const id = loadedId ?? strategyId.trim()
    if (!id || busy) return
    setBusy('ai'); setAiError(''); setAiMeta(null)
    try {
      const resolvedProfileId =
        resolveEntryProfile('strategy_profile_deep_review', aiProfiles.data?.profiles ?? [], aiProfiles.data?.default_id ?? '') || profileId
      const res = await strategyValidateProfile(id, true, resolvedProfileId || undefined)
      setChecks(res.checks)
      setAiReport(res.aiReport ?? '')
      setAiMeta(res.ai_meta ?? null)
      if (res.aiError) setAiError(res.aiError)
    } catch { /* request() 已 toast */ } finally {
      setBusy(null)
    }
  }

  const patchInv = (i: number, key: keyof InvalidationItem, val: string) => {
    setDraft(d => d && ({
      ...d,
      invalidation: d.invalidation.map((it, j) => (j === i ? { ...it, [key]: val } : it)),
    }))
  }

  return (
    <section className="overflow-hidden rounded-card border border-border bg-surface">
      <div className="flex items-center gap-2 border-b border-border px-5 py-3">
        <Stethoscope className="h-4 w-4 text-accent" />
        <h2 className="text-sm font-medium text-foreground">策略体检</h2>
        <span className="text-[11px] text-muted">风险声明:失效信号三要素 + 风险预算 + 复盘节奏</span>
      </div>

      <div className="space-y-4 px-5 py-4">
        {/* strategyId 输入 */}
        <div className="flex flex-wrap items-center gap-2">
          <input
            value={strategyId}
            onChange={e => setStrategyId(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') load() }}
            placeholder="输入 strategyId,如 momentum_breakout"
            className={cn(INPUT, 'w-64 font-mono')}
          />
          <button
            onClick={load}
            disabled={!strategyId.trim() || busy !== null}
            className="inline-flex items-center gap-1 rounded-btn border border-border bg-elevated px-2.5 py-1.5 text-[11px] text-secondary transition-colors hover:text-foreground disabled:opacity-50"
          >
            {busy === 'load' ? <RefreshCw className="h-3 w-3 animate-spin" /> : <ShieldCheck className="h-3 w-3" />}
            加载声明
          </button>
          <button
            onClick={validate}
            disabled={!strategyId.trim() || busy !== null}
            className="inline-flex items-center gap-1 rounded-btn border border-border bg-elevated px-2.5 py-1.5 text-[11px] text-secondary transition-colors hover:text-foreground disabled:opacity-50"
          >
            {busy === 'validate' ? <RefreshCw className="h-3 w-3 animate-spin" /> : <Stethoscope className="h-3 w-3" />}
            跑体检
          </button>
          <button
            onClick={validateAi}
            disabled={!strategyId.trim() || busy !== null}
            className="inline-flex items-center gap-1 rounded-btn bg-accent px-2.5 py-1.5 text-[11px] font-medium text-white transition-colors hover:bg-accent/90 disabled:opacity-50"
            title="AI 对照 7 结构不变量 + 可证伪性语义判断(ai=true)"
          >
            {busy === 'ai' ? <RefreshCw className="h-3 w-3 animate-spin" /> : <Sparkles className="h-3 w-3" />}
            AI 深度体检
          </button>
          <AiProviderSelector entry="strategy_profile_deep_review" value={profileId} onChange={setProfileId} compact />
          {loadedId && (
            <span className="font-mono text-[10px] text-muted">当前:{loadedId}</span>
          )}
        </div>

        {/* 声明编辑 */}
        {draft && loadedId && (
          <div className="space-y-4 rounded-btn border border-border bg-elevated/20 p-4">
            {/* 失效信号三要素 */}
            <div>
              <div className="mb-2 text-[11px] font-medium text-secondary">
                失效信号(name 信号名 / observable 可观察条件 / action 触发动作)
              </div>
              <div className="space-y-2">
                {draft.invalidation.map((it, i) => (
                  <div key={i} className="flex items-start gap-2">
                    <div className="grid flex-1 grid-cols-1 gap-2 sm:grid-cols-3">
                      <input
                        value={it.name}
                        onChange={e => patchInv(i, 'name', e.target.value)}
                        placeholder="信号名,如 跌破年线"
                        className={INPUT}
                      />
                      <input
                        value={it.observable}
                        onChange={e => patchInv(i, 'observable', e.target.value)}
                        placeholder="可观察条件,如 收盘连续 3 日低于 MA250"
                        className={INPUT}
                      />
                      <input
                        value={it.action}
                        onChange={e => patchInv(i, 'action', e.target.value)}
                        placeholder="触发动作,如 无条件清仓"
                        className={INPUT}
                      />
                    </div>
                    <button
                      onClick={() => setDraft(d => d && ({ ...d, invalidation: d.invalidation.filter((_, j) => j !== i) }))}
                      disabled={draft.invalidation.length <= 1}
                      className="mt-1 shrink-0 text-muted transition-colors hover:text-danger disabled:opacity-30"
                      title="删除该信号"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                ))}
              </div>
              <button
                onClick={() => setDraft(d => d && ({ ...d, invalidation: [...d.invalidation, { name: '', observable: '', action: '' }] }))}
                className="mt-2 inline-flex items-center gap-1 text-[11px] text-accent transition-colors hover:text-accent/80"
              >
                <Plus className="h-3 w-3" />添加失效信号
              </button>
            </div>

            {/* 风险预算 + 复盘节奏 */}
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <label className="block">
                <span className="mb-1 block text-[11px] text-secondary">仓位上限 %</span>
                <input
                  type="number" min={0} max={100} step="any"
                  value={draft.risk.positionLimitPct || ''}
                  onChange={e => setDraft(d => d && ({ ...d, risk: { ...d.risk, positionLimitPct: Number(e.target.value) || 0 } }))}
                  placeholder="(0,100]"
                  className={cn(INPUT, 'w-full font-mono')}
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-[11px] text-secondary">亏损预算 %</span>
                <input
                  type="number" min={0} max={100} step="any"
                  value={draft.risk.lossBudgetPct || ''}
                  onChange={e => setDraft(d => d && ({ ...d, risk: { ...d.risk, lossBudgetPct: Number(e.target.value) || 0 } }))}
                  placeholder="(0,100]"
                  className={cn(INPUT, 'w-full font-mono')}
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-[11px] text-secondary">论点周期(月)</span>
                <input
                  type="number" min={1}
                  value={draft.risk.thesisHorizonMonths || ''}
                  onChange={e => setDraft(d => d && ({ ...d, risk: { ...d.risk, thesisHorizonMonths: Number(e.target.value) || 0 } }))}
                  placeholder="正整数"
                  className={cn(INPUT, 'w-full font-mono')}
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-[11px] text-secondary">复盘节奏</span>
                <select
                  value={draft.cadence.review}
                  onChange={e => setDraft(d => d && ({ ...d, cadence: { review: e.target.value } }))}
                  className={cn(INPUT, 'w-full')}
                >
                  {REVIEW_CADENCES.map(c => <option key={c.value} value={c.value}>{c.label}</option>)}
                </select>
              </label>
            </div>

            {/* 策略坐标卡 family (P6.3, 可选 · 仅声明时发送) */}
            <div className="space-y-2">
              <div className="text-[11px] font-medium text-secondary">策略坐标卡 family(可选)</div>
              <label className="block">
                <span className="mb-1 block text-[11px] text-secondary">策略族</span>
                <select
                  value={draft.family ?? ''}
                  onChange={e => setDraft(d => d && ({ ...d, family: e.target.value }))}
                  className={cn(INPUT, 'w-full')}
                >
                  <option value="">不声明</option>
                  {FAMILY_OPTIONS.map(f => <option key={f.value} value={f.value}>{f.label}</option>)}
                </select>
              </label>
              {(draft.family ?? '') === 'mixed' && (
                <div className="rounded-btn border border-warning/20 bg-warning/5 px-3 py-2">
                  <div className="mb-1.5 text-[10px] text-warning">family=mixed 须显式裁决四要素(均必填)</div>
                  <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                    {([
                      ['entryJudge', '入场裁判'],
                      ['invalidationAuthority', '失效权归属'],
                      ['sizingHorizon', '仓位与期限'],
                      ['conflictResolution', '冲突裁决'],
                    ] as const).map(([key, label]) => (
                      <label key={key} className="block">
                        <span className="mb-0.5 block text-[10px] text-secondary">{label}</span>
                        <input
                          value={draft.familyMix?.[key] ?? ''}
                          onChange={e => setDraft(d => d && ({
                            ...d,
                            familyMix: { ...(d.familyMix ?? emptyMix()), [key]: e.target.value },
                          }))}
                          placeholder={label}
                          className={cn(INPUT, 'w-full')}
                        />
                      </label>
                    ))}
                  </div>
                </div>
              )}
            </div>

            {/* 策略剧本 playbook (P6.3, 三文本均可缺省 · 留空不发送) */}
            <div className="space-y-2">
              <div className="text-[11px] font-medium text-secondary">策略剧本 playbook(可选)</div>
              <div className="grid grid-cols-1 gap-2">
                {([
                  ['scope', '适用范围'],
                  ['entry', '入场依据'],
                  ['exit', '退出规则'],
                ] as const).map(([key, label]) => (
                  <label key={key} className="block">
                    <span className="mb-0.5 block text-[10px] text-secondary">{label}</span>
                    <textarea
                      value={draft.playbook?.[key] ?? ''}
                      onChange={e => setDraft(d => d && ({
                        ...d,
                        playbook: { ...(d.playbook ?? emptyPlaybook()), [key]: e.target.value },
                      }))}
                      rows={2}
                      placeholder={label}
                      className={cn(INPUT, 'w-full resize-y')}
                    />
                  </label>
                ))}
              </div>
            </div>

            {/* 422 问题清单 */}
            {problems.length > 0 && (
              <div className="rounded-btn border border-danger/30 bg-danger/5 px-3 py-2.5">
                <div className="mb-1 text-[11px] font-medium text-danger">声明未通过结构校验:</div>
                <ul className="list-disc space-y-0.5 pl-4 text-[11px] leading-relaxed text-danger/90">
                  {problems.map((pr, i) => <li key={i}>{pr}</li>)}
                </ul>
              </div>
            )}

            <div className="flex justify-end">
              <button
                onClick={save}
                disabled={busy !== null}
                className="inline-flex items-center gap-1.5 rounded-btn bg-accent px-3.5 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent/90 disabled:opacity-50"
              >
                {busy === 'save' ? <RefreshCw className="h-3 w-3 animate-spin" /> : <Save className="h-3 w-3" />}
                保存声明
              </button>
            </div>
          </div>
        )}

        {/* 体检结果 */}
        {checks && (
          <div className="rounded-btn border border-border">
            <div className="border-b border-border px-3.5 py-2 text-[11px] font-medium text-secondary">
              体检结果
            </div>
            {checks.length === 0 ? (
              <p className="px-3.5 py-3 text-[11px] text-muted">无检查项返回</p>
            ) : (
              <ul className="divide-y divide-border">
                {checks.map(c => {
                  const meta = CHECK_META[c.status] ?? { label: c.status, badge: 'bg-muted/10 text-muted' }
                  return (
                    <li key={c.id} className="flex items-start gap-2.5 px-3.5 py-2.5">
                      <span className={cn('mt-0.5 shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium', meta.badge)}>
                        {meta.label}
                      </span>
                      <div className="min-w-0">
                        <div className="text-xs font-medium text-foreground">{c.name}</div>
                        <div className="mt-0.5 text-[11px] leading-relaxed text-muted">{c.detail}</div>
                      </div>
                    </li>
                  )
                })}
              </ul>
            )}
          </div>
        )}

        {/* AI 深度体检报告(ai=true 时返回;空串=AI 未配置/失败 → 降级提示) */}
        {aiReport !== undefined && (
          <div className="rounded-btn border border-border">
            <div className="flex items-center gap-1.5 border-b border-border px-3.5 py-2 text-[11px] font-medium text-foreground">
              <Sparkles className="h-3 w-3 text-accent" />
              AI 深度体检
              <AiExecutionMetaBadge meta={aiMeta} className="ml-auto font-normal" />
            </div>
            {aiReport ? (
              <div className="prose prose-invert max-w-none px-3.5 py-3">
                <MarkdownRenderer content={aiReport} />
              </div>
            ) : (
              <div className="px-3.5 py-3 text-[11px] leading-relaxed text-warning">
                {aiError || 'AI 未返回报告,请前往「设置 → AI」检查 AI 配置后重试。'}
              </div>
            )}
          </div>
        )}
      </div>
    </section>
  )
}
