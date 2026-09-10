import { describe, expect, it } from 'vitest'
import type { FundQuote } from '../src/shared/types'
import { FundDataService } from '../src/main/data/service'
import type { FundDataSource, SourceBatchResult, SourceQuote } from '../src/main/data/types'

class FakeSource implements FundDataSource {
  calls = 0

  constructor(
    readonly id: string,
    readonly displayName: string,
    private readonly values: Record<string, SourceQuote>,
    readonly failureThreshold?: number,
    readonly cooldownMs?: number
  ) {}

  async fetch(codes: string[]): Promise<SourceBatchResult> {
    this.calls += 1
    const quotes = new Map<string, SourceQuote>()
    const errors = new Map<string, string>()
    for (const code of codes) {
      const quote = this.values[code]
      if (quote) quotes.set(code, quote)
      else errors.set(code, 'missing')
    }
    return { quotes, errors }
  }
}

const currentQuote = (code: string, source: SourceQuote['source']): SourceQuote => ({
  code,
  name: `基金 ${code}`,
  officialNav: 1,
  officialChange: 0.5,
  officialDate: '2026-07-21',
  estimatedNav: 1.01,
  estimatedChange: 1,
  valuationTime: '2026-07-22 10:00:00',
  source
})

function serviceWith(sources: {
  legacy: FundDataSource
  tiantian: FundDataSource
  sina: FundDataSource
  official: FundDataSource
}): FundDataService {
  return new FundDataService({
    sources,
    now: () => new Date('2026-07-22T02:30:00.000Z')
  })
}

describe('FundDataService', () => {
  it('falls through legacy, Tiantian and Sina per fund without cross-fund failure', async () => {
    const legacy = new FakeSource('legacy', 'legacy', {}, 3, 1_800_000)
    const tiantian = new FakeSource('tiantian', 'tiantian', {
      '000001': currentQuote('000001', 'tiantian'),
      '000002': {
        code: '000002',
        name: '只有净值',
        officialNav: 2,
        officialChange: 0.25,
        officialDate: '2026-07-21',
        source: 'tiantian'
      }
    })
    const sina = new FakeSource('sina', 'sina', {
      '000002': currentQuote('000002', 'sina-primary')
    })
    const official = new FakeSource('official-nav', 'official', {})
    const service = serviceWith({ legacy, tiantian, sina, official })

    const result = await service.refresh(['000001', '000002'], {})
    expect(result['000001']?.source).toBe('tiantian')
    expect(result['000002']).toMatchObject({ source: 'sina-primary', officialNav: 2 })
    expect(result['000001']?.freshness).toBe('today')
  })

  it('opens the legacy circuit after three all-failed refreshes', async () => {
    const legacy = new FakeSource('legacy', 'legacy', {}, 3, 1_800_000)
    const tiantian = new FakeSource('tiantian', 'tiantian', {
      '000001': currentQuote('000001', 'tiantian')
    })
    const service = serviceWith({
      legacy,
      tiantian,
      sina: new FakeSource('sina', 'sina', {}),
      official: new FakeSource('official-nav', 'official', {})
    })

    await service.refresh(['000001'], {})
    await service.refresh(['000001'], {})
    await service.refresh(['000001'], {})
    await service.refresh(['000001'], {})

    expect(legacy.calls).toBe(3)
    expect(service.getHealth().find((item) => item.id === 'legacy')?.disabledUntil).not.toBeNull()
  })

  it('uses official NAV before a stale cached estimate', async () => {
    const officialOnly: SourceQuote = {
      code: '000001',
      name: '官方净值基金',
      officialNav: 1.23,
      officialChange: -0.4,
      officialDate: '2026-07-21',
      source: 'tiantian'
    }
    const cached: FundQuote = {
      code: '000001',
      name: '缓存基金',
      officialNav: 1.2,
      officialChange: 0.3,
      officialDate: '2026-07-20',
      estimatedNav: 1.21,
      estimatedChange: 0.8,
      valuationTime: '2026-07-20 15:00:00',
      source: 'legacy',
      freshness: 'recent',
      fetchedAt: '2026-07-20T07:00:00.000Z'
    }
    const service = serviceWith({
      legacy: new FakeSource('legacy', 'legacy', {}, 3, 1_800_000),
      tiantian: new FakeSource('tiantian', 'tiantian', { '000001': officialOnly }),
      sina: new FakeSource('sina', 'sina', {}),
      official: new FakeSource('official-nav', 'official', {})
    })
    const result = await service.refresh(['000001'], { '000001': cached })
    expect(result['000001']).toMatchObject({
      source: 'official-nav',
      freshness: 'official',
      officialNav: 1.23,
      officialChange: -0.4,
      estimatedNav: null
    })
  })

  it('marks the last successful quote as stale only when every live source fails', async () => {
    const service = serviceWith({
      legacy: new FakeSource('legacy', 'legacy', {}, 3, 1_800_000),
      tiantian: new FakeSource('tiantian', 'tiantian', {}),
      sina: new FakeSource('sina', 'sina', {}),
      official: new FakeSource('official-nav', 'official', {})
    })
    const cached: FundQuote = {
      code: '000001',
      name: '缓存基金',
      officialNav: 1,
      officialChange: 0.2,
      officialDate: '2026-07-20',
      estimatedNav: 1.01,
      estimatedChange: 1,
      valuationTime: '2026-07-20 15:00:00',
      source: 'tiantian',
      freshness: 'recent',
      fetchedAt: '2026-07-20T07:00:00.000Z'
    }
    const result = await service.refresh(['000001'], { '000001': cached })
    expect(result['000001']).toMatchObject({
      source: 'cache',
      cachedSource: 'tiantian',
      freshness: 'stale'
    })
  })
})
