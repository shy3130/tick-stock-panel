import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { getFactor, listFactors } from '../api/catalog'
import { listHypotheses } from '../api/evidence'
import { listRunEvents, getRun, getRunSeries, listRuns } from '../api/runs'
import type { FactorCatalogQuery } from '../model/factor'
import type { EventListQuery, RunListQuery } from '../model/run'
import { researchKeys } from '../queryKeys'

export function useFactorCatalog(filters: FactorCatalogQuery) {
  return useQuery({
    queryKey: researchKeys.catalog(filters),
    queryFn: () => listFactors(filters),
  })
}

export function useFactorDetail(factorId: string | undefined) {
  return useQuery({
    queryKey: researchKeys.factor(factorId ?? ''),
    queryFn: () => getFactor(factorId!),
    enabled: Boolean(factorId),
  })
}

export function useRunList(filters: RunListQuery) {
  return useInfiniteQuery({
    queryKey: researchKeys.runs(filters),
    queryFn: ({ pageParam }) => listRuns({ ...filters, cursor: pageParam, limit: filters.limit ?? 50 }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  })
}

export function useRunDetail(runId: string | undefined) {
  return useQuery({
    queryKey: researchKeys.run(runId ?? ''),
    queryFn: () => getRun(runId!),
    enabled: Boolean(runId),
  })
}

export function useRunEvents(runId: string | undefined, filters: EventListQuery = {}) {
  return useInfiniteQuery({
    queryKey: researchKeys.events(runId ?? '', filters),
    queryFn: ({ pageParam }) => listRunEvents(runId!, { ...filters, cursor: pageParam, limit: 200 }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: Boolean(runId),
  })
}

export function useRunSeries(runId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: researchKeys.series(runId ?? ''),
    queryFn: () => getRunSeries(runId!),
    enabled: Boolean(runId) && enabled,
  })
}

export function useHypothesisList(status?: string, query?: string) {
  return useQuery({
    queryKey: researchKeys.hypotheses(status, query),
    queryFn: () => listHypotheses({ status, query }),
  })
}
