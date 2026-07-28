import { describe, expect, it } from 'vitest'

import {
  actionPresentation,
  presentDailyBriefCandidate,
  presentTrustDatasets,
  resolvePaperActionState,
  selectDailyBriefCandidates,
  type AdvisorActionState,
  type BeginnerDailyBriefCandidate,
  type DailyBriefDataGate,
} from './advisor'

const FORBIDDEN_INTERNAL_COPY = /\b(?:OBSERVE_ONLY|SIMULATE_ONLY|RESEARCH_ONLY|GO|WAIT|NO-GO|PASS|BLOCK)\b/

function candidate(
  symbol: string,
  researchDecision: BeginnerDailyBriefCandidate['research_decision'] = 'GO',
): BeginnerDailyBriefCandidate {
  return {
    symbol,
    name: `示例${symbol}`,
    research_decision: researchDecision,
    deterministic_reasons: ['两项独立规则得到同向结果'],
    observation_conditions: ['下次复核时数据仍然完整'],
    invalidation_conditions: ['下次复核出现硬风险标记'],
    risk_flags: [
      {
        code: 'LIMIT_UP',
        message: '当前价格状态需要等待人工复核',
      },
    ],
  }
}

function gateFixture(): DailyBriefDataGate {
  return {
    decision: 'BLOCK',
    provider: 'tickflow',
    coverage_ratio: 0.92,
    observed_end: '2026-07-28',
    reasons: ['股票清单覆盖不足'],
    next_actions: ['重新同步股票清单'],
    datasets: {
      instruments: {
        status: 'partial',
        provider: 'tickflow-master',
        coverage_ratio: 0.92,
        observed_start: '2026-07-28',
        observed_end: '2026-07-28',
        reasons: ['缺少 378 只股票'],
        next_actions: ['重新同步股票清单'],
      },
      daily: {
        status: 'ok',
        provider: 'tickflow-market',
        coverage_ratio: 0.99,
        observed_start: '2026-01-01',
        observed_end: '2026-07-28',
        reasons: [],
        next_actions: [],
      },
      adj_factor: {
        status: 'ok',
        provider: 'tickflow-factor',
        coverage_ratio: 0.99,
        observed_start: '2020-01-01',
        observed_end: '2026-07-28',
        reasons: [],
        next_actions: [],
      },
      daily_enriched: {
        status: 'invalid',
        provider: 'derived',
        coverage_ratio: 0.75,
        observed_start: '2026-01-01',
        observed_end: '2026-07-28',
        reasons: ['衍生日线缺失率 25%'],
        next_actions: ['重新生成衍生日线'],
      },
    },
  }
}

describe('actionPresentation', () => {
  it.each<[AdvisorActionState, string]>([
    ['OBSERVE_ONLY', '只观察'],
    ['SIMULATE_ONLY', '只模拟'],
    ['RESEARCH_ONLY', '可研究'],
  ])('maps %s to beginner-facing Chinese without exposing internal enums', (state, label) => {
    const presentation = actionPresentation(state)

    expect(presentation.label).toBe(label)
    expect(JSON.stringify(presentation)).not.toMatch(FORBIDDEN_INTERNAL_COPY)
  })
})

describe('resolvePaperActionState', () => {
  it('fails closed when a background daily-brief refresh errors after a research state', () => {
    expect(resolvePaperActionState('RESEARCH_ONLY', false)).toBe('RESEARCH_ONLY')
    expect(resolvePaperActionState('RESEARCH_ONLY', true)).toBe('OBSERVE_ONLY')
  })

  it('keeps the form disabled while no daily brief has loaded', () => {
    expect(resolvePaperActionState(undefined, false)).toBeUndefined()
  })
})

describe('presentTrustDatasets', () => {
  it('keeps the four backend receipts in a fixed beginner-readable order', () => {
    const rows = presentTrustDatasets(gateFixture())

    expect(rows.map(row => row.label)).toEqual([
      '股票清单',
      '日线行情',
      '复权因子',
      '衍生日线',
    ])
    expect(rows.map(row => row.provider)).toEqual([
      'tickflow-master',
      'tickflow-market',
      'tickflow-factor',
      'derived',
    ])
  })

  it('preserves exact backend reasons and next actions instead of recomputing trust', () => {
    const rows = presentTrustDatasets(gateFixture())

    expect(rows[0].reasons).toEqual(['缺少 378 只股票'])
    expect(rows[0].nextActions).toEqual(['重新同步股票清单'])
    expect(rows[3].reasons).toEqual(['衍生日线缺失率 25%'])
    expect(rows[3].nextActions).toEqual(['重新生成衍生日线'])
  })
})

describe('daily brief candidate presentation', () => {
  it('limits the beginner flow to three candidates in backend order', () => {
    const candidates = ['1', '2', '3', '4'].map(symbol => candidate(symbol))

    expect(selectDailyBriefCandidates(candidates).map(item => item.symbol)).toEqual([
      '1',
      '2',
      '3',
    ])
  })

  it('exposes only localized card copy, not backend decisions, flag codes, or strategy ids', () => {
    const card = presentDailyBriefCandidate(candidate('600000.SH', 'WAIT'))
    const visibleCopy = JSON.stringify(card)

    expect(card.statusLabel).toBe('等待更多确认')
    expect(card.riskMessages).toEqual(['当前价格状态需要等待人工复核'])
    expect(visibleCopy).not.toMatch(FORBIDDEN_INTERNAL_COPY)
    expect(visibleCopy).not.toContain('LIMIT_UP')
    expect(visibleCopy).not.toContain('boll_breakout')
  })
})
