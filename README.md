# 市场风险与基金仓位决策系统

Windows 10/11 x64 本地客户端。基金估值、市场状态、未来风险验证、真实持仓及风险预算在一个看板中管理。Electron 负责窗口、托盘与设置，内置 Python/FastAPI 引擎负责数据、计算和 SQLite；用户无需安装 Python。

**客户端 3.0.0 / 模型 4.0 / 数据库 6，为观察版。** 软件完整交付与模型验证通过是两件独立的事。未通过统计、历史可得性或实际观察门槛时，正式概率和操作建议保持关闭，并显示具体原因；不能以界面有数字代替验收。

## 下载

- [安装版 3.0.0](https://github.com/hu619340515/fund-and-panic-desktop/releases/download/v3.0.0/FundAndPanic-Setup-3.0.0-x64.exe)
- [便携版 3.0.0](https://github.com/hu619340515/fund-and-panic-desktop/releases/download/v3.0.0/FundAndPanic-Portable-3.0.0-x64.exe)
- [校验文件与完整交付](https://github.com/hu619340515/fund-and-panic-desktop/releases/tag/v3.0.0)

便携版双击即可运行，安装版可选择目录。两种版本的数据均保存在当前 Windows 用户目录；升级保留基金配置，数据库首次迁移前自动备份。关闭窗口后驻留托盘，托盘菜单“退出”会结束引擎及采集进程。未购买代码签名证书，Windows 可能显示未知发布者，请核对 Release SHA256。

## 看板回答的问题

| 模块 | 显示什么 |
|---|---|
| 基金估值 | 自选基金估值、官方净值、时间、来源与新鲜度，可独立刷新 |
| 当前恐慌 | 5日下跌冲击、60日回撤、20日下行波动的历史分位等权均值，0–100，不是下跌概率 |
| 未来风险 | 5/10/20/30/50交易日的跌幅事件概率与期末收益q10/q50/q90，仅显示通过门槛的项目 |
| 超跌/过热 | 分别观察压力、价格稳定、上涨偏离、趋势与成交活跃程度，满足验证门槛才开放操作建议 |
| 基金仓位 | 当前权重、基准、真实净值、252日收缩协方差风险及约束；冲突时说明原因 |

盘中行情用于预警，完整收盘数据用于模型决策。A股三种风格独立计算，市场概览取中位数；港股与行业指数使用自己的真实历史分布。QVIX、IF、市场宽度和涨跌停是独立佐证，缺失不改变核心权重。接口无可核验来源时间时明确显示时间未核实。

历史区域分开显示“真实行情回算”与“实际正式发布”。回算不使用旧V3分数，不插值补日期/数值，不把今天训练的预测画成过去已发布记录。首批数据在后台下载，已有日期保存后可续传；需要时单独运行训练和验证。

## 持仓与约束

支持手填多批次或中文CSV预览后保存。记录基金代码、份额或市值、估值日期、确认日期、费率、在途金额、基准权重和已核实基金元数据。费率及权重用小数，例如0.001为0.1%。手工基准必须填写生效日期；不同基金不能统一套用A股总分。

| 风格 | 权益上限 | 年化波动目标 |
|---|---:|---:|
| 保守 | 60% | 6% |
| 均衡（默认） | 80% | 10% |
| 进取 | 95% | 14% |

这些是可编辑的策略约束，不是预测。默认单只上限20%、行业上限35%、普通调仓每日总变动最多10个百分点、差异小于5个百分点不触发交易建议。买入不能预支尚未到账的赎回款；未核实费用、批次或交易日历时不输出精确金额。系统不自动下单。

基金收益根据真实单位净值和已核实分红/拆分计算；累计净值百分比不当成复权收益。跨缺失净值的多日收益不当作单日样本。短历史或基准不稳定时仅显示明确标注的参考信息。

## 本地目录

默认 `%APPDATA%/fund-and-panic-desktop`：

```text
config/funds.json       基金列表及窗口设置
config/panic.yaml       本地引擎服务配置
data/panic-index.db     历史、原响应、模型与持仓
backups/               数据库迁移前备份
logs/                  中文诊断日志
reports/               模型验证与导出
cache/                 临时缓存
```

无遥测或云端持仓同步，公网行情请求仅使用公开标的代码。`--user-data-dir=绝对目录` 可启动隔离诊断目录。安装目录不保存用户数据库，卸载保留用户数据。

## 开发、验证与打包

开发需要 Node.js、PowerShell 7、Python 3.12 x64：

```powershell
npm.cmd run install:desktop
python -m venv engine/.venv
engine/.venv/Scripts/python.exe -m pip install -r engine/requirements-windows-lock.txt
npm.cmd run dev
```

验收及构建：

```powershell
npm.cmd run typecheck
npm.cmd test
engine/.venv/Scripts/python.exe -m unittest discover -s engine/tests -v
npm.cmd run build
npm.cmd run build:engine
node packaging/verify-engine.cjs
npm.cmd run dist:win
node packaging/smoke-desktop.cjs dev
pwsh -File packaging/smoke-install.ps1
node packaging/smoke-live.cjs desktop/dist/FundAndPanic-Portable-3.0.0-x64.exe
```

产物在 `desktop/dist/`。打包前校验内置EXE来源哈希、版本、真实spawn成功/异常/超时及机器学习训练链路。安装与便携实网测试使用隔离中文用户目录，不修改用户持仓。

本地可复现诊断：

```powershell
engine/.venv/Scripts/python.exe engine/scripts/risk_v4_cli.py refresh --database output/research/data/panic-index.db
engine/.venv/Scripts/python.exe engine/scripts/risk_v4_cli.py train --database output/research/data/panic-index.db
engine/.venv/Scripts/python.exe engine/scripts/risk_v4_report.py --database output/research/data/panic-index.db --output output/research/delivery
```

## 故障排查和模型门槛

- 引擎未连接：查看设置中的日志目录，确认内置资源完整；意外退出自动恢复一次。
- 行情采集失败：查看失败标的、上游和日期后重试；健康服务不会因行情请求失败而重启，基金估值和已有历史仍可读取。
- 暂无分数：至少需要756个有效历史特征和近期连续的真实收盘价；成立较晚的指数保持缺项。收盘价可用而开高低缺失时可算日终状态，不能虚构开盘价进行交易回测。
- 未来风险为空：查看验证报告。时间外样本、正负事件、Brier、校准误差、时间块区间和历史时点可得性全部纳入门槛；不满足就不发布。
- 仓位目标为空：核对基金基准、连续净值、费率、批次及约束冲突。实际20交易日观察尚未完成时只观察，不启用操作性建议。

详见[模型说明](docs/model-v4.md)、[数据来源](docs/data-sources-v4.md)、[重建审计](docs/model-rebuild-audit.md)、[实网与回测结果](docs/verification-v3.0.0.md)、[接口与迁移](docs/local-api-v2.md)、[Windows验收](docs/windows-acceptance-v3.md)、[3.0.0交付说明](docs/release-notes-v3.0.0.md)。MIT许可，保留上游版权声明。
