import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, CheckCircle2, Play, RefreshCw } from 'lucide-react'
import { api, ApiError } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { type EngineJobPreview, type EngineJobRecord, type EngineJobRequest, type EnginePriceVariant } from '@/lib/engineJobTypes'
import { PriceVariantEditor, type CatalogRun } from './PriceVariantEditor'
import { Button, ErrorNotice, inputClass, LoadingLines } from './ui'
import { decimalLabel, newRequestKey, priceLabel, record } from './viewModel'

export function JobForm({
  initial,
  onCreated,
  onClose,
  onSchedule,
}: {
  initial?: EngineJobRequest
  onCreated: (job: EngineJobRecord) => void
  onClose?: () => void
  onSchedule?: (preview: EngineJobPreview) => void
}) {
  const client = useQueryClient()
  const catalog = useQuery({
    queryKey: QK.engineInputs,
    queryFn: api.engineInputs,
    retry: false,
    staleTime: 0,
  })
  const [chosenRun, setRun] = useState<string | null>(null)
  const [chosenSnapshot, setSnapshot] = useState<string | null>(null)
  const [memory, setMemory] = useState('6144')
  const [minutes, setMinutes] = useState('720')
  const [attempts, setAttempts] = useState('3')
  const [imported, setImported] = useState<EngineJobRequest | undefined>(initial)
  const [fileName, setFileName] = useState(initial ? '从已有任务复制的研究计划' : '')
  const [key, setKey] = useState(newRequestKey)
  const [checked, setChecked] = useState<EngineJobPreview | null>(null)
  const [busy, setBusy] = useState<'preview' | 'submit' | 'import' | 'variant' | null>(null)
  const [editor, setEditor] = useState<string | null>(null)
  const [extraRuns, setExtraRuns] = useState<CatalogRun[]>([])
  const [notice, setNotice] = useState('')
  const [error, setError] = useState<unknown>(null)
  const [fields, setFields] = useState<Record<string, string>>({})
  const generation = useRef(0)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      generation.current += 1
    }
  }, [])
  const resetReview = () => {
    generation.current += 1
    setNotice('')
    setEditor(null)
    setChecked(null)
    setError(null)
    setFields({})
    setKey(newRequestKey())
  }
  const runs = [...(catalog.data?.runs ?? []), ...extraRuns.filter(extra => !catalog.data?.runs.some(item => item.path === extra.path))]
  const run = chosenRun ?? ''
  const selected = runs.find((item) => item.path === run)
  const snapshots = catalog.data?.snapshots.filter((item) => item.snapshot_id === selected?.snapshot_id) ?? []
  const snapshot = chosenSnapshot ?? ''
  const denied = catalog.error instanceof ApiError && [401, 403, 503].includes(catalog.error.status)

  async function saveVariant(body: EnginePriceVariant, nodeId?: string) {
    if (busy) return
    const ticket = generation.current
    setBusy('variant')
    try {
      const result = await api.saveEnginePriceVariant(body)
      if (!mounted.current || ticket !== generation.current) return
      setExtraRuns(previous => [...previous.filter(item => item.path !== result.input.path), result.input])
      if (nodeId) {
        setImported(previous => previous && ({ ...previous, nodes: previous.nodes.map(node =>
          node.node_id === nodeId && node.run ? { ...node, run: result.input.path, snapshot: result.snapshot } : node) }))
      } else {
        setRun(result.input.path)
        setSnapshot(result.snapshot)
      }
      setEditor(null)
      const label = priceLabel(result.input.price_view.mode, result.input.price_view.provider_id)
      setNotice(result.input.lock_id === body.source_lock_id
        ? '当前归档已使用所选口径，可以检查输入。' : '已另存' + label + '回测输入，可以检查输入。')
      void client.invalidateQueries({ queryKey: QK.engineInputs })
    } finally {
      if (mounted.current && ticket === generation.current) setBusy(null)
    }
  }

  function variantControl(source: CatalogRun | undefined, snapshotPath: string, nodeId?: string) {
    if (!source || !snapshotPath || source.semantics_version !== 'strict.daily.v1') return null
    const editingKey = nodeId ? 'node:' + nodeId : 'archive'
    return editor === editingKey
      ? <PriceVariantEditor key={source.lock_id + ':' + snapshotPath} source={source} snapshot={snapshotPath}
          saving={busy === 'variant'} onCancel={() => setEditor(null)} onSave={body => saveVariant(body, nodeId)} />
      : <Button onClick={() => { resetReview(); setEditor(editingKey) }}>更换价格口径</Button>
  }

  async function importPlan(file?: File) {
    if (!file) return
    resetReview()
    const ticket = generation.current
    setBusy('import')
    try {
      if (file.size > 2 * 1024 ** 2) throw new Error('研究计划超过 2 MiB，请缩小计划后重新导入。')
      const value = record(JSON.parse(await file.text()))
      if (
        !Array.isArray(value.nodes) ||
        !value.nodes.length ||
        value.nodes.length > 16 ||
        value.nodes.some((node) => typeof record(node).node_id !== 'string')
      ) {
        throw new Error('文件不是可识别的研究计划，需要包含 1–16 个研究步骤。')
      }
      if (mounted.current && ticket === generation.current) {
        setImported(value as unknown as EngineJobRequest)
        setFileName(file.name)
      }
    } catch (reason) {
      if (mounted.current && ticket === generation.current) {
        setImported(undefined)
        setFileName('')
        setError(reason)
      }
    } finally {
      if (mounted.current && ticket === generation.current) setBusy(null)
    }
  }

  async function check(event: FormEvent) {
    event.preventDefault()
    if (busy || editor) return
    const invalid: Record<string, string> = {}
    if (!imported) {
      if (!run) invalid.run = '请选择回测归档。'
      if (!snapshot) {
        invalid.snapshot = '请选择与回测归档匹配的数据快照。'
      }
      if (
        !Number.isSafeInteger(Number(memory)) ||
        !Number.isSafeInteger(Number(memory) * 1024 ** 2) ||
        Number(memory) < 64
      )
        invalid.memory = '内存上限至少为 64 MiB，填写可表示的整数。'
      if (!Number.isInteger(Number(minutes)) || Number(minutes) < 1 || Number(minutes) > 720)
        invalid.minutes = '时限须为 1–720 分钟。'
      if (!Number.isInteger(Number(attempts)) || Number(attempts) < 1 || Number(attempts) > 10)
        invalid.attempts = '尝试次数须为 1–10。'
    }
    setFields(invalid)
    setError(null)
    setChecked(null)
    if (Object.keys(invalid).length) return
    const body: EngineJobRequest = imported
      ? { ...imported, idempotency_key: key }
      : {
          idempotency_key: key,
          nodes: [{ node_id: 'research', run, snapshot, depends_on: [] }],
          budget: {
            rss_limit_bytes: Number(memory) * 1024 ** 2,
            timeout_seconds: Number(minutes) * 60,
            max_attempts: Number(attempts),
          },
        }
    const ticket = generation.current
    setBusy('preview')
    try {
      const preview = await api.previewEngineJob(body)
      if (mounted.current && ticket === generation.current) setChecked(preview)
    } catch (reason) {
      if (mounted.current && ticket === generation.current) setError(reason)
    } finally {
      if (mounted.current && ticket === generation.current) setBusy(null)
    }
  }

  async function submit() {
    if (!checked || busy) return
    const ticket = generation.current
    setBusy('submit')
    setError(null)
    try {
      const created = await api.submitEngineJob(checked.plan.request, checked.plan_id)
      if (mounted.current && ticket === generation.current) onCreated(created)
    } catch (reason) {
      if (mounted.current && ticket === generation.current) {
        setError(reason)
        if (reason instanceof ApiError && [400, 409, 422].includes(reason.status)) setChecked(null)
      }
    } finally {
      if (mounted.current && ticket === generation.current) setBusy(null)
    }
  }

  return (
    <section className="w-full max-w-3xl space-y-3 p-3 sm:px-4" aria-labelledby="new-research-heading">
      <div className="flex flex-wrap items-start justify-between gap-2 border-b border-border pb-2.5">
        <div>
          <h2 id="new-research-heading" className="text-[16px] font-semibold">
            新建研究任务
          </h2>
          <p className="mt-1 text-sm text-secondary">
            选择已准备好的输入，检查后进入运行队列。
          </p>
        </div>
        {onClose && <Button variant="quiet" compact disabled={busy !== null} onClick={onClose}>
          <ArrowLeft aria-hidden="true" className="size-3.5" /> 返回任务
        </Button>}
      </div>
      {catalog.isPending && !imported && <LoadingLines label="正在查找研究输入" />}
      {catalog.isError && (
        <ErrorNotice error={catalog.error}>
          <Button onClick={() => void catalog.refetch()} disabled={catalog.isFetching}>
            <RefreshCw className="h-4 w-4" aria-hidden="true" />
            重新读取输入
          </Button>
        </ErrorNotice>
      )}
      <form onSubmit={check} className="space-y-3" noValidate>
        <fieldset disabled={busy !== null || denied} className="min-w-0 space-y-3 disabled:opacity-70">
          {!imported && (
            <>
              <div className="space-y-2">
                <label htmlFor="engine-run" className="text-sm font-medium">
                  回测归档
                </label>
                <select
                  id="engine-run"
                  value={run}
                  className={inputClass}
                  aria-invalid={!!fields.run}
                  aria-describedby={fields.run ? 'engine-run-error' : undefined}
                  onChange={(event) => {
                    resetReview()
                    setRun(event.target.value)
                    const item = runs.find((item) => item.path === event.target.value)
                    const matching =
                      catalog.data?.snapshots.filter(
                        (snapshot) => snapshot.snapshot_id === item?.snapshot_id,
                      ) ?? []
                    setSnapshot(matching.length === 1 ? matching[0].path : '')
                  }}
                >
                  <option value="">选择回测归档</option>
                  {runs.map((item) => (
                    <option key={item.path} value={item.path}>
                      {item.path} · {item.start} 至 {item.end} · {priceLabel(item.price_view.mode, item.price_view.provider_id)}
                    </option>
                  ))}
                </select>
                {fields.run && (
                  <p id="engine-run-error" className="text-sm text-red-700 dark:text-danger">
                    {fields.run}
                  </p>
                )}
              </div>
              <div className="space-y-2">
                <label htmlFor="engine-snapshot" className="text-sm font-medium">
                  匹配的数据快照
                </label>
                <select
                  id="engine-snapshot"
                  value={snapshot}
                  className={inputClass}
                  disabled={!run}
                  aria-invalid={!!fields.snapshot}
                  aria-describedby={fields.snapshot ? 'engine-snapshot-error' : 'engine-snapshot-help'}
                  onChange={(event) => {
                    resetReview()
                    setSnapshot(event.target.value)
                  }}
                >
                  <option value="">选择数据快照</option>
                  {snapshots.map((item) => (
                    <option key={item.path} value={item.path}>
                      {item.path} · {item.dataset_count} 个数据集
                    </option>
                  ))}
                </select>
                <p id="engine-snapshot-help" className="text-sm text-secondary">
                  仅列出与归档锁定内容匹配的快照。
                </p>
                {fields.snapshot && (
                  <p id="engine-snapshot-error" className="text-sm text-red-700 dark:text-danger">
                    {fields.snapshot}
                  </p>
                )}
              </div>
              {catalog.data && !runs.length && (
                <p className="rounded-btn border border-border bg-base p-4 text-sm leading-6 text-secondary">
                  尚无可选回测归档。请将 Tivelo 导出的回测与快照放入当前工作区的{' '}
                  <code>research/engine/inputs</code> 目录，或在下方导入包含数据准备步骤的研究计划。
                </p>
              )}
              {selected && (
                <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 border-y border-border py-2.5 text-sm">
                  <dt className="text-secondary">价格口径</dt>
                  <dd>{priceLabel(selected.price_view.mode, selected.price_view.provider_id)}</dd>
                  <dt className="text-secondary">标的数量</dt>
                  <dd className="num">{selected.universe_count}</dd>
                  <dt className="text-secondary">初始资金</dt>
                  <dd className="break-all font-mono">
                    {decimalLabel(selected.initial_capital)} {selected.currency}
                  </dd>
                  <dt className="text-secondary">策略</dt>
                  <dd className="break-all">{selected.strategy_id}</dd>
                </dl>
              )}
              {variantControl(selected, snapshot)}
              <fieldset className="grid min-w-0 gap-3 sm:grid-cols-3">
                <legend className="mb-3 text-sm font-medium">执行预算</legend>
                {[
                  { id: 'memory', label: '内存上限（MiB）', value: memory, set: setMemory, min: 64 },
                  {
                    id: 'minutes',
                    label: '单阶段时限（分钟）',
                    value: minutes,
                    set: setMinutes,
                    min: 1,
                    max: 720,
                  },
                  {
                    id: 'attempts',
                    label: '最多尝试次数',
                    value: attempts,
                    set: setAttempts,
                    min: 1,
                    max: 10,
                  },
                ].map((field) => (
                  <div key={field.id} className="space-y-2">
                    <label htmlFor={`engine-${field.id}`} className="text-sm text-secondary">
                      {field.label}
                    </label>
                    <input
                      id={`engine-${field.id}`}
                      type="number"
                      min={field.min}
                      max={field.max}
                      step="1"
                      value={field.value}
                      onChange={(event) => {
                        resetReview()
                        field.set(event.target.value)
                      }}
                      className={inputClass}
                      aria-invalid={!!fields[field.id]}
                      aria-describedby={fields[field.id] ? `engine-${field.id}-error` : undefined}
                    />
                    {fields[field.id] && (
                      <p id={`engine-${field.id}-error`} className="text-sm text-red-700 dark:text-danger">
                        {fields[field.id]}
                      </p>
                    )}
                  </div>
                ))}
              </fieldset>
            </>
          )}
          <div className="space-y-1.5 border-t border-border pt-2.5">
            <label htmlFor="engine-plan-file" className="text-sm font-medium">
              导入研究计划
            </label>
            <p className="text-sm leading-6 text-secondary">
              可导入含数据准备、参数搜索或滚动验证步骤的计划。导入后创建新任务，原任务保持不变。
            </p>
            <input
              id="engine-plan-file"
              type="file"
              accept=".json,application/json"
              className="block w-full min-w-0 rounded text-sm text-secondary file:mr-2 file:min-h-8 file:rounded-btn file:border file:border-border file:bg-base file:px-2.5 file:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent [@media(pointer:coarse)]:file:min-h-9"
              onChange={(event) => {
                const file = event.target.files?.[0]
                event.target.value = ''
                void importPlan(file)
              }}
            />
            {imported && (
              <div className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
                <p className="break-all">
                  {fileName} · {imported.nodes.length} 个研究步骤
                </p>
                <Button
                  onClick={() => {
                    resetReview()
                    setImported(undefined)
                    setFileName('')
                  }}
                >
                  改用归档选择
                </Button>
              </div>
            )}
            {imported?.nodes.map(node => {
              if (!node.run) return null
              const source = runs.find(item => item.path === node.run)
              if (!source || source.semantics_version !== 'strict.daily.v1') return null
              return <div key={node.node_id} className="space-y-2 border-t border-border py-3">
                <p className="break-all text-sm">{node.node_id} · {priceLabel(source.price_view.mode, source.price_view.provider_id)}</p>
                {variantControl(source, node.snapshot, node.node_id)}
              </div>
            })}
          </div>
        </fieldset>
        {notice && <p role="status" className="text-sm text-secondary">{notice}</p>}
        {catalog.data && (catalog.data.truncated || catalog.data.issues.length > 0) && (
          <details className="text-sm text-secondary">
            <summary className="cursor-pointer py-2 focus-visible:outline-accent">
              输入目录提示{catalog.data.truncated ? ' · 列表已达扫描上限' : ''} · {catalog.data.issues.length}{' '}
              项需要检查
            </summary>
            <ul className="mt-2 space-y-2">
              {catalog.data.issues.map((item, index) => (
                <li key={index} className="break-all">
                  {item.path || '输入目录'}：{item.code}
                </li>
              ))}
            </ul>
          </details>
        )}
        {error != null && <ErrorNotice error={error} />}
        {Object.keys(fields).length > 0 && (
          <p role="alert" className="text-sm text-red-700 dark:text-danger">
            请修正标记的输入，再检查任务。
          </p>
        )}
        <Button type="submit" variant={checked ? 'secondary' : 'primary'} aria-busy={busy === 'preview'}
          disabled={busy !== null || denied || editor !== null}>
          <CheckCircle2 aria-hidden="true" className="h-4 w-4" />
          {busy === 'preview' ? '正在检查输入…' : '检查输入与预算'}
        </Button>
        {checked && (
          <section aria-label="已检查的研究计划" className="space-y-3 border-t border-border pt-3">
            <h3 className="font-medium">输入信息检查通过</h3>
            <p className="text-sm leading-6 text-secondary">
              共 {checked.plan.inputs.length} 个研究步骤。运行前会继续校验实际数据；检查不会执行策略。
            </p>
            <dl className="grid grid-cols-2 gap-2 text-sm">
              <dt className="text-secondary">实际内存上限</dt>
              <dd>
                {checked.plan.effective_budget
                  ? checked.plan.effective_budget.rss_limit_bytes / 1024 ** 2
                  : '未提供'}{' '}
                MiB
              </dd>
              <dt className="text-secondary">单阶段时限</dt>
              <dd>{checked.plan.effective_budget?.timeout_seconds ?? '未提供'} 秒</dd>
              <dt className="text-secondary">最多尝试</dt>
              <dd>{checked.plan.effective_budget?.max_attempts ?? '未提供'} 次</dd>
            </dl>
            <ul className="divide-y divide-border text-sm">
              {checked.plan.request.nodes.map((node) => (
                <li key={node.node_id} className="space-y-1 py-3">
                  <p className="break-all font-medium">
                    {node.node_id} ·{' '}
                    {node.capture ? '采集数据快照' : node.study?.mode === 'walk_forward'
                      ? '滚动验证'
                      : node.study?.mode === 'grid'
                        ? '参数搜索'
                        : '单次回测'}
                  </p>
                  {node.capture ? (
                    <ul className="break-words text-secondary">
                      {node.capture.datasets.map((input) => (
                        <li key={input.request.dataset_id}>
                          {input.request.dataset_id} · {input.kind === 'engine_data_provider' ? input.provider_id : 'expected_provider' in input ? input.expected_provider : input.source_id}
                          {' · '}{input.request.instrument_ids.length} 个标的
                        </li>
                      ))}
                    </ul>
                  ) : node.prepare ? (
                    <>
                      <p className="break-words text-secondary">
                        {node.prepare.run.start} 至 {node.prepare.run.end} ·{' '}
                        {priceLabel(node.prepare.run.price_view.mode, node.prepare.run.price_view.provider_id)} · {node.prepare.run.universe.length}{' '}
                        个标的
                      </p>
                      <p className="break-all text-secondary">策略定义：{node.prepare.definition}</p>
                    </>
                  ) : (
                    <p className="break-all text-secondary">
                      归档：{node.run} · 快照：{node.snapshot}
                    </p>
                  )}
                  {(node.capture ?? node.prepare)?.versions?.map(ref => <p key={ref.dataset_id} className="break-all text-secondary">
                    {ref.dataset_id} · 已发布版本 {ref.version_id}
                  </p>)}
                  {!!node.depends_on?.length && (
                    <p className="break-all text-secondary">依赖步骤：{node.depends_on.join('、')}</p>
                  )}
                </li>
              ))}
            </ul>
            <details className="text-sm">
              <summary className="cursor-pointer py-2 text-secondary">查看完整检查计划</summary>
              <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-all rounded-btn border border-border bg-base p-3 text-xs">
                {JSON.stringify(checked.plan.request, null, 2)}
              </pre>
            </details>
            <p className="text-sm text-secondary">
              输入或预算变化后需要重新检查；重复提交相同请求会返回同一任务。
            </p>
            <div className="flex flex-wrap gap-2"><Button variant="primary" disabled={busy !== null || denied} onClick={() => void submit()}>
              <Play aria-hidden="true" className="h-4 w-4" />
              {busy === 'submit' ? '正在提交…' : '提交研究任务'}
            </Button>
              {onSchedule && <Button disabled={busy !== null || denied} onClick={() => onSchedule(checked)}>设为触发计划</Button>}
            </div>
          </section>
        )}
      </form>
    </section>
  )
}
