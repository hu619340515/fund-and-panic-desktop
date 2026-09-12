import { describe, expect, it } from 'vitest'
import { validCsvText, validHistoryKind, validHistoryLimit, validPortfolio, validSymbol, validSymbols } from '../src/main/risk-validation'
import { adviceRows, auxiliaryRows, componentLine, coreComponentRows, displayDate, forecastRows, fundQualityRows, gateLabel, historyCoordinates, historyPath, historySeries, riskQualityDetails, riskQualitySummary, scoreDisplay } from '../src/renderer/src/risk-view'
import type { AllocationAdvice, Portfolio, RiskHistory, RiskSnapshot } from '../src/shared/types'

const portfolio: Portfolio = {
  version: 1, cash: 1000, profile: 'balanced', constraints: { equity_cap: 0.8 },
  lots: [{ id: 'a', code: '013273', shares: 10, market_value: null, valuation_date: null,
    confirmed_date: null, fee_buy: null, fee_sell: null, in_transit: 0, baseline_weight: null, industry: null }]
}
const snapshot: RiskSnapshot = {
  model_version: '4.0', state: 'ready', as_of: '2026-09-11', score: 55, components: {}, overheat: null,
  explanation: [], missing: [], symbols: [], signal: {action:'observe',reason:'观察中',eligible:false},
  forecast: {state:'loading',as_of:null,published:{'5': {probabilities:{'3':0.3,'5':0.2,'10':0.1},quantiles:{q10:-0.06,q50:0.01,q90:0.04}}},reasons:['尚未发布']},
  observation: {required_days:20,observed_days:5,ready:false}, data_quality: {}, jobs: []
}
describe('v4 IPC 输入边界', () => {
  it('仅接受受控标的、历史类型及条数', () => {
    expect(validSymbol(undefined)).toBe('market')
    expect(validSymbol('hkHSTECH')).toBe('hkHSTECH')
    expect(validSymbol('cn399808')).toBe('cn399808')
    expect(validSymbol('sz980092')).toBe('sz980092')
    expect(validSymbols(['sh000300', 'sh000300'])).toEqual(['sh000300'])
    expect(() => validSymbol('/../../healthz')).toThrow()
    expect(() => validSymbols([''])).toThrow()
    expect(validHistoryKind(undefined)).toBe('published')
    expect(() => validHistoryKind('all')).toThrow()
    expect(() => validHistoryLimit(100000)).toThrow()
  })
  it('拒绝非法持仓、非有限数字及超大 CSV', () => {
    expect(validPortfolio(portfolio)).toEqual(portfolio)
    expect(() => validPortfolio({...portfolio,cash:Infinity})).toThrow()
    expect(() => validPortfolio({...portfolio,lots:[{...portfolio.lots[0],code:'bad'}]})).toThrow()
    expect(() => validPortfolio({...portfolio,constraints:{equity_cap:1.5}})).toThrow()
    expect(() => validPortfolio({...portfolio,lots:[{...portfolio.lots[0],metadata_verified:true}]})).toThrow('核实元数据')
    expect(validPortfolio({...portfolio,lots:[{...portfolio.lots[0],metadata_verified:true,
      benchmark_symbol:'000300',asset_class:'equity',effective_date:'2026-09-10'}]}).lots[0]?.asset_class).toBe('equity')
    expect(validPortfolio({...portfolio,lots:[{...portfolio.lots[0],metadata_verified:true,
      benchmark_symbol:'000300',asset_class:'equity',effective_date:'2026-09-10'}]}).lots[0]?.benchmark_symbol).toBe('sh000300')
    expect(() => validCsvText('x'.repeat(2_000_001))).toThrow()
  })
})
describe('风险展示隔离', () => {
  it('未发布不泄露未来矩阵；只取正式发布预测', () => {
    expect(forecastRows(snapshot)).toEqual([])
    expect(forecastRows({...snapshot,forecast:{...snapshot.forecast,state:'published'}})[0]).toEqual({
      horizon:5,cells:['30.0%','20.0%','10.0%','-6.0%','1.0%','4.0%']
    })
    expect(forecastRows({...snapshot,forecast:{...snapshot.forecast,state:'published',
      published:{probabilities:{'5d_3pct':0.3,'5d_5pct':0.2,'5d_10pct':0.1},
        quantiles:{'5d_q10':-0.06,'5d_q50':0.01,'5d_q90':0.04}} as unknown as RiskSnapshot['forecast']['published']}})[0]?.cells)
      .toEqual(['30.0%','20.0%','10.0%','-6.0%','1.0%','4.0%'])
    expect(gateLabel(snapshot,false)).toContain('5/20')
  })
  it('三项分位按0–100显示，过热嵌套不混入；过期只回顾最后读数', () => {
    const updated: RiskSnapshot = {...snapshot,symbol:'sh000300',components:{
      shock:{raw:1.23,percentile:61}, drawdown:{raw:0.082,percentile:75},
      downside:{raw:0.15,percentile:42}, overheat:{upstretch:{raw:0.3,percentile:80}}
    },overheat:73,forecast:{...snapshot.forecast,state:'published'}}
    expect(coreComponentRows(updated)).toEqual([
      {key:'shock',name:'波动冲击',raw:'1.23',percentile:'61.0%',contribution:'20.3 分'},
      {key:'drawdown',name:'60日回撤',raw:'8.2%',percentile:'75.0%',contribution:'25.0 分'},
      {key:'downside',name:'20日年化下行波动',raw:'15.0%',percentile:'42.0%',contribution:'14.0 分'}
    ])
    expect(coreComponentRows({...updated,symbol:'market'})[0]?.contribution).toBeNull()
    const stale: RiskSnapshot = {...updated,state:'stale',as_of:'2026-09-10'}
    expect(scoreDisplay(stale)).toMatchObject({score:'55',level:'中性观察 · 已过期',overheat:'73',stale:true})
    expect(forecastRows(stale)).toEqual([])
    const overview = coreComponentRows({...updated,symbol:'market',components:{shock:{raw:null,percentile:61},drawdown:{raw:null,percentile:75},downside:{raw:null,percentile:42}}})
    expect(componentLine(overview[0]!)).toBe('历史分位 61.0%')
    expect(overview.every((row) => row.raw === null && row.contribution === null)).toBe(true)
  })
  it('纯交易日期不伪造发布时间，真实时间戳显示时分秒', () => {
    expect(displayDate('2026-09-11')).toBe('2026-09-11')
    expect(displayDate('2026-09-11T15:30:00')).toMatch(/15:30:00/)
    expect(displayDate(null)).toBe('--')
  })
  it('数据质量按真实摘要呈现，缺口长列表只在诊断中展开', () => {
    const quality: RiskSnapshot = {...snapshot,state:'stale',as_of:'2026-09-10',data_quality:{
      source_id:'eastmoney',source_ids:['eastmoney','csindex'],count:900,amount_coverage:0.88,
      fetched_at:'2026-09-11T18:00:00+08:00',quality:{missing_dates:['2026-09-01'],missing_dates_count:21,rejected:[],notice:'公开日线发布时间未核实'}
    }}
    expect(riskQualitySummary(quality)).toContain('已过期 · 来源 eastmoney、csindex · 记录 900 日 · 截至 2026-09-10（起点未提供） · 缺口 21 日 · 成交额覆盖 88.0%')
    expect(riskQualitySummary(quality)).not.toContain('2026-09-01')
    expect(riskQualityDetails(quality)).toContain('公开日线发布时间未核实')
  })
  it('回测和正式记录不混绘', () => {
    const history: RiskHistory = { symbol:'market',kind:'published',missing:[],records:[
      {as_of:'2026-09-10',score:60,model_version:'4.0',record_kind:'backtest',state:'ready'},
      {as_of:'2026-09-11',score:40,model_version:'4.0',record_kind:'published',state:'ready'}
    ]}
    expect(historySeries(history)).toEqual([{date:'2026-09-11',score:40,breakBefore:false}])
  })
  it('历史日期间距保留且缺口断线', () => {
    const history: RiskHistory = {symbol:'sh000300',kind:'backtest',missing:[],records:[
      {as_of:'2026-09-01',score:20,model_version:'4.0',record_kind:'backtest',state:'ready'},
      {as_of:'2026-09-02',score:30,model_version:'4.0',record_kind:'backtest',state:'ready'},
      {as_of:'2026-09-11',score:45,model_version:'4.0',record_kind:'backtest',state:'ready',break_before:true}
    ]}
    const points = historySeries(history)
    expect(historyCoordinates(points).map((point) => Math.round(point.x))).toEqual([20,80,620])
    expect(historyPath(points)).toBe('M20.0 132.0 L80.0 118.0 M620.0 97.0')
  })
  it('佐证只展示四类，标明QVIX口径及未经核实的来源时间', () => {
    const rows = auxiliaryRows({trade_date:'2026-09-11',fetched_at:'2026-09-12T10:00:00',attempts:[{}],
      qvix:{state:'ready',data:{symbol:'300ETF QVIX',value:19.5,previous_close:18.4,previous_5m:19.2},source_id:'qvix_300_etf',upstream:'公开QVIX',timestamp:null,error:null},
      limits:{state:'unavailable',data:null,source_id:null,timestamp:null,error:'公开来源无效'}})
    expect(rows.map((item) => item.name)).toEqual(['QVIX','IF期货','市场宽度','涨跌停'])
    expect(rows[0]?.summary).toContain('300ETF QVIX · 当前 19.50')
    expect(rows[0]?.source).toContain('交易日 2026-09-11 · 来源发布时间未核实')
    expect(rows[3]?.reason).toBe('公开来源无效')
  })
  it('建议仅用审阅标签且明确成本缺项', () => {
    const advice: AllocationAdvice = {state:'ready',as_of:null,profile:'balanced',summary:'',reasons:[],constraints:{},
      items:[{code:'013273',name:'测试基金',current_weight:0.2,target_min:0.15,target_max:0.25,action:'review',reason:'待观察'}]}
    expect(adviceRows(advice,portfolio.lots)[0]).toMatchObject({current:'20.0%',target:'15.0% – 25.0%',action:'人工复核'})
    expect(adviceRows(advice,portfolio.lots)[0]?.reason).toContain('费率缺项')
    const withheld: AllocationAdvice = {...advice, items:[{code:'013273',target_weight:null,target_min:null,target_max:null,
      current_weight:0.2,action:'unavailable',reason:'行业分类冲突，暂不能计算目标'}]}
    expect(adviceRows(withheld,portfolio.lots)[0]).toMatchObject({target:'--',action:'暂不可用'})
    expect(adviceRows(withheld,portfolio.lots)[0]?.reason).toContain('行业分类冲突')
    expect(adviceRows({...advice,items:[{code:'013273',current_weight:.2,target_min:.1,target_max:.3,
      action:'review_buy',reason:'需人工确认成本与份额'}]},portfolio.lots)[0]?.action).toBe('待复核增配')
    expect(adviceRows({...advice,items:[{code:'013273',action:'review_sell'}]},portfolio.lots)[0]?.action).toBe('待复核减配')
  })
  it('基金暴露缺项和未稳定状态有明确标记', () => {
    const advice: AllocationAdvice = {state:'ready',as_of:null,profile:'balanced',items:[],constraints:{},reasons:[],
      fund_quality:[
        {code:'013273',benchmark:'sh000300',nav_date:'2026-09-11',exposure:{available:true,stable:false,beta:0.92,r_squared:0.73,observations:120,reason:'样本仍需验证'}},
        {code:'000001',benchmark:null,nav_date:null,exposure:null}
      ]}
    expect(fundQualityRows(advice)[0]).toMatchObject({benchmark:'sh000300',navDate:'2026-09-11',estimate:'尚未稳定 · β 0.92 · R² 73.0% · 120 期'})
    expect(fundQualityRows(advice)[0]?.reason).toContain('样本仍需验证')
    expect(fundQualityRows(advice)[1]).toMatchObject({benchmark:'--',navDate:'--',estimate:'暂不可用'})
  })
  it('申赎状态区分已核实提交日、关闭和确认周期缺项', () => {
    const advice: AllocationAdvice = {state:'ready',as_of:null,profile:'balanced',items:[],constraints:{},reasons:[],fund_quality:[
      {code:'013273',benchmark:'sh000300',nav_date:'2026-09-11',exposure:null,execution:{
        verified:true,subscription_verified:true,redemption_verified:true,
        subscription_open:true,redemption_open:false,next_subscription_date:'2026-09-14',next_redemption_date:null,
        confirmation_days:null,reason:'仅核实提交状态',missing:['确认天数','未来成交净值'],
        source:{provider:'eastmoney',dataset:'基金申购状态'},fetched_at:'2026-09-12T10:00:00+08:00'}}
    ]}
    expect(fundQualityRows(advice)[0]).toMatchObject({subscription:'2026-09-14',redemption:'当前关闭',confirmation:'确认周期未核实'})
    expect(fundQualityRows(advice)[0]?.reason).toContain('缺项：确认天数')
    expect(fundQualityRows(advice)[0]?.reason).toContain('来源 eastmoney · 基金申购状态 · 抓取 2026-09-12')
    expect(fundQualityRows({...advice,fund_quality:[{...advice.fund_quality![0]!,execution:null}]} )[0]?.subscription).toBe('开放状态未核实')
  })
})
