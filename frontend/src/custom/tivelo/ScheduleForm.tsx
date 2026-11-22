import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import type { EngineJobPreview, EngineScheduleRecord, EngineScheduleRequest } from '@/lib/engineJobTypes'
import { Button, ErrorNotice, inputClass } from './ui'

export function ScheduleForm({ preview, onSaved, onCancel }: {
  preview: EngineJobPreview
  onSaved: (record: EngineScheduleRecord) => void
  onCancel: () => void
}) {
  const [kind, setKind] = useState('trading_day')
  const [key, setKey] = useState('')
  const [venue, setVenue] = useState<'XSHG' | 'XSHE'>('XSHG')
  const [version, setVersion] = useState('')
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [anchor, setAnchor] = useState<'open' | 'close'>('close')
  const [offset, setOffset] = useState('30')
  const [daily, setDaily] = useState<string[]>([])
  const datasets = preview.publication_datasets ?? []
  const [dataset, setDataset] = useState(datasets[0] ?? '')
  const [utcStart, setUtcStart] = useState('')
  const [utcEnd, setUtcEnd] = useState('')
  const [interval, setIntervalValue] = useState('86400')
  const errorRef = useRef<HTMLDivElement>(null)
  const presets = useQuery({ queryKey: QK.engineCalendarPresets, queryFn: api.engineCalendarPresets, enabled: kind === 'trading_day', retry: false })
  const options = (presets.data?.items ?? []).filter(p => p.venues[venue]?.length)
  const selected = options.find(p => p.version === version) ?? options[0]
  const template = preview.plan.request
  const eligible = preview.trading_day_capture_nodes ?? []
  const create = useMutation({ mutationFn: api.createEngineSchedule })
  useEffect(() => { if (create.error) errorRef.current?.focus() }, [create.error])

  function save(event: FormEvent) {
    event.preventDefault()
    if (create.isPending) return
    const common = { schedule_key: key, template: { nodes: template.nodes, budget: template.budget } }
    const body: EngineScheduleRequest = kind === 'trading_day' ? {
      ...common, schedule_version: '1.1', trigger: {
        kind: 'trading_day', market_id: 'cn.equity', venue_id: venue, calendar_version: selected?.version ?? version,
        preset_sha256: selected?.preset_sha256, start_date: start, end_date: end,
        anchor, offset_minutes: Number(offset), daily_capture_nodes: daily.slice().sort(),
      },
    } : kind === 'data_publication' ? {
      ...common, schedule_version: '1.2', trigger: { kind: 'data_publication', dataset_id: dataset },
    } : {
      ...common, schedule_version: '1.0', trigger: {
        kind: 'utc_interval', starts_at: new Date(utcStart + 'Z').toISOString(),
        ends_at: utcEnd ? new Date(utcEnd + 'Z').toISOString() : null, interval_seconds: Number(interval),
      },
    }
    create.mutate(body, { onSuccess: onSaved })
  }

  return <form onSubmit={save} aria-label="创建触发计划" className="w-full max-w-3xl space-y-3 p-3 sm:px-4">
    <header><h2 className="text-[16px] font-semibold">创建触发计划</h2>
      <p className="mt-1 break-words text-sm text-secondary">任务步骤 · {template.nodes.map(n => n.node_id).join('、')}</p></header>
    {create.error && <div tabIndex={-1} ref={errorRef}><ErrorNotice error={create.error} /></div>}
    <fieldset disabled={create.isPending} className="grid min-w-0 gap-2.5 sm:grid-cols-2">
      <label className="space-y-1 text-sm"><span>计划键</span>
        <input required maxLength={128} pattern="[A-Za-z0-9][A-Za-z0-9_.:\-]*" title="以字母或数字开头，可使用点、冒号、下划线或连字符" className={inputClass} name="schedule_key" value={key} onChange={e => setKey(e.target.value)} placeholder="daily-close" /></label>
      <label className="space-y-1 text-sm"><span>触发方式</span><select className={inputClass} name="trigger_kind" value={kind} onChange={e => { create.reset(); setKind(e.target.value) }}>
        <option value="trading_day">交易日</option><option value="utc_interval">UTC 固定间隔</option>
        {!!datasets.length && <option value="data_publication">数据版本发布</option>}
      </select></label>
      {kind === 'trading_day' ? <>
        <label className="space-y-1 text-sm"><span>交易所</span><select className={inputClass} name="venue" value={venue} onChange={e => { setVenue(e.target.value as typeof venue); setVersion('') }}>
          <option value="XSHG">上海证券交易所</option><option value="XSHE">深圳证券交易所</option>
        </select></label>
        <label className="space-y-1 text-sm"><span>日历版本</span>{options.length ?
          <select className={inputClass} name="calendar_version" value={selected.version} onChange={e => setVersion(e.target.value)}>
            {options.map(p => <option key={p.version} value={p.version}>{p.version}</option>)}</select> :
          <input className={inputClass} required name="calendar_version" value={version} onChange={e => setVersion(e.target.value)} placeholder={presets.isPending ? '正在读取已安装日历…' : '输入已安装的日历版本'} />}</label>
        {presets.error && <div className="sm:col-span-2"><ErrorNotice error={presets.error}><Button onClick={() => void presets.refetch()}>重新读取日历</Button></ErrorNotice></div>}
        {selected && <p className="text-sm text-secondary sm:col-span-2">日历覆盖年份 · {selected.venues[venue]?.join('、')}</p>}
        <label className="space-y-1 text-sm"><span>起始交易日期</span><input type="date" required className={inputClass} name="start_date" value={start} onChange={e => setStart(e.target.value)} /></label>
        <label className="space-y-1 text-sm"><span>结束交易日期</span><input type="date" required min={start || undefined} className={inputClass} name="end_date" value={end} onChange={e => setEnd(e.target.value)} /></label>
        <label className="space-y-1 text-sm"><span>派发基准</span><select className={inputClass} name="anchor" value={anchor} onChange={e => setAnchor(e.target.value as typeof anchor)}>
          <option value="open">连续交易开盘</option><option value="close">收盘</option>
        </select></label>
        <label className="space-y-1 text-sm"><span>偏移分钟（负数提前）</span><input type="number" required min={-1440} max={1440} step={1} className={inputClass} name="offset_minutes" value={offset} onChange={e => setOffset(e.target.value)} /></label>
        {!!eligible.length && <fieldset className="space-y-1 sm:col-span-2">
          <legend className="text-sm font-medium">按派发交易日采集</legend>
          {eligible.map(nodeId => <label key={nodeId} className="flex min-h-8 items-center gap-2 text-sm [@media(pointer:coarse)]:min-h-9">
            <input type="checkbox" checked={daily.includes(nodeId)} onChange={e => setDaily(e.target.checked ? [...daily, nodeId] : daily.filter(id => id !== nodeId))} />
            {nodeId}</label>)}
          <p className="text-xs text-secondary">勾选的日线步骤使用该交易日的北京时间零点至次日零点。其余步骤使用模板中的日期。</p>
        </fieldset>}
      </> : kind === 'data_publication' ? <>
        <label className="space-y-1 text-sm sm:col-span-2"><span>跟随的数据集</span>
          <select required className={inputClass} name="dataset_id" value={dataset} onChange={e => setDataset(e.target.value)}>
            {datasets.map(id => <option key={id} value={id}>{id}</option>)}
          </select></label>
      </> : <>
        <label className="space-y-1 text-sm"><span>开始时刻（UTC）</span><input type="datetime-local" required step="1" className={inputClass} name="starts_at" value={utcStart} onChange={e => setUtcStart(e.target.value)} /></label>
        <label className="space-y-1 text-sm"><span>结束时刻（UTC，可选）</span><input type="datetime-local" step="1" min={utcStart || undefined} className={inputClass} name="ends_at" value={utcEnd} onChange={e => setUtcEnd(e.target.value)} /></label>
        <label className="space-y-1 text-sm"><span>间隔秒数</span><input type="number" required min="60" max={31 * 86400} step="1" className={inputClass} name="interval_seconds" value={interval} onChange={e => setIntervalValue(e.target.value)} /></label>
      </>}
    </fieldset>
    <p className="text-sm text-secondary">{kind === 'data_publication'
      ? '从保存计划时开始记录。启用后，每个新版本按发布顺序进入任务，暂停期间的版本也会依次处理。'
      : '保存后先核对派发时点，再启用计划。恢复时只补最近一次，上一次任务仍在运行时跳过。'}</p>
    <div className="flex flex-wrap justify-end gap-2 border-t border-border pt-2.5">
      <Button variant="quiet" onClick={onCancel} disabled={create.isPending}>取消</Button>
      <Button type="submit" variant="primary" disabled={create.isPending || (kind === 'trading_day' && presets.isPending)}>
        {create.isPending ? '正在保存…' : '保存为暂停计划'}
      </Button>
    </div>
  </form>
}
