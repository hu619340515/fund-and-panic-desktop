import './style.css'
import type { ActionResult, AppSnapshot, FundQuote, QuoteSource } from '@shared/types'
import { primaryChange } from './quote-display'
import {
  asRecord,
  chartPaths,
  finiteNumber,
  formatCompactMoney,
  historyPoints,
  marketDate,
  panicUiState,
  type PanicPoint,
  type UnknownRecord
} from './panic-view'

const isMiniMode = new URLSearchParams(window.location.search).get('mode') === 'mini'
document.body.classList.toggle('mini-mode', isMiniMode)

function element<T extends HTMLElement>(id: string): T {
  const value = document.getElementById(id)
  if (!value) throw new Error(`缺少界面元素：${id}`)
  return value as T
}

const fundList = element<HTMLDivElement>('fund-list')
const emptyState = element<HTMLDivElement>('empty-state')
const totalValue = element<HTMLElement>('summary-total')
const liveValue = element<HTMLElement>('summary-live')
const averageValue = element<HTMLElement>('summary-average')
const globalState = element<HTMLElement>('global-state')
const lastRefresh = element<HTMLElement>('last-refresh')
const refreshButton = element<HTMLButtonElement>('refresh-button')
const fundRefreshButton = element<HTMLButtonElement>('fund-refresh-button')
const addToggleButton = element<HTMLButtonElement>('add-toggle-button')
const quickAdd = element<HTMLElement>('quick-add')
const addForm = element<HTMLFormElement>('add-form')
const addButton = element<HTMLButtonElement>('add-button')
const codeInput = element<HTMLInputElement>('fund-code')
const settingsButton = element<HTMLButtonElement>('settings-button')
const settingsDialog = element<HTMLDialogElement>('settings-dialog')
const settingsForm = element<HTMLFormElement>('settings-form')
const settingsClose = element<HTMLButtonElement>('settings-close')
const settingsCancel = element<HTMLButtonElement>('settings-cancel')
const autoRefresh = element<HTMLInputElement>('auto-refresh')
const refreshInterval = element<HTMLSelectElement>('refresh-interval')
const launchAtLogin = element<HTMLInputElement>('launch-at-login')
const alwaysOnTop = element<HTMLInputElement>('always-on-top')
const windowOpacity = element<HTMLInputElement>('window-opacity')
const windowOpacityValue = element<HTMLOutputElement>('window-opacity-value')
const toast = element<HTMLDivElement>('toast')

const panicState = element<HTMLElement>('panic-state')
const panicScore = element<HTMLElement>('panic-score')
const panicLevel = element<HTMLElement>('panic-level')
const panicTime = element<HTMLElement>('panic-time')
const panicDaily = element<HTMLElement>('panic-daily')
const panicDailyLevel = element<HTMLElement>('panic-daily-level')
const panicDailyTime = element<HTMLElement>('panic-daily-time')
const panicRaw = element<HTMLElement>('panic-raw')
const panicConfidence = element<HTMLElement>('panic-confidence')
const panicCoverage = element<HTMLElement>('panic-coverage')
const panicQuality = element<HTMLElement>('panic-quality')
const panicEngineStatus = element<HTMLElement>('panic-engine-status')
const panicRefreshButton = element<HTMLButtonElement>('panic-refresh-button')
const panicComponents = element<HTMLDivElement>('panic-components')
const breadthDetails = element<HTMLDivElement>('breadth-details')
const derivativeDetails = element<HTMLDivElement>('derivative-details')
const liquidityDetails = element<HTMLDivElement>('liquidity-details')
const intradayChart = element<HTMLDivElement>('intraday-chart')
const dailyChart = element<HTMLDivElement>('daily-chart')
const intradayCount = element<HTMLElement>('intraday-count')
const dailyCount = element<HTMLElement>('daily-count')
const sourceSummary = element<HTMLElement>('source-summary')
const sourceList = element<HTMLDivElement>('source-list')
const engineVersion = element<HTMLElement>('engine-version')
const panicError = element<HTMLElement>('panic-error')
const panicErrorText = element<HTMLElement>('panic-error-text')
const panicRetryButton = element<HTMLButtonElement>('panic-retry-button')
const intradayExportButton = element<HTMLButtonElement>('intraday-export-button')
const dailyExportButton = element<HTMLButtonElement>('daily-export-button')

let currentState: AppSnapshot | null = null
let toastTimer: number | null = null
let panicDetailRequest: Promise<void> | null = null
let intradayPoints: PanicPoint[] = []
let dailyPoints: PanicPoint[] = []
let sourceData: UnknownRecord | null = null
const expandedFunds = new Set<string>()

const SOURCE_LABELS: Record<QuoteSource, string> = {
  legacy: '原接口',
  tiantian: '天天基金新接口',
  'sina-primary': '新浪估值',
  'sina-secondary': '新浪备用口径',
  'official-nav': '官方净值',
  cache: '本地缓存',
  unavailable: '不可用'
}

const COMPONENT_LABELS: Record<string, string> = {
  volatility: '波动与跳跃',
  breadth: '市场宽度',
  derivatives: '衍生品压力',
  liquidity: '流动性压力'
}

const QUALITY_LABELS: Record<string, string> = {
  complete: '完整',
  provisional: '暂定',
  degraded: '降级',
  insufficient: '不足',
  unavailable: '不可用'
}

function showToast(message: string, kind: 'normal' | 'error' = 'normal'): void {
  toast.textContent = message
  toast.classList.toggle('toast-error', kind === 'error')
  toast.classList.add('toast-visible')
  if (toastTimer !== null) window.clearTimeout(toastTimer)
  toastTimer = window.setTimeout(() => toast.classList.remove('toast-visible'), 3_000)
}

function formatNumber(value: unknown, digits = 2): string {
  const number = finiteNumber(value)
  return number === null ? '--' : number.toFixed(digits)
}

function formatPercent(value: unknown, ratio = true, digits = 1): string {
  const number = finiteNumber(value)
  return number === null ? '--' : `${(ratio ? number * 100 : number).toFixed(digits)}%`
}

function formatChange(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return '--'
  return `${value > 0 ? '+' : ''}${value.toFixed(2)}%`
}

function valueClass(value: number | null): string {
  if (value === null || !Number.isFinite(value) || value === 0) return 'value-flat'
  return value > 0 ? 'value-up' : 'value-down'
}

function friendlyTime(value: string | null): string {
  if (!value) return '尚未刷新'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit'
  }).format(date)
}

function headerTime(value: string | null): string {
  if (!value) return '正在准备数据…'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', weekday: 'short',
    hour: '2-digit', minute: '2-digit', hour12: false
  }).format(date)
}

function freshnessLabel(quote: FundQuote): string {
  const labels = {
    today: '今日估值', recent: '最近估值', official: '最新净值',
    stale: '缓存数据', unavailable: '暂无数据'
  }
  return labels[quote.freshness]
}

function sourceLabel(quote: FundQuote): string {
  if (quote.source === 'cache' && quote.cachedSource) return `缓存 · ${SOURCE_LABELS[quote.cachedSource]}`
  return SOURCE_LABELS[quote.source]
}

function metric(label: string, value: string, className = ''): HTMLDivElement {
  const node = document.createElement('div')
  node.className = 'metric'
  const labelNode = document.createElement('span')
  labelNode.textContent = label
  const valueNode = document.createElement('strong')
  valueNode.textContent = value
  if (className) valueNode.classList.add(className)
  node.append(labelNode, valueNode)
  return node
}

function actionButton(label: string, title: string, className = ''): HTMLButtonElement {
  const button = document.createElement('button')
  button.type = 'button'
  button.className = `card-action ${className}`.trim()
  button.textContent = label
  button.title = title
  button.setAttribute('aria-label', title)
  return button
}

function renderFundCard(quote: FundQuote, index: number, total: number): HTMLElement {
  const card = document.createElement('details')
  card.className = `fund-card freshness-${quote.freshness}`
  card.open = expandedFunds.has(quote.code)
  card.addEventListener('toggle', () => {
    if (card.open) expandedFunds.add(quote.code)
    else expandedFunds.delete(quote.code)
  })

  const summary = document.createElement('summary')
  summary.className = 'fund-summary'
  const identity = document.createElement('div')
  identity.className = 'fund-identity'
  const name = document.createElement('h3')
  name.textContent = quote.name
  const code = document.createElement('span')
  code.textContent = quote.code
  identity.append(name, code)

  const displayedChange = primaryChange(quote)
  const primary = document.createElement('div')
  primary.className = `fund-primary ${valueClass(displayedChange.value)}`
  primary.title = displayedChange.title
  const primaryLabel = document.createElement('span')
  primaryLabel.className = 'primary-label'
  primaryLabel.textContent = displayedChange.label
  const direction = document.createElement('span')
  direction.className = 'change-direction'
  direction.textContent = displayedChange.value === null || displayedChange.value === 0
    ? '—' : displayedChange.value > 0 ? '▲' : '▼'
  const change = document.createElement('strong')
  change.textContent = formatChange(displayedChange.value)
  primary.append(primaryLabel, direction, change)
  summary.append(identity, primary)

  const values = document.createElement('div')
  values.className = 'fund-values'
  values.append(
    metric('估算净值', formatNumber(quote.estimatedNav, 4)),
    metric('估算涨跌', formatChange(quote.estimatedChange), valueClass(quote.estimatedChange)),
    metric('官方净值', formatNumber(quote.officialNav, 4)),
    metric('官方日涨跌', formatChange(quote.officialChange), valueClass(quote.officialChange)),
    metric('数据时间', quote.valuationTime || '--'),
    metric('净值日期', quote.officialDate || '--')
  )

  const detail = document.createElement('div')
  detail.className = 'fund-detail'
  const meta = document.createElement('div')
  meta.className = 'fund-meta'
  const badges = document.createElement('div')
  badges.className = 'badges'
  const freshness = document.createElement('span')
  freshness.className = `badge badge-${quote.freshness}`
  freshness.textContent = freshnessLabel(quote)
  const source = document.createElement('span')
  source.className = 'badge badge-source'
  source.textContent = sourceLabel(quote)
  badges.append(freshness, source)
  const message = document.createElement('span')
  message.className = quote.error ? 'fund-message warning-message' : 'fund-message'
  message.textContent = quote.error || `更新于 ${friendlyTime(quote.fetchedAt)}`
  meta.append(badges, message)

  const actions = document.createElement('div')
  actions.className = 'card-actions'
  const up = actionButton('↑', `上移 ${quote.name}`)
  up.disabled = index === 0
  up.addEventListener('click', () => void moveFund(index, -1))
  const down = actionButton('↓', `下移 ${quote.name}`)
  down.disabled = index === total - 1
  down.addEventListener('click', () => void moveFund(index, 1))
  const remove = actionButton('删除', `删除 ${quote.name}`, 'danger-action')
  remove.addEventListener('click', () => void removeFund(quote))
  actions.append(up, down, remove)
  detail.append(values, meta, actions)
  card.append(summary, detail)
  return card
}

function render(state: AppSnapshot): void {
  currentState = state
  totalValue.textContent = String(state.funds.length)
  const liveQuotes = state.quotes.filter((quote) => quote.freshness === 'today' && quote.estimatedChange !== null)
  liveValue.textContent = String(liveQuotes.length)
  const average = liveQuotes.length
    ? liveQuotes.reduce((sum, quote) => sum + (quote.estimatedChange ?? 0), 0) / liveQuotes.length
    : null
  averageValue.textContent = formatChange(average)
  averageValue.className = valueClass(average)

  const refreshing = state.refreshing || Boolean(state.panic.refreshing)
  const hasError = state.engine.state === 'error' || Boolean(state.panic.error)
  globalState.textContent = refreshing ? '正在刷新' : hasError ? '部分数据不可用' : '监测正常'
  globalState.className = `status-chip ${refreshing ? 'status-starting' : hasError ? 'status-error' : 'status-ready'}`
  lastRefresh.textContent = `基金 ${headerTime(state.lastRefreshAt)} · 恐慌 ${headerTime(state.panic.refreshedAt)}`
  refreshButton.disabled = refreshing
  refreshButton.classList.toggle('is-loading', refreshing)
  fundRefreshButton.disabled = state.refreshing
  fundRefreshButton.classList.toggle('is-loading', state.refreshing)

  const quoteMap = new Map(state.quotes.map((quote) => [quote.code, quote]))
  const orderedQuotes = state.funds.map((fund) => quoteMap.get(fund.code) ?? ({
    code: fund.code,
    name: `基金 ${fund.code}`,
    officialNav: null,
    officialChange: null,
    officialDate: null,
    estimatedNav: null,
    estimatedChange: null,
    valuationTime: null,
    source: 'unavailable',
    freshness: 'unavailable',
    fetchedAt: fund.addedAt,
    error: '等待首次刷新'
  } satisfies FundQuote))
  fundList.replaceChildren(...orderedQuotes.map((quote, index) => renderFundCard(quote, index, orderedQuotes.length)))
  emptyState.hidden = state.funds.length > 0

  autoRefresh.checked = state.settings.autoRefresh
  refreshInterval.value = String(state.settings.refreshIntervalSeconds)
  launchAtLogin.checked = state.settings.launchAtLogin
  alwaysOnTop.checked = state.settings.alwaysOnTop
  windowOpacity.value = String(Math.round(state.settings.windowOpacity * 100))
  windowOpacityValue.value = `${windowOpacity.value}%`
  renderPanic(state)
}

function renderPanic(state: AppSnapshot): void {
  const realtime = asRecord(state.panic.realtime)
  const daily = asRecord(state.panic.daily)
  const aggregate = asRecord(realtime?.aggregate) ?? {}
  const features = asRecord(realtime?.feature_values) ?? asRecord(daily?.feature_values) ?? {}
  const uiState = panicUiState({
    refreshing: Boolean(state.panic.refreshing),
    realtime,
    daily,
    error: state.panic.error ?? state.engine.error
  })

  const stateMessages = {
    loading: '正在获取首次数据…',
    success: state.panic.refreshing ? '正在更新，当前显示上次成功数据' : '数据已更新',
    stale: '当前显示的是过期数据，请刷新或检查数据源',
    error: state.engine.state === 'ready' ? '行情采集或读取失败，请查看下方数据源错误；已有收盘记录仍可查看' : '引擎未连接，请查看下方错误并重试',
    empty: '暂无真实数据，等待引擎完成首次采集'
  }
  panicState.textContent = stateMessages[uiState]
  panicState.className = `data-state data-state-${uiState}`

  const engineLabels = { ready: state.panic.error ? '引擎已连接 · 数据请求失败' : '引擎已连接', starting: '引擎启动中', stopped: '引擎未连接', error: '引擎未连接' }
  panicEngineStatus.textContent = engineLabels[state.engine.state]
  panicEngineStatus.className = `status-chip status-${state.engine.state}`
  panicRefreshButton.disabled = Boolean(state.panic.refreshing)
  panicRefreshButton.classList.toggle('is-loading', Boolean(state.panic.refreshing))

  panicScore.textContent = formatNumber(realtime?.realtime_panic_index)
  panicRaw.textContent = formatNumber(realtime?.realtime_panic_index_raw)
  panicLevel.textContent = typeof realtime?.level === 'string' ? realtime.level : '暂无数据'
  panicTime.textContent = `数据时间 ${typeof realtime?.timestamp === 'string' ? friendlyTime(realtime.timestamp) : '--'}`
  panicDaily.textContent = formatNumber(daily?.final_panic_index)
  panicDailyLevel.textContent = typeof daily?.level === 'string' ? daily.level : '暂无数据'
  panicDailyTime.textContent = `交易日 ${typeof daily?.trade_date === 'string' ? daily.trade_date : '--'}`
  panicConfidence.textContent = formatPercent(realtime?.confidence ?? daily?.confidence, false)
  panicCoverage.textContent = formatPercent(realtime?.coverage ?? daily?.coverage)
  const quality = realtime?.quality_status ?? daily?.quality_status
  panicQuality.textContent = typeof quality === 'string' ? (QUALITY_LABELS[quality] ?? quality) : '--'

  const components = asRecord(realtime?.components) ?? asRecord(daily?.components) ?? {}
  panicComponents.replaceChildren(...Object.entries(COMPONENT_LABELS).map(([key, label]) => {
    const value = finiteNumber(components[key])
    const card = document.createElement('article')
    card.className = 'component-card'
    const heading = document.createElement('div')
    heading.append(metric(label, value === null ? '--' : value.toFixed(1)))
    const bar = document.createElement('div')
    bar.className = 'component-bar'
    const fill = document.createElement('i')
    fill.style.width = `${Math.max(0, Math.min(100, value ?? 0))}%`
    bar.append(fill)
    card.append(heading, bar)
    return card
  }))

  breadthDetails.replaceChildren(
    metric('上涨 / 下跌', `${aggregate.up_count ?? '--'} / ${aggregate.down_count ?? '--'}`),
    metric('平盘 / 有效股票', `${aggregate.flat_count ?? '--'} / ${aggregate.valid_stock_count ?? '--'}`),
    metric('下跌占比', formatPercent(aggregate.decline_share)),
    metric('跌幅 ≥ 5%', formatPercent(aggregate.decline_5_share)),
    metric('跌幅 ≥ 7%', formatPercent(aggregate.decline_7_share)),
    metric('涨停 / 跌停', `${aggregate.limit_up ?? '--'} / ${aggregate.limit_down ?? '--'}`),
    metric('收益中位数', formatPercent(aggregate.median_return))
  )
  derivativeDetails.replaceChildren(
    metric('QVIX', formatNumber(aggregate.qvix)),
    metric('QVIX 昨收', formatNumber(aggregate.qvix_previous_close)),
    metric('QVIX 5 分钟前', formatNumber(aggregate.qvix_previous_5m)),
    metric('IF 近月合约', String(aggregate.front_contract ?? '--')),
    metric('IF 近月价格', formatNumber(aggregate.front_price)),
    metric('IF 近月年化基差', formatPercent(features.front_annualized_basis)),
    metric('IF 次月合约', String(aggregate.next_contract ?? '--')),
    metric('IF 次月年化基差', formatPercent(features.next_annualized_basis))
  )
  liquidityDetails.replaceChildren(
    metric('累计成交额', formatCompactMoney(aggregate.market_amount)),
    metric('预计全天成交额', formatCompactMoney(aggregate.projected_full_day_amount)),
    metric('近 5 分钟成交额', formatCompactMoney(aggregate.incremental_amount_5m)),
    metric('20 日成交额中位数', formatCompactMoney(aggregate.median_daily_market_amount_20)),
    metric('预期累计进度', formatPercent(aggregate.expected_cumulative_share))
  )

  const error = state.panic.error ?? state.engine.error
  panicError.hidden = !error
  panicErrorText.textContent = error ?? ''
  engineVersion.textContent = [
    `引擎 ${state.engine.version || '--'}`,
    `客户端 ${state.engine.clientVersion || '--'}`,
    `数据库 V${state.engine.databaseVersion ?? '--'}`,
    state.engine.logPath ? `日志 ${state.engine.logPath}` : ''
  ].filter(Boolean).join(' · ')
  renderSources(realtime)
  renderCharts()
}

function renderSources(realtime: UnknownRecord | null): void {
  const aggregate = asRecord(realtime?.aggregate)
  const activeSources = asRecord(aggregate?.sources) ?? {}
  const health = Array.isArray(sourceData?.health)
    ? sourceData.health.map(asRecord).filter((item): item is UnknownRecord => item !== null)
    : []
  const probe = Array.isArray(sourceData?.probe)
    ? sourceData.probe.map(asRecord).filter((item): item is UnknownRecord => item !== null)
    : []
  const sourceCards: HTMLElement[] = []

  for (const [semantic, raw] of Object.entries(activeSources)) {
    const value = asRecord(raw) ?? {}
    const card = document.createElement('article')
    card.className = 'source-card source-active'
    const title = document.createElement('strong')
    title.textContent = `${semantic} · ${String(value.provider ?? '未知')}`
    const detail = document.createElement('span')
    detail.textContent = `本次数据 ${typeof value.source_timestamp === 'string' ? friendlyTime(value.source_timestamp) : '时间未知'}`
    card.append(title, detail)
    sourceCards.push(card)
  }

  for (const item of health) {
    const score = finiteNumber(item.health_score)
    const failures = finiteNumber(item.consecutive_failures) ?? 0
    const openUntil = typeof item.circuit_open_until === 'string' ? item.circuit_open_until : null
    const card = document.createElement('article')
    card.className = `source-card ${failures > 0 || openUntil ? 'source-warning' : 'source-healthy'}`
    const title = document.createElement('strong')
    title.textContent = `${String(item.semantic_type ?? '数据')} · ${String(item.provider ?? '未知')}`
    const detail = document.createElement('span')
    detail.textContent = openUntil
      ? `熔断至 ${friendlyTime(openUntil)}`
      : `健康分 ${score === null ? '--' : score.toFixed(0)} · 连续失败 ${failures}`
    const note = document.createElement('small')
    note.textContent = typeof item.last_error === 'string' && item.last_error ? item.last_error : `最近成功 ${friendlyTime(typeof item.last_success_at === 'string' ? item.last_success_at : null)}`
    card.append(title, detail, note)
    sourceCards.push(card)
  }

  if (sourceCards.length === 0 && probe.length > 0) {
    for (const item of probe) {
      const card = document.createElement('article')
      card.className = `source-card ${item.available ? 'source-healthy' : 'source-warning'}`
      const title = document.createElement('strong')
      title.textContent = `${String(item.semantic_type ?? '数据')} · ${String(item.provider ?? '未知')}`
      const detail = document.createElement('span')
      detail.textContent = item.available ? `探测可用 · ${formatNumber(item.latency_ms, 0)} ms` : `探测不可用 · ${String(item.error ?? '未知原因')}`
      card.append(title, detail)
      sourceCards.push(card)
    }
  }

  sourceList.replaceChildren(...sourceCards)
  const warningCount = health.filter((item) => (finiteNumber(item.consecutive_failures) ?? 0) > 0 || item.circuit_open_until).length
  sourceSummary.textContent = sourceCards.length === 0
    ? '暂无来源健康记录'
    : `${sourceCards.length} 项来源记录${warningCount ? ` · ${warningCount} 项需关注` : ' · 状态正常'}`
}

function renderChart(target: HTMLDivElement, points: PanicPoint[], includeRaw: boolean): void {
  target.replaceChildren()
  if (points.length === 0) {
    const empty = document.createElement('div')
    empty.className = 'chart-empty'
    empty.textContent = '暂无真实历史记录'
    target.append(empty)
    return
  }
  const geometry = chartPaths(points, 640, 180, 24)
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
  svg.setAttribute('viewBox', '0 0 640 180')
  svg.setAttribute('role', 'img')
  svg.setAttribute('aria-label', `包含 ${points.length} 条真实记录的曲线`)
  for (const ratio of [0, 0.5, 1]) {
    const y = 24 + ratio * 132
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line')
    line.setAttribute('x1', '24')
    line.setAttribute('x2', '616')
    line.setAttribute('y1', String(y))
    line.setAttribute('y2', String(y))
    line.setAttribute('class', 'chart-grid-line')
    svg.append(line)
  }
  if (includeRaw && geometry.raw) {
    const raw = document.createElementNS('http://www.w3.org/2000/svg', 'path')
    raw.setAttribute('d', geometry.raw)
    raw.setAttribute('class', 'chart-line chart-line-raw')
    svg.append(raw)
  }
  const display = document.createElementNS('http://www.w3.org/2000/svg', 'path')
  display.setAttribute('d', geometry.display)
  display.setAttribute('class', 'chart-line chart-line-display')
  svg.append(display)
  const labels = document.createElement('div')
  labels.className = 'chart-labels'
  labels.append(
    Object.assign(document.createElement('span'), { textContent: points[0]?.time.slice(0, 16) ?? '' }),
    Object.assign(document.createElement('span'), { textContent: `${geometry.minimum}–${geometry.maximum}` }),
    Object.assign(document.createElement('span'), { textContent: points.at(-1)?.time.slice(0, 16) ?? '' })
  )
  target.append(svg, labels)
}

function renderCharts(): void {
  intradayCount.textContent = `${intradayPoints.length} 条真实记录`
  dailyCount.textContent = `${dailyPoints.length} 条真实记录`
  renderChart(intradayChart, intradayPoints, true)
  renderChart(dailyChart, dailyPoints, false)
}

async function loadPanicDetails(force = false): Promise<void> {
  if (isMiniMode || currentState?.engine.state !== 'ready') return
  if (panicDetailRequest && !force) return panicDetailRequest
  const tradeDate = marketDate()
  panicDetailRequest = (async () => {
    const results = await Promise.allSettled([
      window.fundApp.panic.getRealtimeHistory(tradeDate),
      window.fundApp.panic.getDailyHistory(500),
      window.fundApp.panic.getSources()
    ])
    if (results[0].status === 'fulfilled') intradayPoints = historyPoints(results[0].value, 'intraday')
    if (results[1].status === 'fulfilled') dailyPoints = historyPoints(results[1].value, 'daily')
    if (results[2].status === 'fulfilled') sourceData = asRecord(results[2].value)
    renderCharts()
    renderSources(asRecord(currentState?.panic.realtime))
    const failures = results.filter((result) => result.status === 'rejected')
    if (failures.length === results.length) showToast('历史与来源信息加载失败，可稍后重试', 'error')
  })().finally(() => { panicDetailRequest = null })
  return panicDetailRequest
}

async function consumeResult(result: ActionResult<AppSnapshot>, successMessage?: string): Promise<void> {
  if (!result.ok || !result.data) {
    showToast(result.error || '操作失败', 'error')
    return
  }
  render(result.data)
  if (successMessage) showToast(successMessage)
}

async function moveFund(index: number, offset: number): Promise<void> {
  if (!currentState) return
  const codes = currentState.funds.map((fund) => fund.code)
  const target = index + offset
  if (target < 0 || target >= codes.length) return
  const current = codes[index]
  const other = codes[target]
  if (!current || !other) return
  codes[index] = other
  codes[target] = current
  await consumeResult(await window.fundApp.reorderFunds(codes))
}

async function removeFund(quote: FundQuote): Promise<void> {
  if (!window.confirm(`确定从列表中删除“${quote.name}（${quote.code}）”吗？`)) return
  await consumeResult(await window.fundApp.removeFund(quote.code), '基金已删除')
}

async function refreshPanic(): Promise<void> {
  try {
    await window.fundApp.panic.refresh()
    render(await window.fundApp.getState())
    await loadPanicDetails(true)
    showToast(currentState?.panic.error ? '恐慌指数刷新失败，请查看数据源错误' : '恐慌指数刷新完成', currentState?.panic.error ? 'error' : undefined)
  } catch (error) {
    showToast(error instanceof Error ? error.message : '恐慌指数刷新失败', 'error')
    await loadPanicDetails(true)
  }
}

async function exportChart(type: 'intraday' | 'daily', button: HTMLButtonElement): Promise<void> {
  button.disabled = true
  try {
    const result = await window.fundApp.panic.generateChart(type)
    showToast(`图表已导出：${result.path}`)
  } catch (error) {
    showToast(error instanceof Error ? error.message : '图表导出失败', 'error')
  } finally {
    button.disabled = false
  }
}

addForm.addEventListener('submit', async (event) => {
  event.preventDefault()
  const code = codeInput.value.trim()
  if (!/^\d{6}$/.test(code)) {
    showToast('请输入六位基金代码', 'error')
    codeInput.focus()
    return
  }
  addButton.disabled = true
  addButton.textContent = '验证中…'
  try {
    const result = await window.fundApp.addFund(code)
    if (result.ok) {
      codeInput.value = ''
      quickAdd.hidden = true
      addToggleButton.setAttribute('aria-expanded', 'false')
    }
    await consumeResult(result, '基金已添加')
  } finally {
    addButton.disabled = false
    addButton.textContent = '添加'
  }
})

addToggleButton.addEventListener('click', () => {
  const opening = quickAdd.hidden
  quickAdd.hidden = !opening
  addToggleButton.setAttribute('aria-expanded', String(opening))
  if (opening) codeInput.focus()
})
codeInput.addEventListener('input', () => { codeInput.value = codeInput.value.replace(/\D/g, '').slice(0, 6) })

refreshButton.addEventListener('click', async () => {
  const [fundResult, panicResult] = await Promise.allSettled([
    window.fundApp.refresh(),
    window.fundApp.panic.refresh()
  ])
  if (fundResult.status === 'fulfilled') await consumeResult(fundResult.value)
  render(await window.fundApp.getState())
  await loadPanicDetails(true)
  if (fundResult.status === 'rejected' || panicResult.status === 'rejected') showToast('部分数据刷新失败，请查看状态', 'error')
  else showToast('全部数据刷新完成')
})

fundRefreshButton.addEventListener('click', async () => {
  await consumeResult(await window.fundApp.refresh(), '基金刷新完成')
})

panicRefreshButton.addEventListener('click', () => void refreshPanic())
panicRetryButton.addEventListener('click', () => void refreshPanic())
intradayExportButton.addEventListener('click', () => void exportChart('intraday', intradayExportButton))
dailyExportButton.addEventListener('click', () => void exportChart('daily', dailyExportButton))

settingsButton.addEventListener('click', () => settingsDialog.showModal())
settingsClose.addEventListener('click', () => settingsDialog.close())
settingsCancel.addEventListener('click', () => settingsDialog.close())
settingsDialog.addEventListener('click', (event) => { if (event.target === settingsDialog) settingsDialog.close() })
windowOpacity.addEventListener('input', () => { windowOpacityValue.value = `${windowOpacity.value}%` })
settingsForm.addEventListener('submit', async (event) => {
  event.preventDefault()
  const result = await window.fundApp.updateSettings({
    autoRefresh: autoRefresh.checked,
    refreshIntervalSeconds: Number(refreshInterval.value),
    launchAtLogin: launchAtLogin.checked,
    alwaysOnTop: alwaysOnTop.checked,
    windowOpacity: Number(windowOpacity.value) / 100
  })
  if (result.ok) settingsDialog.close()
  await consumeResult(result, '设置已保存')
})

async function start(): Promise<void> {
  try {
    render(await window.fundApp.getState())
    window.fundApp.onStateChanged((state) => {
      const becameReady = currentState?.engine.state !== 'ready' && state.engine.state === 'ready'
      const refreshFinished = currentState?.panic.refreshing === true && state.panic.refreshing === false
      render(state)
      if (becameReady || refreshFinished) void loadPanicDetails()
    })
    await loadPanicDetails()
  } catch (error) {
    showToast(error instanceof Error ? error.message : '应用初始化失败', 'error')
  }
}

void start()
