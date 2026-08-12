import { describe, expect, it } from 'vitest'

import {
  canRecordPaperTrade,
  createPaperTradeDraft,
  lotGuidance,
  paperMutationErrorMessage,
  portfolioRiskPresentation,
  preparePaperTradeDraftForSubmit,
  toPaperTradeRequest,
  validatePaperTradeDraft,
  type PaperTradeDraft,
  type PaperPortfolioRisk,
} from './paper-account'

function validDraft(overrides: Partial<PaperTradeDraft> = {}): PaperTradeDraft {
  return {
    symbol: '600000.SH',
    name: '浦发银行',
    side: 'BUY',
    quantity: '100',
    price: '10.25',
    trade_date: '2026-07-29',
    plan_note: '仅验证规则是否执行',
    invalidation_note: '数据门禁再次异常',
    ...overrides,
  }
}

describe('lotGuidance', () => {
  it('explains ordinary and STAR Market simulated lot constraints', () => {
    expect(lotGuidance('600000.SH', 'BUY')).toBe('普通股票：100 股的整数倍')
    expect(lotGuidance('688001.SH', 'BUY')).toBe('科创板：至少 200 股，之后按 100 股递增')
    expect(lotGuidance('688001.SH', 'SELL')).toBe('模拟卖出以账户显示的可卖数量为准（T+1）')
  })
})

describe('validatePaperTradeDraft', () => {
  it('blocks the simulated-fill form while the backend daily action is observe-only', () => {
    expect(validatePaperTradeDraft(validDraft(), 'OBSERVE_ONLY')).toEqual([
      '今日安全检查未通过，不能记录模拟买入。下一步：先处理页面显示的数据或市场问题。',
    ])
  })

  it('blocks simulated buys while the candidate is only on its first confirmation day', () => {
    expect(validatePaperTradeDraft(validDraft(), 'RESEARCH_ONLY')).toEqual([
      '候选只完成第 1 个确认日，不能记录模拟买入。下一步：等待下一可信交易日复核。',
    ])
  })

  it('never traps an existing simulated position behind the daily buy gate', () => {
    expect(validatePaperTradeDraft(validDraft({ side: 'SELL' }), 'OBSERVE_ONLY')).toEqual([])
    expect(validatePaperTradeDraft(validDraft({ side: 'SELL' }), 'RESEARCH_ONLY')).toEqual([])
    expect(validatePaperTradeDraft(validDraft({ side: 'SELL' }), 'NO_CANDIDATE')).toEqual([])
    expect(validatePaperTradeDraft(validDraft({ side: 'SELL' }), 'MODEL_WARNING')).toEqual([])
  })

  it('gives beginner-readable required-field guidance', () => {
    expect(validatePaperTradeDraft(validDraft({
      symbol: '',
      name: '',
      quantity: '',
      price: '',
      trade_date: '',
    }), 'SIMULATE_ONLY')).toEqual([
      '请填写带交易所后缀的六位股票代码，例如 600000.SH。',
      '请填写股票名称。',
      '模拟数量必须是大于 0 的整数股数。',
      '模拟成交价必须是大于 0 的有限数值。',
      '请填写模拟成交日期。',
    ])
  })

  it('guides ordinary and STAR Market lot sizes without replacing backend validation', () => {
    expect(validatePaperTradeDraft(validDraft({ quantity: '150' }), 'SIMULATE_ONLY')).toContain(
      '普通股票的模拟数量必须是 100 股的整数倍。',
    )
    expect(validatePaperTradeDraft(validDraft({
      symbol: '688001.SH',
      quantity: '100',
    }), 'SIMULATE_ONLY')).toContain(
      '科创板模拟数量至少为 200 股。',
    )
  })

  it('warns before a name longer than the backend 80-character limit is submitted', () => {
    expect(validatePaperTradeDraft(validDraft({ name: '示'.repeat(81) }), 'SIMULATE_ONLY')).toContain(
      '股票名称不能超过 80 个字符。',
    )
  })
})

describe('canRecordPaperTrade', () => {
  it('enables simulated buys only for ready candidates and always permits sell validation', () => {
    expect(canRecordPaperTrade('SIMULATE_ONLY', 'BUY')).toBe(true)
    expect(canRecordPaperTrade('RESEARCH_ONLY', 'BUY')).toBe(false)
    expect(canRecordPaperTrade('NO_CANDIDATE', 'BUY')).toBe(false)
    expect(canRecordPaperTrade('MODEL_WARNING', 'BUY')).toBe(false)
    expect(canRecordPaperTrade('OBSERVE_ONLY', 'BUY')).toBe(false)
    expect(canRecordPaperTrade(undefined, 'BUY')).toBe(false)
    expect(canRecordPaperTrade('OBSERVE_ONLY', 'SELL')).toBe(true)
    expect(canRecordPaperTrade(undefined, 'SELL')).toBe(true)
  })
})

describe('toPaperTradeRequest', () => {
  it('converts a validated draft to the exact backend payload shape', () => {
    expect(toPaperTradeRequest(validDraft({ side: 'SELL', quantity: '200' }))).toEqual({
      symbol: '600000.SH',
      name: '浦发银行',
      side: 'SELL',
      quantity: 200,
      price: 10.25,
      trade_date: '2026-07-29',
      plan_note: '仅验证规则是否执行',
      invalidation_note: '数据门禁再次异常',
    })
  })

  it('preserves accepted name and notes verbatim while normalizing only the symbol', () => {
    expect(toPaperTradeRequest(validDraft({
      symbol: ' 600000.sh ',
      name: '  浦发银行  ',
      plan_note: '  保留计划原文  ',
      invalidation_note: '  保留失效条件原文  ',
    }))).toMatchObject({
      symbol: '600000.SH',
      name: '  浦发银行  ',
      plan_note: '  保留计划原文  ',
      invalidation_note: '  保留失效条件原文  ',
    })
  })
})

describe('dynamic local trade date', () => {
  it('refreshes an untouched default across local midnight before submission', () => {
    const beforeMidnight = new Date(2026, 6, 29, 23, 59)
    const afterMidnight = new Date(2026, 6, 30, 0, 1)
    const draft = createPaperTradeDraft(beforeMidnight)

    expect(draft.trade_date).toBe('2026-07-29')
    expect(preparePaperTradeDraftForSubmit(draft, false, afterMidnight).trade_date).toBe(
      '2026-07-30',
    )
  })

  it('preserves a manually selected trade date across midnight', () => {
    const draft = createPaperTradeDraft(new Date(2026, 6, 29, 23, 59))
    draft.trade_date = '2026-07-20'

    expect(preparePaperTradeDraftForSubmit(
      draft,
      true,
      new Date(2026, 6, 30, 0, 1),
    ).trade_date).toBe('2026-07-20')
  })
})

describe('paperMutationErrorMessage', () => {
  it('preserves reset and trade backend errors that already include a next step', () => {
    const resetMessage = '模拟账户无法重置。下一步：请刷新账户后重试。'
    const tradeMessage = '模拟账户现金不足。下一步：请降低数量后重试。'

    expect(paperMutationErrorMessage(new Error(resetMessage), 'reset')).toBe(resetMessage)
    expect(paperMutationErrorMessage(new Error(tradeMessage), 'trade')).toBe(tradeMessage)
  })

  it('appends operation-specific next steps to Chinese backend errors without remediation', () => {
    const backendMessage = '模拟账户保存失败, 本次操作未确认写入。'

    expect(paperMutationErrorMessage(new Error(backendMessage), 'reset')).toBe(
      `${backendMessage}下一步：请刷新账户确认当前状态，确认尚未重置后再重新提交。`,
    )
    expect(paperMutationErrorMessage(new Error(backendMessage), 'trade')).toBe(
      `${backendMessage}下一步：请刷新账户及最近记录确认结果，确认尚未记录后再重试。`,
    )
  })

  it('maps network and unknown failures to a Chinese reason and next action', () => {
    expect(paperMutationErrorMessage(new TypeError('Failed to fetch'), 'trade')).toBe(
      '无法连接本地服务，模拟成交尚未确认记录。下一步：检查应用服务是否运行，刷新账户后再重试。',
    )
    expect(paperMutationErrorMessage(undefined, 'reset')).toBe(
      '无法连接本地服务，账户重置尚未确认。下一步：检查应用服务是否运行，再重新提交。',
    )
  })
})

describe('portfolioRiskPresentation', () => {
  it('explains an empty account without inventing concentration risk', () => {
    const risk: PaperPortfolioRisk = {
      position_count: 0,
      cash_pct: 100,
      invested_pct: 0,
      largest_position_pct: 0,
      largest_invested_position_pct: 0,
      concentration_hhi: 0,
      concentration_level: 'NONE',
      warnings: [],
    }

    expect(portfolioRiskPresentation(risk)).toEqual({
      label: '暂无持仓',
      tone: 'neutral',
      detail: '已用资金 0.00% · 现金 100.00%',
    })
  })

  it('makes single-stock concentration explicit even when most cash is unused', () => {
    const risk: PaperPortfolioRisk = {
      position_count: 1,
      cash_pct: 89.95,
      invested_pct: 10.05,
      largest_position_pct: 10.05,
      largest_invested_position_pct: 100,
      concentration_hhi: 1,
      concentration_level: 'EXTREME',
      warnings: [
        {
          code: 'SINGLE_POSITION_CONCENTRATION',
          message: '当前持仓内部100%集中于单一股票',
        },
      ],
    }

    expect(portfolioRiskPresentation(risk)).toEqual({
      label: '极高集中',
      tone: 'danger',
      detail: '已用资金 10.05% · 最大单票占已投资部分 100.00%',
    })
  })
})
