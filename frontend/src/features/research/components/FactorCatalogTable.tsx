import { useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { FlaskConical } from 'lucide-react'
import { cn } from '@/lib/cn'
import { fmtDateTime } from '../lib/format'
import type { FactorCatalogItem } from '../model/factor'
import { scopeShort } from '../model/status'
import { DataStatusBadge, EngineeringBadge, ProfileBadge, PromotionBadge, VerdictBadge } from './StatusBadges'
import { GuidedEmpty } from './QueryState'

export function FactorCatalogTable({ items, filtered }: { items: FactorCatalogItem[]; filtered: boolean }) {
  const navigate = useNavigate()
  const rows = useMemo(() => items, [items])

  if (rows.length === 0) {
    return (
      <GuidedEmpty
        title={filtered ? '没有符合筛选的因子' : '因子目录为空'}
        hint={filtered ? '放宽搜索、分类或四套状态筛选后再看。目录只显示服务端登记的因子。' : '研究服务未返回任何因子。确认 Control Plane 已注册 19 个公开 ID。'}
      />
    )
  }

  return (
    <div className="data-table-scroll">
      <table className="data-table min-w-[56rem]">
        <thead>
          <tr>
            <th>因子</th>
            <th>工程</th>
            <th>数据</th>
            <th>裁决</th>
            <th>晋级</th>
            <th>Scope</th>
            <th>Profile</th>
            <th>最近运行</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((item) => (
            <tr
              key={item.id}
              className="cursor-pointer"
              onClick={() => navigate(`/research/factors/${encodeURIComponent(item.id)}`)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault()
                  navigate(`/research/factors/${encodeURIComponent(item.id)}`)
                }
              }}
              tabIndex={0}
              aria-label={`打开 ${item.title} 工作台`}
            >
              <td>
                <div className="flex min-w-0 items-start gap-2">
                  <FlaskConical className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent" aria-hidden />
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-foreground">{item.title}</p>
                    <p className="truncate font-mono text-[11px] text-muted">{item.id}</p>
                  </div>
                </div>
              </td>
              <td><EngineeringBadge value={item.engineering_status} /></td>
              <td><DataStatusBadge value={item.latest_data_status} /></td>
              <td><VerdictBadge value={item.latest_verdict} /></td>
              <td><PromotionBadge value={item.promotion_status} /></td>
              <td>
                <span className="font-mono text-xs">{scopeShort(item.supported_scopes)}</span>
                {item.id === 'negative-exclusion' ? (
                  <p className="mt-0.5 text-[10px] leading-relaxed text-muted">S: V2/V4/V5 · FM: 仅 V5</p>
                ) : null}
              </td>
              <td><ProfileBadge value={item.result_profile} /></td>
              <td className="font-mono text-xs text-secondary">{fmtDateTime(item.latest_run?.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function CatalogToolbar({
  query,
  category,
  engineering,
  dataStatus,
  verdict,
  scope,
  categories,
  onQuery,
  onCategory,
  onEngineering,
  onDataStatus,
  onVerdict,
  onScope,
}: {
  query: string
  category: string
  engineering: string
  dataStatus: string
  verdict: string
  scope: string
  categories: string[]
  onQuery: (value: string) => void
  onCategory: (value: string) => void
  onEngineering: (value: string) => void
  onDataStatus: (value: string) => void
  onVerdict: (value: string) => void
  onScope: (value: string) => void
}) {
  return (
    <div className="workspace-toolbar">
      <label className="relative min-w-0 flex-1">
        <span className="sr-only">搜索因子</span>
        <input
          value={query}
          onChange={(event) => onQuery(event.target.value)}
          className="control min-h-11 w-full text-sm sm:min-h-[var(--control-h)]"
          placeholder="搜索名称、ID 或描述"
        />
      </label>
      <FilterSelect label="分类" value={category} onChange={onCategory} options={[['全部分类', ''], ...categories.map((item) => [item, item] as const)]} />
      <FilterSelect label="工程" value={engineering} onChange={onEngineering} options={[['全部工程', ''], ['完成', 'completed'], ['部分', 'partial'], ['计划', 'planned']]} />
      <FilterSelect label="数据" value={dataStatus} onChange={onDataStatus} options={[['全部数据', ''], ['可用', 'ready'], ['部分', 'partial'], ['缺失', 'missing'], ['过期', 'stale'], ['删失', 'censored']]} />
      <FilterSelect label="裁决" value={verdict} onChange={onVerdict} options={[['全部裁决', ''], ['接受', 'accepted'], ['拒绝', 'rejected'], ['不可用', 'unavailable'], ['无结论', 'inconclusive']]} />
      <FilterSelect label="范围" value={scope} onChange={onScope} options={[['全部范围', ''], ['标的', 'symbols'], ['全市场', 'full_market']]} />
    </div>
  )
}

function FilterSelect({
  label,
  value,
  onChange,
  options,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  options: readonly (readonly [string, string])[]
}) {
  return (
    <label className="flex min-h-11 items-center gap-1.5 text-xs text-secondary sm:min-h-[var(--control-h)]">
      <span className="sr-only sm:not-sr-only sm:shrink-0">{label}</span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className={cn('control min-h-11 min-w-28 text-xs sm:min-h-[var(--control-h)]')}
      >
        {options.map(([text, val]) => (
          <option key={`${label}-${val}`} value={val}>{text}</option>
        ))}
      </select>
    </label>
  )
}
