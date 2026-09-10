import { describe, expect, it } from 'vitest'
import {
  chartPaths,
  formatCompactMoney,
  historyPoints,
  panicUiState,
  unwrapList,
  unwrapRecord
} from '../src/renderer/src/panic-view'

describe('panic view data normalization', () => {
  it('兼容直接响应和 data/result 包装，但不把普通对象误判为历史数组', () => {
    expect(unwrapRecord({ data: { realtime_panic_index: 61 } })).toEqual({ realtime_panic_index: 61 })
    expect(unwrapRecord({ result: { final_panic_index: 58 } })).toEqual({ final_panic_index: 58 })
    expect(unwrapList({ items: [{ trade_date: '2026-09-09' }] })).toHaveLength(1)
    expect(unwrapList({ realtime_panic_index: 61 })).toEqual([])
  })

  it('盘中曲线只保留有效真实记录，不补点，并按时间排序', () => {
    const points = historyPoints([
      { timestamp: '2026-09-10T10:10:00+08:00', realtime_panic_index: 62, realtime_panic_index_raw: 67 },
      { timestamp: '2026-09-10T10:00:00+08:00', realtime_panic_index: 58, realtime_panic_index_raw: 60 },
      { timestamp: '2026-09-10T10:05:00+08:00', realtime_panic_index: null },
      { timestamp: '2026-09-09T10:05:00+08:00', realtime_panic_index: 55 },
      { timestamp: '无效时间', realtime_panic_index: 99 }
    ], 'intraday', new Date('2026-09-10T12:00:00+08:00'))

    expect(points).toEqual([
      { time: '2026-09-10T10:00:00+08:00', value: 58, raw: 60 },
      { time: '2026-09-10T10:10:00+08:00', value: 62, raw: 67 }
    ])
  })

  it('近一年曲线过滤更早记录，不伪造缺少的交易日', () => {
    const points = historyPoints([
      { trade_date: '2025-09-09', final_panic_index: 40 },
      { trade_date: '2025-09-10', final_panic_index: 41 },
      { trade_date: '2026-09-10', final_panic_index: 55 },
      { trade_date: '2026-09-11', final_panic_index: 90 }
    ], 'daily', new Date('2026-09-10T12:00:00+08:00'))

    expect(points.map((point) => point.time)).toEqual(['2025-09-10', '2026-09-10'])
  })

  it('空历史明确保持为空', () => {
    expect(historyPoints([], 'intraday')).toEqual([])
    expect(chartPaths([])).toEqual({ display: '', raw: '', minimum: 0, maximum: 100 })
  })
})

describe('panic view state', () => {
  const now = new Date('2026-09-10T10:30:00+08:00')

  it('区分 loading、success、stale、error 和 empty', () => {
    expect(panicUiState({ refreshing: true, realtime: null, daily: null, error: null, now })).toBe('loading')
    expect(panicUiState({ refreshing: false, realtime: null, daily: null, error: null, now })).toBe('empty')
    expect(panicUiState({ refreshing: false, realtime: null, daily: null, error: '网络失败', now })).toBe('error')
    expect(panicUiState({
      refreshing: false,
      realtime: { timestamp: '2026-09-10T10:20:00+08:00' },
      daily: null,
      error: null,
      now
    })).toBe('success')
    expect(panicUiState({
      refreshing: false,
      realtime: { timestamp: '2026-09-10T09:55:00+08:00' },
      daily: null,
      error: null,
      now
    })).toBe('stale')
  })

  it('已有数据时刷新错误标记为 stale，保留上次成功数据供展示', () => {
    expect(panicUiState({
      refreshing: false,
      realtime: { timestamp: '2026-09-10T10:25:00+08:00' },
      daily: null,
      error: '刷新失败',
      now
    })).toBe('stale')
  })
})

describe('panic chart and numeric display', () => {
  it('原始值和显示值共用同一纵轴', () => {
    const paths = chartPaths([
      { time: '10:00', value: 50, raw: 50 },
      { time: '10:05', value: 60, raw: 80 }
    ], 100, 100, 10)
    expect(paths.minimum).toBe(46)
    expect(paths.maximum).toBe(84)
    expect(paths.display).toContain('L90.0,60.5')
    expect(paths.raw).toContain('L90.0,18.4')
  })

  it('成交额按中文单位显示并安全处理空值', () => {
    expect(formatCompactMoney(1_260_000_000_000)).toBe('1.26 万亿元')
    expect(formatCompactMoney(88_000_000_000)).toBe('880 亿元')
    expect(formatCompactMoney(null)).toBe('--')
  })
})
