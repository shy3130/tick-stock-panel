import { Fragment, useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import { useQueryClient } from '@tanstack/react-query'
import { DndContext, PointerSensor, closestCenter, useSensor, useSensors } from '@dnd-kit/core'
import { SortableContext, arrayMove, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { ArrowDown, ArrowLeft, ArrowUp, BookOpen, Check, ChevronDown, Code2, Copy, Download, GripVertical, List, Loader2, MoreHorizontal, Play, Plus, SlidersHorizontal, Trash2, Upload, X } from 'lucide-react'
import { downloadBlob } from '@/lib/resourceTransfer'
import { api, ApiError } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useMediaQuery } from '@/lib/useMediaQuery'
import { legacyVolumeCompareSettings, loadChartIndicatorLibrary, migrateVolumeCompare } from '@/lib/chart-indicator-library'
import { volumeRatioLabels, volumeRatioReadout } from '@/lib/chart-indicators'
import {
  copyChartIndicator, hasVolumeOutput, indicatorBars, indicatorPlotSeries, indicatorPaneDrawings, newChartIndicator,
  MAX_ACTIVE_INDICATORS, MAX_INDICATOR_DEFINITIONS,
  type ChartIndicatorDefinition, type ChartIndicatorLibrary, type IndicatorBar, type IndicatorEvaluation,
  type IndicatorLibraryResponse, type IndicatorOutput, type IndicatorParameter, type IndicatorView, type VolumeCompareSettings,
} from '@/lib/custom-chart-indicators'
import type { PlotSeries } from '@/lib/lightweight-model'
import { Modal } from '@/components/Modal'
import { FinancialPlot } from './FinancialPlot'

const INPUT = 'h-9 w-full min-w-0 rounded-btn border border-border bg-base px-2 text-[16px] leading-5 text-foreground outline-none selection:bg-accent/20 focus:border-accent focus-visible:ring-2 focus-visible:ring-accent/20 sm:h-7 sm:text-xs'
const BUTTON = 'inline-flex min-h-9 items-center justify-center gap-1.5 rounded-btn border border-border px-2.5 text-xs text-secondary transition-colors hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 disabled:cursor-not-allowed disabled:opacity-40 sm:min-h-7'
const ICON_BUTTON = 'inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-btn text-secondary transition-colors hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 disabled:cursor-not-allowed disabled:opacity-40 sm:h-7 sm:w-7'
const ORDER_BUTTON = 'inline-flex h-9 w-8 items-center justify-center rounded text-secondary transition-colors hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 disabled:cursor-not-allowed disabled:opacity-30 sm:h-6 sm:w-6'
const SOURCE_EXAMPLE = `{ 双均线与上穿标记 }
短均线:MA(CLOSE,5),COLORRED;
长均线:MA(CLOSE,20),COLORBLUE;
DRAWICON(CROSS(短均线,长均线),LOW,1);`
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value))
interface Props {
  initial: IndicatorLibraryResponse; initialId?: string; symbol: string; view: IndicatorView
  sourceRows: readonly object[]; onClose: () => void
}
interface Preview { scope: string; evaluation: IndicatorEvaluation; bars: IndicatorBar[]; sequence: number }
type EditorSection = 'formula' | 'parameters' | 'display'

function tabKeys(event: KeyboardEvent<HTMLElement>) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  const tabs = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]')].filter(tab => !tab.disabled)
  const current = tabs.indexOf(event.target as HTMLButtonElement)
  if (current < 0) return
  event.preventDefault()
  const index = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (current + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length
  tabs[index].click(); tabs[index].focus()
}

function SourceEditor({ value, onChange, inputRef }: { value: string; onChange: (value: string) => void; inputRef: RefObject<HTMLTextAreaElement> }) {
  const gutter = useRef<HTMLPreElement>(null)
  const lines = value.split('\n').length
  return <div className="flex min-h-80 flex-1 flex-col overflow-hidden rounded-card border border-border bg-base focus-within:border-accent/70 focus-within:ring-2 focus-within:ring-accent/15">
    <div className="flex items-center justify-between gap-2 border-b border-border/70 bg-elevated/40 px-3 py-1.5 text-xs text-secondary">
      <span className="inline-flex items-center gap-2"><Code2 className="h-3.5 w-3.5" aria-hidden="true" />公式源码</span><span>本地计算</span>
    </div>
    <div className="relative flex min-h-0 flex-1">
      <div aria-hidden="true" className="w-9 shrink-0 overflow-hidden border-r border-border/40 bg-surface/40 text-right">
        <pre ref={gutter} className="py-3 pr-2 font-mono text-[11px] leading-6 text-muted sm:leading-[22px]">{Array.from({ length: lines }, (_, i) => i + 1).join('\n')}</pre>
      </div>
      <textarea ref={inputRef} aria-label="公式源码" spellCheck={false} autoCapitalize="off" autoCorrect="off" wrap="off" maxLength={40000}
        value={value} onChange={event => onChange(event.target.value)} placeholder={SOURCE_EXAMPLE}
        onScroll={event => { if (gutter.current) gutter.current.style.transform = `translateY(-${event.currentTarget.scrollTop}px)` }}
        className="min-h-64 min-w-0 flex-1 resize-none overflow-auto bg-transparent p-3 font-mono text-[14px] leading-6 text-foreground caret-accent outline-none placeholder:text-muted selection:bg-accent/20 sm:text-[13px] sm:leading-[22px]" />
    </div>
    <div className="flex justify-between gap-2 border-t border-border/50 px-3 py-2 text-[11px] text-secondary"><span>{lines} 行</span><span className="tabular-nums">{value.length.toLocaleString()} / 40,000 字符</span></div>
  </div>
}

function IndicatorRow({ definition, index, count, label, selected, busy, onSelect, onListed, onMove }: {
  definition: ChartIndicatorDefinition; index: number; count: number; label: string; selected: boolean; busy: boolean
  onSelect: () => void; onListed: (listed: boolean) => void; onMove: (offset: -1 | 1) => void
}) {
  const listed = index >= 0
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, transform, transition, isDragging } = useSortable({ id: definition.id, disabled: !listed || busy })
  return <li ref={setNodeRef} style={{ transform: CSS.Transform.toString(transform), transition }}
    className={`group relative flex items-center rounded-btn px-1 ${isDragging ? 'z-10 bg-elevated shadow-lg' : selected ? 'bg-accent/10' : 'hover:bg-elevated/60'}`}>
    {listed ? <button type="button" ref={setActivatorNodeRef} {...attributes} {...listeners} disabled={busy}
      aria-label={`拖动${definition.name}排序`} title="拖动排序"
      className="inline-flex h-9 w-6 shrink-0 touch-none cursor-grab items-center justify-center rounded text-secondary hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 active:cursor-grabbing disabled:cursor-not-allowed disabled:opacity-30 sm:h-7">
      <GripVertical className="h-3.5 w-3.5" aria-hidden="true" />
    </button> : <span className="w-6 shrink-0" aria-hidden="true" />}
    <label className="flex min-h-11 w-8 shrink-0 cursor-pointer items-center justify-center sm:min-h-8 sm:w-7"><input type="checkbox" aria-label={`在${label}指标栏显示${definition.name}`}
      checked={listed} onChange={event => onListed(event.target.checked)} className="h-3.5 w-3.5 accent-accent" /></label>
    <button type="button" onClick={onSelect} aria-pressed={selected} className="min-h-11 min-w-0 flex-1 rounded py-1.5 pr-1.5 text-left focus-visible:ring-2 focus-visible:ring-accent/60 sm:min-h-8 sm:py-1">
      <span className="block truncate text-xs text-foreground">{definition.name}</span><span className="text-[11px] text-secondary sm:text-[10px]">{definition.pane === 'main' ? '主图叠加' : '独立副图'}</span>
    </button>
    {listed && <div className={`flex shrink-0 transition-opacity ${selected ? '' : 'sm:opacity-0 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100'}`}>
      <button type="button" aria-label={`上移${definition.name}`} title={`上移${definition.name}`} disabled={index === 0} onClick={() => onMove(-1)} className={ORDER_BUTTON}><ArrowUp className="h-3.5 w-3.5" aria-hidden="true" /></button>
      <button type="button" aria-label={`下移${definition.name}`} title={`下移${definition.name}`} disabled={index === count - 1} onClick={() => onMove(1)} className={ORDER_BUTTON}><ArrowDown className="h-3.5 w-3.5" aria-hidden="true" /></button>
    </div>}
  </li>
}

function PreviewChart({ preview, view, volumeCompare }: { preview: Preview; view: IndicatorView; volumeCompare?: VolumeCompareSettings }) {
  const series = useMemo<PlotSeries[]>(() => {
    const result = indicatorPlotSeries(preview.evaluation, view, 0).map(s => ({ ...s, pane: 0 }))
    if (preview.evaluation.results[0]?.pane === 'main') {
      if (view === 'daily') result.unshift({ id: 'preview-price', type: 'Candlestick', pane: 0,
        data: preview.bars.map(r => ({ time: r.time, open: r.open!, high: r.high!, low: r.low!, close: r.close })) })
      else result.unshift(...indicatorPlotSeries({ ...preview.evaluation, results: [{
        id: 'preview-price', name: '价格', pane: 'main', format: 'price', precision: 2, reset_daily: false,
        outputs: [{ key: 'PRICE', label: '价格', style: 'line', color: '#9ca3af', values: preview.bars.map(r => r.close) }],
      }] }, view, 0))
    }
    return result
  }, [preview, view])
  const annotations = useMemo(() => {
    const indicator = preview.evaluation.results[0]
    const base = { dates: preview.evaluation.times, drawings: indicatorPaneDrawings(preview.evaluation, view, 0)[0] ?? [] }
    if (view !== 'daily' || !volumeCompare || !indicator || !hasVolumeOutput(indicator)) return base
    const rows = preview.bars.map(row => ({ date: row.time, open: row.open!, high: row.high!, low: row.low!, close: row.close, volume: row.volume ?? undefined }))
    return { ...base, labels: volumeRatioLabels(rows, preview.evaluation.times, indicator, volumeCompare),
      readouts: volumeRatioReadout(rows, rows.length - 1, volumeCompare) }
  }, [preview, view, volumeCompare])
  return <FinancialPlot scope={`indicator-preview:${preview.sequence}`} series={series} height={220} paneHeights={[1]}
    annotations={annotations} visibleBars="all" intraday={view === 'minute'} />
}

export function IndicatorManager({ initial, initialId, symbol, view, sourceRows, onClose }: Props) {
  const queryClient = useQueryClient()
  const wide = useMediaQuery('(min-width: 1024px)')
  const sidebar = useMediaQuery('(min-width: 640px)')
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 5 } }))
  const titleId = useId()
  const [saved, setSaved] = useState(initial)
  const [draft, setDraft] = useState(() => clone(initial.library))
  const [selectedId, setSelectedId] = useState<string | undefined>(initialId ?? initial.library.views[view][0]?.id ?? initial.library.definitions[0]?.id)
  const [libraryOpen, setLibraryOpen] = useState(false)
  const [editorSection, setEditorSection] = useState<EditorSection>('formula')
  const [expandedOutputs, setExpandedOutputs] = useState<Record<string, number[]>>({})
  const [mobilePreview, setMobilePreview] = useState(false)
  const [helpOpen, setHelpOpen] = useState(false)
  const [deleteConfirm, setDeleteConfirm] = useState(false)
  const [parsedScope, setParsedScope] = useState('')
  const [error, setError] = useState('')
  const [conflict, setConflict] = useState(false)
  const [saving, setSaving] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [previewPending, setPreviewPending] = useState(false)
  const [preview, setPreview] = useState<Preview | null>(null)
  const previewController = useRef<AbortController>()
  const previewSequence = useRef(0)
  const sourceRef = useRef<HTMLTextAreaElement>(null)
  const nameRef = useRef<HTMLInputElement>(null)
  const emptyRef = useRef<HTMLButtonElement>(null)
  const definition = draft.definitions.find(d => d.id === selectedId)
  const openOutputs = definition ? expandedOutputs[definition.id] ?? [0] : []
  const selection = draft.views[view].find(s => s.id === selectedId)
  const orderedDefinitions = useMemo(() => {
    const ids = new Set(draft.views[view].map(s => s.id))
    return [...draft.views[view].flatMap(s => draft.definitions.filter(d => d.id === s.id)),
      ...draft.definitions.filter(d => !ids.has(d.id))]
  }, [draft, view])
  const presets = saved.presets ?? []
  const availableBuiltIns = presets.filter(preset => !draft.definitions.some(d => d.id === preset.id))
  const canAddBuiltIn = draft.definitions.length < MAX_INDICATOR_DEFINITIONS && draft.views[view].length < MAX_ACTIVE_INDICATORS
  const volumeCompare = view === 'daily' && definition && hasVolumeOutput(definition)
    ? selection?.volume_compare ?? legacyVolumeCompareSettings() : undefined
  const resolved = useMemo(() => definition ? { ...definition, parameters: definition.parameters.map(p => ({ ...p, value: selection?.parameters[p.name] ?? p.value })) } : undefined, [definition, selection])
  const previewScope = `${symbol}:${view}:${JSON.stringify(resolved)}`
  const dirty = JSON.stringify(draft) !== JSON.stringify(saved.library)
  const label = view === 'daily' ? '日K' : '分时'
  const showLibrary = sidebar || libraryOpen
  const showPreview = !wide && mobilePreview
  useEffect(() => {
    previewController.current?.abort()
    setPreviewPending(false)
    return () => previewController.current?.abort()
  }, [previewScope])

  const change = (edit: (library: ChartIndicatorLibrary) => void) => {
    setDraft(previous => { const next = clone(previous); edit(next); return next })
    setError('')
  }
  const updateDefinition = (patch: Partial<ChartIndicatorDefinition>) => change(library => {
    const target = library.definitions.find(d => d.id === selectedId)
    if (target) Object.assign(target, patch)
  })
  const setListed = (id: string, listed: boolean) => {
    if (listed && draft.views[view].length >= MAX_ACTIVE_INDICATORS) { setError(`指标栏最多显示${MAX_ACTIVE_INDICATORS}个指标，请先取消勾选一个`); return }
    const visible = presets.some(preset => preset.id === id)
    change(library => {
      const target = library.definitions.find(d => d.id === id)!
      library.views[view] = listed
        ? [...library.views[view].filter(s => s.id !== id), { id, parameters: Object.fromEntries(target.parameters.map(p => [p.name, p.value])), visible,
          ...(view === 'daily' && hasVolumeOutput(target) ? { volume_compare: legacyVolumeCompareSettings() } : {}) }]
        : library.views[view].filter(s => s.id !== id)
    })
  }
  const moveIndicator = (id: string, targetId?: string) => change(library => {
    const selections = library.views[view]
    const index = selections.findIndex(s => s.id === id)
    const target = selections.findIndex(s => s.id === targetId)
    if (index >= 0 && target >= 0 && index !== target) library.views[view] = arrayMove(selections, index, target)
  })
  const selectDefinition = (id?: string) => {
    setSelectedId(id); setEditorSection('formula'); setMobilePreview(false); setLibraryOpen(false); setDeleteConfirm(false); setHelpOpen(false)
    setError(''); setConflict(false)
  }
  const addDefinition = (created: ChartIndicatorDefinition, { comparison, visible = false }: {
    comparison?: VolumeCompareSettings; visible?: boolean
  } = {}) => {
    change(library => {
      if (library.definitions.some(definition => definition.id === created.id)) return
      library.definitions.push(created)
      if (library.views[view].length < MAX_ACTIVE_INDICATORS) library.views[view].push({ id: created.id, parameters: Object.fromEntries(created.parameters.map(p => [p.name, p.value])), visible,
        ...(view === 'daily' && hasVolumeOutput(created) ? { volume_compare: clone(comparison ?? legacyVolumeCompareSettings()) } : {}) })
    })
    selectDefinition(created.id)
    requestAnimationFrame(() => nameRef.current?.focus())
  }
  const updateParameter = (index: number, patch: Partial<IndicatorParameter>) => change(library => {
    const target = library.definitions.find(d => d.id === selectedId)!
    const old = target.parameters[index]
    if (patch.name != null && old.name !== patch.name) for (const choices of Object.values(library.views)) {
      const chosen = choices.find(s => s.id === target.id)
      if (chosen && old.name in chosen.parameters) {
        chosen.parameters[patch.name] = chosen.parameters[old.name]
        delete chosen.parameters[old.name]
      }
    }
    Object.assign(old, patch)
  })
  const updateOutput = (index: number, patch: Partial<IndicatorOutput>) => {
    if (definition) updateDefinition({ outputs: definition.outputs.map((out, i) => i === index ? { ...out, ...patch } : out) })
  }
  const toggleOutput = (index: number) => {
    if (!definition) return
    setExpandedOutputs(previous => {
      const open = previous[definition.id] ?? [0]
      return { ...previous, [definition.id]: open.includes(index) ? open.filter(i => i !== index) : [...open, index] }
    })
  }
  const addOutput = () => {
    if (!definition || definition.outputs.length >= 6) return
    let n = 1; while (definition.outputs.some(o => o.key === `LINE${n}`)) n++
    updateDefinition({ outputs: [...definition.outputs, { key: `LINE${n}`, label: `曲线${n}`, formula: 'C', style: 'line', color: '#a855f7' }] })
    setExpandedOutputs(previous => ({ ...previous, [definition.id]: [...new Set([...(previous[definition.id] ?? [0]), definition.outputs.length])] }))
  }
  const removeOutput = (index: number) => {
    if (!definition || definition.outputs.length <= 1) return
    updateDefinition({ outputs: definition.outputs.filter((_, i) => i !== index) })
    setExpandedOutputs(previous => ({ ...previous, [definition.id]: (previous[definition.id] ?? [0])
      .filter(i => i !== index).map(i => i > index ? i - 1 : i) }))
  }
  const setParameterValue = (name: string, value: number) => change(library => {
    const chosen = library.views[view].find(s => s.id === selectedId)
    if (chosen) chosen.parameters[name] = value
  })
  const close = () => {
    if (!saving && !exporting && (!dirty || window.confirm('放弃尚未保存的指标修改？'))) onClose()
  }
  const exportDefinitions = async (all: boolean) => {
    if (dirty || exporting) return
    setExporting(true); setError('')
    try {
      const ids = all ? saved.library.definitions.map(d => d.id) : selectedId ? [selectedId] : []
      downloadBlob(await api.exportChartIndicators(ids, saved.revision), 'chart-indicators.json')
    } catch (cause) { setError(cause instanceof Error ? cause.message : '导出失败，请重试') }
    finally { setExporting(false) }
  }
  const save = async () => {
    if (!dirty || saving || exporting) return
    setSaving(true); setError(''); setConflict(false)
    try {
      const result = await api.saveChartIndicatorLibrary(migrateVolumeCompare(draft), saved.revision)
      queryClient.setQueryData(QK.chartIndicatorLibrary, result)
      setSaved(result)
      onClose()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '保存失败，请重试')
      setConflict(cause instanceof ApiError && cause.status === 409)
    } finally { setSaving(false) }
  }
  const reload = async () => {
    if (dirty && !window.confirm('重新加载会放弃当前草稿，是否继续？')) return
    try {
      const latest = await loadChartIndicatorLibrary()
      queryClient.setQueryData(QK.chartIndicatorLibrary, latest)
      setSaved(latest); setDraft(clone(latest.library)); selectDefinition(latest.library.definitions[0]?.id)
      setError(''); setConflict(false)
    } catch (cause) { setError(cause instanceof Error ? cause.message : '读取失败，请重试') }
  }
  const runPreview = async () => {
    if (!resolved || saving || exporting || previewPending) return
    const bars = indicatorBars(sourceRows, view)
    previewController.current?.abort()
    const controller = new AbortController()
    previewController.current = controller
    setPreviewPending(true); setError('')
    try {
      await api.compileChartIndicator(resolved, controller.signal)
      if (controller.signal.aborted) return
      if (!bars.length) { setParsedScope(previewScope); setHelpOpen(false); setMobilePreview(true); return }
      setParsedScope('')
      const evaluation = await api.evaluateChartIndicators({ symbol, timeframe: view, definitions: [resolved], rows: bars }, controller.signal)
      if (controller.signal.aborted) return
      if (evaluation.errors.length) { setError(evaluation.errors.map(e => e.message).join('；')); return }
      setPreview({ scope: previewScope, bars, evaluation, sequence: ++previewSequence.current })
      setHelpOpen(false); setMobilePreview(true)
    } catch (cause) {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : '预览失败，请重试')
    } finally { if (previewController.current === controller) setPreviewPending(false) }
  }
  const shownPreview = preview?.scope === previewScope ? preview : null

  const selectSection = (section: EditorSection) => { setEditorSection(section); setMobilePreview(false) }
  const removeDefinition = () => {
    const nextId = draft.definitions.find(d => d.id !== selectedId)?.id
    change(library => {
      library.definitions = library.definitions.filter(d => d.id !== selectedId)
      for (const key of ['daily', 'minute'] as const) library.views[key] = library.views[key].filter(s => s.id !== selectedId)
    })
    setExpandedOutputs(previous => {
      const next = { ...previous }
      if (selectedId) delete next[selectedId]
      return next
    })
    selectDefinition(nextId)
  }
  const focusFormula = () => {
    selectSection('formula')
    requestAnimationFrame(() => {
      const node = sourceRef.current
      if (!node) return
      const line = Number(error.match(/第\s*(\d+)\s*行/)?.[1] ?? 1)
      const start = (definition?.source ?? '').split('\n').slice(0, line - 1).reduce((length, text) => length + text.length + 1, 0)
      const lineHeight = Number.parseFloat(getComputedStyle(node).lineHeight) || 22
      node.focus(); node.setSelectionRange(start, start); node.scrollTop = Math.max(0, (line - 3) * lineHeight)
    })
  }
  const activeTab = showPreview ? 'preview' : editorSection
  const busy = saving || exporting
  const previewStatus = previewPending ? '正在解析…' : shownPreview || parsedScope === previewScope ? '已更新' : preview ? '待重新预览' : '待预览'

  return createPortal(<Modal onClose={close} labelledBy={titleId} closeOnBackdrop={false} initialFocusRef={definition ? nameRef : emptyRef}
    panelClassName="flex h-[94dvh] max-h-[1000px] w-[96vw] max-w-[1480px] flex-col overflow-hidden rounded-dialog border border-border bg-surface shadow-xl">
    <div onKeyDown={event => {
      event.stopPropagation()
      if (!(event.ctrlKey || event.metaKey) || event.nativeEvent.isComposing) return
      if (event.key === 'Enter') { event.preventDefault(); void runPreview() }
      if (event.key.toLowerCase() === 's') { event.preventDefault(); void save() }
    }} className="flex min-h-0 flex-1 flex-col">
      <header className="relative z-20 flex shrink-0 items-center justify-between gap-2 border-b border-border px-3 py-2 sm:px-4">
        <div className="min-w-0">
          <h2 id={titleId} className="text-[14px] font-semibold leading-5 text-foreground">自定义指标</h2>
          <p className="mt-0.5 flex items-center gap-2 text-xs text-secondary"><span>{label}</span><span className="text-muted">/</span><span className="font-mono">{symbol}</span></p>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {dirty && <span className="mr-2 inline-flex items-center gap-1.5 text-xs text-warning"><span className="h-1.5 w-1.5 rounded-full bg-current" />未保存</span>}
          <details className="relative"><summary aria-label="更多指标操作" title="导入与导出" className={`${ICON_BUTTON} list-none cursor-pointer`}><MoreHorizontal className="h-4 w-4" aria-hidden="true" /></summary>
            <div className="absolute right-0 top-full z-30 mt-2 flex w-56 flex-col gap-1 rounded-card border border-border bg-surface p-2 shadow-lg">
              <a href="/settings?tab=import" className={BUTTON} onClick={event => { if (busy || (dirty && !window.confirm('放弃当前草稿并前往导入？'))) event.preventDefault() }}><Upload className="h-3.5 w-3.5" />导入指标</a>
              <button type="button" className={BUTTON} disabled={busy || dirty || !definition} onClick={() => void exportDefinitions(false)}><Download className="h-3.5 w-3.5" />导出当前指标</button>
              <button type="button" className={BUTTON} disabled={busy || dirty || !draft.definitions.length} onClick={() => void exportDefinitions(true)}>导出全部指标</button>
              {dirty && <span className="px-2 py-1 text-xs leading-5 text-secondary">保存修改后可导出</span>}
              {exporting && <span role="status" className="p-2 text-xs text-secondary">正在导出…</span>}
            </div>
          </details>
          <button type="button" aria-label="关闭指标编辑器" onClick={close} disabled={busy} className={ICON_BUTTON}><X className="h-4 w-4" aria-hidden="true" /></button>
        </div>
      </header>
      <div className="flex min-h-0 flex-1">
        <aside aria-label="指标库" hidden={!showLibrary} className={`${showLibrary ? 'flex' : 'hidden'} min-h-0 w-full shrink-0 flex-col border-border bg-base/30 sm:w-56 sm:border-r`}>
          <fieldset disabled={busy} aria-label="指标列表" className="m-0 flex min-h-0 flex-1 flex-col border-0 p-0">
            <div className="shrink-0 border-b border-border/60 px-3 py-2">
              <div className="flex items-center justify-between gap-2">
                <h3 className="text-xs font-medium text-foreground">指标列表 <span className="ml-1 text-xs font-normal tabular-nums text-secondary">{draft.definitions.length}/{MAX_INDICATOR_DEFINITIONS}</span></h3>
                <button type="button" aria-label="新建指标" title="新建空白指标" disabled={draft.definitions.length >= MAX_INDICATOR_DEFINITIONS}
                  onClick={() => addDefinition(newChartIndicator())} className={ICON_BUTTON}><Plus className="h-3.5 w-3.5" aria-hidden="true" /></button>
              </div>
              <p className="mt-2 flex items-center justify-between text-[11px] text-secondary"><span>勾选加入{label}指标栏</span><span className="tabular-nums">{draft.views[view].length}/{MAX_ACTIVE_INDICATORS}</span></p>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto p-2">
              <DndContext sensors={sensors} collisionDetection={closestCenter}
                accessibility={{ screenReaderInstructions: { draggable: '可使用鼠标或触屏拖动排序，也可使用右侧的上移、下移按钮。' }, announcements: {
                  onDragStart: () => '开始调整指标顺序。',
                  onDragOver: ({ over }) => over ? `移至第${draft.views[view].findIndex(s => s.id === over.id) + 1}位。` : undefined,
                  onDragEnd: ({ over }) => over ? '已调整指标顺序，保存方案后生效。' : '已取消排序。',
                  onDragCancel: () => '已取消排序。',
                } }}
                onDragEnd={({ active, over }) => { if (!busy && over && active.id !== over.id) moveIndicator(String(active.id), String(over.id)) }}>
                <SortableContext items={draft.views[view].map(s => s.id)} strategy={verticalListSortingStrategy}>
                  <ul className="space-y-1">{orderedDefinitions.map((d, position) => {
                    const index = draft.views[view].findIndex(s => s.id === d.id)
                    return <Fragment key={d.id}>
                      {position === draft.views[view].length && <li className="px-3 pb-1 pt-4 text-[11px] text-secondary">未加入当前图表</li>}
                      <IndicatorRow definition={d} index={index} count={draft.views[view].length} label={label} selected={d.id === selectedId} busy={busy}
                        onSelect={() => selectDefinition(d.id)} onListed={listed => setListed(d.id, listed)} onMove={offset => moveIndicator(d.id, draft.views[view][index + offset]?.id)} />
                    </Fragment>
                  })}</ul>
                </SortableContext>
              </DndContext>
              {availableBuiltIns.length > 0 && <section aria-labelledby={`${titleId}-built-ins`} className={orderedDefinitions.length ? 'mt-3 border-t border-border/60 pt-2' : ''}>
                <h4 id={`${titleId}-built-ins`} className="px-3 py-1 text-[11px] text-secondary">内置指标</h4>
                <ul className="space-y-1">
                  {availableBuiltIns.map(preset => <li key={preset.id}>
                    <button type="button" disabled={!canAddBuiltIn} onClick={() => addDefinition(clone(preset), { visible: true })}
                      aria-label={`添加${preset.name}到${label}指标栏`}
                      className="flex min-h-11 w-full items-center gap-2 rounded-btn px-3 py-1.5 text-left focus-visible:ring-2 focus-visible:ring-accent/60 hover:bg-elevated/60 disabled:cursor-not-allowed disabled:opacity-45 sm:min-h-8 sm:py-1">
                      <Plus className="h-3.5 w-3.5 shrink-0 text-secondary" aria-hidden="true" />
                      <span className="text-xs text-foreground">{preset.name}</span>
                      <span className="ml-auto shrink-0 text-[11px] text-secondary sm:text-[10px]">{preset.pane === 'main' ? '主图叠加' : '独立副图'}</span>
                    </button>
                  </li>)}
                </ul>
              </section>}
            </div>
            <div className="shrink-0 border-t border-border/60 px-3 py-2">
              <p className="text-[11px] leading-5 text-secondary">拖动手柄排序，保存后同步到图表。</p>
              {!sidebar && <button type="button" onClick={() => setLibraryOpen(false)} className={`${BUTTON} mt-3 w-full`}><ArrowLeft className="h-3.5 w-3.5" />返回编辑</button>}
            </div>
          </fieldset>
        </aside>
        <main hidden={!sidebar && libraryOpen} className={`${!sidebar && libraryOpen ? 'hidden' : 'flex'} min-h-0 min-w-0 flex-1 flex-col`}>
          {!sidebar && <div className="flex shrink-0 items-center justify-between border-b border-border/60 px-3 py-2">
            <button type="button" aria-label="打开指标列表" aria-expanded={libraryOpen} onClick={() => setLibraryOpen(true)} className={BUTTON}><List className="h-3.5 w-3.5" />指标列表<span className="text-xs tabular-nums">{draft.definitions.length}</span></button>
            <button type="button" aria-label="新建指标" title="新建空白指标" onClick={() => addDefinition(newChartIndicator())} disabled={busy || draft.definitions.length >= MAX_INDICATOR_DEFINITIONS} className={ICON_BUTTON}><Plus className="h-3.5 w-3.5" /></button>
          </div>}
          {!definition ? <div className="flex flex-1 flex-col items-start justify-center gap-3 overflow-y-auto px-6 py-10 sm:px-12">
            <Code2 className="h-8 w-8 text-accent" aria-hidden="true" />
            <h3 className="text-lg font-semibold text-foreground">把你的公式放到图上</h3>
            <p className="max-w-md text-xs leading-7 text-secondary">从指标列表添加常用指标，也可以新建空白指标并编写或粘贴公式。解析后可预览当前股票的曲线与读数。</p>
            <button ref={emptyRef} type="button" className={`${BUTTON} mt-2`} disabled={busy} onClick={() => addDefinition(newChartIndicator())}><Plus className="h-3.5 w-3.5" />创建空白指标</button>
          </div> : <>
            <fieldset disabled={busy} aria-label="当前指标信息" className="m-0 flex shrink-0 items-center gap-2 border-0 border-b border-border/60 px-3 py-2 sm:px-4">
              <div className="min-w-0 flex-1">
                <label className="sr-only" htmlFor={`${titleId}-name`}>指标名称</label>
                <input id={`${titleId}-name`} ref={nameRef} aria-label="指标名称" maxLength={40} value={definition.name} placeholder="给指标命名"
                  onChange={event => updateDefinition({ name: event.target.value })}
                  className="h-9 w-full min-w-0 max-w-md rounded border border-transparent bg-transparent px-2 text-[14px] font-semibold leading-5 sm:h-7 text-foreground outline-none hover:border-border focus:border-accent focus:bg-base focus-visible:ring-2 focus-visible:ring-accent/20" />
                <p className="mt-0.5 px-2 text-[11px] text-secondary">{definition.pane === 'main' ? '主图叠加' : '独立副图'}<span className="mx-2 text-muted">·</span>{definition.syntax === 'tdx' ? '公式源码' : '表达式指标'}</p>
              </div>
              <button type="button" aria-label="复制指标" title="复制指标" disabled={draft.definitions.length >= MAX_INDICATOR_DEFINITIONS} onClick={() => addDefinition(copyChartIndicator(resolved ?? definition), { comparison: volumeCompare })} className={ICON_BUTTON}><Copy className="h-3.5 w-3.5" /></button>
              <button type="button" aria-label="删除指标" title="删除指标" onClick={() => setDeleteConfirm(true)} className={`${ICON_BUTTON} hover:text-danger`}><Trash2 className="h-3.5 w-3.5" /></button>
            </fieldset>
            {deleteConfirm && <div role="alert" className="flex shrink-0 flex-wrap items-center gap-2 border-b border-danger/25 bg-danger/5 px-5 py-2 text-xs">
              <p className="min-w-0 flex-1 leading-6 text-foreground">删除「{definition.name}」？保存后将从日K和分时中移除。</p>
              <button type="button" disabled={busy} onClick={() => setDeleteConfirm(false)} className={BUTTON}>取消删除</button><button type="button" disabled={busy} onClick={removeDefinition} className={`${BUTTON} text-danger`}>确认删除</button>
            </div>}
            <div className="flex shrink-0 items-stretch border-b border-border/60 px-3 sm:px-4" role="tablist" aria-label="指标编辑内容" onKeyDown={tabKeys}>
              {([['formula', '公式'], ['parameters', '参数'], ['display', '显示'], ...(!wide ? [['preview', '预览']] : [])] as [EditorSection | 'preview', string][]).map(([key, text]) =>
                <button key={key} type="button" id={`${titleId}-tab-${key}`} role="tab" aria-selected={activeTab === key} tabIndex={activeTab === key ? 0 : -1}
                  aria-controls={`${titleId}-${key === 'preview' ? 'preview' : 'editor'}-panel`} disabled={busy}
                  onClick={() => key === 'preview' ? setMobilePreview(true) : selectSection(key)}
                  className={`relative min-h-9 flex-1 px-2.5 text-xs sm:min-h-8 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/50 sm:flex-none ${activeTab === key ? 'font-medium text-foreground after:absolute after:inset-x-3 after:bottom-0 after:h-0.5 after:bg-accent' : 'text-secondary hover:text-foreground'}`}>{text}</button>)}
            </div>
            <div className={`grid min-h-0 min-w-0 flex-1 ${wide ? 'grid-cols-[minmax(0,1.35fr)_minmax(280px,1fr)]' : 'grid-cols-1'}`}>
              <section aria-label="指标编辑区" id={`${titleId}-editor-panel`} role="tabpanel" aria-labelledby={`${titleId}-tab-${editorSection}`} hidden={showPreview}
                className={`${showPreview ? 'hidden' : 'flex'} min-h-0 min-w-0 flex-col`}>
                <fieldset key={`${selectedId}:${editorSection}`} disabled={busy} aria-label="指标定义" className="m-0 flex min-h-0 flex-1 flex-col overflow-y-auto border-0 p-3 sm:p-4">
                  {editorSection === 'formula' && (definition.syntax === 'tdx' ? <div className="flex min-h-full flex-1 flex-col gap-2">
                    <SourceEditor key={definition.id} value={definition.source ?? ''} onChange={source => updateDefinition({ source })} inputRef={sourceRef} />
                    <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-secondary"><span>修改后需重新解析预览</span><button type="button" onClick={() => { setHelpOpen(true); setMobilePreview(true) }} className="inline-flex min-h-8 sm:min-h-6 items-center gap-1.5 text-secondary hover:text-foreground focus-visible:ring-2 focus-visible:ring-accent/50"><BookOpen className="h-3.5 w-3.5" />语法与示例</button></div>
                  </div> : <section aria-label="输出公式" className="space-y-4">
                    <div className="flex flex-wrap items-center justify-between gap-2"><div><h3 className="text-xs font-medium text-foreground">输出公式</h3><p className="mt-1 text-xs leading-5 text-secondary">按顺序计算，后面的公式可引用前面的变量。</p></div>
                      <button type="button" className={`${BUTTON} text-xs`} disabled={definition.outputs.length >= 6} onClick={addOutput}><Plus className="h-3.5 w-3.5" />添加输出</button>
                    </div>
                    {definition.outputs.map((out, index) => <section key={index} aria-label={`输出${index + 1}设置`} className="overflow-hidden rounded-btn border border-border bg-surface">
                      <button type="button" aria-label={`编辑输出${index + 1}`} aria-expanded={openOutputs.includes(index)} aria-controls={`${titleId}-output-${index}`} onClick={() => toggleOutput(index)}
                        className="flex min-h-9 w-full min-w-0 items-center gap-2 bg-elevated/70 px-3 py-2 text-left hover:bg-elevated focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/50 sm:min-h-8">
                        <span className="shrink-0 text-[10px] text-secondary">输出 {index + 1}</span><span className="h-2 w-2 shrink-0 rounded-full" style={{ backgroundColor: out.color }} aria-hidden="true" /><span className="shrink-0 font-mono text-xs text-foreground">{out.key || `输出${index + 1}`}</span><span className="min-w-0 flex-1 truncate font-mono text-xs text-secondary">{out.formula}</span><ChevronDown className={`h-3.5 w-3.5 shrink-0 text-secondary transition-transform ${openOutputs.includes(index) ? 'rotate-180' : ''}`} aria-hidden="true" />
                      </button>
                      {openOutputs.includes(index) && <div id={`${titleId}-output-${index}`} className="space-y-3 border-t border-border p-3">
                      <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_36px] items-end gap-2">
                        <label className="min-w-0 text-xs text-secondary">变量<input aria-label={`输出${index + 1}变量`} value={out.key} maxLength={24} className={`${INPUT} mt-1.5 font-mono`} onChange={e => updateOutput(index, { key: e.target.value })} /></label>
                        <label className="min-w-0 text-xs text-secondary">名称<input aria-label={`输出${index + 1}名称`} value={out.label} maxLength={24} className={`${INPUT} mt-1.5`} onChange={e => updateOutput(index, { label: e.target.value })} /></label>
                        <button type="button" aria-label={`删除输出${index + 1}`} title="删除输出" disabled={definition.outputs.length === 1} className={`${ICON_BUTTON} hover:text-danger`} onClick={() => removeOutput(index)}><Trash2 className="h-3.5 w-3.5" /></button>
                      </div>
                      <label className="block text-xs text-secondary">公式<textarea aria-label={`输出${index + 1}公式`} value={out.formula} spellCheck={false} maxLength={2000} rows={2}
                        onChange={e => updateOutput(index, { formula: e.target.value })} className="mt-1.5 min-h-16 w-full resize-y rounded-btn border border-border bg-base px-3 py-1.5 font-mono text-[13px] leading-[22px] text-foreground caret-accent outline-none focus:border-accent focus-visible:ring-2 focus-visible:ring-accent/20" /></label>
                      <div className="flex flex-wrap items-center gap-2 text-xs text-secondary">
                        <label className="flex items-center gap-2">颜色<input aria-label={`输出${index + 1}颜色`} type="color" value={out.color} onChange={e => updateOutput(index, { color: e.target.value })} className="h-8 w-8 cursor-pointer rounded border border-border bg-base p-1" /></label>
                        <select aria-label={`输出${index + 1}样式`} value={out.style} disabled={definition.pane === 'main'} className="h-8 min-w-0 rounded border border-border bg-base px-2 text-xs text-foreground disabled:opacity-50"
                          onChange={e => updateOutput(index, { style: e.target.value as IndicatorOutput['style'] })}><option value="line">曲线</option><option value="histogram">柱状图</option><option value="volume">成交量柱</option></select>
                        <label className="inline-flex min-h-8 sm:min-h-6 items-center gap-2"><input type="checkbox" aria-label={`输出${index + 1}隐藏`} checked={out.hidden ?? false} className="accent-accent" onChange={e => updateOutput(index, { hidden: e.target.checked })} />仅作中间变量</label>
                        {out.style !== 'line' && <details className="w-full"><summary className="cursor-pointer py-1">柱状图配色</summary><div className="mt-2 flex flex-wrap items-center gap-2">
                          <label>配色<select aria-label={`输出${index + 1}配色`} className={`${INPUT} mt-1`} value={out.color_mode ?? 'sign'} onChange={e => updateOutput(index, { color_mode: e.target.value as IndicatorOutput['color_mode'] })}><option value="sign">正负值</option><option value="candle">K线涨跌</option><option value="fixed">固定颜色</option></select></label>
                          <label>下跌颜色<input type="color" aria-label={`输出${index + 1}下跌颜色`} value={out.down_color ?? '#2D9B65'} onChange={e => updateOutput(index, { down_color: e.target.value })} className="mt-1 block h-9 w-9 rounded border border-border bg-base p-1" /></label>
                        </div></details>}
                      </div>
                      </div>}
                    </section>)}
                  </section>)}
                  {editorSection === 'parameters' && <section aria-label="指标参数" className="max-w-md space-y-4">
                    <div className="flex flex-wrap items-center justify-between gap-2"><div><h3 className="text-xs font-medium text-foreground">参数取值</h3><p className="mt-1 text-xs leading-5 text-secondary">本图取值只影响当前{label}，默认值用于新加入的图表。</p></div>
                      <button type="button" className={`${BUTTON} text-xs`} disabled={definition.parameters.length >= 8} onClick={() => {
                        let n = 1; while (definition.parameters.some(p => p.name === `P${n}`)) n++
                        updateDefinition({ parameters: [...definition.parameters, { name: `P${n}`, value: 14, min: 2, max: 512, integer: true }] })
                      }}><Plus className="h-3.5 w-3.5" />添加参数</button>
                    </div>
                    {!definition.parameters.length && <p className="border-t border-border/60 py-6 text-xs leading-7 text-secondary">当前没有外部参数。源码可直接写周期数字，也可添加同名参数，例如 N。</p>}
                    {definition.parameters.map((p, index) => <div key={index} className="space-y-3 border-b border-border/60 pb-5 last:border-0">
                      <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_36px] items-end gap-2">
                        <label className="text-xs text-secondary">参数名称<input aria-label={`参数${index + 1}变量`} value={p.name} maxLength={24} className={`${INPUT} mt-1.5 font-mono`} onChange={e => updateParameter(index, { name: e.target.value })} /></label>
                        <label className="text-xs text-secondary">本图取值<input aria-label={`参数${p.name}本图取值`} type="number" step={p.integer ? 1 : 'any'} min={p.min} max={p.max} value={selection?.parameters[p.name] ?? p.value}
                          disabled={!selection} className={`${INPUT} mt-1.5 font-mono disabled:opacity-40`} onChange={e => setParameterValue(p.name, Number(e.target.value))} /></label>
                        <button type="button" className={`${ICON_BUTTON} hover:text-danger`} aria-label={`删除参数${p.name}`} title="删除参数" onClick={() => change(library => {
                          library.definitions.find(d => d.id === selectedId)!.parameters.splice(index, 1)
                          for (const choices of Object.values(library.views)) { const choice = choices.find(s => s.id === selectedId); if (choice) delete choice.parameters[p.name] }
                        })}><Trash2 className="h-3.5 w-3.5" /></button>
                      </div>
                      <details className="text-xs text-secondary"><summary className="cursor-pointer py-1">默认 {p.value} · 范围 {p.min}～{p.max}{p.integer ? ' · 整数' : ''}</summary>
                        <div className="mt-3 grid grid-cols-3 gap-2">
                          <label>默认值<input aria-label={`参数${p.name}默认值`} type="number" step="any" value={p.value} className={`${INPUT} mt-1.5 font-mono`} onChange={e => updateParameter(index, { value: Number(e.target.value) })} /></label>
                          <label>最小值<input aria-label={`参数${p.name}最小值`} type="number" step="any" value={p.min} className={`${INPUT} mt-1.5 font-mono`} onChange={e => updateParameter(index, { min: Number(e.target.value) })} /></label>
                          <label>最大值<input aria-label={`参数${p.name}最大值`} type="number" step="any" value={p.max} className={`${INPUT} mt-1.5 font-mono`} onChange={e => updateParameter(index, { max: Number(e.target.value) })} /></label>
                        </div><label className="mt-3 inline-flex min-h-9 items-center gap-2"><input type="checkbox" className="h-3.5 w-3.5 accent-accent" checked={p.integer} onChange={e => updateParameter(index, { integer: e.target.checked })} />只允许整数</label>
                      </details>
                    </div>)}
                    {!selection && <p className="text-xs leading-6 text-secondary">此指标尚未加入{label}指标栏，可先修改默认值，或在左侧列表勾选后设置本图取值。</p>}
                  </section>}
                  {editorSection === 'display' && <section aria-label="指标显示设置" className="max-w-md space-y-4">
                    <div><h3 className="text-xs font-medium text-foreground">图表显示</h3><p className="mt-1 text-xs leading-6 text-secondary">显示位置、单位和颜色由日K与分时共享。</p></div>
                    <div className="grid grid-cols-2 gap-3">
                      <label className="col-span-2 text-xs text-secondary">显示位置<select aria-label="显示位置" value={definition.pane} className={`${INPUT} mt-1.5`} onChange={e => {
                        const pane = e.target.value as ChartIndicatorDefinition['pane']
                        updateDefinition({ pane, ...(pane === 'main' ? { format: 'price', outputs: definition.outputs.map(o => ({ ...o, style: 'line' })) } : {}) })
                      }}><option value="main">主图叠加</option><option value="sub">独立副图</option></select><span className="mt-2 block leading-5">{definition.pane === 'main' ? '与价格曲线共享主图，不占用额外副图。' : '在价格图下方单独显示，可通过指标列表调整上下顺序。'}</span></label>
                      <label className="text-xs text-secondary">数值单位<select aria-label="数值单位" value={definition.format} disabled={definition.pane === 'main'} className={`${INPUT} mt-1.5 disabled:opacity-50`} onChange={e => updateDefinition({ format: e.target.value as ChartIndicatorDefinition['format'] })}><option value="price">价格</option><option value="number">数值</option><option value="percent">百分数（3表示3%）</option><option value="volume">成交量</option></select></label>
                      <label className="text-xs text-secondary">小数位<input aria-label="小数位" type="number" min={0} max={6} value={definition.precision} className={`${INPUT} mt-1.5`} onChange={e => updateDefinition({ precision: Number(e.target.value) })} /></label>
                    </div>
                    {view === 'minute' && <label className="flex cursor-pointer items-start gap-2 border-t border-border/60 pt-5 text-xs text-foreground"><input type="checkbox" className="mt-1 h-3.5 w-3.5 accent-accent" checked={definition.reset_daily} onChange={e => updateDefinition({ reset_daily: e.target.checked })} /><span>每天重新计算<span className="mt-1 block text-xs leading-6 text-secondary">关闭时跨交易日连续计算；午休不重置周期。</span></span></label>}
                    {volumeCompare && <section aria-label="成交量量比设置" className="space-y-4 border-t border-border/60 pt-5">
                      <div><h4 className="text-xs font-medium text-foreground">量比 · 本图设置</h4><p className="mt-1 text-xs leading-6 text-secondary">当日成交量 ÷ 前 N 个交易日平均成交量，不含当日。不额外绘制曲线。</p></div>
                      <fieldset disabled={!selection} className="space-y-3 disabled:opacity-50">
                        <label className="block max-w-48 text-xs text-secondary">对比周期（交易日）<input type="number" aria-label="量比对比周期" min={1} max={20} step={1} value={volumeCompare.days} className={`${INPUT} mt-1.5`} onChange={event => {
                          const days = Number(event.target.value); change(library => { library.views[view].find(s => s.id === selectedId)!.volume_compare = { ...volumeCompare, days } })
                        }} /></label>
                        {(['show_labels', 'show_readout'] as const).map(key => <label key={key} className="flex min-h-9 items-center gap-2 text-xs text-secondary"><input type="checkbox" aria-label={key === 'show_labels' ? '显示量比倍数标注' : '显示量比读数'} checked={volumeCompare[key]} className="h-3.5 w-3.5 accent-accent" onChange={event => {
                          const checked = event.target.checked; change(library => { library.views[view].find(s => s.id === selectedId)!.volume_compare = { ...volumeCompare, [key]: checked } })
                        }} />{key === 'show_labels' ? '柱顶倍数标注' : '悬停量比读数'}</label>)}
                      </fieldset>
                      {!selection && <p className="text-xs text-secondary">先勾选加入日K指标栏，再设置本图量比。</p>}
                    </section>}
                  </section>}
                </fieldset>
              </section>
              <aside aria-label="指标预览与帮助" id={`${titleId}-preview-panel`} hidden={!wide && !mobilePreview} className={`${wide || mobilePreview ? 'flex' : 'hidden'} min-h-0 min-w-0 flex-col border-border/60 bg-base/20 ${wide ? 'border-l' : ''}`}>
                <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border/60 px-3 py-1.5">
                  <div className="inline-flex items-center gap-1" role="group" aria-label="预览内容">
                    <button type="button" aria-pressed={!helpOpen} onClick={() => setHelpOpen(false)} className={`min-h-8 sm:min-h-6 rounded px-2.5 text-xs focus-visible:ring-2 focus-visible:ring-accent/50 ${!helpOpen ? 'bg-elevated font-medium text-foreground' : 'text-secondary hover:text-foreground'}`}>图表预览</button>
                    <button type="button" aria-pressed={helpOpen} onClick={() => setHelpOpen(true)} className={`min-h-8 sm:min-h-6 rounded px-2.5 text-xs focus-visible:ring-2 focus-visible:ring-accent/50 ${helpOpen ? 'bg-elevated font-medium text-foreground' : 'text-secondary hover:text-foreground'}`}>语法参考</button>
                  </div>
                  {!helpOpen && <span role="status" className="inline-flex shrink-0 items-center gap-1 text-[11px] text-secondary">{previewPending && <Loader2 className="h-3 w-3 animate-spin" />}{previewStatus}</span>}
                </div>
                <div className="min-h-0 flex-1 overflow-y-auto p-3 sm:p-4">
                  {helpOpen ? <section aria-label="公式帮助" className="space-y-4 text-xs leading-6 text-secondary">
                    <div><h3 className="text-xs font-medium text-foreground">{definition.syntax === 'tdx' ? '源码语法' : '表达式语法'}</h3><p className="mt-2">C / O / H / L 为收盘、开盘、最高、最低价，V 为成交量（手），amount 为成交额（元）。价格沿用当前图表复权口径。</p></div>
                    {definition.syntax === 'tdx' ? <>
                      <div><h4 className="font-medium text-foreground">双均线与上穿标记</h4><p className="mt-1">在「显示」中选择「主图叠加」，将以下公式画在价格图上。</p><pre className="mt-3 overflow-x-auto rounded-btn border border-border bg-base p-3 font-mono text-[11px] leading-6 text-foreground"><code>{SOURCE_EXAMPLE}</code></pre></div>
                      <p>MA(CLOSE,5) 计算最近 5 根 K 线的收盘均价；CROSS 检测短均线上穿长均线，DRAWICON 在最低价位置绘制向上箭头。</p>
                      <div><h4 className="font-medium text-foreground">变量与输出</h4><p className="mt-1">:= 定义中间变量，: 定义输出，; 结束语句。支持中文名称、花括号注释、AND / OR / NOT 和条件比较。周期变量在「参数」中添加，也可直接在源码中赋值。</p></div>
                      <div><h4 className="font-medium text-foreground">计算与绘图</h4><p className="mt-1">常用 MA、EMA、SMA、REF、HHV、LLV、STD、COUNT、CROSS、BARSLAST。绘图支持 STICKLINE、DRAWTEXT、DRAWICON（1、2）、DRAWNUMBER、DRAWBAND、DRAWKLINE 和固定位置文字。颜色、线宽及 NODRAW 由源码控制。</p></div>
                      <p className="border-t border-border/60 pt-4">财务、筹码、跨品种、跨周期数据和未来函数尚不支持，解析时会提示行号。</p>
                    </> : <>
                      <p>支持 MA、EMA、SMA、EMA_FIRST、SMA_FIRST、REF、HHV、LLV、STD、SUM、ABS、MAX、MIN、IF 及四则运算。FIRST 函数从首个有效值立即输出，旧配置保留原有计算口径。</p>
                      <p>勾选「仅作中间变量」可隐藏辅助输出。输出名称可用 {'{N1}'} 引用参数，例如 MA{'{N1}'} 会随本图周期显示为 MA5、MA10。百分数格式用 3 表示 3%。</p>
                      <p>周期通常为 2～512 根；REF 仅向过去引用。EMA/SMA 从预热数据的首个有效值初始化，每条公式最多 200 个词元、12 层嵌套，预热最多 4096 根。</p>
                    </>}
                  </section> : <section aria-label="指标预览" className="space-y-4">
                    <div><h3 className="text-xs font-medium text-foreground">{definition.name || '当前指标'}</h3><p className="mt-1 text-xs text-secondary">{symbol} · {label} · 当前行情快照</p></div>
                    {shownPreview ? <>
                      <div className="overflow-hidden rounded-card border border-border bg-base"><PreviewChart preview={shownPreview} view={view} volumeCompare={volumeCompare} /></div>
                      <div className="flex flex-wrap gap-x-4 gap-y-2 border-y border-border/60 py-2">{shownPreview.evaluation.results[0]?.outputs.filter(output => !output.hidden).map(output => <div key={output.key} className="inline-flex min-w-0 items-baseline gap-2"><span className="truncate text-[11px] text-secondary">{output.label}</span><span className="font-mono text-xs" style={{ color: output.color }}>{output.values.at(-1)?.toFixed(definition.precision) ?? '—'}</span></div>)}</div>
                      {shownPreview.evaluation.warnings.map(message => <p key={message} className="text-xs leading-6 text-secondary">{message}</p>)}
                      <p className="text-[11px] leading-6 text-secondary">预览使用当前草稿，不会自动保存修改。</p>
                    </> : <div className="flex min-h-48 flex-col items-center justify-center gap-3 rounded-card border border-dashed border-border/80 px-5 py-8 text-center">
                      {parsedScope === previewScope ? <Check className="h-6 w-6 text-accent" aria-hidden="true" /> : <SlidersHorizontal className="h-6 w-6 text-muted" aria-hidden="true" />}
                      <p className="max-w-64 text-xs leading-7 text-secondary">{parsedScope === previewScope ? '解析通过。当前没有行情，可保存指标，待行情加载后预览。' : preview ? '公式或参数已修改，请重新解析预览。' : '编辑公式后，点击下方“解析并预览”，在这里核对曲线和末值。'}</p>
                      {parsedScope !== previewScope && <button type="button" disabled={busy || previewPending} onClick={() => void runPreview()} className={BUTTON}>{previewPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}{preview ? '刷新预览' : '生成预览'}</button>}
                    </div>}
                    {definition.parameters.length > 0 && (editorSection !== 'parameters' || showPreview) && <section aria-label="快捷参数" className="space-y-3 border-t border-border/60 pt-4">
                      <div className="flex items-center justify-between gap-2"><h4 className="text-xs font-medium text-foreground">本图参数</h4><button type="button" disabled={busy} onClick={() => selectSection('parameters')} className="min-h-8 sm:min-h-6 text-xs text-secondary underline underline-offset-4 hover:text-foreground focus-visible:ring-2 focus-visible:ring-accent/50">管理参数</button></div>
                      <fieldset disabled={busy || !selection} className="grid grid-cols-2 gap-2 disabled:opacity-50">{definition.parameters.map(p => <label key={p.name} className="min-w-0 text-xs text-secondary"><span className="font-mono">{p.name}</span><input aria-label={`快速调整参数${p.name}`} type="number" min={p.min} max={p.max} step={p.integer ? 1 : 'any'} value={selection?.parameters[p.name] ?? p.value} onChange={event => setParameterValue(p.name, Number(event.target.value))} className={`${INPUT} mt-1.5 font-mono`} /></label>)}</fieldset>
                      <p className="text-[11px] leading-5 text-secondary">{selection ? `只修改当前${label}取值，调整后重新解析预览。` : '加入当前图表后可调整本图取值，默认值在参数页签修改。'}</p>
                    </section>}
                  </section>}
                </div>
              </aside>
            </div>
          </>}
        </main>
      </div>
      <footer className="shrink-0 border-t border-border bg-surface px-3 py-2 sm:px-4">
        {error && <div role="alert" className="mb-3 flex max-h-28 flex-wrap items-start gap-x-4 gap-y-2 overflow-y-auto rounded-btn border border-danger/25 bg-danger/5 px-3 py-1.5 text-xs leading-6 text-danger"><span className="min-w-0 flex-1 break-words">{error}</span>
          {conflict ? <button type="button" disabled={busy} onClick={() => void reload()} className="shrink-0 underline underline-offset-2">重新加载配置</button> : definition?.syntax === 'tdx' && /第\s*\d+\s*行/.test(error) && <button type="button" onClick={focusFormula} className="shrink-0 underline underline-offset-2">定位到公式</button>}
        </div>}
        <div className="flex items-center justify-end gap-2">
          <button type="button" onClick={close} disabled={busy} className={`${BUTTON} mr-auto`}>取消</button>
          <span className="mr-2 hidden text-[11px] text-secondary xl:block">Ctrl / Command + Enter 预览</span>
          <button type="button" aria-label="预览指标" title="解析并预览（Ctrl / Command + Enter）" onClick={() => void runPreview()} disabled={busy || previewPending || !definition} className={BUTTON}>{previewPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}解析并预览</button>
          <button type="button" title="保存方案（Ctrl / Command + S）" onClick={() => void save()} disabled={busy || !dirty} className={`${BUTTON} border-accent bg-accent text-white hover:bg-accent/90 hover:text-white`}>{saving && <Loader2 className="h-3.5 w-3.5 animate-spin" />}保存方案</button>
        </div>
      </footer>
    </div>
  </Modal>, document.body)
}
