// 打包前运行当前内置引擎，防止发布缺失资源或旧版本。
const { spawn } = require('node:child_process')
const { mkdtempSync, rmSync, existsSync, readFileSync, readdirSync } = require('node:fs')
const { createHash } = require('node:crypto')
const { tmpdir } = require('node:os')
const { join, resolve } = require('node:path')
const root = resolve(__dirname, '..')
const executable = join(root, 'engine/dist/panic-engine/panic-engine.exe')
if (!existsSync(executable)) throw new Error('请先运行 npm run build:engine，当前内置引擎不存在')
const bundledRiskModel = join(root, 'engine/dist/panic-engine/_internal/config/risk-model-v4.yaml')
if (!existsSync(bundledRiskModel)) throw new Error('内置引擎缺少固定模型配置 risk-model-v4.yaml，请重新打包')
for (const name of ['mini_racer.dll','icudtl.dat']) {
  if (!existsSync(join(root,'engine/dist/panic-engine/_internal/py_mini_racer',name))) {
    throw new Error('内置引擎缺少 PyMiniRacer 资源 ' + name + '，请重新打包')
  }
}
const engineRoot = join(root, 'engine')
const sourceFiles = ['server.py', ...readdirSync(engineRoot).filter(name => /^requirements.*\.txt$/.test(name))]
function collect(directory) {
  for (const entry of readdirSync(join(engineRoot,directory),{withFileTypes:true})) {
    const name = `${directory}/${entry.name}`
    if (entry.isDirectory() && entry.name !== '__pycache__') collect(name)
    else if (entry.isFile() && /\.(?:py|yaml|html)$/.test(name)) sourceFiles.push(name)
  }
}
collect('scripts'); collect('config')
const hash = createHash('sha256')
for (const file of sourceFiles.sort()) hash.update(file, 'utf8').update(Buffer.from([0])).update(readFileSync(join(engineRoot,file)))
const manifest = JSON.parse(readFileSync(join(engineRoot,'dist/panic-engine/build-manifest.json'),'utf8'))
if (hash.digest('hex') !== manifest.sourceSha256 || manifest.engineVersion !== '4.0' ||
  manifest.clientVersion !== '3.0.0' || manifest.databaseVersion !== 6 || manifest.apiVersion !== '2') throw new Error('内置引擎不是当前源码构建，请重新运行 npm run build:engine')
async function frozenRiskWorkerDiagnostics() {
  const child = spawn(executable, ['--risk-worker-self-test'], {
    windowsHide:true, stdio:['ignore','pipe','pipe'],
    env:{...process.env,PYTHONHOME:'',PYTHONPATH:''}
  })
  let stdout = ''
  let stderr = ''
  child.stdout.on('data', data => { stdout = (stdout + String(data)).slice(-64000) })
  child.stderr.on('data', data => { stderr = (stderr + String(data)).slice(-64000) })
  const code = await new Promise((resolve,reject) => {
    const timer = setTimeout(() => { child.kill(); reject(new Error('V4 冻结 worker 自检超过 240 秒')) },240000)
    child.once('error', error => { clearTimeout(timer); reject(error) })
    child.once('close', value => { clearTimeout(timer); resolve(value) })
  })
  if (code !== 0) throw new Error('V4 冻结 worker 自检失败：' + stderr + stdout)
  const lines = stdout.split(/\r?\n/).map(line => line.trim()).filter(Boolean)
  let result
  try { result = JSON.parse(lines.at(-1)) }
  catch { throw new Error('V4 冻结 worker 自检未返回 JSON：' + stdout + stderr) }
  if (result.ok !== true || result.start_method !== 'spawn' ||
      (result.residual_pids ?? []).length ||
      ['success','error','exit','timeout','training','js_runtime'].some(name => !result.cases?.[name]) ||
      result.cases.js_runtime.value !== 42 ||
      result.cases.training.published !== false || result.cases.training.oos_samples < 1) {
    throw new Error('V4 冻结 worker 自检结果不完整：' + JSON.stringify(result))
  }
  console.log('V4 冻结 worker 成功、异常23、错误、硬超时、真实 sklearn 训练/校准/回放、PyMiniRacer JS 执行及回收通过')
}
const directory = mkdtempSync(join(tmpdir(), '引擎打包验收-'))
const server = require('node:net').createServer()
server.listen(0, '127.0.0.1', () => {
  const port = server.address().port
  server.close(async () => {
    const child = spawn(executable, ['--host','127.0.0.1','--port',String(port),'--database',join(directory,'data/panic-index.db'),
      '--config',join(root,'engine/config/settings.yaml'),'--log-directory',join(directory,'logs'),
      '--client-version','3.0.0','--instance-id','build-verification'], {windowsHide:true, stdio:['ignore','pipe','pipe'], env:{...process.env,PYTHONHOME:'',PYTHONPATH:''}})
    let stderr = ''
    child.stderr.on('data', (data) => { stderr += data.toString() })
    let exited = false
    child.on('exit', () => { exited = true })
    child.on('error', (error) => { stderr += error.message; exited = true })
    try {
      let health
      const deadline = Date.now() + 90000
      while (Date.now() < deadline && !exited) {
        try { health = await (await fetch(`http://127.0.0.1:${port}/healthz`, {signal:AbortSignal.timeout(1500)})).json(); break } catch { await new Promise(r=>setTimeout(r,250)) }
      }
      if (!health || health.engine_version !== '4.0' || health.database_schema_version !== 6 ||
        health.client_version !== '3.0.0' || health.api_version !== '2' ||
        health.instance_id !== 'build-verification') throw new Error(`内置引擎验证失败：${stderr}`)
      // 空库也须通过真实 spawn 返回数据不足；完整机器学习依赖另由下面的自检验证。
      const base = `http://127.0.0.1:${port}`
      const submission = await fetch(base + '/api/v2/risk/train', {
        method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({symbols:['sh000300']}),
        signal:AbortSignal.timeout(10000)
      })
      if (!submission.ok) throw new Error('冻结版训练任务提交失败：' + await submission.text())
      const job = await submission.json()
      let terminal
      const trainDeadline = Date.now() + 120000
      while (Date.now() < trainDeadline && !terminal) {
        const response = await fetch(base + '/api/v2/jobs', {signal:AbortSignal.timeout(3000)})
        const jobs = await response.json()
        const current = jobs.jobs.find(item => item.id === job.id)
        if (current && ['completed','failed'].includes(current.status)) terminal = current
        else await new Promise(r => setTimeout(r, 250))
      }
      const validationResponse = await fetch(base + '/api/v2/risk/validation?symbol=sh000300', {signal:AbortSignal.timeout(5000)})
      const validation = await validationResponse.json()
      if (!terminal || terminal.status !== 'completed' || terminal.errors?.length ||
          !validationResponse.ok || validation.status !== 'insufficient_data' || validation.publishable !== false) {
        throw new Error('冻结版空库训练任务或数据不足状态错误：' + JSON.stringify({terminal,validation}))
      }
      console.log('内置 EXE 健康检查通过：客户端 3.0.0 / 引擎 4.0 / 数据库 6 / API 2，中文路径可用')
      console.log('内置 EXE 空库训练 worker 启动、数据不足状态与回收通过')
      await frozenRiskWorkerDiagnostics()
    } catch (error) { console.error(error.message); process.exitCode = 1 }
    finally {
      if (!exited) await new Promise((resolve) => {child.once('exit',resolve);child.kill()})
      rmSync(directory, {recursive:true,force:true})
    }
  })
})
