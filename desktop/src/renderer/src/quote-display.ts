import type { FundQuote } from '@shared/types'

export interface PrimaryChange {
  value: number | null
  label: '实时估' | '收盘' | '旧估' | '暂无'
  title: string
}

export function primaryChange(quote: FundQuote): PrimaryChange {
  const valuationDate = quote.valuationTime?.match(/^(\d{4}-\d{2}-\d{2})/)?.[1]
  const officialCoversEstimate = Boolean(
    quote.officialDate && valuationDate && quote.officialDate >= valuationDate
  )
  if (
    officialCoversEstimate &&
    quote.officialChange !== null &&
    Number.isFinite(quote.officialChange)
  ) {
    return {
      value: quote.officialChange,
      label: '收盘',
      title: `${quote.officialDate}官方净值日涨跌`
    }
  }
  if (
    quote.freshness === 'today' &&
    quote.estimatedChange !== null &&
    Number.isFinite(quote.estimatedChange)
  ) {
    return { value: quote.estimatedChange, label: '实时估', title: '今日盘中估算涨跌' }
  }
  if (quote.officialChange !== null && Number.isFinite(quote.officialChange)) {
    return {
      value: quote.officialChange,
      label: '收盘',
      title: `${quote.officialDate || '最近交易日'}官方净值日涨跌`
    }
  }
  if (quote.estimatedChange !== null && Number.isFinite(quote.estimatedChange)) {
    return { value: quote.estimatedChange, label: '旧估', title: '最近交易时段的估算涨跌' }
  }
  return { value: null, label: '暂无', title: '暂无涨跌数据' }
}
