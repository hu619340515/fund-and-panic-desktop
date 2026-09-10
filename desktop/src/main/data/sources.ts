import { fetchJson, fetchText, mapConcurrent } from './http'
import {
  parseLegacyResponse,
  parsePingzhongResponse,
  parseSinaResponse,
  parseTianTianResponse
} from './parsers'
import type { FundDataSource, SourceBatchResult, SourceQuote } from './types'

function messageFromError(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function emptyBatch(): SourceBatchResult {
  return { quotes: new Map(), errors: new Map() }
}

export class LegacyFundGzSource implements FundDataSource {
  readonly id = 'legacy'
  readonly displayName = '天天基金旧接口'
  readonly failureThreshold = 3
  readonly cooldownMs = 30 * 60 * 1000

  async fetch(codes: string[]): Promise<SourceBatchResult> {
    const result = emptyBatch()
    const settled = await mapConcurrent(codes, 8, async (code) => {
      const url = `https://fundgz.1234567.com.cn/js/${code}.js?rt=${Date.now()}`
      const text = await fetchText(url, {
        timeoutMs: 8_000,
        headers: { Referer: 'https://fund.eastmoney.com/' }
      })
      const quote = parseLegacyResponse(text, code)
      if (!quote) throw new Error('接口未返回有效估值')
      return quote
    })

    settled.forEach((entry, index) => {
      const code = codes[index]
      if (!code) return
      if (entry.status === 'fulfilled') result.quotes.set(code, entry.value)
      else result.errors.set(code, messageFromError(entry.reason))
    })
    return result
  }
}

interface TianTianResponse {
  success?: boolean
  data?: Array<Record<string, unknown>>
}

export class TianTianBatchSource implements FundDataSource {
  readonly id = 'tiantian'
  readonly displayName = '天天基金新接口'

  async fetch(codes: string[]): Promise<SourceBatchResult> {
    const result = emptyBatch()
    const chunks: string[][] = []
    for (let index = 0; index < codes.length; index += 50) {
      chunks.push(codes.slice(index, index + 50))
    }

    const settled = await Promise.allSettled(
      chunks.map(async (chunk) => {
        const fields = 'FCODE,SHORTNAME,GSZZL,GZTIME,GSZ,NAV,NAVCHGRT,PDATE'
        const url =
          'https://fundcomapi.tiantianfunds.com/mm/newCore/FundValuationLast' +
          `?FCODES=${encodeURIComponent(chunk.join(','))}&FIELDS=${encodeURIComponent(fields)}`
        const payload = await fetchJson<TianTianResponse>(url, {
          timeoutMs: 8_000,
          headers: { Referer: 'https://fund.eastmoney.com/' }
        })
        const parsed = parseTianTianResponse(payload)
        return { chunk, parsed }
      })
    )

    settled.forEach((entry, index) => {
      const chunk = chunks[index] ?? []
      if (entry.status === 'rejected') {
        for (const code of chunk) result.errors.set(code, messageFromError(entry.reason))
        return
      }
      for (const code of chunk) {
        const quote = entry.value.parsed.get(code)
        if (quote) result.quotes.set(code, quote)
        else result.errors.set(code, '新接口没有返回该基金')
      }
    })
    return result
  }
}

export class SinaEstimateSource implements FundDataSource {
  readonly id = 'sina'
  readonly displayName = '新浪估值'

  async fetch(codes: string[]): Promise<SourceBatchResult> {
    const result = emptyBatch()
    const settled = await mapConcurrent(codes, 6, async (code) => {
      const url =
        'https://stock.finance.sina.com.cn/fundInfo/api/openapi.php/' +
        `FdFundService.getEstimateNetworthPic?symbol=${encodeURIComponent(code)}`
      const payload = await fetchJson<unknown>(url, {
        timeoutMs: 10_000,
        headers: { Referer: 'https://finance.sina.com.cn/' }
      })
      const quote = parseSinaResponse(payload, code)
      if (!quote) throw new Error('新浪接口没有有效估值')
      return quote
    })

    settled.forEach((entry, index) => {
      const code = codes[index]
      if (!code) return
      if (entry.status === 'fulfilled') result.quotes.set(code, entry.value)
      else result.errors.set(code, messageFromError(entry.reason))
    })
    return result
  }
}

export class EastMoneyOfficialNavSource implements FundDataSource {
  readonly id = 'official-nav'
  readonly displayName = '东方财富官方净值'

  async fetch(codes: string[]): Promise<SourceBatchResult> {
    const result = emptyBatch()
    const settled = await mapConcurrent(codes, 4, async (code) => {
      const url = `https://fund.eastmoney.com/pingzhongdata/${code}.js?v=${Date.now()}`
      const text = await fetchText(url, {
        timeoutMs: 12_000,
        headers: { Referer: `https://fund.eastmoney.com/${code}.html` }
      })
      const quote = parsePingzhongResponse(text, code)
      if (!quote) throw new Error('未找到官方净值')
      return quote
    })

    settled.forEach((entry, index) => {
      const code = codes[index]
      if (!code) return
      if (entry.status === 'fulfilled') result.quotes.set(code, entry.value)
      else result.errors.set(code, messageFromError(entry.reason))
    })
    return result
  }
}

export function createDefaultSources(): {
  legacy: FundDataSource
  tiantian: FundDataSource
  sina: FundDataSource
  official: FundDataSource
} {
  return {
    legacy: new LegacyFundGzSource(),
    tiantian: new TianTianBatchSource(),
    sina: new SinaEstimateSource(),
    official: new EastMoneyOfficialNavSource()
  }
}

export function hasEstimate(quote: SourceQuote | undefined): boolean {
  return Boolean(quote && quote.estimatedNav !== null && quote.estimatedNav !== undefined && quote.estimatedChange !== null && quote.estimatedChange !== undefined)
}
