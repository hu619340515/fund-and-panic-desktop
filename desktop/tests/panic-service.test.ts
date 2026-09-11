import { describe, expect, it, vi } from 'vitest'
import { PanicService, validateDate, validateLimit, validateChartType } from '../src/main/panic-service'
import { EngineHttpError, type PanicEngineClient } from '../src/main/engine-manager'

describe('恐慌数据隔离及IPC参数', () => {
  it('采集失败仍读取正式收盘，不读取旧实时缓存，显示来源及重试时间', async () => {
    const engine = {
      get: vi.fn(async () => ({trade_date:'2026-09-10', final_panic_index:38})),
      post: vi.fn(async () => { throw new EngineHttpError(503, {
        code:'collection_failed',message:'指数采集失败：腾讯行情不可用',retry_after_seconds:60
      }) }),
      status: () => ({logPath:'用户数据/logs/engine.log'})
    } as unknown as PanicEngineClient
    const failed = await new PanicService(engine, vi.fn()).refresh()
    expect(failed.realtime).toBeNull()
    expect(failed.daily).toEqual({trade_date:'2026-09-10',final_panic_index:38})
    expect(engine.get).toHaveBeenCalledExactlyOnceWith('/api/v1/daily/latest')
    expect(failed.error).toContain('腾讯行情不可用')
    expect(failed.error).toContain('60 秒后重试')
  })

  it('收盘读取失败不会抹掉成功采集的实时数据', async () => {
    const engine = {
      get: vi.fn(async (path: string) => {
        if (path.includes('daily')) throw new EngineHttpError(500, '收盘记录读取失败')
        return {realtime_panic_index:42}
      }), post: vi.fn(async () => ({})), status: () => ({logPath:'logs/engine.log'})
    } as unknown as PanicEngineClient
    const result = await new PanicService(engine, vi.fn()).refresh()
    expect(result.realtime).toEqual({realtime_panic_index:42})
    expect(result.error).toContain('收盘记录读取失败')
  })
  it('拒绝非法日期、越界条数及图表路径注入', () => {
    for (const value of ['2026-02-30', '2026-09-10&limit=5000', 123, null]) expect(() => validateDate(value)).toThrow()
    for (const value of [0, -1, 5001, 1.5, '20', null]) expect(() => validateLimit(value)).toThrow()
    expect(validateDate('2026-09-10')).toBe('2026-09-10')
    expect(validateLimit(undefined)).toBe(366)
    expect(() => validateChartType('../../x')).toThrow()
  })
  it('失败清空实时结果，收盘404视为无数据，并发采集合并', async () => {
    const engine = {
      get: vi.fn(async (path: string) => {
        if (path.includes('daily')) throw new EngineHttpError(404, '暂无收盘')
        return {realtime_panic_index:42}
      }),
      post: vi.fn(async () => ({})),
      status: () => ({logPath:'用户数据/logs/engine.log'})
    } as unknown as PanicEngineClient
    const service = new PanicService(engine, vi.fn())
    const [first, second] = await Promise.all([service.refresh(), service.refresh()])
    expect(first.realtime).toEqual({realtime_panic_index:42})
    expect(first.daily).toBeNull()
    expect(second).toEqual(first)
    expect(engine.post).toHaveBeenCalledTimes(1)
    vi.mocked(engine.post).mockRejectedValueOnce(new Error('网络不可用'))
    const failed = await service.refresh()
    expect(failed.realtime).toBeNull()
    expect(failed.error).toContain('网络不可用')
    expect(failed.refreshing).toBe(false)
  })
})
