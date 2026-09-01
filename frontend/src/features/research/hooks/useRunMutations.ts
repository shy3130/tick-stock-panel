import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from '@/components/Toast'
import { cancelRun, createRun, linkRunHypothesis, patchRun } from '../api/runs'
import { isResearchApiError, researchErrorMessage } from '../model/errors'
import type { CreateRunRequest, RunScope } from '../model/run'
import { researchKeys } from '../queryKeys'

export function useCreateRun() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: CreateRunRequest) => createRun(body),
    onSuccess: (created) => {
      void qc.invalidateQueries({ queryKey: researchKeys.all })
      toast(`已创建运行 ${created.run_id}`, 'success')
    },
    onError: (error) => {
      if (isResearchApiError(error) && error.isPreflightBlocked) return
      toast(researchErrorMessage(error), 'error')
    },
  })
}

export function useCancelRun() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (runId: string) => cancelRun(runId),
    onSuccess: (run) => {
      void qc.invalidateQueries({ queryKey: researchKeys.run(run.run_id) })
      void qc.invalidateQueries({ queryKey: researchKeys.all })
      toast('已请求取消运行', 'success')
    },
    onError: (error) => toast(researchErrorMessage(error), 'error'),
  })
}

export function usePatchRun() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ runId, label, favorite }: { runId: string; label?: string; favorite?: boolean }) =>
      patchRun(runId, { label, favorite }),
    onSuccess: (run) => {
      qc.setQueryData(researchKeys.run(run.run_id), run)
      void qc.invalidateQueries({ queryKey: researchKeys.run(run.run_id) })
      void qc.invalidateQueries({ queryKey: researchKeys.all })
    },
    onError: (error) => toast(researchErrorMessage(error), 'error'),
  })
}

export function useLinkRunHypothesis() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ runId, hypothesisId }: { runId: string; hypothesisId: string }) =>
      linkRunHypothesis(runId, hypothesisId),
    onSuccess: (linked) => {
      void qc.invalidateQueries({ queryKey: researchKeys.run(linked.run_id) })
      void qc.invalidateQueries({ queryKey: researchKeys.hypothesesRoot })
      toast(`已关联假设 ${linked.hypothesis.title || linked.hypothesis.id}`, 'success')
    },
    onError: (error) => toast(researchErrorMessage(error), 'error'),
  })
}

export function useRerun() {
  const create = useCreateRun()
  return {
    ...create,
    rerun: (input: { factor_id: string; scope: RunScope; parameters: Record<string, unknown>; source_run_id: string }) =>
      create.mutateAsync({
        factor_id: input.factor_id,
        scope: input.scope,
        parameters: input.parameters,
        source_run_id: input.source_run_id,
      }),
  }
}
