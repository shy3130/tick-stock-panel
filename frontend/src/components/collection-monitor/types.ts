export type EvidenceState = 'live' | 'cached' | 'unavailable'
export type HealthState = 'green' | 'yellow' | 'red' | 'gray' | 'unavailable'
export type CollectorMode = 'production' | 'shadow' | 'backfill'
export type CollectorTechnology = 'rust' | 'websocket' | 'python' | 'batch'
export type MarketKey = 'cn' | 'hk' | 'us'
export type DatasetKey =
  | 'capital_distribution'
  | 'capital_flow'
  | 'candlestick_1m'
  | 'depth'
  | 'trades'

export interface LastConfirmedEvidence {
  evidenceAt: string
  expectedCount?: number
  collectedCount?: number
  freshCount?: number
  staleCount?: number
  missingCount?: number
  latestDataAt?: string
  provenance?: string
}

export interface EvidenceEnvelope {
  evidenceState: EvidenceState
  evidenceAt: string | null
  lastConfirmed?: LastConfirmedEvidence
}

export interface CollectionMonitorOverview extends EvidenceEnvelope {
  tradeDate?: string
  taskCount?: number
  productionHealthyCount?: number
  unhealthyTaskCount?: number
  openGapCount?: number
}

export interface DatasetCollectionEvidence extends EvidenceEnvelope {
  market: MarketKey
  datasetKey?: DatasetKey
  dataset?: DatasetKey
  taskHealth?: HealthState
  dataHealth?: HealthState
  displayState?: HealthState
  status?: HealthState
  expectedCount?: number
  collectedCount?: number
  freshCount?: number
  staleCount?: number
  missingCount?: number
  duplicateCount?: number
  latestDataAt?: string
  provenance?: string
}

export interface MarketCollectionEvidence extends EvidenceEnvelope {
  market: MarketKey
  tradeDate?: string
  datasets: DatasetCollectionEvidence[]
}

export interface CollectionTask extends EvidenceEnvelope {
  taskKey: string
  technology?: CollectorTechnology
  mode?: CollectorMode
  status: HealthState
  markets?: MarketKey[]
  datasets?: DatasetKey[]
  heartbeatAt?: string
  lastSuccessAt?: string
  lastWriteAt?: string
  rowsWritten?: number
  rowsFailed?: number
  retryCount?: number
  queueDepth?: number
  provenance?: string
}

export interface CollectionTaskPage extends EvidenceEnvelope {
  tradeDate: string
  total?: number
  limit?: number
  offset?: number
  tasks: CollectionTask[]
}

export interface CollectionGap extends EvidenceEnvelope {
  market?: MarketKey
  datasetKey?: DatasetKey
  symbol: string
  startMinute: string
  endMinute?: string
  expectedCount?: number
  missingCount?: number
  gapState?: 'open' | 'recovered'
  provenance?: string
}

export interface CollectionGapPage extends EvidenceEnvelope {
  tradeDate: string
  market: MarketKey
  datasetKey: DatasetKey
  total?: number
  limit?: number
  offset?: number
  gaps: CollectionGap[]
}

export interface CollectionMonitorFilters {
  date: string
  market: MarketKey
  dataset: DatasetKey
  status?: HealthState
  technology?: CollectorTechnology
  mode?: CollectorMode
}
