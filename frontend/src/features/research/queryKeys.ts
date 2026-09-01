import type { FactorCatalogQuery } from './model/factor'
import type { EventListQuery, RunListQuery } from './model/run'

export const researchKeys = {
  all: ['research-workbench'] as const,
  catalog: (filters: FactorCatalogQuery = {}) =>
    ['research-workbench', 'catalog', filters.category ?? '', filters.engineering_status ?? '', filters.data_status ?? '', filters.verdict ?? '', filters.scope ?? '', filters.query ?? ''] as const,
  factor: (factorId: string) => ['research-workbench', 'factor', factorId] as const,
  preflight: (factorId: string, revision: number) => ['research-workbench', 'preflight', factorId, revision] as const,
  runs: (filters: RunListQuery = {}) =>
    ['research-workbench', 'runs', filters.factor_id ?? '', filters.job_status ?? '', filters.verdict ?? '', filters.scope_type ?? '', filters.favorite === true ? '1' : '', filters.limit ?? 50] as const,
  run: (runId: string) => ['research-workbench', 'run', runId] as const,
  events: (runId: string, filters: EventListQuery = {}) =>
    ['research-workbench', 'events', runId, filters.symbol ?? '', filters.arm ?? '', filters.qualified ?? '', filters.reachable ?? '', filters.censor_code ?? '', filters.date ?? ''] as const,
  series: (runId: string, kinds?: string) => ['research-workbench', 'series', runId, kinds ?? 'equity,baseline,increment,drawdown'] as const,
  hypothesesRoot: ['research-workbench', 'hypotheses'] as const,
  hypotheses: (status?: string, query?: string) =>
    ['research-workbench', 'hypotheses', status ?? '', query ?? ''] as const,
  hypothesis: (id: string) => ['research-workbench', 'hypothesis', id] as const,
  runCard: (runId: string) => ['research-workbench', 'run-card', runId] as const,
  schedules: ['research-workbench', 'schedules'] as const,
  symbolAnalysis: (symbol: string, start: string, end: string) =>
    ['research-workbench', 'symbol-analysis', symbol, start, end] as const,
}
