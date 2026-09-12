// 开发版与安装/便携版共用：真实 Electron 窗口、隔离目录和 v4 IPC 验收。
const { _electron: electron } = require('../desktop/node_modules/playwright')
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const { execFile } = require('node:child_process')
const { promisify } = require('node:util')
const root = path.resolve(__dirname, '..')
const execFileAsync = promisify(execFile)

async function poll(operation, predicate, timeout = 90000) {
  const deadline = Date.now() + timeout
  let value
  while (Date.now() < deadline) {
    value = await operation()
    if (predicate(value)) return value
    await new Promise(resolve => setTimeout(resolve, 200))
  }
  throw new Error('等待状态超时：' + JSON.stringify(value))
}
async function captureAt(page, selector, file) {
  await page.locator(selector).evaluate(node => window.scrollTo(0,
    node.getBoundingClientRect().top + window.scrollY - 88))
  await page.locator('#toast').evaluate(node => node.classList.remove('toast-visible'))
  await page.screenshot({path:file})
}

async function main() {
  const mode = process.argv[2] ?? 'dev'
  const output = path.join(root, 'output/playwright', mode === 'dev' ? 'dev-v4' : path.basename(mode, '.exe') + '-v4')
  await fs.mkdir(output, {recursive:true})
  const userData = await fs.mkdtemp(path.join(output, '中文用户目录-'))
  await fs.mkdir(path.join(userData, 'config'))
  await fs.writeFile(path.join(userData, 'config/funds.json'), JSON.stringify({
    version:1,funds:[],quotes:{},sourceHealth:[],lastRefreshAt:null,
    settings:{autoRefresh:false,refreshIntervalSeconds:60,launchAtLogin:false,alwaysOnTop:false,windowOpacity:1,closeToTray:true}
  }), 'utf8')
  const env = {...process.env, PYTHONPATH:'', PYTHONHOME:''}
  if (mode === 'dev') {
    env.PYTHON = path.join(root, 'engine/.venv/Scripts/python.exe')
    env.RISK_DISABLE_NETWORK = '1'
    await fs.access(env.PYTHON)
  } else {
    env.PATH = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32')
    env.PYTHON = '不存在的系统Python.exe'
  }
  delete env.ELECTRON_RUN_AS_NODE
  const executablePath = mode === 'dev' ? require('../desktop/node_modules/electron') : path.resolve(mode)
  const args = [...(mode === 'dev' ? [path.join(root,'desktop')] : []), '--user-data-dir=' + userData, '--hidden']
  const app = /Portable-/i.test(executablePath)
    ? await require('./launch-portable.cjs').launchPortable(executablePath,args,env)
    : await electron.launch({executablePath,args,env,timeout:90000})
  const checks = []
  let enginePid = null
  let failure = null
  try {
    const page = await app.firstWindow()
    const pageErrors = []
    page.on('pageerror', error => pageErrors.push(error.message))
    await page.waitForFunction(() => Boolean(window.fundApp))
    const ready = await poll(() => page.evaluate(() => window.fundApp.panic.getHealth()), value => value.state === 'ready' || value.state === 'error')
    assert.equal(ready.state, 'ready', ready.error)
    assert.equal(ready.version, '4.0')
    assert.equal(ready.databaseVersion, 6)
    assert.equal(ready.clientVersion, '3.0.0')
    const health = await (await fetch(ready.baseUrl + '/healthz')).json()
    assert.equal(health.engine_version, '4.0')
    assert.equal(health.database_schema_version, 6)
    assert.equal(health.client_version, '3.0.0')
    assert.equal(health.api_version, '2')
    enginePid = health.pid
    for (const directory of ['config','data','logs','reports','cache']) await fs.access(path.join(userData,directory))
    checks.push('中文独立目录、客户端3.0.0、模型4.0、数据库6、API2')

    const initial = await page.evaluate(async () => ({
      state:await window.fundApp.getState(),
      portfolio:await window.fundApp.portfolio.get(),
      snapshot:await window.fundApp.risk.snapshot(),
      history:await window.fundApp.risk.history('market','published',20),
      validation:await window.fundApp.risk.validation('market'),
      advice:await window.fundApp.risk.advice()
    }))
    assert.equal(initial.state.funds.length, 0)
    assert.equal(initial.portfolio.version, 1)
    assert.deepEqual(initial.portfolio.lots, [])
    assert.equal(initial.snapshot.model_version, '4.0')
    assert.equal(initial.snapshot.forecast.state === 'published', false)
    assert.deepEqual(initial.snapshot.forecast.published, {})
    assert.equal(initial.history.kind, 'published')
    assert.deepEqual(initial.history.records, [])
    assert.equal(initial.validation.publishable, false)
    assert.equal(initial.advice.state, 'empty')
    checks.push('空持仓、无虚构风险数值、未发布预测不可见、正式历史为空')

    if (mode === 'dev') {
      // 仅在隔离测试库写明示夹具，验证回测与正式记录两条真实 IPC/绘图路径。
      const code = [
        'import sys',
        'from a_share_panic_index.risk_v4.store import RiskStore',
        's=RiskStore(sys.argv[1])',
        "s.save_history('market',[{'as_of':'2020-01-02','score':40,'state':'ready'},{'as_of':'2020-01-03','score':60,'state':'ready'}],'backtest')",
        "s.save_history('market',[{'as_of':'2020-01-03','score':55,'state':'ready','published_at':'2020-01-03T15:30:00+08:00'}],'published')"
      ].join('\n')
      await execFileAsync(env.PYTHON,['-c',code,path.join(userData,'data/panic-index.db')],{
        cwd:root,windowsHide:true,env:{...process.env,PYTHONPATH:path.join(root,'engine/scripts'),PYTHONUTF8:'1'}
      })
      await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].show())
      await page.locator('#history-backtest').click()
      await poll(() => page.locator('#history-count').innerText(), text => text.includes('2 条'))
      assert.equal(await page.locator('#history-chart path.chart-line-estimate').count(),1)
      await page.locator('#history-published').click()
      await poll(() => page.locator('#history-count').innerText(), text => text.includes('1 条'))
      assert.equal(await page.locator('#history-chart path.chart-line-display').count(),1)
      assert.equal(await page.locator('#history-chart path.chart-line-estimate').count(),0)
      assert.equal(await page.locator('#history-chart circle').count(),1)
      checks.push('隔离夹具：回测2条与正式发布1条不混绘')
    }

    await assert.rejects(() => page.evaluate(() => window.fundApp.risk.snapshot('../../healthz')))
    await assert.rejects(() => page.evaluate(() => window.fundApp.risk.history('market','all',99999)))
    await assert.rejects(() => page.evaluate(() => window.fundApp.portfolio.save({version:1,cash:Infinity})))
    checks.push('v4 IPC 参数拒绝越界输入')

    await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].show())
    await page.locator('#settings-button').click()
    await page.locator('#refresh-interval').selectOption('120')
    await page.locator('#settings-form button[type=submit]').click()
    await poll(() => page.evaluate(() => window.fundApp.getState()), value => value.settings.refreshIntervalSeconds === 120)
    checks.push('设置对话框与刷新周期保存')

    const today = new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai'}).format(new Date())
    await page.locator('#portfolio-cash').fill('1200')
    await page.locator('#portfolio-profile').selectOption('conservative')
    await page.locator('#lot-add').click()
    const lot = page.locator('#portfolio-lots tr').last()
    await lot.locator('[data-field=code]').fill('013273')
    await lot.locator('[data-field=shares]').fill('10')
    await lot.locator('[data-field=market_value]').fill('100')
    await lot.locator('[data-field=valuation_date]').fill(today)
    await lot.locator('[data-field=fee_buy]').fill('0.001')
    await lot.locator('[data-field=fee_sell]').fill('0.005')
    await lot.locator('[data-field=benchmark_symbol]').fill('000300')
    await lot.locator('[data-field=asset_class]').selectOption('equity')
    await lot.locator('[data-field=metadata_verified]').check()
    await lot.locator('[data-field=effective_date]').fill(today)
    await page.locator('#portfolio-save').click()
    const manual = await poll(() => page.evaluate(() => window.fundApp.portfolio.get()), value => value.lots.length === 1)
    assert.equal(manual.cash, 1200)
    assert.equal(manual.profile, 'conservative')
    assert.equal(manual.lots[0].benchmark_symbol, 'sh000300')
    assert.equal(manual.lots[0].metadata_verified, true)
    await page.locator('#risk-symbol').selectOption('fund:013273')
    await poll(() => page.locator('#risk-state').innerText(), text => text.includes('关联基准'))
    checks.push('多批次手填、风险偏好、参数默认值、基金关联基准')
    await page.locator('.lots-table').evaluate(table => { table.closest('.table-scroll').scrollLeft = 0 })
    await captureAt(page,'.allocation-panel',path.join(output,'manual-portfolio.png'))

    await page.locator('.csv-box summary').click()
    await page.locator('#portfolio-csv').fill('基金代码,估值日期\nwrong,' + today)
    await page.locator('#csv-preview').click()
    await poll(() => page.locator('#csv-result').innerText(), text => text.includes('错误'))
    assert.equal(await page.locator('#csv-apply').isDisabled(), true)
    const csv = '基金代码,持有份额,持有市值,估值日期,申购确认日期,在途金额,申购费率,赎回费率,基准权重,行业\n004070,20,210,' + today + ',,0,0.001,0.005,0.15,科技'
    await page.locator('#portfolio-csv').fill(csv)
    await page.locator('#csv-preview').click()
    await poll(() => page.locator('#csv-result').innerText(), text => text.includes('004070'))
    assert.equal(await page.locator('#csv-apply').isEnabled(), true)
    assert.equal((await page.evaluate(() => window.fundApp.portfolio.get())).lots[0].code, '013273', '预览不可直接写库')
    await page.locator('#csv-apply').click()
    const imported = await poll(() => page.evaluate(() => window.fundApp.portfolio.get()), value => value.lots.length === 1 && value.lots[0].code === '004070')
    assert.equal(imported.cash, 1200)
    assert.equal(imported.profile, 'conservative')
    checks.push('无效CSV阻止保存、有效CSV预览无写入、确认后保存且保留现金/偏好')
    await captureAt(page,'.csv-box',path.join(output,'csv-import.png'))

    const job = await page.evaluate(() => window.fundApp.risk.train())
    assert.equal(typeof job.id, 'string')
    const jobs = await poll(() => page.evaluate(() => window.fundApp.risk.jobs()),
      result => result.jobs.some(entry => entry.id === job.id && ['completed','failed'].includes(entry.status)), 120000)
    assert.ok(jobs.jobs.find(entry => entry.id === job.id))
    const funds = await page.evaluate(() => window.fundApp.refresh())
    assert.equal(funds.ok, true)
    assert.equal((await page.evaluate(() => window.fundApp.getState())).engine.state, 'ready')
    checks.push('训练任务异步返回与结束、基金空列表独立刷新')
    await page.evaluate(() => window.scrollTo(0,0))
    await page.screenshot({path:path.join(output,'dashboard.png')})
    await captureAt(page,'.history-tabs',path.join(output,'risk-history.png'))

    await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].close())
    assert.equal(await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].isVisible()),false)
    await app.evaluate(({app}) => app.emit('second-instance',{},[],''))
    assert.equal(await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].isVisible()),true)
    checks.push('托盘驻留与恢复窗口')
    assert.deepEqual(pageErrors, [], '渲染进程报错：' + pageErrors.join('；'))
  } catch (error) { failure = error }
  finally { await app.close() }
  try {
    if (enginePid) assert.throws(() => process.kill(enginePid,0), '引擎未随客户端退出')
    const saved = JSON.parse(await fs.readFile(path.join(userData,'config/funds.json'),'utf8'))
    assert.equal(saved.settings.refreshIntervalSeconds,120)
    checks.push('退出无引擎残留、设置落盘')
  } catch (error) { failure ||= error }
  const result = {mode,date:new Date().toISOString(),userData,checks,passed:!failure,error:failure?.stack}
  await fs.writeFile(path.join(output,'result.json'),JSON.stringify(result,null,2),'utf8')
  console.log(JSON.stringify({mode,checks,passed:!failure,output,error:failure?.message},null,2))
  if (failure) throw failure
}
main().catch(error => {console.error(error);process.exitCode=1})
