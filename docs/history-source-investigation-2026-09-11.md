# 历史缺项接口核验（2026-09-11）

本次核验针对桌面客户端历史估计缺项。以下区分实网返回结果与待接入工作，不将项目 README 声称支持等同于验收成功。本轮未更换用户正在运行的 EXE。

## IF 合约历史：已验证可用

参考 [AKShare 的交易所日线实现](https://github.com/akfamily/akshare/blob/main/akshare/futures/futures_daily_bar.py)。

中金所官方月度文件：`http://www.cffex.com.cn/sj/historysj/YYYYMM/zip/YYYYMM.zip`。ZIP 内为逐日 CSV，按表头取 `合约代码`、`今收盘`、`今结算`、`成交量`、`持仓量`。原文件为中文编码，应解码后在内部转换；不要直接将外部文件编码当作仓库文本编码。

实网结果：

- 202508、202608 各21个日文件，包含具体 IF 合约。
- 202609 包含2026-09-01至2026-09-10的8个已完成交易日，当前月份同样可下载。
- 2026-09-10 IF2609 官方收盘4542.2点。

新浪的具体合约日线可以作为下载备用，参考 [AKShare 期货接口说明](https://github.com/akfamily/akshare/blob/main/docs/data/futures/futures.md)。接口路径为 `stock2.finance.sina.com.cn/futures/api/jsonp.php/.../InnerFuturesNewService.getDailyKLine`，参数 `symbol=IF2509`，返回字段 `d/o/h/l/c/v/p/s`。

- IF2509 实测164条，2025-01-20至2025-09-19，已到期合约仍可读取。
- IF2609 实测158条，2026-01-19至2026-09-10；最后收盘4542.2点，与同日官方数据一致。
- 新浪样本的 `s` 为0，不能将其当有效结算价；基差应使用 `c` 收盘价，并匹配同日现货收盘。
- 新浪和中金所传播的是同一交易所行情，不是两套独立原始交易观测。

接入要求：按具体合约及当时挂牌范围选择近月/次月，保留合约代码、日期、单位和来源；沿用既有换月与基差公式，核验节假日交割日。不能把 IF0 连续合约当作明确月份合约计算到期年化基差。

## QVIX 历史：已验证可用

参考 [AKShare QVIX 实现](https://github.com/akfamily/akshare/blob/main/akshare/index/index_option_qvix.py)。上游 `http://1.optbbs.com/d/csv/d/k.csv`；`index_option_300etf_qvix()`选取300ETF对应的列，不能混用50ETF或股指期权波动率。

实网近一年有效收盘记录242条，2025-09-11至2026-09-10。2026-09-09收盘17.53，2026-09-10收盘17.88。此来源已用于客户端历史回算，后续需增强请求失败重试、原始日线缓存与逐日覆盖诊断。

## 历史市场宽度：开盘红候选已实测

参考 [levistock 开盘红接口源码](https://github.com/fleetinglife/levistock/blob/main/levistock/market/market_emotion_kph.py) 与 [kpl-dashboard-public 爬取实现](https://github.com/HJ2916/kpl-dashboard-public/blob/master/lib/kaipanla_crawler.py)。两者使用同一上游，只计为一个来源。

历史请求为 POST `https://apphis.kaipanhong.com/w1/api/index.php`，表单参数包含 `a=HisZhangFuDetail`、`c=HisHomeDingPan`、`Day=YYYY-MM-DD`、`PhoneOSNew=1`、`DeviceID`、`VerSion=6.0.6`、`Token=0`、`UserID=0`、`Red=1`、`apiv=w45`。本次未登录、未购买账号即可读取。

实测2026-09-10、2025-09-12、2025-01-02、2024-06-03均返回对应日期数据。2026-09-10样本：上涨 `SZJS=955`、下跌 `XDJS=4512`、平盘 `0=82`、涨停 `ZT=40`、跌停 `DT=13`，另含 `SJZT/SJDT/STZT/STDT` 和正负1至10的涨跌幅分桶。

部分更早日期虽然 `errcode=0`，但 `info` 为空，必须按实际内容校验，不能生成零值。近一年只做了代表日期抽查，未宣称逐日完整率100%。

接入前仍需解决：

- 正负整数分桶的精确边界未由官方文档确认，不能直接声称准确计算“跌幅≥5%/7%”。
- 不提供精确收益中位数，从分桶估计的中位数不能冒充逐股计算的中位数。
- 上涨+下跌+平盘可形成上游统计分母，但市场范围和停牌排除方式需与现有模型核对。
- 同时返回ST相关家数，应先确认是否含ST及总数关系，再映射涨跌停字段。

其他路线：财联社 `x-quote.cls.cn/v2/quote/a/stock/emotion` 是实时接口，未见历史日期参数；搜狐涨跌统计页仅公开最近少量记录，不能支撑一年。BaoStock 免费匿名接口能获取个股 `preclose/pctChg/tradestatus/isST` 等字段，样本 `sh.600000` 日线实测成功，但需逐股汇总并处理历史股票池及退市偏差，且其市场覆盖与含北交所的统计不同，本轮没有抓取全市场。

## 类似项目的边界

- [IndexBasisTracker](https://github.com/qihaiqianqiu/IndexBasisTracker)：可参考基差采集和展示；其历史接口查询本机已落盘数据，并不自动提供过去一年行情。
- [ifuture](https://github.com/gamsing/ifuture)：可参考多合约结构，但 README 写明使用 Tushare token，无 token 演示使用合成行情。合成行情不能用于本客户端历史补全。
- [levistock](https://github.com/fleetinglife/levistock)：包含东方财富、财联社、同花顺等接口，可继续核验宽度和涨跌停。多个封装调用同一上游时不计为新增独立数据源。

本次探测脚本和精简响应位于本地 `output/probe-history-interfaces.py` 与 `output/history-interface-probes.json`，没有写入用户数据库。
