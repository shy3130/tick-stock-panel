import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CalendarDays, Pause, Play, Plus } from 'lucide-react'
import { api } from '@/lib/api'
import type { EngineJobPreview, EngineScheduleDelivery, EngineScheduleRecord } from '@/lib/engineJobTypes'
import { QK } from '@/lib/queryKeys'
import { ScheduleForm } from './ScheduleForm'
import { Button, ErrorNotice, LoadingLines, workspaceClass, workspaceRowClass, workspaceSidebarClass } from './ui'

const dispatchLabels: Record<EngineScheduleDelivery['status'], string> = {
  pending: '待检查', prepared: '已锁定输入', submitted: '已提交任务', skipped_overlap: '已有任务运行，已跳过', failed: '派发失败',
}

function timeAt(value: string, market: boolean) {
  return new Date(value).toLocaleString('zh-CN', { timeZone: market ? 'Asia/Shanghai' : 'UTC', hour12: false }) + (market ? ' 北京时间' : ' UTC')
}

function ScheduleDetail({ id, onJob }: { id: string, onJob: (id: string) => void }) {
  const client = useQueryClient()
  const [showCalendar, setShowCalendar] = useState(false)
  const detail = useQuery({ queryKey: QK.engineSchedule(id), queryFn: () => api.engineSchedule(id), retry: false,
    refetchInterval: q => {
      if (q.state.error || !q.state.data) return false
      if (q.state.data.enabled) return 3000
      return q.state.data.next_due_at || q.state.data.request.trigger.kind === 'data_publication' ? 10000 : false
    } })
  const deliveries = useQuery({ queryKey: QK.engineScheduleDeliveries(id, 50), queryFn: () => api.engineScheduleDeliveries(id, 50),
    retry: false, refetchInterval: q => {
      if (q.state.error) return false
      const last = detail.data?.last_delivery_id
      const items = q.state.data?.items ?? []
      const missing = last && items[0]?.delivery_id !== last
      return detail.data?.enabled || missing || items.some(d => d.status === 'prepared' || d.status === 'pending') ? 3000 : false
    } })
  const calendar = useQuery({ queryKey: QK.engineScheduleCalendar(id), queryFn: () => api.engineScheduleCalendar(id),
    enabled: showCalendar, retry: false, staleTime: Infinity })
  const control = useMutation({ mutationFn: (enabled: boolean) => api.setEngineScheduleEnabled(id, enabled),
    onSuccess: record => {
      client.setQueryData(QK.engineSchedule(id), record)
      void client.invalidateQueries({ queryKey: QK.engineSchedules(100) })
      void client.invalidateQueries({ queryKey: QK.engineScheduleDeliveries(id, 50) })
    },
  })
  if (detail.isPending) return <LoadingLines label="正在读取触发计划" />
  if (detail.error || !detail.data) return <div className="p-4 sm:p-6">
    <ErrorNotice error={detail.error}><Button onClick={() => void detail.refetch()}>重新读取计划</Button></ErrorNotice>
  </div>
  const record = detail.data
  const trigger = record.request.trigger
  const market = trigger.kind === 'trading_day'
  const publication = trigger.kind === 'data_publication'
  const pending = deliveries.data?.items.some(d => d.status === 'prepared' || d.status === 'pending')
  const exhausted = !publication && !record.enabled && !record.next_due_at && deliveries.isSuccess && !pending
  return <article className="w-full min-w-0 max-w-5xl space-y-3 p-3 sm:px-4">
    <header className="flex flex-wrap items-start justify-between gap-2 border-b border-border pb-2.5">
      <div className="min-w-0">
        <h2 className="break-all text-[16px] font-semibold">{record.request.schedule_key}</h2>
        <p className="mt-1 text-xs text-secondary">{record.enabled ? '已启用' : exhausted ? '已结束' : '已暂停'}</p>
      </div>
      <Button variant={!record.enabled && !exhausted ? 'primary' : 'secondary'}
        disabled={control.isPending || exhausted} aria-busy={control.isPending} onClick={() => control.mutate(!record.enabled)}>
        {record.enabled ? <Pause aria-hidden className="h-4 w-4" /> : <Play aria-hidden className="h-4 w-4" />}
        {control.isPending ? '正在更新…' : record.enabled ? '暂停计划' : '启用计划'}</Button>
    </header>
    {control.error && <ErrorNotice error={control.error} />}
    {record.last_error && <ErrorNotice error={publication ? `计划已暂停（${record.last_error.code}）。修复数据流或任务输入后，重新启用会从上次确认位置继续。` : `计划已暂停（${record.last_error.code}），请核对输入或日历。`} />}
    <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-sm">
      {publication ? <>
        <dt className="text-secondary">触发方式</dt><dd>数据版本发布 · 顺序派发</dd>
        <dt className="text-secondary">跟随的数据集</dt><dd className="break-all">{trigger.dataset_id}</dd>
        <dt className="text-secondary">已确认的发布序号</dt><dd>{record.publication_cursor?.sequence ?? '—'}</dd>
      </> : <>
        <dt className="text-secondary">下次派发</dt><dd>{record.next_due_at ? timeAt(record.next_due_at, market) : '已无后续时点'}</dd>
        <dt className="text-secondary">触发方式</dt><dd>{market ? `${trigger.venue_id === 'XSHE' ? '深市' : '沪市'}交易日 · ${trigger.anchor === 'open' ? '连续交易开盘' : '收盘'} · 偏移 ${trigger.offset_minutes ?? 0} 分钟` : `每 ${trigger.interval_seconds} 秒`}</dd>
      </>}
      {market && <><dt className="text-secondary">锁定日历</dt><dd>{trigger.calendar_version} · {trigger.start_date} 至 {trigger.end_date} · {record.calendar?.trading_days} 个交易日</dd></>}
      <dt className="text-secondary">任务模板</dt><dd className="break-all">{record.request.template.nodes.map(n => n.node_id).join('、')}</dd>
      {market && !!trigger.daily_capture_nodes?.length && <><dt className="text-secondary">随交易日采集</dt><dd className="break-all">{trigger.daily_capture_nodes.join('、')}</dd></>}
    </dl>
    {market && <section className="space-y-2">
      <Button aria-expanded={showCalendar} onClick={() => setShowCalendar(!showCalendar)}><CalendarDays aria-hidden className="h-4 w-4" />{showCalendar ? '收起派发日历' : '查看派发日历'}</Button>
      {showCalendar && (calendar.isPending ? <LoadingLines label="正在读取锁定日历" /> : calendar.error ?
        <ErrorNotice error={calendar.error}><Button onClick={() => void calendar.refetch()}>重新读取日历</Button></ErrorNotice> : calendar.data &&
          <div><p className="break-all text-xs text-secondary">日历 SHA-256 · <code>{calendar.data.calendar_sha256}</code></p>
            <ol aria-label="交易日期与派发时点" tabIndex={0} className="mt-2 max-h-56 divide-y divide-border overflow-auto rounded-btn border border-border px-3 text-sm focus-visible:outline-accent">
              {calendar.data.occurrences.map(d => <li key={d.trading_date} className="flex flex-wrap justify-between gap-2 py-1.5"><span>{d.trading_date}</span><time dateTime={d.due_at}>{timeAt(d.due_at, true)}</time></li>)}
            </ol></div>)}
    </section>}
    <section className="space-y-2 border-t border-border pt-2.5"><h3 className="text-sm font-medium">最近派发</h3>
      {deliveries.error ? <ErrorNotice error={deliveries.error}><Button onClick={() => void deliveries.refetch()}>重新读取派发</Button></ErrorNotice> :
        deliveries.isPending ? <LoadingLines label="正在读取派发记录" /> : deliveries.data?.items.length ?
          <ol className="divide-y divide-border">{deliveries.data.items.map(d => <li key={d.delivery_id} className="flex flex-wrap items-center justify-between gap-2 py-1.5 text-sm">
            <div><p>{timeAt(d.due_at, market)} · {dispatchLabels[d.status]}</p>
              <p className="break-all text-xs text-secondary">{d.publication
                ? `发布序号 ${d.publication.sequence} · 数据版本 ${d.publication.version_id}`
                : `${d.trading_date ? `交易日 ${d.trading_date} · ` : ''}错过 ${d.missed_count} 次`}{d.error && ` · ${d.error.code}`}</p></div>
            {d.job_id && <Button onClick={() => onJob(d.job_id!)}>查看任务</Button>}</li>)}</ol> :
          <p className="text-sm text-secondary">暂无派发记录。</p>}
    </section>
  </article>
}

export function Schedules({ id, template, onSelect, onTemplate, onJob }: {
  id: string, template: EngineJobPreview | null, onSelect: (id: string) => void,
  onTemplate: () => void, onJob: (id: string) => void,
}) {
  const client = useQueryClient()
  const list = useQuery({ queryKey: QK.engineSchedules(100), queryFn: () => api.engineSchedules(100), retry: false,
    refetchInterval: q => q.state.error ? false : 10000 })
  const selected = id || list.data?.items[0]?.schedule_id || ''
  function saved(record: EngineScheduleRecord) {
    client.setQueryData(QK.engineSchedule(record.schedule_id), record)
    void client.invalidateQueries({ queryKey: QK.engineSchedules(100) })
    onSelect(record.schedule_id)
  }
  return <div className={workspaceClass}>
    <aside aria-label="触发计划列表" className={workspaceSidebarClass}>
      <div className="flex flex-wrap items-center justify-between gap-2 px-2.5 py-1">
        <h2 className="text-sm font-semibold">触发计划</h2>
        <Button compact onClick={onTemplate}><Plus aria-hidden="true" className="size-3.5" />选择任务模板</Button>
      </div>
      {list.error ? <div className="px-3 pb-3"><ErrorNotice error={list.error}><Button onClick={() => void list.refetch()}>重新读取列表</Button></ErrorNotice></div> :
        list.isPending ? <LoadingLines label="正在读取计划列表" /> : list.data?.items.length ?
          <ul className="max-h-56 space-y-0.5 overflow-y-auto px-1.5 pb-1.5 lg:max-h-none lg:overflow-visible">{list.data.items.map(s => <li key={s.schedule_id}>
            <button type="button" aria-current={!template && s.schedule_id === selected ? 'true' : undefined} onClick={() => onSelect(s.schedule_id)}
              className={workspaceRowClass}>
              <span className="block break-all text-sm font-medium">{s.request.schedule_key}</span>
              <span className="mt-0.5 block text-xs text-secondary">{s.request.trigger.kind === 'trading_day' ? '交易日' : s.request.trigger.kind === 'data_publication' ? '数据版本发布' : 'UTC 间隔'} · {s.enabled ? '已启用' : '已停用'}</span>
            </button></li>)}</ul> : <p className="px-4 pb-4 text-sm text-secondary">还没有触发计划。</p>}
    </aside>
    <section aria-label="触发计划详情" className="min-h-0 min-w-0 lg:overflow-y-auto">
      {template ? <ScheduleForm preview={template} onSaved={saved} onCancel={() => onSelect(id)} /> :
        selected ? <ScheduleDetail key={selected} id={selected} onJob={onJob} /> :
          !list.isPending && !list.error && <p className="p-3 text-sm leading-5 text-secondary sm:px-4">选择并检查任务模板后，可以设置触发计划。</p>}
    </section>
  </div>
}
