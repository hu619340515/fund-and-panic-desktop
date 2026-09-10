export type UnknownRecord = Record<string, unknown>

export interface PanicPoint {
  time: string
  value: number
  raw?: number
}

export interface ChartPaths {
  display: string
  raw: string
  minimum: number
  maximum: number
}

export type PanicUiState = 'loading' | 'success' | 'stale' | 'error' | 'empty'

export function asRecord(value: unknown): UnknownRecord | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as UnknownRecord)
    : null
}

export function unwrapRecord(value: unknown): UnknownRecord | null {
  const record = asRecord(value)
  if (!record) return null
  return asRecord(record.data) ?? asRecord(record.result) ?? record
}

export function unwrapList(value: unknown): UnknownRecord[] {
  if (Array.isArray(value)) {
    return value.map(asRecord).filter((item): item is UnknownRecord => item !== null)
  }
  const record = asRecord(value)
  const nested = record?.data ?? record?.result ?? record?.items ?? record?.history
  return Array.isArray(nested)
    ? nested.map(asRecord).filter((item): item is UnknownRecord => item !== null)
    : []
}

export function finiteNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

/** 仅转换接口中的真实记录；不会插值、复制末值或生成占位点。 */
export function marketDate(now = new Date()): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(now)
}

export function historyPoints(
  value: unknown,
  kind: 'intraday' | 'daily',
  now = new Date()
): PanicPoint[] {
    const today = marketDate(now)
    const cutoff = new Date(`${today}T00:00:00Z`)
    cutoff.setUTCFullYear(cutoff.getUTCFullYear() - 1)
    const cutoffDate = cutoff.toISOString().slice(0, 10)
  return unwrapList(value)
    .map((row): PanicPoint | null => {
      const timeValue = kind === 'intraday' ? row.timestamp : row.trade_date
      const time = typeof timeValue === 'string' ? timeValue : ''
      const point = finiteNumber(
        kind === 'intraday' ? row.realtime_panic_index : row.final_panic_index
      )
      const raw = kind === 'intraday' ? finiteNumber(row.realtime_panic_index_raw) : null
      const parsed = new Date(time)
      if (!time || point === null || Number.isNaN(parsed.getTime())) return null
        if (kind === 'intraday' && marketDate(parsed) !== today) return null
        if (kind === 'daily' && (time.slice(0, 10) < cutoffDate || time.slice(0, 10) > today)) return null
      return { time, value: point, ...(raw === null ? {} : { raw }) }
    })
    .filter((item): item is PanicPoint => item !== null)
    .sort((left, right) => new Date(left.time).getTime() - new Date(right.time).getTime())
}

export function panicUiState(options: {
  refreshing: boolean
  realtime: unknown
  daily: unknown
  error: string | null
  now?: Date
  staleAfterMinutes?: number
}): PanicUiState {
  const realtime = unwrapRecord(options.realtime)
  const daily = unwrapRecord(options.daily)
  if (options.refreshing && !realtime && !daily) return 'loading'
  if (options.error && !realtime && !daily) return 'error'
  if (!realtime && !daily) return 'empty'
  if (!realtime) return options.error ? 'stale' : 'success'

  const timestamp = typeof realtime.timestamp === 'string' ? realtime.timestamp : ''
  const timestampMs = new Date(timestamp).getTime()
  const age = (options.now ?? new Date()).getTime() - timestampMs
  if (timestamp && !Number.isNaN(timestampMs) && age > (options.staleAfterMinutes ?? 20) * 60_000) {
    return 'stale'
  }
  return options.error ? 'stale' : 'success'
}

function pointPath(
  points: PanicPoint[],
  key: 'value' | 'raw',
  minimum: number,
  maximum: number,
  width: number,
  height: number,
  padding: number
): string {
  const available = points
    .map((point, sourceIndex) => ({ point, sourceIndex }))
    .filter(({ point }) => finiteNumber(point[key]) !== null)
  if (available.length === 0) return ''
  const innerWidth = Math.max(1, width - padding * 2)
  const innerHeight = Math.max(1, height - padding * 2)
  const range = Math.max(1, maximum - minimum)
  return available.map(({ point, sourceIndex }, outputIndex) => {
    const x = padding + (points.length === 1
      ? innerWidth / 2
      : sourceIndex / (points.length - 1) * innerWidth)
    const numeric = point[key] as number
    const y = padding + (1 - (numeric - minimum) / range) * innerHeight
    return `${outputIndex === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
}

/** 显示值与原始值使用同一个纵轴，避免叠加曲线产生视觉偏差。 */
export function chartPaths(
  points: PanicPoint[],
  width = 640,
  height = 180,
  padding = 18
): ChartPaths {
  if (points.length === 0) return { display: '', raw: '', minimum: 0, maximum: 100 }
  const values = points.flatMap((point) => point.raw === undefined ? [point.value] : [point.value, point.raw])
  const minimum = Math.max(0, Math.floor(Math.min(...values) - 4))
  const maximum = Math.min(100, Math.ceil(Math.max(...values) + 4))
  const safeMaximum = maximum <= minimum ? Math.min(100, minimum + 1) : maximum
  return {
    display: pointPath(points, 'value', minimum, safeMaximum, width, height, padding),
    raw: pointPath(points, 'raw', minimum, safeMaximum, width, height, padding),
    minimum,
    maximum: safeMaximum
  }
}

export function formatCompactMoney(value: unknown): string {
  const number = finiteNumber(value)
  if (number === null) return '--'
  if (Math.abs(number) >= 1e12) return `${(number / 1e12).toFixed(2)} 万亿元`
  if (Math.abs(number) >= 1e8) return `${(number / 1e8).toFixed(0)} 亿元`
  if (Math.abs(number) >= 1e4) return `${(number / 1e4).toFixed(0)} 万元`
  return `${number.toFixed(0)} 元`
}
