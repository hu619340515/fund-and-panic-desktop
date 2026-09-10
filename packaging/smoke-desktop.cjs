// 使用独立中文用户目录，对开发版或指定安装目录中的 EXE 做真实端到端验收。
const { _electron: electron } = require('../desktop/node_modules/playwright')
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const root = path.resolve(__dirname, '..')

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

async function main() {
  const mode = process.argv[2] ?? 'dev'
  const output = path.join(root, 'output/playwright', mode === 'dev' ? 'dev' : path.basename(mode, '.exe'))
  await fs.mkdir(output, {recursive:true})
  const userData = await fs.mkdtemp(path.join(output, '中文用户目录-'))
  await fs.mkdir(path.join(userData, 'config'))
  await fs.writeFile(path.join(userData, 'config/funds.json'), JSON.stringify({version:1,funds:[],quotes:{},sourceHealth:[],lastRefreshAt:null,
    settings:{autoRefresh:false,refreshIntervalSeconds:60,launchAtLogin:false,alwaysOnTop:false,windowOpacity:1,closeToTray:true}}), 'utf8')
  const env = {...process.env, PYTHONPATH:'', PYTHONHOME:''}
  if (mode !== 'dev') {
    env.PATH = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32')
    env.PYTHON = '不存在的系统Python.exe'
  }
  delete env.ELECTRON_RUN_AS_NODE
  const executablePath = mode === 'dev' ? require('../desktop/node_modules/electron') : path.resolve(mode)
  const args = [...(mode === 'dev' ? [path.join(root,'desktop')] : []), `--user-data-dir=${userData}`, '--hidden']
  const app = /Portable-/i.test(executablePath)
    ? await require('./launch-portable.cjs').launchPortable(executablePath,args,env)
    : await electron.launch({executablePath,args,env,timeout:60000})
  const checks = []
  let enginePid
  let engineUrl
  try {
    const page = await app.firstWindow()
    await page.waitForFunction(() => Boolean(window.fundApp))
    await page.locator('#settings-button').waitFor({state:'attached'})
    const ready = await poll(() => page.evaluate(() => window.fundApp.panic.getHealth()), value => value.state === 'ready' || value.state === 'error')
    assert.equal(ready.state, 'ready', ready.error)
    assert.equal(ready.version, '3.0-realtime')
    assert.equal(ready.databaseVersion, 5)
    engineUrl = ready.baseUrl
    const health = await (await fetch(`${engineUrl}/healthz`)).json()
    enginePid = health.pid
    checks.push('内置版本、健康检查、中文用户目录')
    for (const directory of ['config','data','logs','reports','cache']) await fs.access(path.join(userData,directory))
    const state = await page.evaluate(() => window.fundApp.getState())
    assert.equal(state.settings.autoRefresh,false)
    assert.equal(state.funds.length,0)
    await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].show())
    await page.locator('#settings-button').click()
    await page.locator('#refresh-interval').selectOption('120')
    await page.locator('#settings-form button[type=submit]').click()
    await poll(() => page.evaluate(() => window.fundApp.getState()), value => value.settings.refreshIntervalSeconds === 120)
    checks.push('设置界面、自动刷新开关、周期保存')
    if (mode === 'dev') {
      const before = (await page.evaluate(() => window.fundApp.getState())).lastRefreshAt
      await page.evaluate(() => window.fundApp.updateSettings({autoRefresh:true,refreshIntervalSeconds:30}))
      await poll(() => page.evaluate(() => window.fundApp.getState()), value => value.lastRefreshAt !== before, 40000)
      await page.evaluate(() => window.fundApp.updateSettings({autoRefresh:false,refreshIntervalSeconds:120}))
      checks.push('真实30秒自动刷新定时器与关闭开关')
    }
    await assert.rejects(() => page.evaluate(() => window.fundApp.panic.getRealtimeHistory('2026-02-30')))
    await assert.rejects(() => page.evaluate(() => window.fundApp.panic.getDailyHistory(-1)))
    await assert.rejects(() => page.evaluate(() => window.fundApp.panic.generateChart('../../invalid')))
    checks.push('真实IPC参数白名单')
    await page.screenshot({path:path.join(output,'dashboard.png'),fullPage:true})
    await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].close())
    assert.equal(await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].isVisible()),false)
    await app.evaluate(({app}) => app.emit('second-instance',{},[],''))
    assert.equal(await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].isVisible()),true)
    checks.push('关闭驻留、恢复主窗口')
    await app.evaluate(() => { globalThis.fetch = async () => { throw new Error('验收模拟：网络不可用') } })
    const offline = await page.evaluate(() => window.fundApp.addFund('110022'))
    assert.equal(offline.ok, false)
    assert.match(offline.error, /不可用|未找到/)
    checks.push('网络不可用时基金操作返回明确错误')
    if (Number.isInteger(enginePid)) {
      process.kill(enginePid)
      const recovered = await poll(() => page.evaluate(() => window.fundApp.panic.getHealth()), value => value.state === 'ready' && value.baseUrl !== engineUrl)
      engineUrl = recovered.baseUrl
      enginePid = (await (await fetch(`${engineUrl}/healthz`)).json()).pid
      process.kill(enginePid)
      await poll(() => page.evaluate(() => window.fundApp.panic.getHealth()), value => value.state === 'error')
      const funds = await page.evaluate(() => window.fundApp.refresh())
      assert.equal(funds.ok,true)
      assert.equal(funds.data.panic.realtime,null)
      await page.screenshot({path:path.join(output,'engine-error.png'),fullPage:true})
      checks.push('异常退出一次重启、二次失败清空实时值、基金独立刷新')
    }
  } finally {
    await app.close()
  }
  if (enginePid) assert.throws(() => process.kill(enginePid,0))
  const persisted = JSON.parse(await fs.readFile(path.join(userData,'config/funds.json'),'utf8'))
  assert.equal(persisted.settings.refreshIntervalSeconds,120)
  checks.push('退出无引擎残留、设置落盘')
  await fs.writeFile(path.join(output,'result.json'),JSON.stringify({mode,date:new Date().toISOString(),userData,checks},null,2),'utf8')
  console.log(JSON.stringify({mode,checks,output},null,2))
}
main().catch(error => {console.error(error);process.exitCode=1})
