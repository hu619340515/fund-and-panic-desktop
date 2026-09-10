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

export interface PanicApi {
  getRealtime(): Promise<unknown>
  getDailyLatest(): Promise<unknown>
  getRealtimeHistory(date?: string): Promise<unknown>
  getDailyHistory(limit?: number): Promise<unknown>
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
  PANIC_SOURCES: 'panic:get-sources',
  PANIC_ENGINE_STATUS: 'panic:engine-status',
  PANIC_REFRESH: 'panic:refresh',
  PANIC_CHART: 'panic:chart',
  STATE_CHANGED: 'fund-app:state-changed'
} as const
