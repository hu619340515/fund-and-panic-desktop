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

/** 历史估计与正式记录使用共同日期轴和纵轴，互不覆盖或拼接。 */
export function historicalChartSeries(formal: PanicPoint[], estimated: PanicPoint[]) {
  const all = [...formal, ...estimated].sort((a, b) => a.time.localeCompare(b.time))
  const minimum = 0
  const maximum = 100
  const first = all[0]?.time ?? ''
  const last = all.at(-1)?.time ?? ''
  const start = Date.parse(first)
  const span = Date.parse(last) - start
  const coordinates = (points: PanicPoint[]) => points.map(point => ({
    ...point,
    x: span > 0 ? 24 + (Date.parse(point.time) - start) / span * 592 : 320,
    y: 24 + (1 - point.value / 100) * 132
  }))
  const formalPoints = coordinates(formal)
  const estimatedPoints = coordinates(estimated)
  const line = (points: typeof formalPoints) => points.map((point, index) =>
    `${index ? 'L' : 'M'}${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(' ')
  return {minimum, maximum, first, last, formalPoints, estimatedPoints,
    display:line(formalPoints), estimate:line(estimatedPoints)}
}

export function historicalEstimateView(payload: unknown, now = new Date()) {
  const value = asRecord(payload)
  const records = Array.isArray(value?.records) ? value.records.map(asRecord).filter((row): row is UnknownRecord =>
    Boolean(row && row.finality === 'estimated' && row.quality_status === 'historical_estimate')) : []
  const points = historyPoints(records, 'daily', now)
  const dates = new Set(points.map(point => point.time))
  const visible = records.filter(row => dates.has(String(row.trade_date)))
  const coverage = visible.map(row => finiteNumber(row.coverage)).filter((x): x is number => x !== null)
  const missing = [...new Set(visible.flatMap(row => Array.isArray(row.missing_features)
    ? row.missing_features.filter((key): key is string => typeof key === 'string') : []))]
  const labels: Record<string, string> = {
    up_count:'上涨家数', down_count:'下跌家数', limit_up:'涨停家数', limit_down:'跌停家数',
    decline_share:'下跌占比', decline_5_share:'跌幅≥5%占比', decline_7_share:'跌幅≥7%占比',
    median_return:'收益中位数', front_annualized_basis:'IF近月基差', next_annualized_basis:'IF次月基差',
    qvix_level:'QVIX', qvix_daily_change:'QVIX日变化', market_amount:'成交额',
    amount_ratio:'成交额比率', breadth:'市场宽度', derivatives:'衍生品',
    ewma_volatility_5:'5日加权波动率', realized_volatility_20:'20日实际波动率',
    downside_volatility_20:'20日下行波动率', parkinson_volatility_10:'10日高低价波动率',
    daily_down_jump:'单日下行跳跃', basis_curve_stress:'基差曲线压力', basis_expansion_3d:'3日基差扩大',
    daily_amount_shortfall:'成交额不足', daily_amihud:'日度非流动性', daily_downside_turnover:'下跌成交压力',
    limit_down_share:'跌停占比', limit_up_down_imbalance:'涨跌停失衡',
    severe_decline_share:'跌幅≥5%占比', extreme_decline_share:'跌幅≥7%占比',
    median_return_stress:'收益中位数压力', limit_down_intensity:'跌停强度', limit_imbalance:'涨跌停失衡'
  }
  const status = asRecord(value?.status)
  const lines = [points.length
    ? `${points[0]?.time} 至 ${points.at(-1)?.time} · ${points.length} 条历史估计`
    : '暂无可回算历史估计，点击“补全历史”获取可用数据']
  if (coverage.length) lines.push(`覆盖率 ${(Math.min(...coverage) * 100).toFixed(0)}%–${(Math.max(...coverage) * 100).toFixed(0)}%`)
  if (missing.length) lines.push(`缺项：${missing.map(key => labels[key] ?? key).join('、')}`)
  lines.push('历史估计基于可取得的历史数据回算，与完整数据生成的正式收盘分开显示。')
  if (typeof status?.message === 'string' && status.message) lines.push(status.message)
  if (status?.state === 'error') lines.push('历史补全失败，可重试；保留已成功日期。')
  if (Array.isArray(status?.errors) && status.errors.length) lines.push(`失败详情：${status.errors.map(error =>
    typeof error === 'string' ? error : JSON.stringify(error)).join('；')}`)
  if (typeof status?.updated_at === 'string') lines.push(`最近更新 ${status.updated_at}`)
  return {points, text:lines.join('\n')}
}
