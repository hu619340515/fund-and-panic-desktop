import type { FundQuote, QuoteSource, SourceHealth } from '@shared/types'
import { normalizeCompactDate } from './parsers'
import { hasEstimate } from './sources'
import type { FundDataSource, SourceBatchResult, SourceQuote } from './types'

interface SourceSet {
  legacy: FundDataSource
  tiantian: FundDataSource
  sina: FundDataSource
  official: FundDataSource
}

interface ServiceOptions {
  sources: SourceSet
  initialHealth?: SourceHealth[]
  now?: () => Date
  onHealthChanged?: (health: SourceHealth[]) => void
}

function shanghaiDate(date: Date): string {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit'
  }).format(date)
}

function iso(date: Date): string {
  return date.toISOString()
}

function isFiniteValue(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function mergeMetadata(existing: SourceQuote | undefined, incoming: SourceQuote): SourceQuote {
  if (!existing) return { ...incoming }
  const merged: SourceQuote = { ...existing }
  if (!merged.name && incoming.name) merged.name = incoming.name

  const currentDate = merged.officialDate ?? ''
  const incomingDate = incoming.officialDate ?? ''
  if (
    isFiniteValue(incoming.officialNav) &&
    (!isFiniteValue(merged.officialNav) || incomingDate > currentDate)
  ) {
    merged.officialNav = incoming.officialNav
    merged.officialChange = isFiniteValue(incoming.officialChange) ? incoming.officialChange : null
    merged.officialDate = incoming.officialDate ?? null
  } else if (
    incomingDate === currentDate &&
    isFiniteValue(incoming.officialChange) &&
    !isFiniteValue(merged.officialChange)
  ) {
    merged.officialChange = incoming.officialChange
  }
  return merged
}

export class FundDataService {
  private readonly sources: SourceSet
  private readonly health = new Map<string, SourceHealth>()
  private readonly now: () => Date
  private readonly onHealthChanged?: (health: SourceHealth[]) => void

  constructor(options: ServiceOptions) {
    this.sources = options.sources
    this.now = options.now ?? (() => new Date())
    this.onHealthChanged = options.onHealthChanged

    const initial = new Map((options.initialHealth ?? []).map((item) => [item.id, item]))
    for (const source of Object.values(this.sources)) {
      const stored = initial.get(source.id)
      this.health.set(source.id, {
        id: source.id,
        displayName: source.displayName,
        consecutiveFailures: stored?.consecutiveFailures ?? 0,
        disabledUntil: stored?.disabledUntil ?? null,
        lastSuccessAt: stored?.lastSuccessAt ?? null,
        lastFailureAt: stored?.lastFailureAt ?? null
      })
    }
  }

  getHealth(): SourceHealth[] {
    return Array.from(this.health.values()).map((item) => ({ ...item }))
  }

  private notifyHealth(): void {
    this.onHealthChanged?.(this.getHealth())
  }

  private canUse(source: FundDataSource): boolean {
    const state = this.health.get(source.id)
    if (!state?.disabledUntil) return true
    if (new Date(state.disabledUntil).getTime() <= this.now().getTime()) {
      state.disabledUntil = null
      state.consecutiveFailures = 0
      this.notifyHealth()
      return true
    }
    return false
  }

  private recordSuccess(source: FundDataSource): void {
    const state = this.health.get(source.id)
    if (!state) return
    state.consecutiveFailures = 0
    state.disabledUntil = null
    state.lastSuccessAt = iso(this.now())
    this.notifyHealth()
  }

  private recordFailure(source: FundDataSource): void {
    const state = this.health.get(source.id)
    if (!state) return
    state.consecutiveFailures += 1
    state.lastFailureAt = iso(this.now())
    if (
      source.failureThreshold &&
      source.cooldownMs &&
      state.consecutiveFailures >= source.failureThreshold
    ) {
      state.disabledUntil = iso(new Date(this.now().getTime() + source.cooldownMs))
    }
    this.notifyHealth()
  }

  private async runSource(source: FundDataSource, codes: string[]): Promise<SourceBatchResult> {
    if (codes.length === 0 || !this.canUse(source)) {
      return { quotes: new Map(), errors: new Map() }
    }
    try {
      const result = await source.fetch(codes)
      if (result.quotes.size > 0) this.recordSuccess(source)
      else this.recordFailure(source)
      return result
    } catch (error) {
      this.recordFailure(source)
      const message = error instanceof Error ? error.message : String(error)
      return {
        quotes: new Map(),
        errors: new Map(codes.map((code) => [code, message]))
      }
    }
  }

  async refresh(codes: string[], cachedQuotes: Record<string, FundQuote>): Promise<Record<string, FundQuote>> {
    const uniqueCodes = [...new Set(codes.filter((code) => /^\d{6}$/.test(code)))]
    const metadata = new Map<string, SourceQuote>()
    const estimates = new Map<string, SourceQuote>()

    const absorb = (quote: SourceQuote): void => {
      metadata.set(quote.code, mergeMetadata(metadata.get(quote.code), quote))
      if (!estimates.has(quote.code) && hasEstimate(quote)) estimates.set(quote.code, quote)
    }

    const legacy = await this.runSource(this.sources.legacy, uniqueCodes)
    legacy.quotes.forEach(absorb)

    // The batch source is also the cheapest reliable source of the official
    // daily return. Query all funds while retaining the legacy estimate as
    // first priority when it is available.
    const tiantian = await this.runSource(this.sources.tiantian, uniqueCodes)
    tiantian.quotes.forEach(absorb)

    const missingAfterTianTian = uniqueCodes.filter((code) => !estimates.has(code))
    const sina = await this.runSource(this.sources.sina, missingAfterTianTian)
    sina.quotes.forEach(absorb)

    const needOfficial = uniqueCodes.filter((code) => {
      const quote = metadata.get(code)
      return !quote?.name || !isFiniteValue(quote.officialNav) || !isFiniteValue(quote.officialChange)
    })
    const official = await this.runSource(this.sources.official, needOfficial)
    official.quotes.forEach(absorb)

    const fetchedAt = iso(this.now())
    const today = shanghaiDate(this.now())
    const output: Record<string, FundQuote> = {}

    for (const code of uniqueCodes) {
      const meta = metadata.get(code)
      const estimate = estimates.get(code)
      const cached = cachedQuotes[code]
      const name = meta?.name || cached?.name || `基金 ${code}`

      if (estimate && hasEstimate(estimate)) {
        const valuationDate = normalizeCompactDate(estimate.valuationTime)
        output[code] = {
          code,
          name,
          officialNav: isFiniteValue(meta?.officialNav) ? meta.officialNav : null,
          officialChange: isFiniteValue(meta?.officialChange) ? meta.officialChange : null,
          officialDate: meta?.officialDate ?? null,
          estimatedNav: estimate.estimatedNav ?? null,
          estimatedChange: estimate.estimatedChange ?? null,
          valuationTime: estimate.valuationTime ?? null,
          source: estimate.source,
          freshness: valuationDate === today ? 'today' : 'recent',
          fetchedAt
        }
        continue
      }

      if (meta && isFiniteValue(meta.officialNav)) {
        output[code] = {
          code,
          name,
          officialNav: meta.officialNav,
          officialChange: isFiniteValue(meta.officialChange) ? meta.officialChange : null,
          officialDate: meta.officialDate ?? null,
          estimatedNav: null,
          estimatedChange: null,
          valuationTime: null,
          source: 'official-nav',
          freshness: 'official',
          fetchedAt,
          error: '当前没有可用的盘中估值'
        }
        continue
      }

      if (cached && cached.freshness !== 'unavailable') {
        const cachedSource: QuoteSource = cached.source === 'cache' ? cached.cachedSource ?? 'unavailable' : cached.source
        output[code] = {
          ...cached,
          code,
          name,
          source: 'cache',
          cachedSource,
          freshness: 'stale',
          error: '网络数据不可用，显示最后成功数据'
        }
        continue
      }

      output[code] = {
        code,
        name,
        officialNav: null,
        officialChange: null,
        officialDate: null,
        estimatedNav: null,
        estimatedChange: null,
        valuationTime: null,
        source: 'unavailable',
        freshness: 'unavailable',
        fetchedAt,
        error: '暂时无法获取基金数据'
      }
    }
    return output
  }
}
