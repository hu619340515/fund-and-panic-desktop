// 真实 Electron 图表 E2E：数据库内容仅为现有离线 fixture 派生的测试夹具，不是生产行情。
const { _electron: electron } = require('../desktop/node_modules/playwright')
const assert = require('node:assert/strict')
const { execFile } = require('node:child_process')
const fs = require('node:fs/promises')
const path = require('node:path')
const { promisify } = require('node:util')

const execFileAsync = promisify(execFile)
const root = path.resolve(__dirname, '..')
const output = path.join(root, 'output', 'playwright', 'chart-fixture')
const python = path.join(root, 'engine', '.venv', 'Scripts', 'python.exe')
const seedScript = path.join(root, 'packaging', 'seed-chart-fixture.py')

async function poll(operation, predicate, timeout = 60000) {
  const deadline = Date.now() + timeout
  let value
  while (Date.now() < deadline) {
    value = await operation()
    if (predicate(value)) return value
    await new Promise(resolve => setTimeout(resolve, 150))
  }
  throw new Error(`等待状态超时：${JSON.stringify(value)}`)
}

async function seed(database) {
  await fs.access(python)
  const { stdout } = await execFileAsync(python, ['-X', 'utf8', seedScript, database], {
    cwd: root,
    env: { ...process.env, PYTHONUTF8: '1' },
    windowsHide: true
  })
  return JSON.parse(stdout)
}

async function sqliteCounts(database, tradeDate) {
  const code = [
    'import json, sqlite3, sys',
    'db, day = sys.argv[1:]',
    'con = sqlite3.connect(db)',
    'out = {',
    "  'intraday_current_records': con.execute('select count(*) from realtime_panic_index where trade_date=?', (day,)).fetchone()[0],",
    "  'intraday_all_records': con.execute('select count(*) from realtime_panic_index').fetchone()[0],",
    "  'daily_records': con.execute('select count(*) from daily_panic_index').fetchone()[0],",
    "  'daily_dates': [row[0] for row in con.execute('select trade_date from daily_panic_index order by trade_date')]",
    '}',
    'print(json.dumps(out, ensure_ascii=False))'
  ].join('\n')
  const { stdout } = await execFileAsync(python, ['-c', code, database, tradeDate], { cwd: root, windowsHide: true })
  return JSON.parse(stdout)
}

function assertPng(result, reportsDirectory) {
  assert.match(result.path, new RegExp(`^${reportsDirectory.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`))
  assert.match(result.path, /\.png$/i)
  assert.match(result.dataUrl, /^data:image\/png;base64,/)
  const bytes = Buffer.from(result.dataUrl.slice('data:image/png;base64,'.length), 'base64')
  assert.ok(bytes.length > 8)
  assert.deepEqual([...bytes.subarray(0, 8)], [137, 80, 78, 71, 13, 10, 26, 10])
}

async function main() {
  const requestedMode = process.argv[2] ?? 'dev'
  const packaged = requestedMode !== 'dev'
  const executablePath = packaged
    ? path.resolve(requestedMode)
    : require('../desktop/node_modules/electron')
  const mode = packaged ? path.basename(executablePath, path.extname(executablePath)) : 'dev'
  await fs.access(path.join(root, 'desktop', 'out', 'main', 'index.js'))
  await fs.access(executablePath)
  await fs.mkdir(output, { recursive: true })
  const userData = await fs.mkdtemp(path.join(output, `中文用户目录-${mode}-`))
  const database = path.join(userData, 'data', 'panic-index.db')
  const reports = path.join(userData, 'reports')
  await fs.mkdir(path.dirname(database), { recursive: true })
  await fs.mkdir(path.join(userData, 'config'), { recursive: true })
  await fs.writeFile(path.join(userData, 'config', 'funds.json'), JSON.stringify({
    version: 1, funds: [], quotes: {}, sourceHealth: [], lastRefreshAt: null,
    settings: { autoRefresh: false, refreshIntervalSeconds: 60, launchAtLogin: false, alwaysOnTop: false, windowOpacity: 1, closeToTray: true }
  }), 'utf8')
  const seeded = await seed(database)
  const dbBeforeLaunch = await sqliteCounts(database, seeded.trade_date)
  assert.deepEqual(dbBeforeLaunch, {
    intraday_current_records: seeded.intraday_current_records,
    intraday_all_records: seeded.intraday_all_records,
    daily_records: seeded.daily_records,
    daily_dates: seeded.daily_dates
  })

  const env = { ...process.env, PYTHON: packaged ? '不存在的系统Python.exe' : 'python', PYTHONHOME: '', PYTHONPATH: '' }
  if (packaged) env.PATH = path.join(process.env.SystemRoot, 'System32')
  delete env.ELECTRON_RUN_AS_NODE
  console.log(`图表 E2E 启动：${packaged ? executablePath : 'desktop/out'}；测试夹具目录：${userData}`)
  const app = /Portable-/i.test(executablePath)
    ? await require('./launch-portable.cjs').launchPortable(executablePath, [`--user-data-dir=${userData}`, '--hidden'], env)
    : await electron.launch({
    executablePath,
    args: [...(packaged ? [] : [path.join(root, 'desktop')]), `--user-data-dir=${userData}`, '--hidden'],
    env,
    timeout: 60000
  })
  const checks = []
  try {
    const page = await app.firstWindow()
    await page.waitForFunction(() => Boolean(window.fundApp))
    console.log('图表 E2E：preload 已就绪，等待内置引擎')
    const health = await poll(
      () => page.evaluate(() => window.fundApp.panic.getHealth()),
      value => value.state === 'ready' || value.state === 'error'
    )
    assert.equal(health.state, 'ready', health.error)
    assert.equal(health.version, '3.0-realtime')
    console.log('图表 E2E：内置引擎已就绪，等待曲线渲染')
    await page.locator('#intraday-chart svg').waitFor({ state: 'attached' })
    await page.locator('#daily-chart svg').waitFor({ state: 'attached' })
    console.log('图表 E2E：曲线已渲染')

    const histories = await page.evaluate(async tradeDate => ({
      current: await window.fundApp.panic.getRealtimeHistory(tradeDate),
      all: await window.fundApp.panic.getRealtimeHistory(),
      daily: await window.fundApp.panic.getDailyHistory(5000)
    }), seeded.trade_date)
    assert.equal(histories.current.length, dbBeforeLaunch.intraday_current_records)
    assert.equal(histories.all.length, dbBeforeLaunch.intraday_all_records)
    assert.equal(histories.daily.length, dbBeforeLaunch.daily_records)
    assert.ok(histories.current.every(item => item.trade_date === seeded.trade_date), 'getRealtimeHistory(trade_date) 未过滤当日')
    assert.ok(histories.all.some(item => item.trade_date !== seeded.trade_date), '测试夹具缺少非当日盘中记录')
    assert.deepEqual(histories.daily.map(item => item.trade_date), dbBeforeLaunch.daily_dates)
    assert.ok(histories.daily.some((item, index) => index > 0 &&
      (Date.parse(item.trade_date) - Date.parse(histories.daily[index - 1].trade_date)) / 86400000 > 1), '日线夹具没有日期缺口')

    // “当日盘中曲线”只允许显示当前上海日期的记录；无日期 IPC 的全库结果已在上方独立校验。
    await expectText(page, '#intraday-count', `${dbBeforeLaunch.intraday_current_records} 条真实记录`)
    await expectText(page, '#daily-count', `${dbBeforeLaunch.daily_records} 条正式记录`)
    const rendered = await page.evaluate(() => ({
      intraday: document.querySelector('#intraday-chart svg')?.getAttribute('aria-label'),
      daily: document.querySelector('#daily-chart svg')?.getAttribute('aria-label')
    }))
    assert.equal(rendered.intraday, `包含 ${dbBeforeLaunch.intraday_current_records} 条真实记录的曲线`)
    assert.equal(rendered.daily, `包含 ${dbBeforeLaunch.daily_records} 条正式记录的曲线`)
    const legacy = await page.evaluate(() => window.fundApp.panic.getHistoricalEstimates())
    assert.equal(legacy.records.length, 1, '测试数据库必须保留一条旧估计记录')
    assert.equal(legacy.records[0].final_panic_index, 99)
    assert.equal(await page.locator('#history-backfill-button, #history-estimate-status, .chart-line-estimate').count(), 0)
    assert.equal(await page.locator('#daily-chart circle').count(), dbBeforeLaunch.daily_records)
    const annualBox = await page.locator('#annual-panel').boundingBox()
    const componentsBox = await page.locator('#components-panel').boundingBox()
    const marketBox = await page.locator('.market-grid').boundingBox()
    assert.ok(annualBox.y < marketBox.y && marketBox.y < componentsBox.y, '正式曲线应在市场数据上方，一级组件在下方')
    checks.push('正式收盘曲线上移，一级组件下移；旧估计记录不绘制，补全入口已移除')
    checks.push('当日盘中曲线记录数与当前上海日期 SQLite 记录一致；getRealtimeHistory(当前日期) 已过滤当日记录')

    const generated = await page.evaluate(async () => ({
      intraday: await window.fundApp.panic.generateChart('intraday'),
      daily: await window.fundApp.panic.generateChart('daily')
    }))
    assertPng(generated.intraday, reports)
    assertPng(generated.daily, reports)
    await fs.access(generated.intraday.path)
    await fs.access(generated.daily.path)
    checks.push('generateChart(intraday/daily) 返回有效 PNG dataURL，且文件位于独立 userData/reports')

    const beforeExports = (await fs.readdir(reports)).filter(name => name.endsWith('.png')).length
    await page.locator('#intraday-export-button').click()
    await page.locator('#daily-export-button').click()
    const exportFiles = await poll(
      async () => (await fs.readdir(reports)).filter(name => name.endsWith('.png')),
      files => files.length >= beforeExports + 2
    )
    assert.ok(exportFiles.length >= beforeExports + 2)
    checks.push('界面两个“导出图片”按钮均通过真实 IPC 写出 PNG')

    await app.evaluate(({ BrowserWindow }) => { const w = BrowserWindow.getAllWindows()[0]; if (w.isMinimized()) w.restore(); w.show(); w.focus() })
    await page.screenshot({ path: path.join(output, packaged ? `fixture-${mode}.png` : 'fixture.png'), fullPage: true })
  } finally {
    await app.close()
  }
  const result = {
    mode: packaged ? `packaged:${executablePath}` : 'dev:desktop/out',
    fixtureNotice: '仅测试：数据由 engine/tests/fixtures/realtime 派生，不是生产行情，也不交付生产数据库。',
    userData,
    database,
    reports,
    seeded,
    databaseCounts: dbBeforeLaunch,
    checks
  }
  await fs.writeFile(path.join(output, 'result.json'), JSON.stringify(result, null, 2), 'utf8')
  console.log(JSON.stringify(result, null, 2))
}

async function expectText(page, selector, expected) {
  await poll(() => page.locator(selector).textContent(), value => value === expected)
}

main().catch(error => { console.error(error); process.exitCode = 1 })
