# 本地风险接口与数据迁移

客户端 3.0.0、模型 4.0、数据库 6、本地服务 API 2。引擎只监听 `127.0.0.1`，Electron 随机分配端口并检查实例、版本和健康状态。Renderer 通过类型化 preload/IPC 调用，不能拼接任意网址或读取本地文件。

| IPC | 本地服务 | 说明 |
|---|---|---|
| risk.snapshot | GET `/api/v2/risk/snapshot?symbol=market` | 当前状态、日期、分项、预测发布白名单、缺项、观察进度 |
| risk.history | GET `/api/v2/risk/history?symbol=market&kind=backtest&limit=252` | `backtest` 真实行情回算，`published` 当日实际发布 |
| risk.validation | GET `/api/v2/risk/validation?symbol=market` | 时间外统计、PIT、策略及观察门槛 |
| risk.refresh | POST `/api/v2/risk/refresh` | 可传 `symbols`、仅含代码的 `fund_codes`，返回后台任务 |
| risk.train | POST `/api/v2/risk/train` | 对已下载标的逐个训练，不阻塞基金模块 |
| risk.jobs | GET `/api/v2/jobs` | 进度、失败类别及重试信息 |
| portfolio.get | GET `/api/v2/portfolio` | 本地现金、持仓批次、偏好和约束 |
| portfolio.update | PUT `/api/v2/portfolio` | 完整校验后保存 |
| portfolio.importCsv | POST `/api/v2/portfolio/import-csv` | 中文CSV预览，单独保存才写库 |
| advice.get | GET `/api/v2/advice` | 当前权重、联合目标、风险与约束冲突、费用门槛 |

比例和收益以小数表示，0.05 为 5%；状态分范围 0–100。正式预测中的失败事件不放键、不填零。`market` 状态为三个 A 股风格状态中位数，未来风险与信号明确以沪深300为参照，不能把不同指数概率加总。

下载、训练和回放在后台任务中执行，网络/CPU 工作由真实 `spawn` 子进程隔离。任务中断保留已经保存的指数和日期，下次补取失败日期；成功响应与规范日线原子落库。引擎退出会取消任务并回收子进程；父进程意外退出也由 worker 监视器处理。

持仓只在本机保存。行情请求只传公开标的代码，不传份额、资金、费率或CSV。原始指数响应、基金原始记录、事件、元数据及修订保存到本机审计库；前端接收摘要而非完整原文。

## 升级与恢复

数据库位于 `userData/data/panic-index.db`，默认 userData 为 `%APPDATA%/fund-and-panic-desktop`。V5首次升级前通过 SQLite 在线备份保留含 WAL 的已提交记录，备份在 `userData/backups/`。基金配置仍在 `config/funds.json`，不会写入安装目录。

新增风险日线、原响应、修订、模型、回算、正式发布、持仓和观察记录独立存储。原 V3 表保留，不能与 V4 曲线拼接。新版数据库不允许被旧模式降级写入；恢复旧客户端前，先退出程序，复制备份为独立数据库并保留当前库。卸载程序保留用户数据。

引擎故障和行情采集失败分别显示。普通 API 400/500 不会触发健康引擎重启；服务异常退出才自动恢复一次。失败的实时请求不把旧行情标为实时，已保存的有效收盘会标明日期及过期原因。

组合协方差使用基金自己的真实总回报。国内基金按其净值日期与境内交易日逐日核对，即使主动基准未核实也可保留真实组合风险；QDII等跨市场日历无法核实时停止精确日频风险。基准暴露和操作信号另行核实，不把基准收益当基金自身收益。
