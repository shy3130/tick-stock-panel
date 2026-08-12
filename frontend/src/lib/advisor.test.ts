import { describe, expect, it } from 'vitest'

import {
  actionPresentation,
  dataPhasePresentation,
  formatAdvisorCoverage,
  presentDailyBriefCandidate,
  presentResearchSnapshot,
  presentTrustDatasets,
  resolvePaperActionState,
  selectDailyBriefCandidates,
  selectPlanMonitorStrategyIds,
  type AdvisorActionState,
  type BeginnerDailyBriefCandidate,
  type DailyBriefDataGate,
} from './advisor'

const FORBIDDEN_INTERNAL_COPY = /\b(?:OBSERVE_ONLY|SIMULATE_ONLY|RESEARCH_ONLY|NO_CANDIDATE|MODEL_WARNING|GO1|READY|GO|WAIT|NO-GO|PASS|BLOCK)\b/

function candidate(
  symbol: string,
  researchDecision: BeginnerDailyBriefCandidate['research_decision'] = 'GO',
): BeginnerDailyBriefCandidate {
  return {
    symbol,
    name: `示例${symbol}`,
    research_decision: researchDecision,
    candidate_state: 'GO1',
    go_streak: 1,
    global_rank: 1,
    lot_size: 100,
    lot_cost: 1000,
    previous_as_of: null,
    previous_decision: null,
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
    ['OBSERVE_ONLY', '安全拦截'],
    ['SIMULATE_ONLY', '可模拟练习'],
    ['RESEARCH_ONLY', '确认第1天'],
    ['NO_CANDIDATE', '本批淘汰'],
    ['MODEL_WARNING', '模型需校准'],
  ])('maps %s to beginner-facing Chinese without exposing internal enums', (state, label) => {
    const presentation = actionPresentation(state)

    expect(presentation.label).toBe(label)
    expect(JSON.stringify(presentation)).not.toMatch(FORBIDDEN_INTERNAL_COPY)
  })
})

describe('dataPhasePresentation', () => {
  it('labels live and post-close rows as unsealed instead of stale failures', () => {
    expect(dataPhasePresentation('LIVE_PROVISIONAL', false)).toEqual({
      label: '盘中数据未封存',
      tone: 'warning',
    })
    expect(dataPhasePresentation('EOD_PENDING', false)).toEqual({
      label: '等待盘后封存',
      tone: 'warning',
    })
  })

  it('only shows success for a sealed bundle whose data gate passed', () => {
    expect(dataPhasePresentation('EOD_SEALED', true)).toEqual({
      label: '数据检查已通过',
      tone: 'success',
    })
    expect(dataPhasePresentation('EOD_SEALED', false)).toEqual({
      label: '数据检查未通过',
      tone: 'danger',
    })
  })
})

describe('resolvePaperActionState', () => {
  it('treats a background daily-brief refresh error as unknown instead of fabricating an action', () => {
    expect(resolvePaperActionState('RESEARCH_ONLY', false)).toBe('RESEARCH_ONLY')
    expect(resolvePaperActionState('RESEARCH_ONLY', true)).toBeUndefined()
  })

  it('keeps the form disabled while no daily brief has loaded', () => {
    expect(resolvePaperActionState(undefined, false)).toBeUndefined()
  })
})

describe('presentTrustDatasets', () => {
  it('keeps near-complete coverage visibly below 100%', () => {
    expect(formatAdvisorCoverage(0.999638)).toBe('99.96%')
  })

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

describe('presentResearchSnapshot', () => {
  it('shows a stable short receipt while preserving the full published id', () => {
    const fullId = 'a'.repeat(64)

    expect(presentResearchSnapshot(fullId, '2026-07-24T08:10:00+00:00')).toEqual({
      statusLabel: '已发布',
      shortId: 'aaaaaaaaaaaa',
      fullId,
      publishedAt: '2026-07-24T08:10:00+00:00',
    })
  })

  it('makes a missing publication explicit instead of inventing a receipt', () => {
    expect(presentResearchSnapshot(null, null)).toEqual({
      statusLabel: '未发布',
      shortId: '无',
      fullId: null,
      publishedAt: '未提供',
    })
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

  it('deduplicates only the strategy ids sealed into the visible plan', () => {
    const candidates = [
      {
        ...candidate('600000.SH'),
        plan_monitor: {
          status: 'PENDING' as const,
          as_of: '2026-07-27',
          last_price: null,
          change_pct: null,
          evidence: [],
          strategy_ids: ['trend_breakout', 'bullish_alignment'],
        },
      },
      {
        ...candidate('600001.SH'),
        plan_monitor: {
          status: 'PENDING' as const,
          as_of: '2026-07-27',
          last_price: null,
          change_pct: null,
          evidence: [],
          strategy_ids: ['bullish_alignment', '', 'ma_golden_cross'],
        },
      },
    ]

    expect(selectPlanMonitorStrategyIds(candidates)).toEqual([
      'trend_breakout',
      'bullish_alignment',
      'ma_golden_cross',
    ])
  })

  it('exposes the explicit confirmation state, not raw backend decisions or flag codes', () => {
    const card = presentDailyBriefCandidate(candidate('600000.SH', 'WAIT'))
    const visibleCopy = JSON.stringify(card)

    expect(card.statusLabel).toBe('确认第1天')
    expect(card.riskMessages).toEqual(['当前价格状态需要等待人工复核'])
    expect(visibleCopy).not.toMatch(FORBIDDEN_INTERNAL_COPY)
    expect(visibleCopy).not.toContain('LIMIT_UP')
    expect(visibleCopy).not.toContain('boll_breakout')
  })

  it('labels a two-day candidate as ready for simulated practice', () => {
    const card = presentDailyBriefCandidate({
      ...candidate('600000.SH'),
      candidate_state: 'READY',
      go_streak: 2,
    })

    expect(card.statusLabel).toBe('可模拟练习')
    expect(card.goStreak).toBe(2)
  })

  it.each([
    ['PENDING', '未触发', 'warning'],
    ['TRIGGERED', '已触发', 'success'],
    ['INVALIDATED', '已失效', 'danger'],
  ] as const)('localizes the intraday plan status %s', (status, label, tone) => {
    const card = presentDailyBriefCandidate({
      ...candidate('600000.SH'),
      plan_monitor: {
        status,
        as_of: '2026-07-27',
        last_price: 10.25,
        change_pct: 0.025,
        evidence: ['当前有两条独立策略继续同向确认'],
      },
    })

    expect(card.planMonitor).toEqual({
      label,
      tone,
      asOf: '2026-07-27',
      lastPrice: 10.25,
      changePct: 0.025,
      evidence: ['当前有两条独立策略继续同向确认'],
    })
    expect(JSON.stringify(card)).not.toContain(status)
  })
})
