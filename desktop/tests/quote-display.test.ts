import { describe, expect, it } from 'vitest'
import { primaryChange } from '../src/renderer/src/quote-display'
import type { FundQuote } from '../src/shared/types'

function quote(overrides: Partial<FundQuote> = {}): FundQuote {
  return {
    code: '013273',
    name: '测试基金',
    officialNav: 0.2269,
    officialChange: -0.4,
    officialDate: '2026-07-21',
    estimatedNav: 0.2263,
    estimatedChange: -0.65,
    valuationTime: '2026-07-21 15:00',
    source: 'tiantian',
    freshness: 'recent',
    fetchedAt: '2026-07-22T01:00:00.000Z',
    ...overrides
  }
}

describe('compact quote display', () => {
  it('shows the official close when it covers the latest estimate date', () => {
    expect(primaryChange(quote())).toMatchObject({ label: '收盘', value: -0.4 })
  })

  it('shows a current intraday estimate while the official NAV is from yesterday', () => {
    expect(
      primaryChange(
        quote({
          officialDate: '2026-07-21',
          valuationTime: '2026-07-22 10:30',
          freshness: 'today'
        })
      )
    ).toMatchObject({ label: '实时估', value: -0.65 })
  })

  it('labels a historical estimate when no official daily return is available', () => {
    expect(primaryChange(quote({ officialChange: null }))).toMatchObject({
      label: '旧估',
      value: -0.65
    })
  })
})
