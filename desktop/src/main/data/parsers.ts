import type { SourceQuote } from './types'

function finiteNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

function decodeJavascriptString(value: string): string {
  try {
    return JSON.parse(`"${value.replace(/"/g, '\\"')}"`) as string
  } catch {
    return value
  }
}

export function normalizeCompactDate(value: unknown): string | null {
  const text = String(value ?? '').trim()
  const compact = text.match(/^(\d{4})(\d{2})(\d{2})$/)
  if (compact) return `${compact[1]}-${compact[2]}-${compact[3]}`
  const standard = text.match(/^(\d{4}-\d{2}-\d{2})/)
  return standard?.[1] ?? null
}

export function parseLegacyResponse(text: string, requestedCode: string): SourceQuote | null {
  const match = text.match(/jsonpgz\s*\(\s*(\{[\s\S]*?\})\s*\)\s*;?/)
  if (!match?.[1]) return null
  try {
    const payload = JSON.parse(match[1]) as Record<string, unknown>
    const estimatedNav = finiteNumber(payload.gsz)
    const estimatedChange = finiteNumber(payload.gszzl)
    if (estimatedNav === null && estimatedChange === null) return null
    return {
      code: String(payload.fundcode ?? requestedCode),
      name: typeof payload.name === 'string' ? payload.name : undefined,
      officialNav: finiteNumber(payload.dwjz),
      officialDate: normalizeCompactDate(payload.jzrq),
      estimatedNav,
      estimatedChange,
      valuationTime: typeof payload.gztime === 'string' ? payload.gztime : null,
      source: 'legacy'
    }
  } catch {
    return null
  }
}

interface TianTianPayload {
  success?: boolean
  data?: Array<Record<string, unknown>>
}

export function parseTianTianResponse(payload: TianTianPayload): Map<string, SourceQuote> {
  const result = new Map<string, SourceQuote>()
  if (!payload.success || !Array.isArray(payload.data)) return result
  for (const item of payload.data) {
    const code = String(item.FCODE ?? '').trim()
    if (!/^\d{6}$/.test(code)) continue
    result.set(code, {
      code,
      name: typeof item.SHORTNAME === 'string' ? item.SHORTNAME : undefined,
      officialNav: finiteNumber(item.NAV),
      officialChange: finiteNumber(item.NAVCHGRT),
      officialDate: normalizeCompactDate(item.PDATE),
      estimatedNav: finiteNumber(item.GSZ),
      estimatedChange: finiteNumber(item.GSZZL),
      valuationTime: typeof item.GZTIME === 'string' && item.GZTIME ? item.GZTIME : null,
      source: 'tiantian'
    })
  }
  return result
}

interface SinaPoint extends Record<string, unknown> {
  pre_nav?: unknown
  growthrate?: unknown
  pre_nav2?: unknown
  growthrate2?: unknown
  pre_date?: unknown
  min_time?: unknown
}

export function parseSinaResponse(payload: unknown, code: string): SourceQuote | null {
  const root = payload as {
    result?: { data?: { networth?: SinaPoint[]; worth?: unknown; worth_date?: unknown } }
  }
  const data = root?.result?.data
  const points = data?.networth
  if (!Array.isArray(points) || points.length === 0) return null
  const point = points.at(-1)
  if (!point) return null

  const primaryNav = finiteNumber(point.pre_nav)
  const primaryRate = finiteNumber(point.growthrate)
  const secondaryNav = finiteNumber(point.pre_nav2)
  const secondaryRate = finiteNumber(point.growthrate2)

  let estimatedNav: number | null = null
  let estimatedChange: number | null = null
  let source: SourceQuote['source'] = 'sina-primary'
  if (primaryNav !== null && primaryRate !== null) {
    estimatedNav = primaryNav
    estimatedChange = Number((primaryRate * 100).toFixed(6))
  } else if (secondaryNav !== null && secondaryRate !== null) {
    estimatedNav = secondaryNav
    estimatedChange = Number((secondaryRate * 100).toFixed(6))
    source = 'sina-secondary'
  } else {
    return null
  }

  const date = normalizeCompactDate(point.pre_date)
  const time = typeof point.min_time === 'string' ? point.min_time : null
  return {
    code,
    officialNav: finiteNumber(data?.worth),
    officialDate: normalizeCompactDate(data?.worth_date),
    estimatedNav,
    estimatedChange,
    valuationTime: date && time ? `${date} ${time}` : date,
    source
  }
}

export function parsePingzhongResponse(text: string, requestedCode: string): SourceQuote | null {
  const nameMatch = text.match(/var\s+fS_name\s*=\s*"((?:\\.|[^"\\])*)"\s*;/)
  const trendMatch = text.match(/var\s+Data_netWorthTrend\s*=\s*(\[[\s\S]*?\])\s*;/)
  if (!trendMatch?.[1]) return null
  try {
    const trend = JSON.parse(trendMatch[1]) as Array<Record<string, unknown>>
    const last = trend.at(-1)
    if (!last) return null
    const officialNav = finiteNumber(last.y)
    const timestamp = finiteNumber(last.x)
    if (officialNav === null || timestamp === null) return null
    const officialDate = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Shanghai',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit'
    }).format(new Date(timestamp))
    return {
      code: requestedCode,
      name: nameMatch?.[1] ? decodeJavascriptString(nameMatch[1]) : undefined,
      officialNav,
      officialChange: finiteNumber(last.equityReturn),
      officialDate,
      estimatedNav: null,
      estimatedChange: null,
      valuationTime: null,
      source: 'official-nav'
    }
  } catch {
    return null
  }
}
