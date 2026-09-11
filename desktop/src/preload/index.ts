import { contextBridge, ipcRenderer } from 'electron'
import {
  IPC_CHANNELS,
  type ActionResult,
  type AppSnapshot,
  type FundAppApi,
  type SettingsPatch
} from '@shared/types'

const api: FundAppApi = {
  panic: {
    getRealtime: () => ipcRenderer.invoke(IPC_CHANNELS.PANIC_REALTIME),
    getDailyLatest: () => ipcRenderer.invoke(IPC_CHANNELS.PANIC_DAILY),
    getRealtimeHistory: (date) => ipcRenderer.invoke(IPC_CHANNELS.PANIC_REALTIME_HISTORY, date),
    getDailyHistory: (limit) => ipcRenderer.invoke(IPC_CHANNELS.PANIC_DAILY_HISTORY, limit),
    getHistoricalEstimates: () => ipcRenderer.invoke(IPC_CHANNELS.PANIC_HISTORICAL_ESTIMATES),
    backfillHistory: () => ipcRenderer.invoke(IPC_CHANNELS.PANIC_BACKFILL_HISTORY),
    getSources: () => ipcRenderer.invoke(IPC_CHANNELS.PANIC_SOURCES),
    getHealth: () => ipcRenderer.invoke(IPC_CHANNELS.PANIC_ENGINE_STATUS),
    refresh: () => ipcRenderer.invoke(IPC_CHANNELS.PANIC_REFRESH),
    generateChart: (type) => ipcRenderer.invoke(IPC_CHANNELS.PANIC_CHART, type)
  },
  getState: () => ipcRenderer.invoke(IPC_CHANNELS.GET_STATE) as Promise<AppSnapshot>,
  addFund: (code: string) =>
    ipcRenderer.invoke(IPC_CHANNELS.ADD_FUND, code) as Promise<ActionResult<AppSnapshot>>,
  removeFund: (code: string) =>
    ipcRenderer.invoke(IPC_CHANNELS.REMOVE_FUND, code) as Promise<ActionResult<AppSnapshot>>,
  reorderFunds: (codes: string[]) =>
    ipcRenderer.invoke(IPC_CHANNELS.REORDER_FUNDS, codes) as Promise<ActionResult<AppSnapshot>>,
  refresh: () => ipcRenderer.invoke(IPC_CHANNELS.REFRESH) as Promise<ActionResult<AppSnapshot>>,
  updateSettings: (patch: SettingsPatch) =>
    ipcRenderer.invoke(IPC_CHANNELS.UPDATE_SETTINGS, patch) as Promise<ActionResult<AppSnapshot>>,
  getPanicRealtime: () => ipcRenderer.invoke(IPC_CHANNELS.PANIC_REALTIME),
  getPanicDailyLatest: () => ipcRenderer.invoke(IPC_CHANNELS.PANIC_DAILY),
  getPanicRealtimeHistory: (date?: string) => ipcRenderer.invoke(IPC_CHANNELS.PANIC_REALTIME_HISTORY, date),
  getPanicDailyHistory: (limit?: number) => ipcRenderer.invoke(IPC_CHANNELS.PANIC_DAILY_HISTORY, limit),
  getPanicSources: () => ipcRenderer.invoke(IPC_CHANNELS.PANIC_SOURCES),
  getEngineStatus: () => ipcRenderer.invoke(IPC_CHANNELS.PANIC_ENGINE_STATUS),
  refreshPanic: () => ipcRenderer.invoke(IPC_CHANNELS.PANIC_REFRESH),
  onStateChanged: (listener: (state: AppSnapshot) => void) => {
    const handler = (_event: Electron.IpcRendererEvent, state: AppSnapshot): void => listener(state)
    ipcRenderer.on(IPC_CHANNELS.STATE_CHANGED, handler)
    return () => ipcRenderer.removeListener(IPC_CHANNELS.STATE_CHANGED, handler)
  }
}
contextBridge.exposeInMainWorld('fundApp', api)

declare global {
  interface Window {
    fundApp: FundAppApi
  }
}
