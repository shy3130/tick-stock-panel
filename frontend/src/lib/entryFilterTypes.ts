export type MarketFilterSource = 'off' | 'builtin' | 'extension'

export interface ExtensionEntryCondition {
  table_id: string
  field: string
  op: '>' | '>=' | '<' | '<=' | '==' | '!=' | 'contains'
  value: string
}

export interface EntryFilters {
  market: ExtensionEntryCondition[]
  stocks: ExtensionEntryCondition[]
}
