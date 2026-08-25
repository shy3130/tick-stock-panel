export interface NavPresentation {
  isCandidateExecution: boolean
  navLabel: string
  navAxisLabel: string
  allowsBenchmark: boolean
}

/**
 * 将候选独立执行与资金受约束账户回测的展示口径隔离。
 * 候选样本曲线不是可交易账户净值，不能与基准同图比较。
 */
export function getNavPresentation(fullKind: unknown): NavPresentation {
  const isCandidateExecution = fullKind === 'candidate_execution'

  if (isCandidateExecution) {
    return {
      isCandidateExecution: true,
      navLabel: '候选样本曲线',
      navAxisLabel: '样本净值',
      allowsBenchmark: false,
    }
  }

  return {
    isCandidateExecution: false,
    navLabel: '策略净值',
    navAxisLabel: '策略资金',
    allowsBenchmark: true,
  }
}
