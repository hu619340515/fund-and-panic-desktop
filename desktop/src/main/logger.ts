import { appendFileSync, existsSync, mkdirSync, renameSync, statSync } from 'node:fs'
import { join } from 'node:path'

// 桌面诊断仅写入本地日志；不记录配置对象、环境变量或基金组合。
export function createLogger(userData: string): (message: string, error?: unknown) => void {
  const directory = join(userData, 'logs')
  const file = join(directory, 'desktop.log')
  return (message, error) => {
    const detail = error instanceof Error ? error.message : error === undefined ? '' : String(error)
    try {
      mkdirSync(directory, { recursive: true })
      if (existsSync(file) && statSync(file).size > 5 * 1024 * 1024) renameSync(file, join(directory, `desktop-${Date.now()}.log`))
      appendFileSync(file, `${new Date().toISOString()} ${message} ${detail}\n`, 'utf8')
    } catch (writeError) { console.error('本地日志写入失败', writeError) }
  }
}
