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
  lastRefreshAt: persisted.lastRefreshAt
}

app.disableHardwareAcceleration()

app.whenReady().then(async () => {
  ipcMain.handle('fund-app:get-state', () => snapshot)
  const window = new BrowserWindow({
    width: 420,
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
  const outputDirectory = join(__dirname, '..', 'dist')
  mkdirSync(outputDirectory, { recursive: true })
  writeFileSync(join(outputDirectory, 'ui-preview.png'), image.toPNG())

  await window.webContents.executeJavaScript("document.getElementById('settings-button').click()")
  await new Promise((resolve) => setTimeout(resolve, 100))
  const settingsImage = await window.webContents.capturePage()
  writeFileSync(join(outputDirectory, 'ui-settings-preview.png'), settingsImage.toPNG())

  await window.webContents.executeJavaScript("document.getElementById('settings-dialog').close(); document.querySelector('.fund-summary').click()")
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
