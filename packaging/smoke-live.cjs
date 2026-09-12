// 便携版真实网络采集验收：只确认来源与状态，未通过门槛的预测不得显示为正式结果。
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const { launchPortable } = require('./launch-portable.cjs')
const root = path.resolve(__dirname, '..')
const executable = path.resolve(process.argv[2] ?? path.join(root, 'desktop/dist/FundAndPanic-Portable-3.0.0-x64.exe'))

async function poll(operation, predicate, timeout = 600000) {
  const deadline = Date.now() + timeout
  let value
  while (Date.now() < deadline) {
    value = await operation()
    if (predicate(value)) return value
    await new Promise(resolve => setTimeout(resolve, 500))
  }
  throw new Error('等待真实采集超时：' + JSON.stringify(value))
}
async function main() {
  const output = path.join(root, 'output/playwright/live-v4')
  await fs.mkdir(output,{recursive:true})
  const userData = await fs.mkdtemp(path.join(output,'实网中文用户目录-'))
  await fs.mkdir(path.join(userData,'config'))
  await fs.writeFile(path.join(userData,'config/funds.json'),JSON.stringify({
    version:1,funds:[],quotes:{},sourceHealth:[],lastRefreshAt:null,
    settings:{autoRefresh:false,refreshIntervalSeconds:60,launchAtLogin:false,alwaysOnTop:false,windowOpacity:1,closeToTray:true}
  }),'utf8')
  const env = {...process.env,PATH:path.join(process.env.SystemRoot || 'C:\\Windows','System32'),
    PYTHON:'不存在的系统Python.exe',PYTHONHOME:'',PYTHONPATH:''}
  delete env.ELECTRON_RUN_AS_NODE
  const report = {executable,userData,startedAt:new Date().toISOString(),checks:[]}
  let app
  let enginePid
  let failure
  try {
    app = await launchPortable(executable,['--user-data-dir=' + userData,'--hidden'],env)
    const page = await app.firstWindow()
    await page.waitForFunction(() => Boolean(window.fundApp))
    const ready = await poll(() => page.evaluate(() => window.fundApp.panic.getHealth()),
      value => value.state === 'ready' || value.state === 'error',90000)
    assert.equal(ready.state,'ready',ready.error)
    const health = await (await fetch(ready.baseUrl + '/healthz')).json()
    assert.equal(health.client_version,'3.0.0')
    assert.equal(health.engine_version,'4.0')
    assert.equal(health.database_schema_version,6)
    assert.equal(health.api_version,'2')
    enginePid = health.pid
    report.health = health
    report.checks.push('无系统Python、中文空目录、v4内置引擎')
    for (const code of ['013273','021458','012414']) {
      const added = await page.evaluate(value => window.fundApp.addFund(value),code)
      assert.equal(added.ok,true,added.error)
      assert.ok(added.data.funds.some(fund => fund.code === code))
    }
    const core = ['sh000300','sh000905','sh000852']
    const job = await page.evaluate(symbols => window.fundApp.risk.refresh(symbols),core)
    assert.equal(typeof job.id,'string')
    // 正在下载三个市场标的时，基金估值应仍可独立请求。
    const quoteRefresh = await page.evaluate(() => window.fundApp.refresh())
    assert.equal(quoteRefresh.ok,true,quoteRefresh.error)
    assert.ok(quoteRefresh.data.funds.some(fund => fund.code === '013273'))
    assert.equal((await page.evaluate(() => window.fundApp.panic.getHealth())).state,'ready')
    report.checks.push('公开测试基金013273加入自选、风险任务期间基金独立刷新仍可用')
    report.job = await poll(() => page.evaluate(() => window.fundApp.risk.jobs()),
      value => value.jobs.find(item => item.id === job.id && ['completed','failed'].includes(item.status)))
    const terminal = report.job.jobs.find(item => item.id === job.id)
    assert.equal(terminal.status,'completed',JSON.stringify(terminal.errors))
    assert.equal(terminal.errors.length,0,JSON.stringify(terminal.errors))
    const { DatabaseSync } = require('node:sqlite')
    const auditDb = new DatabaseSync(path.join(userData,'data/panic-index.db'),{readOnly:true})
    try {
      for (const [code,date,cash] of [['021458','2024-11-11',0.016],['012414','2021-09-07',0.012]]) {
        const saved = JSON.parse(auditDb.prepare('SELECT payload FROM risk_values WHERE key=?').get('fund:'+code).payload)
        const history = saved.history
        assert.equal(history.total_return.available,true,code + '复权不可用')
        const index = history.history.findIndex(row => row.trade_date === date)
        assert.ok(index > 0,code + '缺少真实除息日净值')
        const expected = (history.history[index].unit_nav + cash) / history.history[index-1].unit_nav - 1
        const actual = history.total_return.series.find(row => row.trade_date === date)?.return
        assert.ok(typeof actual === 'number' && Math.abs(actual-expected)<1e-10,code + '每十份分红未正确换算')
      }
    } finally { auditDb.close() }
    report.checks.push('冻结版实网021458/012414每10份分红与真实净值复权公式逐值一致')
    report.market = await page.evaluate(() => window.fundApp.risk.snapshot('market'))
    const marketRows = core.map(symbol => report.market.symbols.find(item => item.symbol === symbol))
    assert.ok(marketRows.every(item => item && item.state === 'ready' && item.score !== null && item.as_of),'三个风格未形成完整状态')
    assert.equal(new Set(marketRows.map(item => item.as_of)).size,1,'三种风格日期不一致')
    assert.equal(report.market.state,'ready')
    assert.equal(report.market.as_of,marketRows[0].as_of)
    assert.ok(report.market.score !== null,'市场中位数未形成')
    report.snapshot = await page.evaluate(() => window.fundApp.risk.snapshot('sh000300'))
    assert.ok(report.snapshot.data_quality?.count > 0,'真实来源未形成历史日线')
    assert.equal(report.snapshot.model_version,'4.0')
    assert.ok(['ready','stale','insufficient_data'].includes(report.snapshot.state))
    if (report.snapshot.forecast.state !== 'published') assert.deepEqual(report.snapshot.forecast.published,{})
    if (report.market.forecast.state !== 'published') assert.deepEqual(report.market.forecast.published,{})
    report.checks.push('沪深300/500/1000真实同日状态、沪深300来源；未发布概率保持隐藏')
    await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].show())
    await page.locator('#risk-symbol').selectOption('sh000300')
    await page.locator('#risk-symbol').selectOption('market')
    const displayedAsOf = report.market.as_of
    await page.waitForFunction(asOf => document.querySelector('#risk-time')?.textContent?.includes(asOf) &&
      document.querySelector('#risk-score')?.textContent?.trim() !== '--' &&
      document.querySelectorAll('#risk-components .component-card').length === 3,
    displayedAsOf,{timeout:120000})
    assert.equal(await page.locator('#forecast-body tr').count(),report.market.forecast.state === 'published' ? 5 : 0)
    await page.screenshot({path:path.join(output,'dashboard.png')})
    report.checks.push('市场概览渲染新状态后截图')
  } catch (error) { failure = error; report.error = error.stack || String(error) }
  finally {
    if (app) await app.close()
    try {
      if (enginePid) assert.throws(() => process.kill(enginePid,0),'引擎进程未随客户端退出')
      report.checks.push('退出无引擎残留')
    } catch (error) { failure ||= error; report.shutdownError = error.stack || String(error) }
    report.completedAt = new Date().toISOString()
    report.passed = !failure
    await fs.writeFile(path.join(output,'result.json'),JSON.stringify(report,null,2),'utf8')
  }
  console.log(JSON.stringify({passed:report.passed,output,checks:report.checks,error:failure?.message},null,2))
  if (failure) throw failure
}
main().catch(error => {console.error(error);process.exitCode=1})
