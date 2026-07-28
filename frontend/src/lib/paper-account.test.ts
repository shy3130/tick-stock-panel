import { describe, expect, it } from 'vitest'

import {
  lotGuidance,
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
