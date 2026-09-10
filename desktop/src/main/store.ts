import { mkdir, readFile, rename, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import {
  DEFAULT_FUND_CODES,
  type AppSettings,
  type FundConfig,
  type FundQuote,
  type PersistedState,
  type SourceHealth
} from '@shared/types'

const DEFAULT_SETTINGS: AppSettings = {
  autoRefresh: true,
  refreshIntervalSeconds: 60,
  launchAtLogin: false,
  alwaysOnTop: true,
  windowOpacity: 0.94,
  closeToTray: true
}

function defaultFunds(now = new Date()): FundConfig[] {
  return DEFAULT_FUND_CODES.map((code, order) => ({
    code,
    order,
    addedAt: now.toISOString()
  }))
}

export function createDefaultState(now = new Date()): PersistedState {
  return {
    version: 1,
    funds: defaultFunds(now),
    quotes: {},
    settings: { ...DEFAULT_SETTINGS },
    sourceHealth: [],
    lastRefreshAt: null,
    panic: { realtime: null, daily: null, error: null, refreshedAt: null }
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value))
}

function sanitizeFunds(value: unknown): FundConfig[] {
  if (!Array.isArray(value)) return []
  const seen = new Set<string>()
  const funds: FundConfig[] = []
  for (const item of value) {
    if (!isRecord(item)) continue
    const code = String(item.code ?? '').trim()
    if (!/^\d{6}$/.test(code) || seen.has(code)) continue
    seen.add(code)
    funds.push({
      code,
      order: Number.isFinite(Number(item.order)) ? Number(item.order) : funds.length,
      addedAt: typeof item.addedAt === 'string' ? item.addedAt : new Date().toISOString()
    })
  }
  return funds.sort((a, b) => a.order - b.order).map((fund, order) => ({ ...fund, order }))
}

function sanitizeSettings(value: unknown): AppSettings {
  if (!isRecord(value)) return { ...DEFAULT_SETTINGS }
  const interval = Number(value.refreshIntervalSeconds)
  const opacity = Number(value.windowOpacity)
  return {
    refreshIntervalSeconds: [30, 60, 120, 300].includes(interval) ? interval : 60,
    autoRefresh: value.autoRefresh !== false,
    launchAtLogin: value.launchAtLogin === true,
    alwaysOnTop: value.alwaysOnTop !== false,
    windowOpacity: Number.isFinite(opacity) && opacity >= 0.55 && opacity <= 1 ? opacity : 0.94,
    closeToTray: true
  }
}

function sanitizeQuotes(value: unknown): Record<string, FundQuote> {
  if (!isRecord(value)) return {}
  const quotes: Record<string, FundQuote> = {}
  for (const [code, raw] of Object.entries(value)) {
    if (!/^\d{6}$/.test(code) || !isRecord(raw)) continue
    if (typeof raw.name !== 'string' || typeof raw.fetchedAt !== 'string') continue
    quotes[code] = raw as unknown as FundQuote
  }
  return quotes
}

function sanitizeHealth(value: unknown): SourceHealth[] {
  if (!Array.isArray(value)) return []
  return value.filter((item): item is SourceHealth => {
    return isRecord(item) && typeof item.id === 'string' && typeof item.displayName === 'string'
  })
}

export class AppStore {
  private readonly filePath: string
  private readonly legacyPath: string
  private saveQueue: Promise<void> = Promise.resolve()

  constructor(userDataPath: string) {
    this.filePath = join(userDataPath, 'config', 'funds.json')
    this.legacyPath = join(userDataPath, 'state.json')
  }

  async load(): Promise<PersistedState> {
    try {
      let migrated = false
      const content = await readFile(this.filePath, 'utf8').catch(async (error: NodeJS.ErrnoException) => {
        if (error.code !== 'ENOENT') throw error
        migrated = true
        return readFile(this.legacyPath, 'utf8')
      })
      const parsed = JSON.parse(content) as unknown
      if (!isRecord(parsed) || parsed.version !== 1) return createDefaultState()
      const restored: PersistedState = {
        version: 1,
        windowBounds: isRecord(parsed.windowBounds) && ['x', 'y', 'width', 'height'].every((key) => Number.isFinite((parsed.windowBounds as Record<string, unknown>)[key]))
          ? parsed.windowBounds as PersistedState['windowBounds'] : undefined,
        funds: sanitizeFunds(parsed.funds),
        quotes: sanitizeQuotes(parsed.quotes),
        settings: sanitizeSettings(parsed.settings),
        sourceHealth: sanitizeHealth(parsed.sourceHealth),
        lastRefreshAt: typeof parsed.lastRefreshAt === 'string' ? parsed.lastRefreshAt : null,
        panic: { realtime: null, daily: null, error: null, refreshedAt: null }
      }
      if (migrated) await this.save(restored)
      return restored
    } catch {
      return createDefaultState()
    }
  }

  save(state: PersistedState): Promise<void> {
    const { panic: _panic, ...fundState } = state
    const content = `${JSON.stringify(fundState, null, 2)}\n`
    const pending = this.saveQueue.catch(() => {}).then(async () => {
      await mkdir(dirname(this.filePath), { recursive: true })
      const temporaryPath = `${this.filePath}.tmp`
      await writeFile(temporaryPath, content, 'utf8')
      await rename(temporaryPath, this.filePath)
    })
    this.saveQueue = pending
    return pending
  }
}
