# 基金与 A 股风险看板

面向 Windows 10/11 x64 的本地桌面客户端，将基金组合估值与 A 股恐慌指数放在同一个看板。Electron 管理窗口、托盘和设置，内置 Python 引擎负责 V3 计算、数据源、SQLite V5 和本地服务。普通用户无需安装 Python。

## 下载与安装

- [安装版 2.0.3](https://github.com/hu619340515/fund-and-panic-desktop/releases/download/v2.0.3/FundAndPanic-Setup-2.0.3-x64.exe)：可选安装目录、桌面和开始菜单快捷方式。
- [便携版 2.0.3](https://github.com/hu619340515/fund-and-panic-desktop/releases/download/v2.0.3/FundAndPanic-Portable-2.0.3-x64.exe)：直接双击运行，数据仍保存在用户目录。
- [版本与校验文件](https://github.com/hu619340515/fund-and-panic-desktop/releases/tag/v2.0.3)。安装包未购买代码签名证书，Windows 可能显示未知发布者；请从本仓库 Release 下载并核对 SHA-256。

关闭主窗口后驻留托盘，点击托盘恢复，托盘菜单“退出”会终止引擎。卸载不会删除用户数据。开机启动、自动刷新、刷新周期、窗口置顶和透明度可在设置中调整。

## 看板与数据

基金区域支持增删、排序，显示估算净值与涨跌幅、官方净值、日期、来源和新鲜度。恐慌指数区域展示盘中 provisional 估计、收盘 final 指数、raw/display、等级、confidence/coverage、四组件及 QVIX、IF 合约、基差和市场宽度。两模块可以分别刷新；引擎断开不会影响基金模块。

盘中曲线读取当日真实记录；近一年区域分别显示正式收盘值与联网回算的历史估计。点击“补全历史”即可下载历史行情，不必等待逐日积累；输入不足的特征保持缺项，并显示覆盖率。正式收盘值仍要求完整收盘数据，历史估计不会冒充正式记录。

实时网页来源可能因休市、接口限流、网络或上游变更而不可用。失败时显示错误和日志位置，不将旧缓存标为实时。指数只用于市场压力研究，不预测涨跌。

## 本地目录

默认使用 Electron `app.getPath("userData")`，通常位于 `%APPDATA%/fund-and-panic-desktop`。以界面显示的日志路径为准。

```text
userData/
├── config/funds.json
├── config/panic.yaml
├── data/panic-index.db
├── backups/
├── logs/
├── reports/
└── cache/
```

配置、数据库、报告和日志保存在本机；无遥测或云端同步。行情请求直接访问公开数据源。数据库使用 WAL，迁移前自动备份。备份位置及服务状态可通过本地健康检查和日志确认。

## 开发与构建

开发环境需要 Node.js、PowerShell 7 和 Python 3.12 x64。

```powershell
npm.cmd run install:desktop
python -m pip install -r engine/requirements.txt
npm.cmd run dev
```

仓库根目录验收：

```powershell
npm.cmd run typecheck
npm.cmd test
python -m unittest discover -s engine/tests -v
npm.cmd run build
npm.cmd run build:engine
npm.cmd run dist:win
npm.cmd run test:e2e
node packaging/smoke-charts.cjs
pwsh -File packaging/smoke-install.ps1
node packaging/verify-worker-diagnostics.cjs
node packaging/smoke-live.cjs
```

引擎构建脚本使用独立虚拟环境和 PyInstaller；客户端打包前验证内置 EXE 的健康检查、版本和真实多进程采集机制。`smoke-live.cjs` 必须在交易时段联网执行，与离线测试分开记录。产物在 `desktop/dist/`：

```text
FundAndPanic-Setup-2.0.3-x64.exe
FundAndPanic-Portable-2.0.3-x64.exe
```

开发诊断 CLI 保留在 `engine/scripts/cli.py`，可以查询数据、探测来源和生成图表；正式入口为桌面程序。服务只监听 `127.0.0.1`，主进程分配随机端口，并校验实例标识、引擎版本和数据库版本。

## 故障排查

- **引擎未连接**：启动或意外退出后自动重试一次；失败时查看显示的 `engine-process.log`。点击恐慌区域刷新可再次尝试。
- **版本不匹配或内置文件缺失**：重新安装同一 Release 的完整安装包，不从历史目录拼接 EXE 或资源。
- **暂无数据**：首次启动、非交易时段或采集不足可能没有指数；等待交易时段并刷新，检查来源健康状态。
- **数据过期或网络错误**：检查网络和系统时钟，稍后重试；基金和恐慌区域分别显示各自状态。
- **行情采集失败**：查看错误中的数据类别、来源及重试时间；服务健康时不会因此重启，已有正式收盘及历史仍可读取。冷启动缺少历史的指标保持空值并标记暂定质量。
- **需要隔离诊断**：可用 `--user-data-dir=绝对目录` 启动独立用户目录。普通使用无需此参数。

详见 [集成说明](docs/local-desktop-integration-plan.md)、[2.0.1 修复说明](docs/release-notes-v2.0.1.md)、[2.0.1 验收记录](docs/verification-v2.0.1.md) 与 [2.0.0 历史验收](docs/verification.md)。

## 许可

MIT，保留上游项目版权声明。基于原基金 Windows 桌面版及 A 股恐慌指数 V3 引擎整合。

## 历史补全

在“近一年收盘与历史估计”中点击“补全历史”，下载真实历史行情后逐日回算。绿色虚线为历史估计，明确显示覆盖率与缺项，蓝色为正式收盘；两者独立保存。详见 [2.0.3 说明](docs/release-notes-v2.0.3.md)。
