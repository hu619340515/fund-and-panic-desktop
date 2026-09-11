// 运行真实 PyInstaller EXE，验收 spawn worker 的成功、异常退出、硬超时和回收。
const { spawn } = require('node:child_process')
const { existsSync } = require('node:fs')
const { join, resolve } = require('node:path')

const root = resolve(__dirname, '..')
const executable = join(root, 'engine/dist/panic-engine/panic-engine.exe')
if (!existsSync(executable)) {
  throw new Error('请先运行 npm run build:engine，当前内置引擎不存在')
}

function pidIsAlive(pid) {
  try {
    process.kill(pid, 0)
    return true
  } catch (error) {
    if (error && error.code === 'ESRCH') return false
    throw error
  }
}

function parseResult(stdout) {
  const lines = stdout.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    try {
      return JSON.parse(lines[index])
    } catch {}
  }
  throw new Error(`诊断输出中没有 JSON 结果：${stdout}`)
}

const child = spawn(executable, ['--worker-self-test'], {
  windowsHide: true,
  stdio: ['ignore', 'pipe', 'pipe'],
  env: { ...process.env, PYTHONHOME: '', PYTHONPATH: '' },
})
let stdout = ''
let stderr = ''
child.stdout.on('data', (data) => { stdout += data.toString('utf8') })
child.stderr.on('data', (data) => { stderr += data.toString('utf8') })

const deadline = setTimeout(() => {
  child.kill()
  console.error('PyInstaller worker 诊断超过 45 秒')
  process.exitCode = 1
}, 45000)

child.on('error', (error) => {
  clearTimeout(deadline)
  console.error(`无法启动 PyInstaller 引擎：${error.message}`)
  process.exitCode = 1
})

child.on('close', (code, signal) => {
  clearTimeout(deadline)
  if (process.exitCode) return
  try {
    if (code !== 0) {
      throw new Error(`PyInstaller worker 诊断退出码 ${code}，信号 ${signal ?? '无'}：${stderr}`)
    }
    const result = parseResult(stdout)
    const caseNames = Object.keys(result.cases ?? {}).sort()
    if (!result.ok || result.start_method !== 'spawn') throw new Error(`诊断状态异常：${JSON.stringify(result)}`)
    if (caseNames.join(',') !== 'abnormal_exit,hard_timeout,success') throw new Error(`诊断场景不完整：${caseNames.join(',')}`)
    if (result.cases.abnormal_exit.exit_code !== 23) throw new Error('异常退出场景未返回预期退出码 23')
    if (result.cases.hard_timeout.exception_type !== 'ProviderTimeout') throw new Error('硬超时场景未返回 ProviderTimeout')
    const residualPids = (result.worker_pids ?? []).filter(pidIsAlive)
    if ((result.residual_pids ?? []).length || residualPids.length) throw new Error(`诊断 worker 未完全回收：${residualPids}`)
    console.log(`PyInstaller spawn worker 诊断通过：成功、异常退出、硬超时均符合预期，${result.worker_pids.length} 个 worker 已回收`)
  } catch (error) {
    console.error(error.message)
    process.exitCode = 1
  }
})
