const ALLOWED_HOSTS = new Set([
  'fundgz.1234567.com.cn',
  'fundcomapi.tiantianfunds.com',
  'stock.finance.sina.com.cn',
  'fund.eastmoney.com'
])

export function assertAllowedUrl(rawUrl: string): URL {
  const url = new URL(rawUrl)
  if (url.protocol !== 'https:' || !ALLOWED_HOSTS.has(url.hostname)) {
    throw new Error(`不允许访问的数据源地址：${url.hostname}`)
  }
  return url
}

export async function fetchText(
  rawUrl: string,
  options: { timeoutMs?: number; headers?: Record<string, string> } = {}
): Promise<string> {
  const url = assertAllowedUrl(rawUrl)
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), options.timeoutMs ?? 10_000)
  try {
    const response = await fetch(url, {
      signal: controller.signal,
      redirect: 'error',
      headers: {
        Accept: 'application/json,text/javascript,text/plain,*/*',
        'User-Agent': 'FundAndPanicDesktop/2.0.4',
        ...options.headers
      }
    })
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`)
    }
    return await response.text()
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      throw new Error('请求超时')
    }
    throw error
  } finally {
    clearTimeout(timer)
  }
}

export async function fetchJson<T>(
  rawUrl: string,
  options: { timeoutMs?: number; headers?: Record<string, string> } = {}
): Promise<T> {
  const text = await fetchText(rawUrl, options)
  try {
    return JSON.parse(text) as T
  } catch {
    throw new Error('接口返回的不是合法 JSON')
  }
}

export async function mapConcurrent<T, R>(
  items: T[],
  concurrency: number,
  mapper: (item: T) => Promise<R>
): Promise<PromiseSettledResult<R>[]> {
  const results: PromiseSettledResult<R>[] = new Array(items.length)
  let nextIndex = 0

  const worker = async (): Promise<void> => {
    while (nextIndex < items.length) {
      const index = nextIndex++
      const item = items[index]
      if (item === undefined) continue
      try {
        results[index] = { status: 'fulfilled', value: await mapper(item) }
      } catch (reason) {
        results[index] = { status: 'rejected', reason }
      }
    }
  }

  const workerCount = Math.max(1, Math.min(concurrency, items.length))
  await Promise.all(Array.from({ length: workerCount }, () => worker()))
  return results
}
