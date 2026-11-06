// @vitest-environment jsdom
import { expect, it } from 'vitest'
import { buildExtColumnsParam } from './Page'

it('requests a shared concept/industry field once while preserving both display configurations', () => {
  const fields = {
    concept: { field: 'ext_gn_ths.所属概念' },
    industry: { field: 'ext_gn_ths.所属概念' },
  }
  const before = JSON.stringify(fields)
  expect(buildExtColumnsParam(fields)).toBe('ext_gn_ths.所属概念')
  expect(JSON.stringify(fields)).toBe(before)
})

it('preserves distinct fields in concept/industry order', () => {
  expect(buildExtColumnsParam({
    concept: { field: 'ext_gn_ths.所属概念' },
    industry: { field: 'ext_hy_ths.所属行业' },
  })).toBe('ext_gn_ths.所属概念,ext_hy_ths.所属行业')
})

it('keeps an optional single field and omits the parameter when no field is selected', () => {
  expect(buildExtColumnsParam({ industry: { field: 'ext_hy_ths.所属行业' } }))
    .toBe('ext_hy_ths.所属行业')
  expect(buildExtColumnsParam({ concept: { field: '' } })).toBeUndefined()
})
