import { lazy, Suspense, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useIsMutating, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, SlidersHorizontal } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { CHART_LAYOUT } from '@/lib/chart-layout'
import type { DailyChartProps } from '@/lib/chart-types'
import { loadChartIndicatorLibrary } from '@/lib/chart-indicator-library'
import {
  indicatorBars, indicatorSourceIdentity, resolveChartIndicators,
  type IndicatorEvaluationRequest, type IndicatorLibraryResponse, type IndicatorView,
} from '@/lib/custom-chart-indicators'
import { LightweightDaily } from './LightweightDaily'
import { LightweightIntraday, type LightweightIntradayProps } from './LightweightIntraday'

const IndicatorManager = lazy(() => import('./IndicatorManager').then(m => ({ default: m.IndicatorManager })))
const EMPTY_ROWS: object[] = []

function useIndicators(symbol: string, view: IndicatorView, sourceRows: readonly object[], revision?: string | number) {
  const library = useQuery({ queryKey: QK.chartIndicatorLibrary, queryFn: loadChartIndicatorLibrary, staleTime: 10_000, retry: 1 })
  const definitions = useMemo(() => resolveChartIndicators(library.data?.library, view), [library.data?.library, view])
  const visibleIds = useMemo(() => new Set(library.data?.library.views[view].filter(s => s.visible !== false).map(s => s.id)), [library.data?.library, view])
  const active = definitions.length > 0
  const bars = useMemo(() => active ? indicatorBars(sourceRows, view) : [], [sourceRows, view, active])
  const program = useMemo(() => JSON.stringify(definitions), [definitions])
  const source = `${indicatorSourceIdentity(sourceRows)}:${revision ?? ''}`
  const snapshot = useMemo(() => ({ symbol, view, program, source,
    body: { symbol, timeframe: view, definitions, rows: bars } satisfies IndicatorEvaluationRequest,
  }), [symbol, view, program, source, bars])
  const [ready, setReady] = useState(snapshot)
  useEffect(() => {
    // Do not start discarded work while the user rapidly switches symbols.
    const timer = setTimeout(() => setReady(snapshot), 120)
    return () => clearTimeout(timer)
  }, [snapshot])
  const sameContext = ready.symbol === symbol && ready.view === view && ready.program === program
  const values = useQuery({
    queryKey: QK.chartIndicatorValues(ready.symbol, ready.view, ready.program, ready.source),
    queryFn: ({ signal }) => api.evaluateChartIndicators(ready.body, signal),
    enabled: sameContext && !!symbol && ready.body.rows.length > 0 && ready.body.definitions.length > 0 && visibleIds.size > 0,
    staleTime: Infinity, gcTime: 15_000, retry: false, refetchOnWindowFocus: false,
    placeholderData: (previous, query) => query?.queryKey[1] === symbol && query.queryKey[2] === view && query.queryKey[3] === program
      && previous?.times[0] === ready.body.rows[0]?.time && previous.times.length <= ready.body.rows.length ? previous : undefined,
  })
  // 绘图显隐不改变计算程序，复用相同结果即时切换，也避免其他副图随点击闪烁。
  const result = useMemo(() => sameContext && visibleIds.size > 0 && values.data ? {
    ...values.data, results: values.data.results.filter(r => visibleIds.has(r.id)), errors: values.data.errors.filter(e => visibleIds.has(e.id)),
  } : undefined, [sameContext, visibleIds, values.data])
  return { library, definitions, visibleIds, values, result }
}

type State = ReturnType<typeof useIndicators>
function IndicatorControls({ state, symbol, view, rows, notice }: { state: State; symbol: string; view: IndicatorView; rows: readonly object[]; notice?: ReactNode }) {
  const [editor, setEditor] = useState<{ id?: string } | null>(null)
  const queryClient = useQueryClient()
  const savingVisibility = useIsMutating({ mutationKey: QK.chartIndicatorLibrary }) > 0
  const visibility = useMutation({
    mutationKey: QK.chartIndicatorLibrary,
    mutationFn: (next: IndicatorLibraryResponse) => api.saveChartIndicatorLibrary(next.library, next.revision),
    onMutate: async next => {
      await queryClient.cancelQueries({ queryKey: QK.chartIndicatorLibrary })
      const previous = queryClient.getQueryData<IndicatorLibraryResponse>(QK.chartIndicatorLibrary)
      queryClient.setQueryData(QK.chartIndicatorLibrary, next)
      return { previous, optimistic: queryClient.getQueryData(QK.chartIndicatorLibrary) }
    },
    onSuccess: result => { queryClient.setQueryData(QK.chartIndicatorLibrary, result) },
    onError: (_error, _next, context) => {
      if (context?.previous && queryClient.getQueryData(QK.chartIndicatorLibrary) === context.optimistic) {
        queryClient.setQueryData(QK.chartIndicatorLibrary, context.previous)
      }
      return queryClient.invalidateQueries({ queryKey: QK.chartIndicatorLibrary })
    },
  })
  const toggle = (id: string) => {
    const current = state.library.data
    if (!current || savingVisibility) return
    visibility.mutate({ ...current, library: { ...current.library, views: { ...current.library.views,
      [view]: current.library.views[view].map(selection => selection.id === id ? { ...selection, visible: selection.visible === false } : selection),
    } } })
  }
  useEffect(() => setEditor(null), [symbol, view])
  const messages = [...(state.result?.errors.map(e => `${e.name}：${e.message}`) ?? []), ...(state.result?.warnings ?? [])]
  return <>
    <div className="flex min-h-10 min-w-0 flex-wrap content-center items-center gap-1 border-b border-border/50 px-2 py-1 text-xs sm:text-[11px]">
      <button type="button" aria-label={`${view === 'daily' ? '日K' : '分时'}自定义指标`}
        title={`${view === 'daily' ? '日K' : '分时'}自定义指标`}
        disabled={!state.library.data || savingVisibility} onClick={() => setEditor({})}
        className="inline-flex min-h-8 w-8 shrink-0 items-center justify-center rounded text-secondary transition-colors hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 disabled:opacity-50 sm:min-h-6 sm:w-6">
        <SlidersHorizontal className="h-3.5 w-3.5 sm:h-3 sm:w-3" aria-hidden="true" />
      </button>
      {state.definitions.map(d => <button type="button" key={d.id} onClick={() => toggle(d.id)} disabled={savingVisibility}
        aria-pressed={state.visibleIds.has(d.id)} title={`${state.visibleIds.has(d.id) ? '隐藏' : '显示'}${d.name}`}
        className={`min-h-8 max-w-36 truncate rounded px-2 py-1 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 disabled:cursor-wait sm:min-h-6 sm:px-1.5 sm:py-0.5 ${state.visibleIds.has(d.id) ? 'bg-accent/10 text-accent hover:bg-accent/20' : 'bg-elevated/50 text-secondary hover:bg-elevated hover:text-foreground'}`}>{d.name}</button>)}
      {!state.definitions.length && !state.library.isPending && !state.library.isError && <span className="text-secondary">选择指标或新建公式</span>}
      {(state.library.isPending || state.values.isFetching) && <span className="inline-flex items-center gap-1 text-secondary"><Loader2 className="h-3 w-3 animate-spin" />{state.library.isPending ? '读取配置…' : '指标更新中…'}</span>}
      {state.library.isError && <span role="status" className="text-danger">指标配置加载失败 <button type="button" onClick={() => void state.library.refetch()} className="underline underline-offset-2">重试</button></span>}
      {state.library.data?.volumeCompareMigrationError && <span role="status" className="basis-full text-danger">旧量比设置尚未保存：{state.library.data.volumeCompareMigrationError}
        <button type="button" onClick={() => void state.library.refetch()} className="ml-2 underline underline-offset-2">重试</button>
      </span>}
      {state.values.isError && state.visibleIds.size > 0 && <span role="status" className="text-danger">指标计算失败 <button type="button" onClick={() => void state.values.refetch()} className="underline underline-offset-2">重试</button></span>}
      {visibility.isError && <span role="status" className="basis-full text-danger">指标显示状态保存失败：{visibility.error.message}</span>}
      {messages.length > 0 && <details className="basis-full text-secondary"><summary className="cursor-pointer py-1">指标提示（{messages.length}）</summary>
        <ul className="space-y-1 break-words pb-1 pl-4">{messages.map((message, i) => <li key={i}>{message}</li>)}</ul>
      </details>}
      {notice && <div className="basis-full">{notice}</div>}
      {editor && state.library.data && <Suspense fallback={<span role="status" className="basis-full px-3 text-xs text-secondary">正在打开指标编辑器…</span>}>
      <IndicatorManager key={`${symbol}:${view}`} initial={state.library.data} initialId={editor.id} symbol={symbol} view={view}
        sourceRows={rows} onClose={() => setEditor(null)} />
      </Suspense>}
    </div>
  </>
}

export function CustomDaily(props: DailyChartProps) {
  const source = props.indicatorRows ?? EMPTY_ROWS
  const state = useIndicators(props.symbol ?? '', 'daily', source, props.indicatorRevision)
  const volumeCompare = useMemo(() => Object.fromEntries((state.library.data?.library.views.daily ?? [])
    .flatMap(selection => selection.volume_compare ? [[selection.id, selection.volume_compare]] : [])), [state.library.data?.library])
  const subplots = state.result?.results.filter(r => r.pane === 'sub').length ?? 0
  return <>
    <IndicatorControls state={state} symbol={props.symbol ?? ''} view="daily" rows={source} />
    {props.data.length > 0 && <LightweightDaily {...props} mainTimeAxis showMA={false} activeIndicators={[]}
      height={(props.height ?? 480) + subplots * CHART_LAYOUT.subPaneSlot}
      customVolumeCompare={volumeCompare} customIndicatorValues={state.result} />}
  </>
}

export function CustomIntraday(props: LightweightIntradayProps) {
  const source = useMemo(() => props.sessions?.flatMap(s => s.rows) ?? props.data ?? EMPTY_ROWS, [props.sessions, props.data])
  const state = useIndicators(props.symbol, 'minute', source, props.indicatorRevision)
  const subplots = state.result?.results.filter(r => r.pane === 'sub').length ?? 0
  return <LightweightIntraday {...props} mainTimeAxis showVolume={false}
    indicatorControls={<IndicatorControls state={state} symbol={props.symbol} view="minute" rows={source} notice={props.chartNotice} />}
    height={(props.height ?? 420) + subplots * CHART_LAYOUT.subPaneSlot} customIndicatorValues={state.result} />
}
