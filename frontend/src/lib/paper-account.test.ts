import { describe, expect, it } from 'vitest'

import {
  createPaperTradeDraft,
  lotGuidance,
  paperMutationErrorMessage,
  preparePaperTradeDraftForSubmit,
  toPaperTradeRequest,
  validatePaperTradeDraft,
  type PaperTradeDraft,
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
      '数据检查尚未通过，当前只能观察，不能记录模拟成交。下一步：先刷新数据并处理可信度问题。',
    ])
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
    expect(validatePaperTradeDraft(validDraft({ quantity: '150' }), 'RESEARCH_ONLY')).toContain(
      '普通股票的模拟数量必须是 100 股的整数倍。',
    )
    expect(validatePaperTradeDraft(validDraft({
      symbol: '688001.SH',
      quantity: '100',
    }), 'RESEARCH_ONLY')).toContain(
      '科创板模拟数量至少为 200 股。',
    )
  })

  it('warns before a name longer than the backend 80-character limit is submitted', () => {
    expect(validatePaperTradeDraft(validDraft({ name: '示'.repeat(81) }), 'SIMULATE_ONLY')).toContain(
      '股票名称不能超过 80 个字符。',
    )
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
