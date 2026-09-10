import type { SettingsPatch } from '../shared/types'

export function validateSettingsPatch(value: unknown): SettingsPatch {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('设置参数无效')
  const patch = value as Record<string, unknown>
  const allowed = new Set(['autoRefresh', 'refreshIntervalSeconds', 'launchAtLogin', 'alwaysOnTop', 'windowOpacity'])
  if (Object.keys(patch).some((key) => !allowed.has(key))) throw new Error('包含未知设置')
  for (const key of ['autoRefresh', 'launchAtLogin', 'alwaysOnTop']) {
    if (patch[key] !== undefined && typeof patch[key] !== 'boolean') throw new Error('开关参数必须为布尔值')
  }
  if (patch.refreshIntervalSeconds !== undefined && ![30, 60, 120, 300].includes(patch.refreshIntervalSeconds as number)) throw new Error('刷新间隔无效')
  if (patch.windowOpacity !== undefined && (typeof patch.windowOpacity !== 'number' || !Number.isFinite(patch.windowOpacity) || patch.windowOpacity < 0.55 || patch.windowOpacity > 1)) throw new Error('窗口透明度无效')
  return Object.fromEntries(Object.entries(patch).filter(([, item]) => item !== undefined)) as SettingsPatch
}
