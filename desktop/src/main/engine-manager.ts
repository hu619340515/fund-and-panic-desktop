import { appendFileSync, copyFileSync, existsSync, mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { spawn, type ChildProcess } from 'node:child_process'
import { request } from 'node:http'
import { createServer } from 'node:net'
import { randomUUID } from 'node:crypto'
import type { EngineStatus } from '../shared/types'

export type { EngineStatus } from '../shared/types'
export const ENGINE_VERSION = '3.0-realtime'
export const DATABASE_VERSION = 5

export interface PanicEngineClient {
  start(): Promise<EngineStatus>
  stop(): Promise<void>
  restart(): Promise<EngineStatus>
  status(): EngineStatus
  get(path: string): Promise<unknown>
  post(path: string): Promise<unknown>
}

interface EngineManagerOptions {
  resourcesPath: string
  userDataPath: string
  isPackaged: boolean
  clientVersion?: string
  pythonCommand?: string
  startupTimeoutMs?: number
  requestTimeoutMs?: number
  allocatePort?: () => Promise<number>
  onStatusChanged?: (status: EngineStatus) => void
}

export class EngineHttpError extends Error {
  readonly code?: string
  readonly retryAfterSeconds?: number

  constructor(public readonly statusCode: number, detail: unknown) {
    const value = detail && typeof detail === 'object' ? detail as Record<string, unknown> : null
    const message = typeof detail === 'string' ? detail : typeof value?.message === 'string' ? value.message : '请求失败'
    super(`引擎 HTTP ${statusCode}：${message}`)
    this.code = typeof value?.code === 'string' ? value.code : undefined
    this.retryAfterSeconds = typeof value?.retry_after_seconds === 'number' && value.retry_after_seconds > 0
      ? value.retry_after_seconds : undefined
  }
}

export function engineRequest(url: string, method = 'GET', timeoutMs = 5000): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const target = new URL(url)
    const req = request({ hostname: target.hostname, port: target.port,
      path: target.pathname + target.search, method }, (response) => {
      let body = ''
      response.setEncoding('utf8')
      response.on('data', (chunk: string) => {
        body += chunk
        if (body.length > 16 * 1024 * 1024) req.destroy(new Error('引擎响应超过限制'))
      })
      response.on('error', reject)
      response.on('end', () => {
        try {
          const value = JSON.parse(body) as Record<string, unknown>
          if ((response.statusCode ?? 500) >= 400) {
            reject(new EngineHttpError(response.statusCode ?? 500, value.detail ?? '请求失败'))
          } else resolve(value)
        } catch { reject(new Error('引擎返回无效 JSON')) }
      })
    })
    const deadline = setTimeout(() => req.destroy(new Error('引擎请求超时')), timeoutMs)
    req.on('close', () => clearTimeout(deadline))
    req.on('error', reject)
    req.end()
  })
}

export function allocateLocalPort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = createServer()
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      if (!address || typeof address === 'string') { server.close(); reject(new Error('无法分配本地端口')); return }
      server.close((error) => error ? reject(error) : resolve(address.port))
    })
  })
}

export class PanicEngineManager implements PanicEngineClient {
  private child: ChildProcess | null = null
  private startPromise: Promise<EngineStatus> | null = null
  private stopped = true
  private generation = 0
  private retries = 0
  private current: EngineStatus

  constructor(private readonly options: EngineManagerOptions) {
    this.current = { state: 'stopped', baseUrl: null, error: null, version: ENGINE_VERSION,
      databaseVersion: null, clientVersion: options.clientVersion ?? '2.0.2',
      logPath: join(options.userDataPath, 'logs', 'engine-process.log') }
  }

  status(): EngineStatus { return { ...this.current } }

  private update(patch: Partial<EngineStatus>): void {
    this.current = { ...this.current, ...patch }
    this.options.onStatusChanged?.(this.status())
  }

  private log(message: string): void {
    try { appendFileSync(this.current.logPath, `${new Date().toISOString()} ${message}\n`, 'utf8') }
    catch (error) { console.error('引擎日志写入失败', error) }
  }

  start(): Promise<EngineStatus> {
    if (this.startPromise) return this.startPromise
    if (this.current.state === 'ready') return Promise.resolve(this.status())
    this.stopped = false
    const generation = ++this.generation
    this.startPromise = this.startWithRecovery(generation).finally(() => { this.startPromise = null })
    return this.startPromise
  }

  private async startWithRecovery(generation: number): Promise<EngineStatus> {
    do {
      try { return await this.launch(generation) }
      catch (error) {
        await this.terminateChild()
        if (this.stopped || generation !== this.generation) return this.status()
        const message = error instanceof Error ? error.message : String(error)
        this.update({ state: 'error', baseUrl: null, error: `${message}；日志：${this.current.logPath}` })
        if (this.retries++ >= 1) return this.status()
      }
    } while (!this.stopped && generation === this.generation)
    return this.status()
  }

  private async launch(generation: number): Promise<EngineStatus> {
    for (const directory of ['config', 'data', 'logs', 'reports', 'cache']) {
      mkdirSync(join(this.options.userDataPath, directory), { recursive: true })
    }
    this.update({ state: 'starting', baseUrl: null, error: null })
    const root = join(this.options.resourcesPath, 'engine')
    const config = join(this.options.userDataPath, 'config', 'panic.yaml')
    if (!existsSync(config)) copyFileSync(join(root, 'config', 'settings.yaml'), config)
    const command = this.options.isPackaged ? join(root, 'panic-engine', 'panic-engine.exe')
      : this.options.pythonCommand ?? process.env.PYTHON ?? 'python'
    if (this.options.isPackaged && !existsSync(command)) throw new Error(`当前版本内置引擎缺失：${command}`)
    const port = await (this.options.allocatePort ?? allocateLocalPort)()
    if (this.stopped || generation !== this.generation) return this.status()
    const instanceId = randomUUID()
    const args = [...(this.options.isPackaged ? [] : [join(root, 'server.py')]),
      '--host', '127.0.0.1', '--port', String(port), '--database', join(this.options.userDataPath, 'data', 'panic-index.db'),
      '--config', config, '--log-directory', join(this.options.userDataPath, 'logs'),
      '--client-version', this.current.clientVersion, '--instance-id', instanceId, '--parent-pid', String(process.pid)]
    this.log(`启动 client=${this.current.clientVersion} engine=${ENGINE_VERSION} port=${port}`)
    const child = spawn(command, args, { cwd: this.options.userDataPath, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    this.child = child
    let launchError: string | null = null
    child.stdout?.on('data', (data) => this.log(String(data).trimEnd()))
    child.stderr?.on('data', (data) => this.log(String(data).trimEnd()))
    child.once('error', (error) => { launchError = `引擎无法启动：${error.message}` })
    child.once('exit', (code, signal) => {
      launchError = `引擎已退出，代码 ${code ?? signal ?? 'unknown'}（可能为端口占用，请查看日志）`
      if (this.child !== child || this.stopped) return
      this.child = null
      if (this.current.state === 'ready') {
        this.update({ state: 'error', baseUrl: null, error: `${launchError}；日志：${this.current.logPath}` })
        if (this.retries++ < 1) void this.start()
      }
    })
    const baseUrl = `http://127.0.0.1:${port}`
    const deadline = Date.now() + (this.options.startupTimeoutMs ?? 45000)
    while (Date.now() < deadline && !this.stopped && generation === this.generation) {
      if (launchError) throw new Error(launchError)
      let health: Record<string, unknown>
      try { health = await engineRequest(`${baseUrl}/healthz`, 'GET', 1500) as Record<string, unknown> }
      catch { await new Promise((resolve) => setTimeout(resolve, 100)); continue }
      if (health.instance_id !== instanceId) throw new Error('本地端口被其他服务占用，实例校验失败')
      if (health.engine_version !== ENGINE_VERSION || health.database_schema_version !== DATABASE_VERSION || health.client_version !== this.current.clientVersion || health.ok !== true) {
        throw new Error(`引擎版本不匹配：${String(health.engine_version)}，数据库 ${String(health.database_schema_version)}`)
      }
      if (launchError || child.exitCode !== null || this.stopped || generation !== this.generation) continue
      this.update({ state: 'ready', baseUrl, error: null, databaseVersion: DATABASE_VERSION })
      return this.status()
    }
    if (this.stopped || generation !== this.generation) return this.status()
    throw new Error('引擎启动超时')
  }

  private async terminateChild(): Promise<void> {
    const child = this.child
    this.child = null
    if (!child || child.exitCode !== null || child.signalCode !== null) return
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => { child.kill('SIGKILL') }, 3000)
      child.once('exit', () => { clearTimeout(timer); resolve() })
      child.once('error', () => { clearTimeout(timer); resolve() })
      if (!child.kill()) { clearTimeout(timer); resolve() }
    })
  }

  async stop(): Promise<void> {
    this.stopped = true
    ++this.generation
    this.update({ state: 'stopped', baseUrl: null })
    await this.terminateChild()
    await this.startPromise
  }

  async restart(): Promise<EngineStatus> {
    await this.stop()
    this.retries = 0
    return this.start()
  }

  private async call(path: string, method: string): Promise<unknown> {
    if (!/^\/(?:api\/v1\/[a-z/]+|healthz)(?:\?[^#]*)?$/.test(path)) throw new Error('引擎路径不在白名单中')
    const status = this.current
    if (!status.baseUrl || status.state !== 'ready') throw new Error(status.error ?? '引擎未连接')
    try {
      return await engineRequest(`${status.baseUrl}${path}`, method, method === 'POST' ? 180000 : this.options.requestTimeoutMs ?? 10000)
    } catch (error) {
      if (this.stopped || this.current.baseUrl !== status.baseUrl || this.current.state !== 'ready') throw error
      if (!(error instanceof EngineHttpError)) {
        // 单个采集请求超时或响应损坏并不代表服务进程失效。
        let healthy = false
        try {
          const health = await engineRequest(`${status.baseUrl}/healthz`, 'GET', 1500) as Record<string, unknown>
          healthy = health.ok === true && health.engine_version === ENGINE_VERSION &&
            health.database_schema_version === DATABASE_VERSION && health.client_version === this.current.clientVersion
        } catch { /* 健康检查也失败，进入现有的一次恢复流程。 */ }
        if (healthy) throw error
        if (this.stopped || this.current.baseUrl !== status.baseUrl || this.current.state !== 'ready') throw error
        this.update({ state: 'error', baseUrl: null, error: `${String(error)}；日志：${this.current.logPath}` })
        if (this.retries++ < 1 && !this.stopped) {
          await this.terminateChild()
          await this.start()
        }
      }
      throw error
    }
  }

  get(path: string): Promise<unknown> { return this.call(path, 'GET') }
  post(path: string): Promise<unknown> { return this.call(path, 'POST') }
}
