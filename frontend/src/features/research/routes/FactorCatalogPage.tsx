import { useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import { PageHeader } from '@/components/PageHeader'
import { CatalogToolbar, FactorCatalogTable } from '../components/FactorCatalogTable'
import { HttpErrorState, LoadingState } from '../components/QueryState'
import { useFactorCatalog } from '../hooks/useResearchQueries'

export function FactorCatalogPage() {
  const [params, setParams] = useSearchParams()
  const filters = {
    query: params.get('query') ?? '',
    category: params.get('category') ?? '',
    engineering_status: params.get('engineering_status') ?? '',
    data_status: params.get('data_status') ?? '',
    verdict: params.get('verdict') ?? '',
    scope: params.get('scope') ?? '',
  }
  const catalog = useFactorCatalog({
    query: filters.query || undefined,
    category: filters.category || undefined,
    engineering_status: filters.engineering_status || undefined,
    data_status: filters.data_status || undefined,
    verdict: filters.verdict || undefined,
    scope: filters.scope || undefined,
  })
  const items = catalog.data?.items ?? []
  const categories = useMemo(() => Array.from(new Set(items.map((item) => String(item.category)))).sort(), [items])
  const filtered = Boolean(filters.query || filters.category || filters.engineering_status || filters.data_status || filters.verdict || filters.scope)

  const patch = (key: string, value: string) => {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    setParams(next, { replace: true })
  }

  return (
    <div className="workspace-page h-full min-h-0 overflow-auto">
      <PageHeader title="因子目录" subtitle="19 个公开因子共用一套 catalog / workbench，不为每个因子手写页面" />
      <div className="workspace-content space-y-3">
        <CatalogToolbar
          query={filters.query}
          category={filters.category}
          engineering={filters.engineering_status}
          dataStatus={filters.data_status}
          verdict={filters.verdict}
          scope={filters.scope}
          categories={categories}
          onQuery={(value) => patch('query', value)}
          onCategory={(value) => patch('category', value)}
          onEngineering={(value) => patch('engineering_status', value)}
          onDataStatus={(value) => patch('data_status', value)}
          onVerdict={(value) => patch('verdict', value)}
          onScope={(value) => patch('scope', value)}
        />
        {catalog.isPending ? <LoadingState label="读取因子目录" /> : null}
        {catalog.isError ? <HttpErrorState error={catalog.error} onRetry={() => void catalog.refetch()} /> : null}
        {catalog.data ? <FactorCatalogTable items={items} filtered={filtered} /> : null}
      </div>
    </div>
  )
}
