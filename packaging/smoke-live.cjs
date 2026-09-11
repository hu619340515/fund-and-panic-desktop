// 实网验收最终便携版；独立空数据目录，不注入行情、不使用历史测试库。
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
    await new Promise(resolve => setTimeout(resolve, 250))
  }
  throw new Error(`等待状态超时：${JSON.stringify(value)}`)
}

async function main() {
  const executablePath = path.resolve(process.argv[2] ?? path.join(root, 'desktop/dist/FundAndPanic-Portable-2.0.1-x64.exe'))
  const output = path.join(root, 'output/live-201')
  await fs.mkdir(output, { recursive: true })
  const userData = await fs.mkdtemp(path.join(output, '实网中文用户目录-'))
  await fs.mkdir(path.join(userData, 'config'))
  await fs.writeFile(path.join(userData, 'config/funds.json'), JSON.stringify({
    version: 1, funds: [], quotes: {}, sourceHealth: [], lastRefreshAt: null,
    settings: {autoRefresh:false, refreshIntervalSeconds:60, launchAtLogin:false, alwaysOnTop:false, windowOpacity:1, closeToTray:true}
  }), 'utf8')
  const env = {...process.env, PATH:path.join(process.env.SystemRoot || 'C:\\Windows', 'System32'),
    PYTHON:'不存在的系统Python.exe', PYTHONHOME:'', PYTHONPATH:''}
  delete env.ELECTRON_RUN_AS_NODE
  const report = { startedAt:new Date().toISOString(), executablePath, userData, fixture:false, checks:[] }
  let app
  let enginePid
  let failure
  try {
    app = await launchPortable(executablePath, [`--user-data-dir=${userData}`, '--hidden'], env)
    const page = await app.firstWindow()
    await page.waitForFunction(() => Boolean(window.fundApp))
    const ready = await poll(() => page.evaluate(() => window.fundApp.panic.getHealth()),
      value => value.state === 'ready' || value.state === 'error')
    report.initialEngine = ready
    assert.equal(ready.state, 'ready', ready.error)
    report.health = await (await fetch(`${ready.baseUrl}/healthz`, {signal:AbortSignal.timeout(5000)})).json()
    enginePid = report.health.pid
    assert.equal(report.health.client_version, '2.0.1')
    assert.equal(report.health.engine_version, '3.0-realtime')
    assert.equal(report.health.database_schema_version, 5)
    await poll(() => page.evaluate(() => window.fundApp.getState()), value => !value.panic.refreshing)
    report.beforeRefresh = await page.evaluate(() => window.fundApp.getState())
    assert.equal(report.beforeRefresh.panic.realtime, null, '空用户目录不应已有实时缓存')
    report.refreshStartedAt = new Date().toISOString()
    // 通过正式 preload / IPC 触发真实采集，保留全部结果而非模拟 UI 数据。
    report.refreshResult = await page.evaluate(() => window.fundApp.panic.refresh())
    report.snapshot = await page.evaluate(() => window.fundApp.getState())
    report.finalEngine = await page.evaluate(() => window.fundApp.panic.getHealth())
    report.sources = await page.evaluate(async () => {
      try { return {ok:true, data:await window.fundApp.panic.getSources()} }
      catch (error) { return {ok:false, error:String(error)} }
    })
    report.finalHealth = await (await fetch(`${report.finalEngine.baseUrl}/healthz`, {signal:AbortSignal.timeout(5000)})).json()
    await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].show())
    await page.screenshot({path:path.join(output, 'dashboard.png'), fullPage:true})
    report.ui = await page.locator('body').innerText()
    const log = await fs.readFile(path.join(userData, 'logs/engine-process.log'), 'utf8')
    report.argumentParserError = /the following arguments are required|unrecognized arguments:.*multiprocessing/i.test(log)
    assert.equal(report.argumentParserError, false, '冻结采集子进程仍进入了服务参数解析')
    assert.equal(report.finalEngine.state, 'ready')
    assert.equal(report.finalHealth.pid, enginePid, '行情采集期间不应重启健康引擎')
    report.checks.push('当前内置版本、中文空用户目录、无系统Python、实网采集期间服务保持健康')
    assert.equal(report.snapshot.panic.error, null, report.snapshot.panic.error ?? '')
    const realtime = report.snapshot.panic.realtime
    assert.ok(realtime, '真实采集未生成实时记录，检查报告中的来源与错误')
    assert.equal(typeof realtime.realtime_panic_index, 'number')
    const timestamp = Date.parse(realtime.timestamp)
    assert.ok(Number.isFinite(timestamp), '实时记录缺少有效时间')
    report.realtimeAgeSeconds = (Date.now() - timestamp) / 1000
    assert.ok(report.realtimeAgeSeconds >= -60 && report.realtimeAgeSeconds <= 120,
      `实时记录距当前 ${report.realtimeAgeSeconds} 秒，不能当作新鲜行情验收`)
    assert.equal(report.sources.ok, true)
    report.checks.push('正式IPC返回真实实时结果、数据时间在2分钟内、来源信息可读、界面截图')
  } catch (error) {
    failure = error
    report.error = error.stack || String(error)
  } finally {
    try {
      if (app) await app.close()
      if (enginePid) assert.throws(() => process.kill(enginePid, 0), '引擎进程未随客户端退出')
      report.checks.push('退出无引擎残留')
    } catch (error) {
      failure ||= error
      report.shutdownError = error.stack || String(error)
    }
    report.completedAt = new Date().toISOString()
    report.passed = !failure
    await fs.writeFile(path.join(output, 'result.json'), JSON.stringify(report, null, 2), 'utf8')
    console.log(JSON.stringify({passed:report.passed, output, userData, checks:report.checks, error:report.error}, null, 2))
  }
  if (failure) throw failure
}

main().catch(error => {console.error(error); process.exitCode = 1})
