import { useState } from 'react'
import { InstrumentSearchInput } from '@/components/instruments/InstrumentSearchInput'
import type { InstrumentSearchResult } from '@/lib/api'

interface Props {
  onSelect: (symbol: string, name: string) => void
}

/**
 * 财务与个股分析的主标的入口。
 * 搜索行为由共享控件统一：代码、名称、全拼和简拼均可选择。
 */
export function StockFinancialSearch({ onSelect }: Props) {
  const [query, setQuery] = useState('')

  const handleSelect = (result: InstrumentSearchResult) => {
    onSelect(result.symbol, result.name)
  }

  return (
    <InstrumentSearchInput
      value={query}
      onChange={setQuery}
      onSelect={handleSelect}
      clearOnSelect
      placeholder="输入代码、名称或拼音，如 600000 / 浦发 / pfy"
      ariaLabel="个股搜索"
      className="mx-auto w-full max-w-xl"
      inputClassName="h-11 w-full rounded-card border border-border bg-surface pr-10 text-sm text-foreground placeholder:text-muted transition-colors focus:border-accent/50 focus:bg-base focus:outline-none"
    />
  )
}
