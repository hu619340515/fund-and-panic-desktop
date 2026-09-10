import type { FundAppApi } from '@shared/types'

declare global {
  interface Window {
    fundApp: FundAppApi
  }
}

export {}
