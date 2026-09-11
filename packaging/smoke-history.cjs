// 真实便携版历史回算验收：独立空库，点击正式按钮；不注入行情或触发实时采集。
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const { launchPortable } = require('./launch-portable.cjs')
const root = path.resolve(__dirname, '..')

async function poll(operation, predicate, timeout = 90000) {
  const deadline = Date.now() + timeout
  let value
  while (Date.now() < deadline) {
    value = await operation()
    if (predicate(value)) return value
    await new Promise(resolve => setTimeout(resolve, 300))
  }
  throw new Error(`等待状态超时：${JSON.stringify(value)}`)
}

async function main() {
  const executablePath = path.resolve(process.argv[2] ?? path.join(root, 'desktop/dist/FundAndPanic-Portable-2.0.2-x64.exe'))
  const output = path.join(root, 'output/playwright/history-202')
  await fs.mkdir(output, {recursive:true})
  const userData = await fs.mkdtemp(path.join(output, '历史验收中文用户目录-'))
  await fs.mkdir(path.join(userData, 'config'))
  await fs.writeFile(path.join(userData, 'config/funds.json'), JSON.stringify({
    version:1, funds:[], quotes:{}, sourceHealth:[], lastRefreshAt:null,
    settings:{autoRefresh:false, refreshIntervalSeconds:60, launchAtLogin:false, alwaysOnTop:false, windowOpacity:1, closeToTray:true}
  }), 'utf8')
  const env = {...process.env, PATH:path.join(process.env.SystemRoot || 'C:\\Windows', 'System32'),
    PYTHON:'不存在的系统Python.exe', PYTHONHOME:'', PYTHONPATH:''}
  delete env.ELECTRON_RUN_AS_NODE
  const report = {startedAt:new Date().toISOString(), executablePath, userData, fixture:false, checks:[]}
  let app
  let enginePid
  let failure

  async function launch() {
    app = await launchPortable(executablePath, [`--user-data-dir=${userData}`, '--hidden'], env)
    const page = await app.firstWindow()
    await page.waitForFunction(() => Boolean(window.fundApp))
    const status = await poll(() => page.evaluate(() => window.fundApp.panic.getHealth()),
      value => value.state === 'ready' || value.state === 'error')
    assert.equal(status.state, 'ready', status.error)
    const health = await (await fetch(`${status.baseUrl}/healthz`, {signal:AbortSignal.timeout(5000)})).json()
    enginePid = health.pid
    assert.equal(health.client_version, '2.0.2')
    assert.equal(health.engine_version, '3.0-realtime')
    await page.locator('#history-backfill-button').waitFor({state:'attached'})
    await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].show())
    return {page, health}
  }

  async function close() {
    if (app) {
      await app.close()
      app = null
    }
    if (enginePid) assert.throws(() => process.kill(enginePid, 0), '关闭客户端后引擎仍在运行')
  }

  try {
    const first = await launch()
    const page = first.page
    report.health = first.health
    const initial = await page.evaluate(() => window.fundApp.panic.getHistoricalEstimates())
    report.initial = initial
    assert.deepEqual(initial.records, [], '空数据库不应存在历史估计')
    report.checks.push('当前内置引擎、中文空目录、无系统Python、初始化历史为空')
    await page.locator('#history-backfill-button').click()
    await page.waitForFunction(() => document.querySelector('#history-backfill-button')?.textContent === '补全中…')
    await page.waitForFunction(() => document.querySelector('#history-backfill-button')?.textContent === '补全历史', undefined, {timeout:180000})
    report.history = await page.evaluate(() => window.fundApp.panic.getHistoricalEstimates())
    report.daily = await page.evaluate(() => window.fundApp.panic.getDailyHistory(500))
    report.uiStatus = await page.locator('#history-estimate-status').innerText()
    report.dailyCount = await page.locator('#daily-count').innerText()
    await page.screenshot({path:path.join(output, 'history.png'), fullPage:true})
    const records = report.history.records
    const today = new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date())
    assert.ok(records.length > 150, `历史不足：${records.length} 条，${report.uiStatus}`)
    for (const record of records) {
      assert.equal(record.finality, 'estimated')
      assert.equal(record.quality_status, 'historical_estimate')
      assert.ok(record.coverage > 0 && record.coverage < 1, `${record.trade_date} 覆盖率不符合缺项回算`)
      assert.ok(record.trade_date < today, '估计只能包含过去日期')
      assert.ok(Array.isArray(record.missing_features) && record.missing_features.length > 0)
    }
    assert.deepEqual(report.daily, [], '历史估计不能写入正式收盘表')
    report.range = [records[0].trade_date, records.at(-1).trade_date]
    report.coverageRange = [Math.min(...records.map(row => row.coverage)), Math.max(...records.map(row => row.coverage))]
    report.missingFeatures = [...new Set(records.flatMap(row => row.missing_features))]
    assert.ok(await page.locator('#daily-chart .chart-line-estimate').count(), '估计曲线未显示')
    assert.match(report.uiStatus, /覆盖率/)
    assert.match(report.uiStatus, /缺项/)
    report.checks.push('真实按钮回算超过150条、全部标记估计且覆盖率不足100%、正式收盘保持为空、独立估计曲线与缺项展示')
    await close()

    const restarted = await launch()
    report.restartedHealth = restarted.health
    report.cached = await restarted.page.evaluate(() => window.fundApp.panic.getHistoricalEstimates())
    assert.deepEqual(report.cached.records, records, '重启读取的历史估计应与已回算记录一致')
    assert.equal(report.cached.status.updated_at, report.history.status.updated_at, '重启不得自动重新补全历史')
    await poll(() => restarted.page.locator('#daily-count').innerText(), value => value.includes(`${records.length} 条历史估计`))
    await restarted.page.screenshot({path:path.join(output, 'history-cached.png'), fullPage:true})
    report.checks.push('重启直接读取持久化缓存，无需再次点击补全、更新时间保持一致')
  } catch (error) {
    failure = error
    report.error = error.stack || String(error)
  } finally {
    try { await close(); report.checks.push('两次退出均无引擎残留') }
    catch (error) { failure ||= error; report.shutdownError = error.stack || String(error) }
    report.completedAt = new Date().toISOString()
    report.passed = !failure
    await fs.writeFile(path.join(output, 'result.json'), JSON.stringify(report, null, 2), 'utf8')
    console.log(JSON.stringify({passed:report.passed, output, userData, checks:report.checks, error:report.error}, null, 2))
  }
  if (failure) throw failure
}

main().catch(error => {console.error(error); process.exitCode=1})
