import type { AllocationAdvice, CoreComponent, PortfolioLot, RiskHistory, RiskSnapshot } from '@shared/types'

export const HORIZONS = [5, 10, 20, 30, 50] as const
export const COMPONENT_NAMES: Record<string, string> = {
  volatility: '波动', breadth: '市场宽度', liquidity: '流动性',
  market_volatility: '市场波动', drawdown: '回撤', volume: '量价',
  sentiment: '市场情绪', correlation: '相关性',
  shock: '波动冲击', downside: '下行压力'
}

export function numberText(value: unknown, digits = 1): string {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '--'
}

export function ratioText(value: unknown): string {
  return typeof value === 'number' && Number.isFinite(value) ? (value * 100).toFixed(1) + '%' : '--'
}

export function displayDate(value: string | null | undefined): string {
  if (!value) return '--'
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}

export function scoreDisplay(snapshot: RiskSnapshot | null): { score: string; level: string; overheat: string; stale: boolean } {
  const stale = snapshot?.state === 'stale'
  const visible = snapshot?.state === 'ready' || stale
  const score = visible ? snapshot?.score : null
  const level = typeof score === 'number' && Number.isFinite(score)
    ? score >= 70 ? '高压力' : score >= 40 ? '中性观察' : '低压力' : '暂不可用'
  return { score: numberText(score, 0), level: stale && score !== null ? level + ' · 已过期' : level,
    overheat: visible ? numberText(snapshot?.overheat, 0) : '--', stale }
}

export interface CoreComponentRow { key: CoreComponent; name: string; raw: string | null; percentile: string; contribution: string | null }
export function coreComponentRows(snapshot: RiskSnapshot | null): CoreComponentRow[] {
  const names = { shock: '波动冲击', drawdown: '60日回撤', downside: '20日年化下行波动' } as const
  return (Object.keys(names) as CoreComponent[]).flatMap((key) => {
    const reading = snapshot?.components?.[key]
    if (!reading) return []
    const percentile = reading.percentile
    return [{ key, name: names[key], raw: reading.raw == null ? null : key === 'shock' ? numberText(reading.raw, 2) : ratioText(reading.raw),
      percentile: typeof percentile === 'number' && Number.isFinite(percentile) ? numberText(percentile, 1) + '%' : '--',
      contribution: snapshot?.symbol === 'market' || typeof percentile !== 'number' || !Number.isFinite(percentile)
        ? null : numberText(percentile / 3, 1) + ' 分' }]
  })
}

export function componentLine(reading: CoreComponentRow): string {
  return (reading.raw === null ? '' : (reading.key === 'shock' ? '标准化冲击 ' : '原始幅度 ') + reading.raw + ' · ') +
    '历史分位 ' + reading.percentile + (reading.contribution ? ' · 分数贡献 ' + reading.contribution : '')
}

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

export function riskQualitySummary(snapshot: RiskSnapshot | null): string {
  const source = snapshot?.data_quality ?? {}
  const quality = record(source.quality)
  const ids = Array.isArray(source.source_ids) ? source.source_ids.filter((item): item is string => typeof item === 'string') : []
  const missing = typeof quality.missing_dates_count === 'number' ? quality.missing_dates_count :
    Array.isArray(quality.missing_dates) ? quality.missing_dates.length : null
  const start = source.first_date ?? quality.first_date ?? source.start_date ?? quality.start_date
  const end = source.last_date ?? quality.last_date ?? snapshot?.as_of
  const period = typeof start === 'string' && typeof end === 'string'
    ? '日期 ' + start + ' 至 ' + end
    : typeof end === 'string' ? '截至 ' + end + '（起点未提供）' : '日期待获取'
  const state = ({ ready:'已更新', stale:'已过期', loading:'计算中', insufficient_data:'数据不足', error:'请求失败' } as Record<string,string>)[snapshot?.state ?? ''] ?? '待获取'
  return (snapshot?.symbol === 'market' ? '风险参照（沪深300）数据质量：' : '数据质量：') + state + ' · 来源 ' + (ids.join('、') || (typeof source.source_id === 'string' ? source.source_id : '待获取')) +
    ' · 记录 ' + numberText(source.count, 0) + ' 日 · ' + period + ' · 缺口 ' +
    (missing === null ? '--' : String(missing)) + ' 日 · 成交额覆盖 ' + ratioText(source.amount_coverage)
}

export function riskQualityDetails(snapshot: RiskSnapshot | null): string[] {
  const source = snapshot?.data_quality ?? {}
  const quality = record(source.quality)
  const list = (value: unknown): string => Array.isArray(value) ? value.slice(0, 20).map(String).join('、') || '无' : '未提供'
  return [
    '来源地址：' + (typeof source.source_url === 'string' ? source.source_url : '未提供') +
      ' · 抓取时间：' + (typeof source.fetched_at === 'string' ? source.fetched_at : '未提供'),
    '缺失交易日：' + list(quality.missing_dates) + ' · 异常行：' + list(quality.rejected),
    '成交额缺项：' + list(quality.amount_missing_dates),
    ...(snapshot?.missing ?? []).map((item) => '模型缺项：' + item),
    ...(typeof quality.notice === 'string' ? [quality.notice] : [])
  ]
}

export function forecastRows(snapshot: RiskSnapshot | null): Array<{ horizon: number; cells: string[] }> {
  if (!snapshot || snapshot.state !== 'ready' || snapshot.forecast?.state !== 'published') return []
  const published = snapshot.forecast.published as Record<string, unknown>
  const flatProbabilities = published.probabilities as Record<string, number> | undefined
  const flatQuantiles = published.quantiles as Record<string, number> | undefined
  return HORIZONS.flatMap((horizon) => {
    const row = snapshot.forecast.published[String(horizon)]
    if (!row && flatProbabilities && flatQuantiles &&
        [3, 5, 10].some((threshold) => flatProbabilities[String(horizon) + 'd_' + threshold + 'pct'] != null)) {
      return [{ horizon, cells: [3, 5, 10].map((threshold) =>
        ratioText(flatProbabilities[String(horizon) + 'd_' + threshold + 'pct'])).concat(
        [10, 50, 90].map((quantile) => ratioText(flatQuantiles[String(horizon) + 'd_q' + quantile]))) }]
    }
    if (!row) return []
    const probabilities = row.probabilities ?? {}
    const probability = (threshold: number): string =>
      ratioText(probabilities[String(threshold)] ?? probabilities[threshold + '%'] ?? probabilities[String(threshold / 100)])
    return [{ horizon, cells: [probability(3), probability(5), probability(10),
      ratioText(row.quantiles?.q10), ratioText(row.quantiles?.q50), ratioText(row.quantiles?.q90)] }]
  })
}

export function historySeries(history: RiskHistory | null): Array<{ date: string; score: number; breakBefore: boolean }> {
  if (!history) return []
  return history.records.filter((row) =>
    row.record_kind === history.kind && typeof row.score === 'number' && Number.isFinite(row.score) &&
    row.score >= 0 && row.score <= 100 && /^\d{4}-\d{2}-\d{2}/.test(row.as_of) &&
    Number.isFinite(Date.parse(row.as_of.slice(0, 10) + 'T00:00:00Z')))
    .map((row) => ({ date: row.as_of, score: row.score as number, breakBefore: row.break_before === true }))
    .sort((a, b) => a.date.localeCompare(b.date))
}

export function historyCoordinates(points: Array<{ date: string; score: number; breakBefore: boolean }>): Array<{ x: number; y: number; breakBefore: boolean }> {
  const times = points.map((point) => Date.parse(point.date.slice(0, 10) + 'T00:00:00Z'))
  const start = Math.min(...times)
  const span = Math.max(...times) - start
  return points.map((point, index) => ({
    x: span ? 20 + 600 * (times[index]! - start) / span : 320,
    y: 160 - point.score * 1.4,
    breakBefore: point.breakBefore
  }))
}

export function historyPath(points: Array<{ date: string; score: number; breakBefore: boolean }>): string {
  return historyCoordinates(points).map((point, index) =>
    (index === 0 || point.breakBefore ? 'M' : 'L') + point.x.toFixed(1) + ' ' + point.y.toFixed(1)).join(' ')
}

export function auxiliaryRows(auxiliary: RiskSnapshot['auxiliary']): Array<{ name: string; state: string; summary: string; source: string; reason: string }> {
  const descriptions = {qvix: 'QVIX', futures: 'IF期货', breadth: '市场宽度', limits: '涨跌停'} as const
  const number = (data: Record<string, unknown>, key: string): string => numberText(data[key], 2)
  return (Object.keys(descriptions) as Array<keyof typeof descriptions>).map((key) => {
    const entry = auxiliary?.[key]
    const data = entry?.data
    let summary = '数据缺失'
    if (data && key === 'qvix') summary = (typeof data.symbol === 'string' ? data.symbol : '口径未注明') +
      ' · 当前 ' + number(data, 'value') + ' · 前收 ' + number(data, 'previous_close') + ' · 5分钟前 ' + number(data, 'previous_5m')
    if (data && key === 'futures') {
      const contracts = Array.isArray(data.contracts) ? data.contracts : []
      summary = 'IF合约 ' + contracts.length + ' 只' + (contracts.length ? ' · ' + contracts.slice(0, 2).map((contract: unknown) => {
        const record = contract && typeof contract === 'object' ? contract as Record<string, unknown> : {}
        return String(record.symbol ?? '--') + ' ' + number(record, 'last')
      }).join(' / ') : '')
    }
    if (data && key === 'breadth') summary = '上涨 ' + numberText(data.up_count, 0) + ' · 下跌 ' +
      numberText(data.down_count, 0) + ' · 下跌占比 ' + ratioText(data.decline_share)
    if (data && key === 'limits') summary = '涨停 ' + numberText(data.limit_up, 0) + ' · 跌停 ' + numberText(data.limit_down, 0)
    return {
      name: descriptions[key], state: entry?.state ?? '未获取', summary,
      source: '来源 ' + (entry?.source_id || '未核实') + ' · 上游 ' + (entry?.upstream || '未核实') +
        ' · 交易日 ' + (auxiliary?.trade_date || '--') + ' · ' +
        (entry?.timestamp ? '来源时间 ' + entry.timestamp : '来源发布时间未核实'),
      reason: entry?.error ?? ''
    }
  })
}

export function adviceRows(advice: AllocationAdvice | null, lots: PortfolioLot[]): Array<{ code: string; name: string; current: string; target: string; action: string; reason: string }> {
  if (!advice || !Array.isArray(advice.items)) return []
  return advice.items.map((item) => {
    const code = typeof item.code === 'string' ? item.code : '--'
    const matching = lots.filter((lot) => lot.code === code)
    const missing = matching.some((lot) => lot.market_value === null || lot.fee_buy === null || lot.fee_sell === null)
    const target = item.target_min == null || item.target_max == null
      ? '--' : ratioText(item.target_min) + ' – ' + ratioText(item.target_max)
    return {
      code,
      name: typeof item.name === 'string' ? item.name : code,
      current: ratioText(item.current_weight),
      target,
      action: item.action === 'hold' ? '维持观察' :
        item.action === 'review_buy' ? '待复核增配' :
        item.action === 'review_sell' ? '待复核减配' :
        item.action === 'review' ? '人工复核' : '暂不可用',
      reason: [typeof item.reason === 'string' ? item.reason : '', missing ? '市值/费率缺项，无法计算精确金额' : ''].filter(Boolean).join('；') || '--'
    }
  })
}

export function fundQualityRows(advice: AllocationAdvice | null): Array<{
  code: string; benchmark: string; navDate: string; estimate: string
  subscription: string; redemption: string; confirmation: string; reason: string
}> {
  return (Array.isArray(advice?.fund_quality) ? advice.fund_quality : []).map((item) => {
    const exposure = item.exposure
    const execution = item.execution
    const available = exposure?.available === true
    const estimate = available
      ? (exposure?.stable ? '稳定 · ' : '尚未稳定 · ') + 'β ' + numberText(exposure?.beta, 2) +
        ' · R² ' + ratioText(exposure?.r_squared) + ' · ' + numberText(exposure?.observations, 0) + ' 期'
      : '暂不可用'
    const nextDate = (verified: boolean | undefined, open: boolean | null | undefined, date: string | null | undefined): string => {
      if (!execution?.verified || !verified) return '开放状态未核实'
      if (open === false) return '当前关闭'
      return typeof date === 'string' && date ? date : '下一提交日未核实'
    }
    const source = execution?.source
    const sourceText = source?.dataset || source?.provider
      ? '来源 ' + [source.provider, source.dataset].filter(Boolean).join(' · ') +
        ' · 抓取 ' + (execution?.fetched_at || '--')
      : '申赎来源未获取'
    return {
      code: typeof item.code === 'string' ? item.code : '--',
      benchmark: typeof item.benchmark === 'string' && item.benchmark ? item.benchmark : '--',
      navDate: typeof item.nav_date === 'string' && item.nav_date ? item.nav_date : '--',
      estimate,
      subscription: nextDate(execution?.subscription_verified, execution?.subscription_open, execution?.next_subscription_date),
      redemption: nextDate(execution?.redemption_verified, execution?.redemption_open, execution?.next_redemption_date),
      confirmation: typeof execution?.confirmation_days === 'number' && Number.isFinite(execution.confirmation_days) && execution.confirmation_days > 0
        ? execution.confirmation_days + ' 日（按基金规则复核）' : '确认周期未核实',
      reason: [typeof exposure?.reason === 'string' && exposure.reason ? exposure.reason : available ? '仅供风险估计参考' : '缺少有效净值或基准数据',
        execution?.reason, ...(Array.isArray(execution?.missing) ? execution.missing.map((value) => '缺项：' + value) : []), sourceText]
        .filter(Boolean).join('；')
    }
  })
}

export function gateLabel(snapshot: RiskSnapshot | null, publishable: boolean | null): string {
  if (!snapshot) return '等待数据'
  const days = snapshot.observation
  if (!days?.ready) return '观察期 ' + (days?.observed_days ?? 0) + '/' + (days?.required_days ?? 20) + ' 日'
  if (publishable !== true) return '验证未通过'
  return snapshot.signal?.eligible ? '已满足观察与验证门槛' : '信号暂不符合条件'
}
