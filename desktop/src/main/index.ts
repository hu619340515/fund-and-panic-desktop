import { join, resolve, relative, isAbsolute } from 'node:path'
import { readFile, realpath } from 'node:fs/promises'
import {
  app,
  BrowserWindow,
  ipcMain,
  Menu,
  nativeImage,
  screen,
  Tray,
  type MenuItemConstructorOptions
} from 'electron'
import {
  IPC_CHANNELS,
  type ActionResult,
  type AppSnapshot,
  type PersistedState,
  type SettingsPatch
} from '@shared/types'
import { FundDataService } from './data/service'
import { createDefaultSources } from './data/sources'
import { AppStore } from './store'
import { PanicEngineManager } from './engine-manager'
import { PanicService, validateDate, validateLimit, validateChartType } from './panic-service'
import { createLogger } from './logger'
import { validateSettingsPatch } from './settings'

const APP_ID = 'com.hu619340515.fundandpanic'
// 独立诊断目录也由 Electron userData 管理；正常启动沿用系统默认目录。
const customUserData = app.commandLine.getSwitchValue('user-data-dir')
if (customUserData && isAbsolute(customUserData)) app.setPath('userData', customUserData)
const log = createLogger(app.getPath('userData'))
const hiddenLaunch = process.argv.includes('--hidden')
let revealRequested = !hiddenLaunch
const gotSingleInstanceLock = app.requestSingleInstanceLock()

if (!gotSingleInstanceLock) {
  app.quit()
}

let mainWindow: BrowserWindow | null = null
let miniWindow: BrowserWindow | null = null
let tray: Tray | null = null
let isQuitting = false
let refreshTimer: NodeJS.Timeout | null = null
let miniHideTimer: NodeJS.Timeout | null = null
let refreshPromise: Promise<void> | null = null
let refreshing = false
let persistedState: PersistedState
let store: AppStore
let dataService: FundDataService
let panicEngine: PanicEngineManager
let panicService: PanicService
let shutdownComplete = false

function resourcePath(fileName: string): string {
  if (app.isPackaged) return join(process.resourcesPath, 'resources', fileName)
  return join(__dirname, '..', '..', 'resources', fileName)
}

function loginExecutablePath(): string {
  return process.env.PORTABLE_EXECUTABLE_FILE || process.execPath
}

function applyLoginSetting(enabled: boolean): void {
  app.setLoginItemSettings({
    openAtLogin: enabled,
    path: loginExecutablePath(),
    args: ['--hidden']
  })
}

function snapshot(): AppSnapshot {
  const funds = [...persistedState.funds].sort((a, b) => a.order - b.order)
  return {
    funds,
    quotes: funds
      .map((fund) => persistedState.quotes[fund.code])
      .filter((quote) => quote !== undefined),
    settings: { ...persistedState.settings },
    sourceHealth: dataService?.getHealth() ?? persistedState.sourceHealth,
    refreshing,
    lastRefreshAt: persistedState.lastRefreshAt,
    panic: persistedState.panic ?? { realtime: null, daily: null, error: null, refreshedAt: null },
    engine: panicEngine?.status() ?? { state: 'stopped', baseUrl: null, error: null, version: '3.0-realtime', databaseVersion: null, clientVersion: app.getVersion(), logPath: join(app.getPath('userData'), 'logs', 'engine-process.log') }
  }
}

function broadcastState(): void {
  const current = snapshot()
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(IPC_CHANNELS.STATE_CHANGED, current)
  }
  if (miniWindow && !miniWindow.isDestroyed()) {
    miniWindow.webContents.send(IPC_CHANNELS.STATE_CHANGED, current)
  }
  updateTray()
}

function showWindow(): void {
  revealRequested = true
  hideMiniWindow()
  if (!mainWindow || mainWindow.isDestroyed()) return
  if (mainWindow.isMinimized()) mainWindow.restore()
  mainWindow.show()
  mainWindow.focus()
}

function updateTray(): void {
  if (!tray) return
  const template: MenuItemConstructorOptions[] = [
    { label: '打开基金与A股风险看板', click: showWindow },
    {
      label: refreshing ? '正在刷新…' : '立即刷新',
      enabled: !refreshing,
      click: () => { void refreshFunds(); if (panicEngine.status().state === 'ready') void panicService.refresh() }
    },
    { type: 'separator' },
    {
      label: '开机启动',
      type: 'checkbox',
      checked: persistedState.settings.launchAtLogin,
      click: (item) => void updateSettings({ launchAtLogin: item.checked })
    },
    { type: 'separator' },
    {
      label: '退出',
      click: () => {
        isQuitting = true
        app.quit()
      }
    }
  ]
  tray.setContextMenu(Menu.buildFromTemplate(template))
  const time = persistedState.lastRefreshAt
    ? new Date(persistedState.lastRefreshAt).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    : '尚未刷新'
  tray.setToolTip(`基金与A股风险看板 · ${time}`)
}

function createMiniWindow(): BrowserWindow {
  if (miniWindow && !miniWindow.isDestroyed()) return miniWindow

  miniWindow = new BrowserWindow({
    width: 326,
    height: 440,
    show: false,
    frame: false,
    focusable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    resizable: false,
    movable: false,
    minimizable: false,
    maximizable: false,
    backgroundColor: '#0e1117',
    webPreferences: {
      preload: join(__dirname, '..', 'preload', 'index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true
    }
  })

  miniWindow.setOpacity(persistedState.settings.windowOpacity)
  miniWindow.setIgnoreMouseEvents(true)
  miniWindow.setMenu(null)
  miniWindow.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  miniWindow.webContents.on('will-navigate', (event) => event.preventDefault())
  miniWindow.on('closed', () => {
    miniWindow = null
  })

  if (!app.isPackaged && process.env.ELECTRON_RENDERER_URL) {
    void miniWindow.loadURL(`${process.env.ELECTRON_RENDERER_URL}?mode=mini`)
  } else {
    void miniWindow.loadFile(join(__dirname, '..', 'renderer', 'index.html'), {
      query: { mode: 'mini' }
    })
  }
  return miniWindow
}

function positionMiniWindow(window: BrowserWindow): void {
  if (!tray) return
  const trayBounds = tray.getBounds()
  const display = screen.getDisplayNearestPoint({ x: trayBounds.x, y: trayBounds.y })
  const workArea = display.workArea
  const bounds = window.getBounds()
  const centerX = trayBounds.x + Math.round(trayBounds.width / 2)
  const desiredX = Math.round(centerX - bounds.width / 2)
  const x = Math.min(
    Math.max(desiredX, workArea.x),
    workArea.x + workArea.width - bounds.width
  )
  const taskbarIsBelow = trayBounds.y > workArea.y + workArea.height / 2
  const desiredY = taskbarIsBelow
    ? trayBounds.y - bounds.height - 8
    : trayBounds.y + trayBounds.height + 8
  const y = Math.min(
    Math.max(desiredY, workArea.y),
    workArea.y + workArea.height - bounds.height
  )
  window.setPosition(x, y, false)
}

function showMiniWindow(): void {
  if (miniHideTimer) {
    clearTimeout(miniHideTimer)
    miniHideTimer = null
  }
  const window = createMiniWindow()
  const reveal = (): void => {
    if (window.isDestroyed()) return
    positionMiniWindow(window)
    window.showInactive()
  }
  if (window.webContents.isLoading()) window.webContents.once('did-finish-load', reveal)
  else reveal()
}

function hideMiniWindow(delayMs = 0): void {
  if (miniHideTimer) clearTimeout(miniHideTimer)
  miniHideTimer = null
  if (delayMs > 0) {
    miniHideTimer = setTimeout(() => hideMiniWindow(), delayMs)
    return
  }
  if (miniWindow && !miniWindow.isDestroyed()) miniWindow.hide()
}

function createTray(): void {
  const image = nativeImage.createFromPath(resourcePath('tray.ico'))
  tray = new Tray(image)
  tray.on('mouse-enter', showMiniWindow)
  tray.on('mouse-leave', () => hideMiniWindow(350))
  tray.on('click', showWindow)
  tray.on('double-click', showWindow)
  tray.on('right-click', () => hideMiniWindow())
  updateTray()
}

function createWindow(): void {
  const savedBounds = persistedState.windowBounds
  const workArea = screen.getDisplayMatching(savedBounds ?? { x: 0, y: 0, width: 1320, height: 920 }).workArea
  const width = Math.min(workArea.width, Math.max(900, savedBounds?.width ?? 1320))
  const height = Math.min(workArea.height, Math.max(620, savedBounds?.height ?? 920))
  mainWindow = new BrowserWindow({
    width,
    height,
    ...(savedBounds ? { x: Math.max(workArea.x, Math.min(savedBounds.x, workArea.x + workArea.width - width)),
      y: Math.max(workArea.y, Math.min(savedBounds.y, workArea.y + workArea.height - height)) } : {}),
    minWidth: 900,
    minHeight: 620,
    show: false,
    alwaysOnTop: persistedState.settings.alwaysOnTop,
    minimizable: true,
    skipTaskbar: false,
    title: '基金与A股风险看板',
    icon: resourcePath('app.ico'),
    backgroundColor: '#f6f7f9',
    webPreferences: {
      preload: join(__dirname, '..', 'preload', 'index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true
    }
  })

  mainWindow.setOpacity(persistedState.settings.windowOpacity)

  mainWindow.setMenu(null)
  mainWindow.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  mainWindow.webContents.on('will-navigate', (event) => event.preventDefault())
  mainWindow.on('close', (event) => {
    if (mainWindow && !mainWindow.isMaximized()) persistedState.windowBounds = mainWindow.getBounds()
    if (!isQuitting) void persist().catch((error) => log('窗口设置保存失败', error))
    if (!isQuitting) {
      event.preventDefault()
      mainWindow?.hide()
    }
  })
  mainWindow.on('closed', () => {
    mainWindow = null
  })

  if (!app.isPackaged && process.env.ELECTRON_RENDERER_URL) {
    void mainWindow.loadURL(process.env.ELECTRON_RENDERER_URL)
  } else {
    void mainWindow.loadFile(join(__dirname, '..', 'renderer', 'index.html'))
  }

  mainWindow.once('ready-to-show', () => {
    if (revealRequested) showWindow()
  })
}

async function persist(): Promise<void> {
  persistedState.sourceHealth = dataService.getHealth()
  await store.save(persistedState)
}

async function refreshFunds(): Promise<void> {
  if (refreshPromise) return refreshPromise
  refreshPromise = (async () => {
    refreshing = true
    broadcastState()
    try {
      const codes = persistedState.funds.sort((a, b) => a.order - b.order).map((fund) => fund.code)
      if (codes.length > 0) {
        const refreshed = await dataService.refresh(codes, persistedState.quotes)
        const nextQuotes: PersistedState['quotes'] = {}
        for (const fund of persistedState.funds) {
          const quote = refreshed[fund.code] ?? persistedState.quotes[fund.code]
          if (quote) nextQuotes[fund.code] = quote
        }
        persistedState.quotes = nextQuotes
      } else {
        persistedState.quotes = {}
      }
      persistedState.lastRefreshAt = new Date().toISOString()
      await persist()
    } catch (error) {
      log('刷新基金数据失败', error)
    } finally {
      refreshing = false
      refreshPromise = null
      broadcastState()
    }
  })()
  return refreshPromise
}

function scheduleRefresh(): void {
  if (refreshTimer) clearInterval(refreshTimer)
  refreshTimer = null
  if (!persistedState.settings.autoRefresh) return
  refreshTimer = setInterval(
    () => { void refreshFunds(); if (panicEngine.status().state === 'ready') void panicService.refresh() },
    persistedState.settings.refreshIntervalSeconds * 1000
  )
}

async function updateSettings(rawPatch: SettingsPatch): Promise<ActionResult<AppSnapshot>> {
  try {
    const patch = validateSettingsPatch(rawPatch)
    const settings = { ...persistedState.settings, ...patch }
    await store.save({ ...persistedState, settings })
    persistedState.settings = settings
    if (patch.launchAtLogin !== undefined) applyLoginSetting(settings.launchAtLogin)
    if (patch.alwaysOnTop !== undefined) mainWindow?.setAlwaysOnTop(settings.alwaysOnTop)
    if (patch.windowOpacity !== undefined) {
      mainWindow?.setOpacity(settings.windowOpacity)
      miniWindow?.setOpacity(settings.windowOpacity)
    }
    scheduleRefresh()
    broadcastState()
    return { ok: true, data: snapshot() }
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : '设置保存失败' }
  }
}

function registerIpc(): void {
  const handle = (channel: string, listener: Parameters<typeof ipcMain.handle>[1]): void => {
    ipcMain.handle(channel, (event, ...args) => {
      const trusted = event.sender === mainWindow?.webContents || event.sender === miniWindow?.webContents
      if (!trusted || event.senderFrame !== event.sender.mainFrame) throw new Error('拒绝非客户端主页面的 IPC 请求')
      return listener(event, ...args)
    })
  }
  handle(IPC_CHANNELS.GET_STATE, () => snapshot())

  handle(IPC_CHANNELS.ADD_FUND, async (_event, rawCode: unknown): Promise<ActionResult<AppSnapshot>> => {
    const code = String(rawCode ?? '').trim()
    if (!/^\d{6}$/.test(code)) return { ok: false, error: '请输入六位基金代码' }
    if (persistedState.funds.some((fund) => fund.code === code)) {
      return { ok: false, error: '该基金已经在列表中' }
    }

    const validation = await dataService.refresh([code], {})
    if (persistedState.funds.some((fund) => fund.code === code)) return { ok: false, error: '该基金已经在列表中' }
    const quote = validation[code]
    if (!quote || quote.source === 'unavailable') {
      return { ok: false, error: '未找到该基金，或当前数据源暂时不可用' }
    }

    persistedState.funds.push({
      code,
      order: persistedState.funds.length,
      addedAt: new Date().toISOString()
    })
    persistedState.quotes[code] = quote
    await persist()
    broadcastState()
    return { ok: true, data: snapshot() }
  })

  handle(IPC_CHANNELS.REMOVE_FUND, async (_event, rawCode: unknown): Promise<ActionResult<AppSnapshot>> => {
    const code = String(rawCode ?? '').trim()
    const before = persistedState.funds.length
    persistedState.funds = persistedState.funds
      .filter((fund) => fund.code !== code)
      .map((fund, order) => ({ ...fund, order }))
    if (persistedState.funds.length === before) return { ok: false, error: '基金不存在' }
    delete persistedState.quotes[code]
    await persist()
    broadcastState()
    return { ok: true, data: snapshot() }
  })

  handle(
    IPC_CHANNELS.REORDER_FUNDS,
    async (_event, rawCodes: unknown): Promise<ActionResult<AppSnapshot>> => {
      if (!Array.isArray(rawCodes)) return { ok: false, error: '排序数据无效' }
      const codes = rawCodes.map(String)
      const existing = persistedState.funds.map((fund) => fund.code)
      if (
        codes.length !== existing.length ||
        new Set(codes).size !== codes.length ||
        existing.some((code) => !codes.includes(code))
      ) {
        return { ok: false, error: '排序数据与基金列表不一致' }
      }
      const byCode = new Map(persistedState.funds.map((fund) => [fund.code, fund]))
      persistedState.funds = codes.map((code, order) => ({ ...byCode.get(code)!, order }))
      await persist()
      broadcastState()
      return { ok: true, data: snapshot() }
    }
  )

  handle(IPC_CHANNELS.REFRESH, async (): Promise<ActionResult<AppSnapshot>> => {
    await refreshFunds()
    return { ok: true, data: snapshot() }
  })

  handle(
    IPC_CHANNELS.UPDATE_SETTINGS,
    (_event, patch: SettingsPatch): Promise<ActionResult<AppSnapshot>> => updateSettings(patch ?? {})
  )
  handle(IPC_CHANNELS.PANIC_ENGINE_STATUS, () => panicEngine.status())
  handle(IPC_CHANNELS.PANIC_REALTIME, async () => panicEngine.get('/api/v1/realtime'))
  handle(IPC_CHANNELS.PANIC_DAILY, async () => panicEngine.get('/api/v1/daily/latest'))
  handle(IPC_CHANNELS.PANIC_REALTIME_HISTORY, async (_event, rawDate: unknown) => {
    const date = validateDate(rawDate)
    return panicEngine.get(`/api/v1/realtime/history${date ? `?trade_date=${date}` : ''}`)
  })
  handle(IPC_CHANNELS.PANIC_DAILY_HISTORY, async (_event, rawLimit: unknown) =>
    panicEngine.get(`/api/v1/daily/history?limit=${validateLimit(rawLimit)}`))
  handle(IPC_CHANNELS.PANIC_SOURCES, async () => panicEngine.get('/api/v1/sources'))
  handle(IPC_CHANNELS.PANIC_REFRESH, async () => {
    if (panicEngine.status().state === 'error' || panicEngine.status().state === 'stopped') await panicEngine.restart()
    else if (panicEngine.status().state === 'starting') await panicEngine.start()
    return panicService.refresh()
  })
  handle(IPC_CHANNELS.PANIC_CHART, async (_event, rawType: unknown) => {
    const type = validateChartType(rawType)
    const result = await panicEngine.post(`/api/v1/chart?type=${type}`) as { path: string }
    const root = await realpath(join(app.getPath('userData'), 'reports'))
    const path = await realpath(resolve(result.path))
    const childPath = relative(root, path)
    if (!childPath || childPath.startsWith('..') || isAbsolute(childPath) || !path.endsWith('.png')) throw new Error('图表路径无效')
    const content = await readFile(path)
    return { path, dataUrl: `data:image/png;base64,${content.toString('base64')}` }
  })
}

async function bootstrap(): Promise<void> {
  log(`客户端启动 version=${app.getVersion()} platform=${process.platform} arch=${process.arch}`)
  app.setAppUserModelId(APP_ID)
  Menu.setApplicationMenu(null)

  store = new AppStore(app.getPath('userData'))
  persistedState = await store.load()
  dataService = new FundDataService({
    sources: createDefaultSources(),
    initialHealth: persistedState.sourceHealth,
    onHealthChanged: (health) => {
      persistedState.sourceHealth = health
    }
  })
  panicEngine = new PanicEngineManager({
    resourcesPath: app.isPackaged ? process.resourcesPath : join(__dirname, '..', '..', '..'),
    userDataPath: app.getPath('userData'),
    isPackaged: app.isPackaged,
    clientVersion: app.getVersion(),
    onStatusChanged: (status) => {
      if (status.state === 'error') panicService?.disconnected()
      if (persistedState) broadcastState()
    }
  })
  panicService = new PanicService(panicEngine, (value) => {
    persistedState.panic = value
    broadcastState()
  })

  applyLoginSetting(persistedState.settings.launchAtLogin)
  registerIpc()
  createWindow()
  createTray()
  scheduleRefresh()
  void refreshFunds()
  void panicEngine.start().then((status) => {
    if (status.state === 'ready') return panicService.refresh(persistedState.settings.autoRefresh)
    panicService.disconnected()
  }).catch((error) => log('引擎初始化失败', error))
}

if (gotSingleInstanceLock) {
  app.on('second-instance', showWindow)
  app.on('before-quit', (event) => {
    if (shutdownComplete) return
    event.preventDefault()
    isQuitting = true
    if (refreshTimer) clearInterval(refreshTimer)
    if (miniHideTimer) clearTimeout(miniHideTimer)
    if (mainWindow && !mainWindow.isDestroyed() && !mainWindow.isMaximized()) persistedState.windowBounds = mainWindow.getBounds()
    void Promise.allSettled([panicEngine?.stop(), store && persistedState ? persist() : Promise.resolve()])
      .finally(() => { shutdownComplete = true; app.quit() })
  })
  app.on('window-all-closed', () => {
    // 主窗口关闭时应用继续驻留系统托盘。
  })
  app.on('activate', showWindow)
  void app.whenReady().then(bootstrap)
}
