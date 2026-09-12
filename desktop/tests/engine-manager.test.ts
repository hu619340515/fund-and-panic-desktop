import { mkdtemp, mkdir, writeFile, readFile, rm } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { createServer } from 'node:http'
import { afterEach, describe, expect, it } from 'vitest'
import { PanicEngineManager, engineRequest } from '../src/main/engine-manager'

const resources: string[] = []
const managers: PanicEngineManager[] = []
const fixture = `
const http = require('node:http');
const fs = require('node:fs');
const arg = (key) => process.argv[process.argv.indexOf(key) + 1];
fs.appendFileSync(arg('--log-directory') + '/starts.txt', process.pid + '\\n');
const server = http.createServer(async (req, res) => {
 let body = '';
 for await (const chunk of req) body += chunk;
 const url = new URL(req.url, 'http://localhost');
 if (url.pathname === '/api/v1/bad') { res.end('bad json'); return; }
 if (url.pathname === '/api/v1/slow') { return; }
 if (url.pathname === '/api/v1/collection') {res.statusCode=503;res.end(JSON.stringify({detail:{code:'collection_failed',message:'指数采集失败：腾讯与新浪行情不可用',retry_after_seconds:60}}));return;}
 if (url.pathname === '/api/v1/missing') {res.statusCode=404;res.end(JSON.stringify({detail:'暂无数据'}));return;}
 if (url.pathname === '/api/v2/failure') {res.statusCode=500;res.end('Internal Server Error');return;}
 res.setHeader('Content-Type', 'application/json');
 res.end(JSON.stringify(url.pathname === '/healthz' ? {
  ok:true, engine_version:'4.0', database_schema_version:6,
  client_version:arg('--client-version'), instance_id:arg('--instance-id')
 } : {method:req.method, query:url.search, body:body ? JSON.parse(body) : null, pid:process.pid, config:arg('--config'), database:arg('--database')}));
 if (url.pathname === '/api/v1/crash') setTimeout(() => process.exit(42), 20);
});
server.listen(Number(arg('--port')), '127.0.0.1');
`

async function make(source = fixture, options: Partial<ConstructorParameters<typeof PanicEngineManager>[0]> = {}) {
  const root = await mkdtemp(join(tmpdir(), '看板引擎-'))
  resources.push(root)
  await mkdir(join(root, 'engine', 'config'), { recursive: true })
  await writeFile(join(root, 'engine', 'config', 'settings.yaml'), '{}\n', 'utf8')
  await writeFile(join(root, 'engine', 'server.py'), source, 'utf8')
  const manager = new PanicEngineManager({ resourcesPath: root, userDataPath: join(root, '用户数据'),
    isPackaged: false, pythonCommand: process.execPath, startupTimeoutMs: 2500, ...options })
  managers.push(manager)
  return { manager, root }
}

afterEach(async () => {
  await Promise.all(managers.splice(0).map((manager) => manager.stop()))
  await Promise.all(resources.splice(0).map((root) => rm(root, { recursive: true, force: true })))
})

describe('引擎实际子进程', () => {
  it('保留结构化采集错误，健康服务不会因HTTP失败、坏响应或请求超时重启', async () => {
    const { manager, root } = await make(fixture, { requestTimeoutMs: 100 })
    await manager.start()
    await expect(manager.get('/api/v1/collection')).rejects.toMatchObject({
      statusCode: 503, code: 'collection_failed', retryAfterSeconds: 60,
      message: expect.stringContaining('腾讯与新浪')
    })
    await expect(manager.get('/api/v1/bad')).rejects.toThrow('无效 JSON')
    await expect(manager.get('/api/v2/failure')).rejects.toMatchObject({statusCode:500})
    await expect(manager.get('/api/v1/slow')).rejects.toThrow('请求超时')
    expect(manager.status().state).toBe('ready')
    expect((await readFile(join(root, '用户数据/logs/starts.txt'), 'utf8')).trim().split('\n')).toHaveLength(1)
  })
  it('合并并发启动，使用中文用户路径，保留GET查询和POST方法，退出后端口关闭', async () => {
    const { manager, root } = await make()
    const [first, second] = await Promise.all([manager.start(), manager.start()])
    expect(first.state).toBe('ready')
    expect(first.baseUrl).toBe(second.baseUrl)
    expect(first.databaseVersion).toBe(6)
    expect((await readFile(join(root, '用户数据/logs/starts.txt'), 'utf8')).trim().split('\n')).toHaveLength(1)
    expect(await manager.get('/api/v1/realtime/history?trade_date=2026-09-10&limit=7')).toMatchObject({query:'?trade_date=2026-09-10&limit=7',database:join(root,'用户数据/data/panic-index.db')})
    expect(await manager.post('/api/v1/realtime/refresh')).toMatchObject({method:'POST'})
    expect(await manager.post('/api/v2/risk/refresh', {symbols:['market']})).toMatchObject({method:'POST',body:{symbols:['market']}})
    expect(await manager.put('/api/v2/portfolio', {cash:100})).toMatchObject({method:'PUT',body:{cash:100}})
    await expect(manager.get('/api/v1/missing')).rejects.toThrow('404')
    expect(manager.status().state).toBe('ready')
    await manager.stop()
    await expect(engineRequest(`${first.baseUrl}/healthz`)).rejects.toThrow()
  })

  it('缺失内置引擎时不会回退到系统Python', async () => {
    const { manager } = await make(fixture, { isPackaged: true })
    expect((await manager.start()).error).toContain('当前版本内置引擎缺失')
  })

  it('处理命令不存在，避免未处理的spawn error', async () => {
    const { manager } = await make(fixture, { pythonCommand: 'nonexistent-panic-engine-command' })
    expect((await manager.start()).error).toContain('引擎无法启动')
  })

  it('无健康响应时启动超时并终止进程', async () => {
    const { manager } = await make('setInterval(() => {}, 1000)', { startupTimeoutMs: 180 })
    expect((await manager.start()).error).toContain('启动超时')
  })

  it('拒绝旧引擎版本', async () => {
    const { manager } = await make(fixture.replace("engine_version:'4.0'", "engine_version:'2.0'"))
    expect((await manager.start()).error).toContain('版本不匹配')
  })

  it('端口被其他服务占用时拒绝错误实例', async () => {
    const server = createServer((_req, res) => res.end(JSON.stringify({ ok: true })))
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
    const address = server.address() as { port: number }
    try {
      const { manager } = await make(fixture, { allocatePort: async () => address.port })
      expect((await manager.start()).error).toMatch(/占用|退出/)
    } finally { await new Promise<void>((resolve) => server.close(() => resolve())) }
  })

  it('异常退出只自动重启一次，第二次失败保持可诊断错误', async () => {
    const { manager, root } = await make()
    await manager.start()
    const first = await manager.get('/api/v1/crash') as { pid: number }
    await expect.poll(async () => {
      if (manager.status().state !== 'ready') return false
      try { return (await manager.get('/api/v1/realtime') as {pid:number}).pid !== first.pid } catch { return false }
    }, {timeout:4000}).toBe(true)
    await expect.poll(() => manager.status().state, {timeout:4000}).toBe('ready')
    await manager.get('/api/v1/crash')
    await expect.poll(() => manager.status().state).toBe('error')
    expect((await readFile(join(root, '用户数据/logs/starts.txt'), 'utf8')).trim().split('\n')).toHaveLength(2)
  })
})
