import type { QuoteSource, SourceHealth } from '@shared/types'

export interface SourceQuote {
  code: string
  name?: string
  officialNav?: number | null
  officialChange?: number | null
  officialDate?: string | null
  estimatedNav?: number | null
  estimatedChange?: number | null
  valuationTime?: string | null
  source: Exclude<QuoteSource, 'cache' | 'unavailable'>
}

export interface SourceBatchResult {
  quotes: Map<string, SourceQuote>
  errors: Map<string, string>
}

export interface FundDataSource {
  readonly id: string
  readonly displayName: string
  readonly failureThreshold?: number
  readonly cooldownMs?: number
  fetch(codes: string[]): Promise<SourceBatchResult>
}

export type HealthMap = Map<string, SourceHealth>
