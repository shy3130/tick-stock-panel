/** Each mounted live panel owns its focus; closing one must not clear another. */
const focused = new Map<symbol, string>()

export function setFocusSymbol(symbol: string): () => void {
  const owner = Symbol(symbol)
  focused.set(owner, symbol)
  return () => { focused.delete(owner) }
}

export function clearFocusSymbol(): void { focused.clear() }
export function focusSymbols(): string[] { return [...new Set(focused.values())] }
