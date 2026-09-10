import { describe, expect, it, vi } from 'vitest'
import { PanicService, validateDate, validateLimit, validateChartType } from '../src/main/panic-service'
import { EngineHttpError, type PanicEngineClient } from '../src/main/engine-manager'

describe('恐慌数据隔离及IPC参数', () => {
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
