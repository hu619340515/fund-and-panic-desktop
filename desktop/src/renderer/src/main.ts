import './style.css'
import type { AllocationAdvice, AppSnapshot, EngineJob, FundQuote, Portfolio, PortfolioImportPreview, PortfolioLot, RiskHistory, RiskSnapshot, RiskValidation } from '@shared/types'
import { primaryChange } from './quote-display'
import { adviceRows, auxiliaryRows, componentLine, coreComponentRows, displayDate, forecastRows, fundQualityRows, gateLabel, historyCoordinates, historyPath, historySeries, numberText, ratioText, riskQualityDetails, riskQualitySummary, scoreDisplay } from './risk-view'

const miniMode = new URLSearchParams(location.search).get('mode') === 'mini'
document.body.classList.toggle('mini-mode', miniMode)
function el<T extends HTMLElement>(id: string): T {
  const found = document.getElementById(id)
  if (!found) throw new Error('缺少界面元素：' + id)
  return found as T
}
function value(id: string, text: unknown): void { el<HTMLElement>(id).textContent = String(text ?? '--') }
function cell(text: unknown): HTMLTableCellElement {
  const node = document.createElement('td')
  node.textContent = String(text ?? '--')
  return node
}
function row(values: unknown[]): HTMLTableRowElement {
  const tr = document.createElement('tr')
  tr.append(...values.map(cell))
  return tr
}
function lines(id: string, entries: string[]): void {
  el<HTMLElement>(id).replaceChildren(...entries.map((message) => {
    const node = document.createElement('p')
    node.textContent = message
    return node
  }))
}
function errorText(error: unknown): string { return error instanceof Error ? error.message : String(error) }
let toastTimer = 0
function toast(message: string, isError = false): void {
  const node = el<HTMLElement>('toast')
  node.textContent = message
  node.classList.toggle('toast-error', isError)
  node.classList.add('toast-visible')
  window.clearTimeout(toastTimer)
  toastTimer = window.setTimeout(() => node.classList.remove('toast-visible'), 3500)
}
const time = displayDate
function percentChange(value: number | null): string {
  return value === null || !Number.isFinite(value) ? '--' : (value > 0 ? '+' : '') + value.toFixed(2) + '%'
}

let appState: AppSnapshot | null = null
let risk: RiskSnapshot | null = null
let validation: RiskValidation | null = null
let portfolio: Portfolio | null = null
let advice: AllocationAdvice | null = null
let selectedSymbol = 'market'
const fundBenchmark = new Map<string, string>()
function querySymbol(): string { return fundBenchmark.get(selectedSymbol) ?? selectedSymbol }
let historyKind: 'backtest' | 'published' = 'published'
let history: RiskHistory | null = null
let preview: PortfolioImportPreview | null = null
let riskRequest = 0
let jobsTimer: number | null = null
let lastCompletedJobs = ''
const expandedFunds = new Set<string>()
const constraintLabels: Record<string, string> = {
  equity_cap: '权益仓位上限', vol_target: '目标波动率上限', single_fund_cap: '单只基金上限',
  industry_cap: '单行业上限', daily_change_cap: '单日调整上限', no_trade_band: '免调整带宽'
}
const lotFields = [
  ['code', 'text'], ['shares', 'number'], ['market_value', 'number'], ['valuation_date', 'date'],
  ['confirmed_date', 'date'], ['in_transit', 'number'], ['fee_buy', 'number'], ['fee_sell', 'number'],
  ['baseline_weight', 'number'], ['industry', 'text'], ['benchmark_symbol', 'text'],
  ['asset_class', 'text'], ['metadata_verified', 'checkbox'], ['effective_date', 'date']
] as const

function renderFunds(state: AppSnapshot): void {
  appState = state
  value('summary-total', state.funds.length)
  const live = state.quotes.filter((q) => q.freshness === 'today' && q.estimatedChange !== null)
  value('summary-live', live.length)
  value('summary-average', live.length ? percentChange(live.reduce((sum, q) => sum + (q.estimatedChange ?? 0), 0) / live.length) : '--')
  value('global-state', state.engine.state === 'ready' ? state.refreshing ? '基金刷新中' : '引擎已连接' : state.engine.state === 'starting' ? '引擎启动中' : '引擎不可用')
  value('last-refresh', '基金 ' + time(state.lastRefreshAt) + ' · 风险 ' + time(risk?.as_of))
  value('engine-version', '引擎 ' + state.engine.version + ' · 数据库 V' + (state.engine.databaseVersion ?? '--') + ' · 客户端 ' + state.engine.clientVersion)
  el<HTMLInputElement>('auto-refresh').checked = state.settings.autoRefresh
  el<HTMLSelectElement>('refresh-interval').value = String(state.settings.refreshIntervalSeconds)
  el<HTMLInputElement>('launch-at-login').checked = state.settings.launchAtLogin
  el<HTMLInputElement>('always-on-top').checked = state.settings.alwaysOnTop
  el<HTMLInputElement>('window-opacity').value = String(Math.round(state.settings.windowOpacity * 100))
  value('window-opacity-value', Math.round(state.settings.windowOpacity * 100) + '%')
  el<HTMLButtonElement>('fund-refresh-button').disabled = state.refreshing
  const quotes = new Map(state.quotes.map((quote) => [quote.code, quote]))
  const container = el<HTMLDivElement>('fund-list')
  container.replaceChildren(...state.funds.flatMap((fund, index) => {
    const quote = quotes.get(fund.code)
    return quote ? [fundCard(quote, index, state.funds.length)] : []
  }))
  el<HTMLElement>('empty-state').hidden = state.funds.length !== 0
}

function fundCard(quote: FundQuote, index: number, count: number): HTMLElement {
  const card = document.createElement('details')
  card.className = 'fund-card freshness-' + quote.freshness
  card.open = expandedFunds.has(quote.code)
  card.addEventListener('toggle', () => card.open ? expandedFunds.add(quote.code) : expandedFunds.delete(quote.code))
  const summary = document.createElement('summary')
  summary.className = 'fund-summary'
  const identity = document.createElement('div')
  identity.className = 'fund-identity'
  const name = document.createElement('h3')
  name.textContent = quote.name
  const code = document.createElement('span')
  code.textContent = quote.code
  identity.append(name, code)
  const change = primaryChange(quote)
  const primary = document.createElement('div')
  primary.className = 'fund-primary ' + (change.value === null ? 'value-flat' : change.value > 0 ? 'value-up' : 'value-down')
  primary.title = change.title
  const changeLabel = document.createElement('span')
  changeLabel.className = 'primary-label'
  changeLabel.textContent = change.label
  const strong = document.createElement('strong')
  strong.textContent = percentChange(change.value)
  primary.append(changeLabel, strong)
  summary.append(identity, primary)
  const detail = document.createElement('div')
  detail.className = 'fund-detail'
  const metrics = document.createElement('div')
  metrics.className = 'fund-values'
  for (const [label, text] of [
    ['估算净值', numberText(quote.estimatedNav, 4)], ['估算涨跌', percentChange(quote.estimatedChange)],
    ['官方净值', numberText(quote.officialNav, 4)], ['官方日涨跌', percentChange(quote.officialChange)],
    ['数据时间', quote.valuationTime ?? '--'], ['净值日期', quote.officialDate ?? '--'],
    ['来源', quote.source], ['数据状态', quote.freshness]
  ]) {
    const metric = document.createElement('div')
    metric.className = 'metric'
    const key = document.createElement('span')
    key.textContent = label ?? ''
    const val = document.createElement('strong')
    val.textContent = text ?? '--'
    metric.append(key, val)
    metrics.append(metric)
  }
  const actions = document.createElement('div')
  actions.className = 'card-actions'
  const button = (label: string, handler: () => void, disabled = false): HTMLButtonElement => {
    const node = document.createElement('button')
    node.className = 'card-action'
    node.type = 'button'
    node.textContent = label
    node.disabled = disabled
    node.addEventListener('click', handler)
    return node
  }
  actions.append(
    button('↑', () => void moveFund(index, -1), index === 0),
    button('↓', () => void moveFund(index, 1), index === count - 1),
    button('删除', () => void removeFund(quote.code))
  )
  detail.append(metrics, actions)
  card.append(summary, detail)
  return card
}

async function moveFund(index: number, direction: number): Promise<void> {
  if (!appState) return
  const codes = appState.funds.map((fund) => fund.code)
  const first = codes[index]
  const second = codes[index + direction]
  if (!first || !second) return
  codes[index] = second
  codes[index + direction] = first
  const result = await window.fundApp.reorderFunds(codes)
  if (result.ok && result.data) renderFunds(result.data)
  else toast(result.error ?? '排序失败', true)
}
async function removeFund(code: string): Promise<void> {
  const result = await window.fundApp.removeFund(code)
  if (result.ok && result.data) renderFunds(result.data)
  else toast(result.error ?? '删除失败', true)
}
async function refreshFunds(): Promise<void> {
  try {
    const result = await window.fundApp.refresh()
    if (result.ok && result.data) renderFunds(result.data)
    else toast(result.error ?? '基金刷新失败', true)
  } catch (error) { toast('基金刷新失败：' + errorText(error), true) }
}

function renderRisk(): void {
  const status = risk?.state ?? 'loading'
  const symbolSelect = el<HTMLSelectElement>('risk-symbol')
  for (const item of risk?.symbols ?? []) {
    if (!Array.from(symbolSelect.options).some((option) => option.value === item.symbol)) {
      const option = document.createElement('option')
      option.value = item.symbol
      option.textContent = item.name + ' · ' + item.symbol
      symbolSelect.append(option)
    }
  }
  renderFundBenchmarks()
  symbolSelect.value = selectedSymbol
  const stateLabel = ({
    ready: '状态已更新', loading: '数据计算中', insufficient_data: '历史数据不足，暂不发布风险读数',
    stale: '数据已过期：仅展示最后有效读数，预测与操作关闭', error: '数据请求失败'
  } as Record<string, string>)[status] ?? status
  value('risk-state', stateLabel + (fundBenchmark.has(selectedSymbol) ? ' · 基金仅参照已核实关联基准，非基金自身预测' : ''))
  el<HTMLElement>('risk-state').className = 'data-state data-state-' + status
  const display = scoreDisplay(risk)
  value('risk-score', display.score)
  value('risk-level', display.level)
  value('risk-time', (display.stale ? '最后有效数据 ' : '数据时间 ') + time(risk?.as_of))
  value('risk-overheat', display.overheat)
  value('risk-gate', display.stale ? '过期数据，等待重新验证' : gateLabel(risk, validation?.publishable ?? null))
  value('risk-signal', display.stale ? '最后读数仅供回顾；预测和操作已关闭' : risk?.signal?.reason || '信号正在观察；未验证前不构成交易指令')
  const signalText = (signal: RiskSnapshot['opportunity'] | RiskSnapshot['heat_signal']) =>
    risk?.state === 'ready' && signal?.label ? signal.label : '等待'
  value('risk-opportunity', signalText(risk?.opportunity))
  value('risk-heat-signal', signalText(risk?.heat_signal))
  lines('risk-opportunity-reasons', risk?.state === 'ready' && risk?.opportunity?.reasons?.length
    ? risk.opportunity.reasons : ['数据或观察验证尚未就绪，不能判断是否可分批介入。'])
  lines('risk-heat-reasons', risk?.state === 'ready' && risk?.heat_signal?.reasons?.length
    ? risk.heat_signal.reasons : ['数据或观察验证尚未就绪；不追涨，减配条件待核实。'])
  const components = coreComponentRows(risk)
  value('risk-components-note', selectedSymbol === 'market'
    ? '三种风格分位的中位数，不可相加为概览总分' : '三项历史分位各占总分三分之一')
  el<HTMLElement>('risk-components').replaceChildren(...components.map((reading) => {
    const panel = document.createElement('article')
    panel.className = 'component-card'
    const title = document.createElement('strong')
    title.textContent = reading.name
    const data = document.createElement('p')
    data.textContent = componentLine(reading)
    panel.append(title, data)
    return panel
  }))
  if (!components.length) lines('risk-explanation', ['分项待数据就绪'])
  else lines('risk-explanation', [
    ...(risk?.explanation ?? []), ...(risk?.missing ?? []).map((item) => '缺失：' + item),
    risk?.signal?.eligible ? '超跌/过热信号已满足观测条件，仍需人工复核。' : '超跌/过热仅供观察，验证门槛未通过时不提供可执行指令。'
  ])
  const rows = forecastRows(risk)
  el<HTMLElement>('forecast-body').replaceChildren(...rows.map((item) => row([item.horizon + ' 日', ...item.cells])))
  value('forecast-state', rows.length
    ? '参照标的 ' + (risk?.forecast.symbol ?? querySymbol()) + ' · 正式预测截至 ' + time(risk?.forecast.as_of)
    : (risk?.forecast?.reasons ?? []).join('；') || '暂无正式发布预测；训练、校准或验证尚未满足发布条件。')
  value('risk-quality', riskQualitySummary(risk))
  lines('risk-quality-details', riskQualityDetails(risk))
  value('risk-validation', validation
    ? '验证：' + validation.status + ' · ' + (validation.publishable ? '可发布' : '暂不可发布') + (validation.reasons?.length ? ' · ' + validation.reasons.join('；') : '')
    : '验证资料待加载')
  value('risk-intraday-note', risk?.intraday?.notice || '盘中报价仅为观察，不参与正式预测')
  const intraday = el<HTMLElement>('risk-intraday')
  intraday.replaceChildren(...(risk?.intraday?.rows ?? []).map((entry) => {
    const card = document.createElement('article')
    card.className = 'source-card'
    const title = document.createElement('strong')
    title.textContent = entry.name + ' · ' + entry.symbol + ' ' + numberText(entry.last, 2)
    const detail = document.createElement('span')
    detail.textContent = '涨跌 ' + ratioText(entry.change) + ' · ' + time(entry.timestamp) + ' · ' + entry.state
    const source = document.createElement('small')
    source.textContent = '来源 ' + (entry.source_id || '--')
    card.append(title, detail, source)
    return card
  }))
  const auxiliary = el<HTMLElement>('risk-auxiliary')
  auxiliary.replaceChildren(...auxiliaryRows(risk?.auxiliary).map((entry) => {
    const card = document.createElement('article')
    card.className = 'source-card'
    const title = document.createElement('strong')
    title.textContent = entry.name + ' · ' + entry.state
    const detail = document.createElement('span')
    detail.textContent = entry.summary
    const message = document.createElement('small')
    message.textContent = entry.source + (entry.reason ? ' · ' + entry.reason : '')
    card.append(title, detail, message)
    return card
  }))
  if (appState) value('last-refresh', '基金 ' + time(appState.lastRefreshAt) + ' · 风险 ' + time(risk?.as_of))
  value('history-count', history ? history.records.length + ' 条' : '正在加载')
  renderHistory()
}

function renderHistory(): void {
  const target = el<HTMLElement>('history-chart')
  target.replaceChildren()
  const points = historySeries(history)
  el<HTMLButtonElement>('history-backtest').classList.toggle('selected', historyKind === 'backtest')
  el<HTMLButtonElement>('history-published').classList.toggle('selected', historyKind === 'published')
  if (points.length === 0) {
    target.textContent = historyKind === 'published' ? '暂无正式发布记录' : '暂无可用回测历史'
    return
  }
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
  svg.setAttribute('viewBox', '0 0 640 180')
  svg.setAttribute('role', 'img')
  svg.setAttribute('aria-label', (historyKind === 'published' ? '正式记录' : '回测历史') + '曲线，共 ' + points.length + ' 条')
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path')
  path.setAttribute('class', historyKind === 'published' ? 'chart-line chart-line-display' : 'chart-line chart-line-estimate')
  path.setAttribute('d', historyPath(points))
  svg.append(path)
  historyCoordinates(points).forEach((point, index) => {
    const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle')
    dot.setAttribute('cx', point.x.toFixed(1))
    dot.setAttribute('cy', point.y.toFixed(1))
    dot.setAttribute('r', points.length === 1 ? '4' : '2.5')
    dot.setAttribute('fill', historyKind === 'published' ? '#7689ff' : '#46cfb5')
    const title = document.createElementNS('http://www.w3.org/2000/svg', 'title')
    title.textContent = displayDate(points[index]!.date) + ' · ' + numberText(points[index]!.score, 1)
    dot.append(title)
    svg.append(dot)
  })
  const caption = document.createElement('div')
  caption.className = 'chart-labels'
  caption.append(Object.assign(document.createElement('span'), { textContent: displayDate(points[0]?.date) }),
    Object.assign(document.createElement('span'), { textContent: displayDate(points[points.length - 1]?.date) }))
  target.append(svg, caption)
}

async function loadRisk(): Promise<void> {
  if (miniMode || appState?.engine.state !== 'ready') return
  const request = ++riskRequest
  const symbol = querySymbol()
  const kind = historyKind
  const results = await Promise.allSettled([
    window.fundApp.risk.snapshot(symbol),
    window.fundApp.risk.validation(symbol),
    window.fundApp.risk.history(symbol, kind, 252),
    window.fundApp.risk.advice()
  ])
  if (request !== riskRequest) return
  const [snapshotResult, validationResult, historyResult, adviceResult] = results
  risk = snapshotResult.status === 'fulfilled' ? snapshotResult.value : null
  validation = validationResult.status === 'fulfilled' ? validationResult.value : null
  if (historyKind === kind) history = historyResult.status === 'fulfilled' ? historyResult.value : null
  advice = adviceResult.status === 'fulfilled' ? adviceResult.value : null
  renderRisk()
  renderAdvice()
  if (snapshotResult.status === 'rejected') value('risk-state', '风险数据暂不可用：' + errorText(snapshotResult.reason))
  if (adviceResult.status === 'rejected') value('advice-summary', '仓位建议暂不可用：' + errorText(adviceResult.reason))
}

async function loadHistory(): Promise<void> {
  if (appState?.engine.state !== 'ready') return
  const symbol = querySymbol()
  const kind = historyKind
  value('history-count', '正在加载')
  try {
    const result = await window.fundApp.risk.history(symbol, kind, 252)
    if (symbol !== querySymbol() || kind !== historyKind) return
    history = result
    value('history-count', result.records.length + ' 条')
    renderHistory()
  } catch (error) { value('history-count', '历史请求失败：' + errorText(error)) }
}

function renderJobs(jobs: EngineJob[]): void {
  lines('risk-jobs', jobs.length ? jobs.slice(0, 8).map((job) =>
    job.kind + ' · ' + job.status + ' · ' + numberText(job.progress * (job.progress <= 1 ? 100 : 1), 0) + '% · ' +
    (job.message ?? '') + (job.errors?.length ? ' · ' + job.errors.map((entry) =>
      typeof entry === 'string' ? entry : (entry.symbol ?? entry.fund ?? '任务') + '：' + entry.error).join('；') : '')) : ['暂无后台任务'])
}
async function pollJobs(): Promise<void> {
  if (miniMode || appState?.engine.state !== 'ready') return
  try {
    const result = await window.fundApp.risk.jobs()
    renderJobs(result.jobs ?? [])
    const completed = (result.jobs ?? []).filter((job) => job.status === 'completed' || job.status === 'failed')
      .map((job) => job.id + ':' + job.status).join(',')
    if (lastCompletedJobs && completed !== lastCompletedJobs) void loadRisk()
    lastCompletedJobs = completed
    if (jobsTimer) window.clearTimeout(jobsTimer)
    const busy = result.jobs?.some((job) => job.status === 'queued' || job.status === 'running')
    jobsTimer = window.setTimeout(() => void pollJobs(), busy ? 2500 : 10000)
  } catch (error) {
    lines('risk-jobs', ['任务状态暂不可用：' + errorText(error)])
    if (jobsTimer) window.clearTimeout(jobsTimer)
    jobsTimer = window.setTimeout(() => void pollJobs(), 10000)
  }
}
async function startJob(kind: 'refresh' | 'train'): Promise<void> {
  const button = el<HTMLButtonElement>(kind === 'refresh' ? 'risk-refresh' : 'risk-train')
  button.disabled = true
  try {
    await (kind === 'refresh' ? window.fundApp.risk.refresh([querySymbol()]) : window.fundApp.risk.train())
    toast('已提交后台任务；基金估值仍可使用')
    await pollJobs()
    if (!jobsTimer) void loadRisk()
  } catch (error) { toast('提交失败：' + errorText(error), true) }
  finally { button.disabled = false }
}

function fieldInput(lot: PortfolioLot, key: keyof PortfolioLot, type: string): HTMLInputElement | HTMLSelectElement {
  if (key === 'asset_class') {
    const select = document.createElement('select')
    select.dataset.field = key
    for (const [code, label] of [['', '未填写'], ['equity', '权益'], ['bond', '债券'], ['money', '货币'], ['commodity', '商品'], ['other', '其他']]) {
      const option = document.createElement('option')
      option.value = code ?? ''
      option.textContent = label ?? ''
      select.append(option)
    }
    select.value = lot.asset_class ?? ''
    return select
  }
  const input = document.createElement('input')
  input.type = type
  input.dataset.field = key
  if (type === 'checkbox') input.checked = lot.metadata_verified === true
  else input.value = lot[key] == null ? '' : String(lot[key])
  if (type === 'number') {
    input.min = '0'
    input.step = 'any'
  }
  if (type === 'text' && key === 'code') { input.maxLength = 6; input.placeholder = '六位代码' }
  return input
}
function lotRow(lot: PortfolioLot): HTMLTableRowElement {
  const tr = document.createElement('tr')
  tr.dataset.id = lot.id
  for (const [key, type] of lotFields) {
    const td = document.createElement('td')
    td.append(fieldInput(lot, key, type))
    tr.append(td)
  }
  const remove = document.createElement('button')
  remove.className = 'card-action danger-action'
  remove.type = 'button'
  remove.textContent = '删除'
  remove.addEventListener('click', () => { tr.remove(); value('portfolio-state', '尚未保存的改动') })
  const td = document.createElement('td')
  td.append(remove)
  tr.append(td)
  return tr
}
function renderPortfolio(): void {
  if (!portfolio) return
  value('portfolio-state', '本地持仓 · ' + portfolio.lots.length + ' 批 · 保存后重新计算建议')
  el<HTMLInputElement>('portfolio-cash').value = String(portfolio.cash)
  el<HTMLSelectElement>('portfolio-profile').value = portfolio.profile
  const body = el<HTMLTableSectionElement>('portfolio-lots')
  body.replaceChildren(...portfolio.lots.map(lotRow))
  const constraints = el<HTMLElement>('portfolio-constraints')
  constraints.replaceChildren(...Object.entries(constraintLabels).map(([key, label]) => {
    const wrap = document.createElement('label')
    wrap.textContent = label + '（0–100%）'
    const input = document.createElement('input')
    input.type = 'number'
    input.min = '0'
    input.max = '100'
    input.step = '0.1'
    input.dataset.constraint = key
    const stored = portfolio?.constraints[key]
    input.value = typeof stored === 'number' ? String(stored * 100) : ''
    wrap.append(input)
    return wrap
  }))
}
function optionalNumber(input: HTMLInputElement): number | null {
  if (input.value.trim() === '') return null
  const number = Number(input.value)
  if (!Number.isFinite(number) || number < 0) throw new Error('批次数字必须为非负有限数')
  return number
}
function collectPortfolio(overrideLots?: PortfolioLot[]): Portfolio {
  if (!portfolio) throw new Error('持仓尚未加载')
  const cash = optionalNumber(el<HTMLInputElement>('portfolio-cash'))
  if (cash === null) throw new Error('请填写现金')
  const profile = el<HTMLSelectElement>('portfolio-profile').value as Portfolio['profile']
  const lots = overrideLots ?? Array.from(el<HTMLElement>('portfolio-lots').querySelectorAll<HTMLTableRowElement>('tr')).map((tr) => {
    const input = (key: string): HTMLInputElement | HTMLSelectElement => tr.querySelector<HTMLInputElement | HTMLSelectElement>('[data-field="' + key + '"]')!
    const code = input('code').value.trim()
    if (!/^\d{6}$/.test(code)) throw new Error('每个持仓批次须填写六位基金代码')
    return {
      id: tr.dataset.id ?? crypto.randomUUID(), code,
      shares: optionalNumber(input('shares') as HTMLInputElement), market_value: optionalNumber(input('market_value') as HTMLInputElement),
      valuation_date: input('valuation_date').value || null, confirmed_date: input('confirmed_date').value || null,
      in_transit: optionalNumber(input('in_transit') as HTMLInputElement) ?? 0,
      fee_buy: optionalNumber(input('fee_buy') as HTMLInputElement), fee_sell: optionalNumber(input('fee_sell') as HTMLInputElement),
      baseline_weight: optionalNumber(input('baseline_weight') as HTMLInputElement), industry: input('industry').value.trim() || null,
      benchmark_symbol: input('benchmark_symbol').value.trim() || null,
      asset_class: input('asset_class').value.trim() || null,
      metadata_verified: (input('metadata_verified') as HTMLInputElement).checked,
      effective_date: input('effective_date').value || null
    }
  })
  const constraints: Record<string, number> = {}
  for (const input of el<HTMLElement>('portfolio-constraints').querySelectorAll<HTMLInputElement>('input')) {
    const val = optionalNumber(input)
    if (val === null) continue
    if (val > 100) throw new Error('仓位参数不得超过 100%')
    constraints[input.dataset.constraint!] = val / 100
  }
  return { cash, profile, lots, constraints, version: 1 }
}
async function savePortfolio(next?: Portfolio): Promise<boolean> {
  try {
    portfolio = await window.fundApp.portfolio.save(next ?? collectPortfolio())
    renderPortfolio()
    renderFundBenchmarks()
    toast('持仓已保存')
    await loadRisk()
    return true
  } catch (error) { toast('持仓保存失败：' + errorText(error), true); return false }
}
async function loadPortfolio(): Promise<void> {
  if (miniMode || appState?.engine.state !== 'ready') return
  try {
    portfolio = await window.fundApp.portfolio.get()
    renderPortfolio()
    renderFundBenchmarks()
    renderAdvice()
  } catch (error) { value('portfolio-state', '持仓暂不可用：' + errorText(error)) }
}
function renderFundBenchmarks(): void {
  const group = el<HTMLOptGroupElement>('fund-risk-options')
  group.replaceChildren()
  fundBenchmark.clear()
  const aliases: Record<string, string> = { '000300': 'sh000300', '000905': 'sh000905', '000852': 'sh000852', HSI: 'hkHSI', HSTECH: 'hkHSTECH' }
  const supported = new Set(['sh000300', 'sh000905', 'sh000852', 'hkHSI', 'hkHSTECH',
    ...(risk?.symbols ?? []).map((item) => item.symbol)])
  const today = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(new Date())
  const addBenchmark = (code: string, benchmark: string | null | undefined): void => {
    const symbol = aliases[benchmark ?? ''] ?? benchmark
    if (!symbol || !supported.has(symbol)) return
    const key = 'fund:' + code
    if (fundBenchmark.has(key)) return
    fundBenchmark.set(key, symbol)
    const option = document.createElement('option')
    option.value = key
    option.textContent = '基金 ' + code + ' → ' + symbol + '（基准）'
    group.append(option)
  }
  for (const lot of portfolio?.lots ?? []) {
    if (lot.metadata_verified && lot.effective_date && lot.effective_date <= today)
      addBenchmark(lot.code, lot.benchmark_symbol)
  }
  for (const item of advice?.fund_quality ?? []) addBenchmark(item.code, item.benchmark)
  if (selectedSymbol.startsWith('fund:') && !fundBenchmark.has(selectedSymbol)) selectedSymbol = 'market'
  el<HTMLSelectElement>('risk-symbol').value = selectedSymbol
}
function renderAdvice(): void {
  const summary = advice?.summary
  value('advice-summary', typeof summary === 'string' ? summary : summary
    ? '总资产 ' + numberText(summary.total_assets, 2) + ' 元 · 现金 ' + numberText(summary.cash, 2) +
      ' 元 · 在途 ' + numberText(summary.in_transit, 2) + ' 元 · 基金 ' + (summary.fund_count ?? 0) + ' 只'
    : '当前无可用仓位建议；仅供观察，不会自动下单。')
  const body = el<HTMLElement>('advice-body')
  body.replaceChildren(...adviceRows(advice, portfolio?.lots ?? []).map((item) =>
    row([item.name + ' · ' + item.code, item.current, item.target, item.action, item.reason])))
  lines('advice-reasons', advice?.reasons?.length ? advice.reasons : ['目标区间依赖可验证历史、现有仓位及成本资料。'])
  const quality = fundQualityRows(advice)
  value('fund-quality-count', quality.length ? quality.length + ' 只' : '暂无基金资料')
  el<HTMLElement>('fund-quality-body').replaceChildren(...quality.map((item) =>
    row([item.code, item.benchmark, item.navDate, item.estimate, item.subscription,
      item.redemption, item.confirmation, item.reason])))
}
async function previewCsv(): Promise<void> {
  preview = null
  el<HTMLButtonElement>('csv-apply').disabled = true
  try {
    const result = await window.fundApp.portfolio.previewCsv(el<HTMLTextAreaElement>('portfolio-csv').value)
    preview = result
    const issue = (entry: string | { field?: string; message: string }): string =>
      typeof entry === 'string' ? entry : (entry.field ? entry.field + '：' : '') + entry.message
    const count = result.portfolio?.lots?.length ?? 0
    lines('csv-result', [
      '预览 ' + count + ' 批（尚未保存）',
      ...(result.errors ?? []).map((message) => '错误：' + issue(message)),
      ...(result.warnings ?? []).map((message) => '提醒：' + issue(message))
    ])
    const visibleRows = result.portfolio?.lots ?? result.rows ?? []
    if (visibleRows.length) {
      const table = document.createElement('table')
      table.className = 'risk-table'
      const head = document.createElement('thead')
      const titles = document.createElement('tr')
      for (const label of ['基金代码', '份额', '市值', '估值日期', '核实基准']) {
        const th = document.createElement('th')
        th.textContent = label
        titles.append(th)
      }
      head.append(titles)
      const body = document.createElement('tbody')
      body.append(...visibleRows.slice(0, 20).map((lot) => row([
        lot.code, lot.shares, lot.market_value, lot.valuation_date, lot.benchmark_symbol ?? '--'
      ])))
      table.append(head, body)
      el<HTMLElement>('csv-result').append(table)
    }
    el<HTMLButtonElement>('csv-apply').disabled = result.valid === false || Boolean(result.errors?.length) || !result.portfolio
  } catch (error) { lines('csv-result', ['预览失败：' + errorText(error)]) }
}

el<HTMLButtonElement>('add-toggle-button').addEventListener('click', () => {
  const section = el<HTMLElement>('quick-add')
  section.hidden = !section.hidden
  el<HTMLButtonElement>('add-toggle-button').setAttribute('aria-expanded', String(!section.hidden))
  if (!section.hidden) el<HTMLInputElement>('fund-code').focus()
})
el<HTMLFormElement>('add-form').addEventListener('submit', (event) => {
  event.preventDefault()
  void (async () => {
    const input = el<HTMLInputElement>('fund-code')
    const result = await window.fundApp.addFund(input.value.trim())
    if (result.ok && result.data) { input.value = ''; renderFunds(result.data); toast('基金已添加') }
    else toast(result.error ?? '添加失败', true)
  })()
})
el<HTMLButtonElement>('fund-refresh-button').addEventListener('click', () => void refreshFunds())
el<HTMLButtonElement>('refresh-button').addEventListener('click', () => {
  void refreshFunds()
  void startJob('refresh')
})
el<HTMLSelectElement>('risk-symbol').addEventListener('change', (event) => {
  selectedSymbol = (event.target as HTMLSelectElement).value
  history = null
  risk = null
  renderRisk()
  void loadRisk()
})
el<HTMLButtonElement>('risk-refresh').addEventListener('click', () => void startJob('refresh'))
el<HTMLButtonElement>('risk-train').addEventListener('click', () => void startJob('train'))
for (const kind of ['backtest', 'published'] as const) {
  el<HTMLButtonElement>('history-' + kind).addEventListener('click', () => {
    historyKind = kind
    history = null
    renderHistory()
    void loadHistory()
  })
}
el<HTMLButtonElement>('portfolio-save').addEventListener('click', () => void savePortfolio())
el<HTMLButtonElement>('lot-add').addEventListener('click', () => {
  if (!portfolio) return
  el<HTMLElement>('portfolio-lots').append(lotRow({ id: crypto.randomUUID(), code: '', shares: null, market_value: null, valuation_date: null,
    confirmed_date: null, in_transit: 0, fee_buy: null, fee_sell: null, baseline_weight: null, industry: null,
    benchmark_symbol: null, asset_class: null, metadata_verified: false, effective_date: null }))
  value('portfolio-state', '尚未保存的批次')
})
el<HTMLSelectElement>('portfolio-profile').addEventListener('change', () => {
  const profile = el<HTMLSelectElement>('portfolio-profile').value
  const defaults = profile === 'conservative' ? [60, 6] : profile === 'aggressive' ? [95, 14] : [80, 10]
  for (const [index, key] of ['equity_cap', 'vol_target'].entries()) {
    const input = el<HTMLElement>('portfolio-constraints').querySelector<HTMLInputElement>('[data-constraint="' + key + '"]')
    if (input) input.value = String(defaults[index])
  }
  value('portfolio-state', '风险偏好及对应默认上限待保存')
})
el<HTMLInputElement>('portfolio-file').addEventListener('change', () => {
  const file = el<HTMLInputElement>('portfolio-file').files?.[0]
  if (!file) return
  if (file.size > 2_000_000) { toast('CSV 超过 2 MB', true); return }
  void file.text().then((text) => { el<HTMLTextAreaElement>('portfolio-csv').value = text.replace(/^\uFEFF/, ''); preview = null; el<HTMLButtonElement>('csv-apply').disabled = true })
})
el<HTMLButtonElement>('csv-preview').addEventListener('click', () => void previewCsv())
el<HTMLButtonElement>('csv-apply').addEventListener('click', () => {
  if (!preview || preview.errors.length || !preview.portfolio) return
  let merged: Portfolio
  try {
    merged = collectPortfolio(preview.portfolio.lots)
  } catch (error) { toast('请先修正当前现金与参数：' + errorText(error), true); return }
  void savePortfolio(merged).then((saved) => {
    if (saved) {
      preview = null
      el<HTMLButtonElement>('csv-apply').disabled = true
    }
  })
})
el<HTMLButtonElement>('settings-button').addEventListener('click', () => el<HTMLDialogElement>('settings-dialog').showModal())
el<HTMLButtonElement>('settings-close').addEventListener('click', () => el<HTMLDialogElement>('settings-dialog').close())
el<HTMLButtonElement>('settings-cancel').addEventListener('click', () => el<HTMLDialogElement>('settings-dialog').close())
el<HTMLInputElement>('window-opacity').addEventListener('input', (event) => value('window-opacity-value', (event.target as HTMLInputElement).value + '%'))
el<HTMLFormElement>('settings-form').addEventListener('submit', (event) => {
  event.preventDefault()
  void (async () => {
    const result = await window.fundApp.updateSettings({
      autoRefresh: el<HTMLInputElement>('auto-refresh').checked,
      refreshIntervalSeconds: Number(el<HTMLSelectElement>('refresh-interval').value),
      launchAtLogin: el<HTMLInputElement>('launch-at-login').checked,
      alwaysOnTop: el<HTMLInputElement>('always-on-top').checked,
      windowOpacity: Number(el<HTMLInputElement>('window-opacity').value) / 100
    })
    if (result.ok && result.data) { renderFunds(result.data); el<HTMLDialogElement>('settings-dialog').close(); toast('设置已保存') }
    else toast(result.error ?? '设置保存失败', true)
  })()
})

window.fundApp.onStateChanged((state) => {
  const wasReady = appState?.engine.state === 'ready'
  renderFunds(state)
  if (!wasReady && state.engine.state === 'ready') { void loadPortfolio(); void loadRisk(); void pollJobs() }
  if (state.engine.state === 'error') value('risk-state', state.engine.error ?? '引擎连接失败')
})
void window.fundApp.getState().then((state) => {
  renderFunds(state)
  if (state.engine.state === 'ready') { void loadPortfolio(); void loadRisk(); void pollJobs() }
}).catch((error) => toast('初始化失败：' + errorText(error), true))
