import { mkdtemp, readFile, writeFile, mkdir } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { AppStore, createDefaultState } from '../src/main/store'

describe('AppStore', () => {
  it('迁移旧配置并保留空基金列表，但不恢复任何恐慌实时缓存', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'fund-migration-'))
    const legacy = createDefaultState()
    legacy.funds = []
    legacy.settings.autoRefresh = false
    legacy.panic = { realtime: {realtime_panic_index:55}, daily:null, error:null, refreshedAt:new Date().toISOString() }
    await writeFile(join(directory,'state.json'), JSON.stringify(legacy), 'utf8')
    const restored = await new AppStore(directory).load()
    expect(restored.funds).toEqual([])
    expect(restored.settings.autoRefresh).toBe(false)
    expect(restored.panic?.realtime).toBeNull()
    const saved = JSON.parse(await readFile(join(directory,'config/funds.json'),'utf8'))
    expect(saved.panic).toBeUndefined()
    expect(JSON.parse(await readFile(join(directory,'state.json'),'utf8')).panic.realtime.realtime_panic_index).toBe(55)
  })

  it('新配置中意外存在的实时缓存也不会恢复', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'fund-cache-'))
    await mkdir(join(directory,'config'))
    await writeFile(join(directory,'config/funds.json'), JSON.stringify({...createDefaultState(),panic:{realtime:{realtime_panic_index:99}}}), 'utf8')
    expect((await new AppStore(directory).load()).panic?.realtime).toBeNull()
  })
  it('starts with the original eleven funds', () => {
    const state = createDefaultState()
    expect(state.funds).toHaveLength(11)
    expect(state.settings.alwaysOnTop).toBe(true)
    expect(state.settings.windowOpacity).toBe(0.94)
  })

  it('persists an intentionally empty fund list', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'fund-valuation-store-'))
    const store = new AppStore(directory)
    const state = createDefaultState()
    state.funds = []
    await store.save(state)
    const restored = await store.load()
    expect(restored.funds).toEqual([])
    expect(JSON.parse(await readFile(join(directory, 'config', 'funds.json'), 'utf8')).version).toBe(1)
  })

  it('persists floating-window preferences', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'fund-valuation-store-'))
    const store = new AppStore(directory)
    const state = createDefaultState()
    state.settings.alwaysOnTop = false
    state.settings.windowOpacity = 0.72
    await store.save(state)

    const restored = await store.load()
    expect(restored.settings.alwaysOnTop).toBe(false)
    expect(restored.settings.windowOpacity).toBe(0.72)
  })
})
