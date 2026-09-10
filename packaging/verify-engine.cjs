// 打包前运行当前内置引擎，防止发布缺失资源或旧版本。
const { spawn } = require('node:child_process')
const { mkdtempSync, rmSync, existsSync, readFileSync, readdirSync } = require('node:fs')
const { createHash } = require('node:crypto')
const { tmpdir } = require('node:os')
const { join, resolve } = require('node:path')
const root = resolve(__dirname, '..')
const executable = join(root, 'engine/dist/panic-engine/panic-engine.exe')
if (!existsSync(executable)) throw new Error('请先运行 npm run build:engine，当前内置引擎不存在')
const engineRoot = join(root, 'engine')
const sourceFiles = ['server.py', 'requirements.txt']
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
if (hash.digest('hex') !== manifest.sourceSha256 || manifest.engineVersion !== '3.0-realtime') throw new Error('内置引擎不是当前源码构建，请重新运行 npm run build:engine')
const directory = mkdtempSync(join(tmpdir(), '引擎打包验收-'))
const server = require('node:net').createServer()
server.listen(0, '127.0.0.1', () => {
  const port = server.address().port
  server.close(async () => {
    const child = spawn(executable, ['--host','127.0.0.1','--port',String(port),'--database',join(directory,'data/panic-index.db'),
      '--config',join(root,'engine/config/settings.yaml'),'--log-directory',join(directory,'logs'),
      '--client-version','2.0.0','--instance-id','build-verification'], {windowsHide:true, stdio:['ignore','pipe','pipe'], env:{...process.env,PYTHONHOME:'',PYTHONPATH:''}})
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
      if (!health || health.engine_version !== '3.0-realtime' || health.database_schema_version !== 5 || health.instance_id !== 'build-verification') throw new Error(`内置引擎验证失败：${stderr}`)
      console.log('内置 EXE 健康检查通过：客户端 2.0.0 / 引擎 3.0-realtime / 数据库 5，中文路径可用')
    } catch (error) { console.error(error.message); process.exitCode = 1 }
    finally {
      if (!exited) await new Promise((resolve) => {child.once('exit',resolve);child.kill()})
      rmSync(directory, {recursive:true,force:true})
    }
  })
})
