import type { PanicSnapshot } from '../shared/types'
import { EngineHttpError, type PanicEngineClient } from './engine-manager'

export function validateDate(value: unknown): string | undefined {
  if (value === undefined) return undefined
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value) ||
    !Number.isFinite(Date.parse(value)) || new Date(value).toISOString().slice(0, 10) !== value) throw new Error('日期必须是有效的 YYYY-MM-DD')
  return value
}

export function validateLimit(value: unknown): number {
  if (value === undefined) return 366
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 1 || value > 5000) throw new Error('历史条数必须为 1 至 5000 的整数')
  return value
}

export function validateChartType(value: unknown): 'intraday' | 'daily' {
  if (value !== 'intraday' && value !== 'daily') throw new Error('图表类型无效')
  return value
}

export class PanicService {
  private pending: Promise<PanicSnapshot> | null = null
  private current: PanicSnapshot = { realtime: null, daily: null, error: null, refreshedAt: null, refreshing: false }

  constructor(private readonly engine: PanicEngineClient, private readonly onChange: (value: PanicSnapshot) => void) {}

  private publish(patch: Partial<PanicSnapshot>): void {
    this.current = { ...this.current, ...patch }
    this.onChange({ ...this.current })
  }

  disconnected(): void {
    this.publish({ realtime: null, error: this.engine.status().error ?? '引擎未连接' })
  }

  refresh(collect = true): Promise<PanicSnapshot> {
    if (this.pending) return this.pending
    this.pending = this.run(collect).finally(() => { this.pending = null })
    return this.pending
  }

  private async read(path: string): Promise<Record<string, unknown> | null> {
    try { return await this.engine.get(path) as Record<string, unknown> }
    catch (error) { if (error instanceof EngineHttpError && error.statusCode === 404) return null; throw error }
  }

  private async run(collect: boolean): Promise<PanicSnapshot> {
    this.publish({ refreshing: true, error: null })
    const [realtimeResult, dailyResult] = await Promise.allSettled([
      (async () => {
        if (collect) await this.engine.post('/api/v1/realtime/refresh')
        return this.read('/api/v1/realtime')
      })(),
      this.read('/api/v1/daily/latest')
    ])
    const errors = [realtimeResult, dailyResult].flatMap((result) => {
      if (result.status === 'fulfilled') return []
      const error: unknown = result.reason
      const retry = error instanceof EngineHttpError && error.retryAfterSeconds
        ? `请在 ${error.retryAfterSeconds} 秒后重试` : '请检查网络和数据源后重试'
      return [`${error instanceof Error ? error.message : String(error)}。${retry}`]
    })
    this.publish({
      realtime: realtimeResult.status === 'fulfilled' ? realtimeResult.value : null,
      daily: dailyResult.status === 'fulfilled' ? dailyResult.value : this.current.daily,
      error: errors.length ? `${errors.join('；')}；日志：${this.engine.status().logPath}` : null,
      refreshedAt: new Date().toISOString(), refreshing: false
    })
    return { ...this.current }
  }
}
