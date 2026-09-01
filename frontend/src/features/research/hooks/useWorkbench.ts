import { useEffect, useMemo, useReducer, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { createPreflight } from '../api/preflight'
import {
  buildParameterForm,
  defaultParameters,
  structurallyValid,
  type ParameterFormModel,
} from '../model/schema'
import type { FactorDetail } from '../model/factor'
import type { RunScope } from '../model/status'
import { researchKeys } from '../queryKeys'
import { useFactorDetail } from './useResearchQueries'

interface WorkbenchState {
  scope: RunScope
  parameters: Record<string, unknown>
  revision: number
  hydratedFor: string | null
}

type Action =
  | { type: 'hydrate'; factorId: string; scope: RunScope; parameters: Record<string, unknown> }
  | { type: 'scope'; scope: RunScope }
  | { type: 'param'; name: string; value: unknown }
  | { type: 'params'; parameters: Record<string, unknown> }

function reducer(state: WorkbenchState, action: Action): WorkbenchState {
  if (action.type === 'hydrate') {
    return { scope: action.scope, parameters: action.parameters, revision: 0, hydratedFor: action.factorId }
  }
  if (action.type === 'scope') {
    return { ...state, scope: action.scope, revision: state.revision + 1 }
  }
  if (action.type === 'params') {
    return { ...state, parameters: action.parameters, revision: state.revision + 1 }
  }
  return {
    ...state,
    parameters: { ...state.parameters, [action.name]: action.value },
    revision: state.revision + 1,
  }
}

function initialScope(detail: FactorDetail | undefined): RunScope {
  if (detail?.supported_scopes.includes('symbols')) return { type: 'symbols', symbols: [] }
  return { type: 'full_market' }
}

export function useWorkbench(factorId: string | undefined) {
  const detailQuery = useFactorDetail(factorId)
  const detail = detailQuery.data
  const form: ParameterFormModel = useMemo(
    () => buildParameterForm(detail?.parameter_schema ?? null, detail?.ui_groups),
    [detail],
  )
  const [state, dispatch] = useReducer(reducer, {
    scope: { type: 'symbols', symbols: [] },
    parameters: {},
    revision: 0,
    hydratedFor: null,
  })

  useEffect(() => {
    if (!detail || state.hydratedFor === detail.id) return
    dispatch({
      type: 'hydrate',
      factorId: detail.id,
      scope: initialScope(detail),
      parameters: defaultParameters(form),
    })
  }, [detail, form, state.hydratedFor])

  const [idle, setIdle] = useState(true)
  useEffect(() => {
    setIdle(false)
    const timer = window.setTimeout(() => setIdle(true), 400)
    return () => window.clearTimeout(timer)
  }, [state.revision])

  const structureError = structurallyValid(form, state.parameters, state.scope)
  const preflightQuery = useQuery({
    queryKey: researchKeys.preflight(factorId ?? '', state.revision),
    queryFn: () => createPreflight({
      factor_id: factorId!,
      scope: state.scope,
      parameters: state.parameters,
    }),
    enabled: Boolean(factorId) && state.hydratedFor === factorId && idle && !structureError,
    retry: false,
  })

  const preflightCurrent = preflightQuery.isSuccess && idle && !structureError
  const canRun = Boolean(preflightCurrent && preflightQuery.data?.ready)

  return {
    detailQuery,
    detail,
    form,
    scope: state.scope,
    parameters: state.parameters,
    revision: state.revision,
    structureError,
    preflightQuery,
    preflightCurrent,
    canRun,
    setScope: (scope: RunScope) => dispatch({ type: 'scope', scope }),
    setParam: (name: string, value: unknown) => dispatch({ type: 'param', name, value }),
    replaceParameters: (parameters: Record<string, unknown>) => dispatch({ type: 'params', parameters }),
  }
}
