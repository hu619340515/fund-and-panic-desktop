export const DEFAULT_FUND_CODES = [
  '013273',
  '004070',
  '024618',
  '007883',
  '013172',
  '006195',
  '024003',
  '021458',
  '012414',
  '018561',
  '014415'
] as const

export type QuoteSource =
  | 'legacy'
  | 'tiantian'
  | 'sina-primary'
  | 'sina-secondary'
  | 'official-nav'
  | 'cache'
  | 'unavailable'

export type QuoteFreshness = 'today' | 'recent' | 'official' | 'stale' | 'unavailable'

export interface FundConfig {
  code: string
  addedAt: string
  order: number
}

export interface FundQuote {
  code: string
  name: string
  officialNav: number | null
  officialChange: number | null
  officialDate: string | null
  estimatedNav: number | null
  estimatedChange: number | null
  valuationTime: string | null
  source: QuoteSource
  cachedSource?: QuoteSource
  freshness: QuoteFreshness
  fetchedAt: string
  error?: string
}

export interface SourceHealth {
  id: string
  displayName: string
  consecutiveFailures: number
  disabledUntil: string | null
  lastSuccessAt: string | null
  lastFailureAt: string | null
}

export interface AppSettings {
  autoRefresh: boolean
  refreshIntervalSeconds: number
  launchAtLogin: boolean
  alwaysOnTop: boolean
  windowOpacity: number
  closeToTray: true
}

export interface AppSnapshot {
  funds: FundConfig[]
  quotes: FundQuote[]
  settings: AppSettings
  sourceHealth: SourceHealth[]
  refreshing: boolean
  lastRefreshAt: string | null
  panic: PanicSnapshot
  engine: EngineStatus
}

export interface PanicSnapshot {
  refreshing?: boolean
  realtime: Record<string, unknown> | null
  daily: Record<string, unknown> | null
  error: string | null
  refreshedAt: string | null
}

export interface EngineStatus {
  logPath: string
  databaseVersion: number | null
  clientVersion: string
  state: 'stopped' | 'starting' | 'ready' | 'error'
  baseUrl: string | null
  error: string | null
  version: string
}

export interface PersistedState {
  windowBounds?: { x: number; y: number; width: number; height: number }
  version: 1
  funds: FundConfig[]
  quotes: Record<string, FundQuote>
  settings: AppSettings
  sourceHealth: SourceHealth[]
  lastRefreshAt: string | null
  panic?: PanicSnapshot
}

export type SettingsPatch = Partial<
  Pick<AppSettings, 'autoRefresh' | 'refreshIntervalSeconds' | 'launchAtLogin' | 'alwaysOnTop' | 'windowOpacity'>
>

export interface ActionResult<T> {
  ok: boolean
  data?: T
  error?: string
}

export interface FundAppApi {
  panic: PanicApi
  risk: RiskApi
  portfolio: PortfolioApi
  getState(): Promise<AppSnapshot>
  addFund(code: string): Promise<ActionResult<AppSnapshot>>
  removeFund(code: string): Promise<ActionResult<AppSnapshot>>
  reorderFunds(codes: string[]): Promise<ActionResult<AppSnapshot>>
  refresh(): Promise<ActionResult<AppSnapshot>>
  updateSettings(patch: SettingsPatch): Promise<ActionResult<AppSnapshot>>
  getPanicRealtime(): Promise<unknown>
  getPanicDailyLatest(): Promise<unknown>
  getPanicRealtimeHistory(date?: string): Promise<unknown>
  getPanicDailyHistory(limit?: number): Promise<unknown>
  getPanicSources(): Promise<unknown>
  getEngineStatus(): Promise<EngineStatus>
  refreshPanic(): Promise<unknown>
  onStateChanged(listener: (state: AppSnapshot) => void): () => void
}

export type RiskState = 'ready' | 'loading' | 'insufficient_data' | 'stale' | 'error'
export type Profile = 'conservative' | 'balanced' | 'aggressive'
export type CoreComponent = 'shock' | 'drawdown' | 'downside'
export interface ComponentReading { raw: number | null; percentile: number | null }
export interface AuxiliaryReading {
  state: string
  data: Record<string, unknown> | null
  source_id: string | null
  upstream?: string | null
  timestamp: string | null
  error: string | null
}
export interface RiskSnapshot {
  symbol?: string
  model_version: string
  state: RiskState
  as_of: string | null
  score: number | null
  components: Partial<Record<CoreComponent, ComponentReading>> & { overheat?: Record<string, ComponentReading> }
  overheat: number | null
  explanation: string[]
  missing: string[]
  symbols: Array<{ symbol: string; name: string; score: number | null; state: RiskState; as_of: string | null }>
  forecast: { state: string; as_of: string | null; symbol?: string; published: Record<string, { probabilities: Record<string, number>; quantiles: { q10: number; q50: number; q90: number } }>; reasons: string[] }
  signal: { action: string; reason: string; eligible: boolean }
  opportunity?: { state: string; label: string; reasons: string[] }
  heat_signal?: { state: string; label: string; reasons: string[] }
  data_quality: Record<string, unknown>
  observation: { required_days: number; observed_days: number; ready: boolean }
  jobs: EngineJob[]
  intraday?: { rows: Array<{ symbol: string; name: string; last: number | null; change: number | null; timestamp: string | null; source_id: string; state: string }>; notice: string }
  auxiliary?: { qvix?: AuxiliaryReading; futures?: AuxiliaryReading; breadth?: AuxiliaryReading; limits?: AuxiliaryReading; trade_date?: string; fetched_at?: string; attempts?: unknown[]; state?: string; error?: string }
}
export interface RiskHistory {
  symbol: string
  kind: 'backtest' | 'published'
  records: Array<{ as_of: string; score: number | null; model_version: string; record_kind: 'backtest' | 'published'; state: string; break_before?: boolean }>
  missing: string[]
}
export interface RiskValidation {
  status: string
  publishable: boolean
  reasons: string[]
  metrics: Record<string, unknown>
}
export interface EngineJob {
  id: string
  kind: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  progress: number
  message: string
  errors: Array<string | { symbol?: string; fund?: string; error: string }>
  started_at: string | null
  completed_at: string | null
}
export interface PortfolioLot {
  id: string
  code: string
  shares: number | null
  market_value: number | null
  valuation_date: string | null
  confirmed_date: string | null
  fee_buy: number | null
  fee_sell: number | null
  in_transit: number
  baseline_weight: number | null
  industry: string | null
  benchmark_symbol?: string | null
  asset_class?: string | null
  metadata_verified?: boolean
  effective_date?: string | null
}
export interface Portfolio {
  cash: number
  profile: Profile
  lots: PortfolioLot[]
  constraints: Record<string, unknown>
  version: number
}
export interface PortfolioImportPreview {
  valid?: boolean
  portfolio: Portfolio | null
  rows?: Record<string, unknown>[]
  errors: Array<string | { field?: string; message: string }>
  warnings?: Array<string | { field?: string; message: string }>
}
export interface FundExecution {
  verified: boolean
  subscription_verified: boolean
  redemption_verified: boolean
  subscription_open?: boolean | null
  redemption_open?: boolean | null
  next_subscription_date: string | null
  next_redemption_date: string | null
  confirmation_days: number | null
  reason?: string | null
  missing?: string[]
  source?: { provider?: string; dataset?: string; url?: string } | null
  fetched_at?: string | null
}
export interface AllocationAdvice {
  state: string
  as_of: string | null
  profile: Profile
  summary?: string | { total_assets?: number; cash?: number; in_transit?: number; fund_count?: number; risk_budget?: Record<string, number> }
  items: Record<string, unknown>[]
  constraints: Record<string, unknown>
  reasons: string[]
  fund_quality?: Array<{
    code: string
    benchmark: string | null
    nav_date: string | null
    exposure: { available: boolean; stable: boolean; beta: number | null; r_squared: number | null; observations: number | null; reason: string | null } | null
    execution?: FundExecution | null
  }>
}
export interface RiskApi {
  snapshot(symbol?: string): Promise<RiskSnapshot>
  history(symbol?: string, kind?: 'backtest' | 'published', limit?: number): Promise<RiskHistory>
  validation(symbol?: string): Promise<RiskValidation>
  refresh(symbols?: string[]): Promise<{ job_id?: string; id?: string }>
  train(): Promise<{ job_id?: string; id?: string }>
  jobs(): Promise<{ jobs: EngineJob[] }>
  advice(): Promise<AllocationAdvice>
}
export interface PortfolioApi {
  get(): Promise<Portfolio>
  save(value: Portfolio): Promise<Portfolio>
  previewCsv(text: string): Promise<PortfolioImportPreview>
}

export interface PanicApi {
  getRealtime(): Promise<unknown>
  getDailyLatest(): Promise<unknown>
  getRealtimeHistory(date?: string): Promise<unknown>
  getDailyHistory(limit?: number): Promise<unknown>
  getHistoricalEstimates(): Promise<unknown>
  backfillHistory(): Promise<unknown>
  getSources(): Promise<unknown>
  getHealth(): Promise<EngineStatus>
  refresh(): Promise<unknown>
  generateChart(type: 'intraday' | 'daily'): Promise<{ path: string; dataUrl: string }>
}

export const IPC_CHANNELS = {
  GET_STATE: 'fund-app:get-state',
  ADD_FUND: 'fund-app:add-fund',
  REMOVE_FUND: 'fund-app:remove-fund',
  REORDER_FUNDS: 'fund-app:reorder-funds',
  REFRESH: 'fund-app:refresh',
  UPDATE_SETTINGS: 'fund-app:update-settings',
  PANIC_REALTIME: 'panic:get-realtime',
  PANIC_DAILY: 'panic:get-daily',
  PANIC_REALTIME_HISTORY: 'panic:get-realtime-history',
  PANIC_DAILY_HISTORY: 'panic:get-daily-history',
  PANIC_HISTORICAL_ESTIMATES: 'panic:get-historical-estimates',
  PANIC_BACKFILL_HISTORY: 'panic:backfill-history',
  PANIC_SOURCES: 'panic:get-sources',
  PANIC_ENGINE_STATUS: 'panic:engine-status',
  PANIC_REFRESH: 'panic:refresh',
  PANIC_CHART: 'panic:chart',
  RISK_SNAPSHOT: 'risk:snapshot',
  RISK_HISTORY: 'risk:history',
  RISK_VALIDATION: 'risk:validation',
  RISK_REFRESH: 'risk:refresh',
  RISK_TRAIN: 'risk:train',
  RISK_JOBS: 'risk:jobs',
  RISK_ADVICE: 'risk:advice',
  PORTFOLIO_GET: 'portfolio:get',
  PORTFOLIO_SAVE: 'portfolio:save',
  PORTFOLIO_PREVIEW_CSV: 'portfolio:preview-csv',
  STATE_CHANGED: 'fund-app:state-changed'
} as const
