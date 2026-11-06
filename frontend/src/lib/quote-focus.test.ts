import { afterEach, expect, it } from 'vitest'
import { clearFocusSymbol, focusSymbols, setFocusSymbol } from './quote-focus'

afterEach(clearFocusSymbol)
it('deduplicates the same symbol and releases only its own panel', () => {
  const first = setFocusSymbol('000985.SH')
  const second = setFocusSymbol('000985.SH')
  const etf = setFocusSymbol('159558.SZ')
  expect(focusSymbols()).toEqual(['000985.SH', '159558.SZ'])
  first()
  expect(focusSymbols()).toContain('000985.SH')
  etf()
  expect(focusSymbols()).toEqual(['000985.SH'])
  second()
  expect(focusSymbols()).toEqual([])
})
