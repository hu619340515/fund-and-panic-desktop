import { describe, expect, it } from 'vitest'
import {
  parseLegacyResponse,
  parsePingzhongResponse,
  parseSinaResponse,
  parseTianTianResponse
} from '../src/main/data/parsers'

describe('data source parsers', () => {
  it('parses the legacy jsonpgz payload', () => {
    const quote = parseLegacyResponse(
      'jsonpgz({"fundcode":"110022","name":"易方达消费行业股票","jzrq":"2026-07-21","dwjz":"2.9160","gsz":"2.9071","gszzl":"-0.31","gztime":"2026-07-22 10:15"});',
      '110022'
    )
    expect(quote).toMatchObject({
      code: '110022',
      officialNav: 2.916,
      officialDate: '2026-07-21',
      estimatedNav: 2.9071,
      estimatedChange: -0.31,
      source: 'legacy'
    })
  })

  it('rejects a 200-status HTML error page from the legacy endpoint', () => {
    expect(parseLegacyResponse('<!doctype html><title>页面未找到</title>', '110022')).toBeNull()
  })

  it('keeps official NAV when the Tiantian response has no estimate', () => {
    const result = parseTianTianResponse({
      success: true,
      data: [
        {
          FCODE: '000001',
          SHORTNAME: '华夏成长混合',
          NAV: 1.445,
          NAVCHGRT: -0.4,
          PDATE: '2026-07-21',
          GSZ: null,
          GSZZL: null,
          GZTIME: null
        }
      ]
    })
    expect(result.get('000001')).toMatchObject({
      name: '华夏成长混合',
      officialNav: 1.445,
      officialChange: -0.4,
      estimatedNav: null,
      estimatedChange: null
    })
  })

  it('keeps the official close return separate from the intraday estimate', () => {
    const result = parseTianTianResponse({
      success: true,
      data: [
        {
          FCODE: '013273',
          SHORTNAME: '招商沪深300地产等权重指数C',
          NAV: '0.2269',
          NAVCHGRT: '-0.40',
          PDATE: '2026-07-21',
          GSZ: '0.2263',
          GSZZL: '-0.65',
          GZTIME: '2026-07-21 15:00'
        }
      ]
    })
    expect(result.get('013273')).toMatchObject({
      officialNav: 0.2269,
      officialChange: -0.4,
      estimatedNav: 0.2263,
      estimatedChange: -0.65
    })
  })

  it('uses Sina primary fields and converts the rate to a percentage', () => {
    const quote = parseSinaResponse(
      {
        result: {
          data: {
            worth: '2.9160',
            worth_date: '20260721',
            networth: [
              {
                pre_nav: '2.9071',
                growthrate: -0.007815,
                pre_nav2: '2.8990',
                growthrate2: '-0.010580',
                pre_date: '2026-07-22',
                min_time: '10:30:00'
              }
            ]
          }
        }
      },
      '110022'
    )
    expect(quote).toMatchObject({
      source: 'sina-primary',
      estimatedNav: 2.9071,
      estimatedChange: -0.7815,
      officialDate: '2026-07-21'
    })
  })

  it('falls back to the second Sina estimate only when primary fields are incomplete', () => {
    const quote = parseSinaResponse(
      {
        result: {
          data: {
            networth: [
              {
                pre_nav: null,
                growthrate: null,
                pre_nav2: '1.2345',
                growthrate2: '0.0123',
                pre_date: '2026-07-22',
                min_time: '11:00:00'
              }
            ]
          }
        }
      },
      '000001'
    )
    expect(quote).toMatchObject({
      source: 'sina-secondary',
      estimatedNav: 1.2345,
      estimatedChange: 1.23
    })
  })

  it('parses latest official NAV from pingzhongdata without evaluating script', () => {
    const timestamp = new Date('2026-07-21T00:00:00+08:00').getTime()
    const quote = parsePingzhongResponse(
      `var fS_name = "测试基金";var Data_netWorthTrend = [{"x":${timestamp},"y":1.2345,"equityReturn":0.5}];`,
      '123456'
    )
    expect(quote).toMatchObject({
      code: '123456',
      name: '测试基金',
      officialNav: 1.2345,
      officialChange: 0.5,
      officialDate: '2026-07-21',
      source: 'official-nav'
    })
  })
})
