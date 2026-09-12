const { mkdirSync, readFileSync, writeFileSync } = require('node:fs')
const { join } = require('node:path')
const { app, BrowserWindow, ipcMain } = require('electron')

const statePath = process.env.FUND_VALUATION_STATE_PATH || process.argv[2]
if (!statePath) {
  throw new Error('Usage: electron scripts/capture-ui.cjs <state.json>')
}
const persisted = JSON.parse(readFileSync(statePath, 'utf8'))
const orderedFunds = [...persisted.funds].sort((left, right) => left.order - right.order)
const snapshot = {
  funds: orderedFunds,
  quotes: orderedFunds.map((fund) => persisted.quotes[fund.code]).filter(Boolean),
  settings: persisted.settings,
  sourceHealth: persisted.sourceHealth || [],
  refreshing: false,
  lastRefreshAt: persisted.lastRefreshAt,
  panic: {realtime:null,daily:null,error:null,refreshedAt:null},
  engine: {state:'ready',baseUrl:null,error:null,version:'4.0',databaseVersion:6,
    clientVersion:'3.0.0',logPath:''}
}
const risk = {model_version:'4.0',state:'insufficient_data',as_of:null,score:null,components:{},overheat:null,
  explanation:['截图夹具：无真实历史'],missing:['真实历史'],symbols:[],forecast:{state:'unavailable',as_of:null,published:{},reasons:['未发布']},
  signal:{action:'observe',reason:'等待真实数据',eligible:false},data_quality:{},observation:{required_days:20,observed_days:0,ready:false},jobs:[]}
const portfolio = {version:1,cash:0,profile:'balanced',lots:[],constraints:{
  equity_cap:.8,vol_target:.1,single_fund_cap:.2,industry_cap:.35,daily_change_cap:.1,no_trade_band:.05}}

app.disableHardwareAcceleration()

app.whenReady().then(async () => {
  ipcMain.handle('fund-app:get-state', () => snapshot)
  ipcMain.handle('risk:snapshot', () => risk)
  ipcMain.handle('risk:validation', () => ({status:'not_trained',publishable:false,reasons:['截图夹具'],metrics:{}}))
  ipcMain.handle('risk:history', (_event,symbol,kind) => ({symbol:symbol ?? 'market',kind:kind ?? 'published',records:[],missing:[]}))
  ipcMain.handle('risk:jobs', () => ({jobs:[]}))
  ipcMain.handle('risk:advice', () => ({state:'empty',as_of:null,profile:'balanced',summary:'截图夹具',items:[],constraints:{},reasons:[]}))
  ipcMain.handle('portfolio:get', () => portfolio)
  const window = new BrowserWindow({
    width: 1320,
    height: 920,
    show: false,
    backgroundColor: '#0e1117',
    webPreferences: {
      preload: join(__dirname, '..', 'out', 'preload', 'index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  })
  await window.loadFile(join(__dirname, '..', 'out', 'renderer', 'index.html'))
  await new Promise((resolve) => setTimeout(resolve, 400))
  const image = await window.webContents.capturePage()
  const outputDirectory = join(__dirname, '..', '..', 'output', 'playwright', 'capture')
  mkdirSync(outputDirectory, { recursive: true })
  writeFileSync(join(outputDirectory, 'ui-preview.png'), image.toPNG())

  await window.webContents.executeJavaScript("document.getElementById('settings-button').click()")
  await new Promise((resolve) => setTimeout(resolve, 100))
  const settingsImage = await window.webContents.capturePage()
  writeFileSync(join(outputDirectory, 'ui-settings-preview.png'), settingsImage.toPNG())

  await window.webContents.executeJavaScript("document.getElementById('settings-dialog').close(); document.querySelector('.fund-summary')?.click()")
  await new Promise((resolve) => setTimeout(resolve, 100))
  const detailImage = await window.webContents.capturePage()
  writeFileSync(join(outputDirectory, 'ui-detail-preview.png'), detailImage.toPNG())

  window.setContentSize(326, 440)
  await window.loadFile(join(__dirname, '..', 'out', 'renderer', 'index.html'), {
    query: { mode: 'mini' }
  })
  await new Promise((resolve) => setTimeout(resolve, 150))
  const miniImage = await window.webContents.capturePage()
  writeFileSync(join(outputDirectory, 'ui-mini-preview.png'), miniImage.toPNG())
  window.destroy()
  app.quit()
}).catch((error) => {
  console.error(error)
  app.exit(1)
})
