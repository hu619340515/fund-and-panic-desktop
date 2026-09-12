import type { Portfolio } from '../shared/types'

export function validSymbol(value: unknown): string {
  if (value === undefined || value === null || value === '') return 'market'
  if (typeof value !== 'string' || !/^(?:market|(?:sh|sz|cn)\d{6}|hk[A-Za-z0-9]{2,20}|\d{6}|industry:[\w-]{1,32})$/.test(value)) {
    throw new Error('标的代码无效')
  }
  return value
}

export function validHistoryKind(value: unknown): 'backtest' | 'published' {
  if (value === undefined || value === 'published') return 'published'
  if (value === 'backtest') return value
  throw new Error('历史记录类型无效')
}

export function validHistoryLimit(value: unknown): number {
  if (value === undefined) return 252
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 1 || value > 2000) throw new Error('历史记录条数无效')
  return value
}

export function validSymbols(value: unknown): string[] | undefined {
  if (value === undefined) return undefined
  if (!Array.isArray(value) || value.length > 20 || value.length === 0) throw new Error('刷新标的列表无效')
  const symbols = value.map(validSymbol)
  if (symbols.includes('market') && value.some((item) => item !== 'market')) throw new Error('刷新标的列表含空代码')
  return [...new Set(symbols)]
}

function finiteNonnegative(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
}

export function validPortfolio(value: unknown): Portfolio {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('持仓数据无效')
  const raw = value as Record<string, unknown>
  if (raw.version !== 1 || !finiteNonnegative(raw.cash) || !['conservative', 'balanced', 'aggressive'].includes(String(raw.profile))) throw new Error('持仓概要无效')
  if (!Array.isArray(raw.lots) || raw.lots.length > 2000 || !raw.constraints || typeof raw.constraints !== 'object' || Array.isArray(raw.constraints)) throw new Error('持仓列表或约束无效')
  for (const lot of raw.lots) {
    if (!lot || typeof lot !== 'object' || Array.isArray(lot)) throw new Error('持仓批次无效')
    const row = lot as Record<string, unknown>
    if (typeof row.id !== 'string' || row.id.length > 100 || !/^\d{6}$/.test(String(row.code))) throw new Error('持仓批次代码无效')
    for (const field of ['shares', 'market_value', 'fee_buy', 'fee_sell', 'baseline_weight']) {
      if (row[field] !== null && !finiteNonnegative(row[field])) throw new Error(`持仓${field}无效`)
    }
    if (!finiteNonnegative(row.in_transit) || (row.industry !== null && typeof row.industry !== 'string') ||
        (row.valuation_date !== null && typeof row.valuation_date !== 'string') ||
        (row.confirmed_date !== null && typeof row.confirmed_date !== 'string')) throw new Error('持仓日期、行业或在途数量无效')
    if (row.metadata_verified !== undefined && typeof row.metadata_verified !== 'boolean') throw new Error('元数据核实状态无效')
    if (row.metadata_verified === true &&
        (typeof row.benchmark_symbol !== 'string' || !row.benchmark_symbol.trim() || row.benchmark_symbol.length > 100 ||
          !['equity', 'bond', 'money', 'commodity', 'other'].includes(String(row.asset_class)) ||
          typeof row.effective_date !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(row.effective_date))) {
      throw new Error('核实元数据时须填写基准、资产类别与生效日')
    }
  }
  for (const [key, constraint] of Object.entries(raw.constraints as Record<string, unknown>)) {
    if (!['equity_cap', 'vol_target', 'single_fund_cap', 'industry_cap', 'daily_change_cap', 'no_trade_band'].includes(key) ||
      typeof constraint !== 'number' || !Number.isFinite(constraint) || constraint < 0 || constraint > 1) throw new Error('仓位参数无效')
  }
  const portfolio = value as Portfolio
  const aliases: Record<string, string> = { '000300':'sh000300', '000905':'sh000905', '000852':'sh000852', HSI:'hkHSI', HSTECH:'hkHSTECH' }
  return { ...portfolio, lots: portfolio.lots.map(lot => lot.benchmark_symbol && aliases[lot.benchmark_symbol]
    ? { ...lot, benchmark_symbol: aliases[lot.benchmark_symbol] } : lot) }
}

export function validCsvText(value: unknown): string {
  if (typeof value !== 'string' || value.length === 0 || value.length > 2_000_000) throw new Error('CSV 内容为空或超过 2 MB')
  return value
}
