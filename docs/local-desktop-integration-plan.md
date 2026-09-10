# 本地桌面客户端整合说明

## 架构与入口

`desktop/` 基于原基金 Windows 桌面壳，保留基金数据源和托盘功能；`engine/` 保留 V3 计算及 V5 数据库。桌面客户端为正式入口，Python CLI 仅供本地诊断。

客户端 2.0.0、引擎 3.0-realtime、数据库 5。主进程通过 `PanicEngineManager` 管理启动、停止、重启、健康等待与状态查询。开发模式运行 `engine/server.py`；发行模式仅运行 `resources/engine/panic-engine/panic-engine.exe`，不存在系统 Python 或历史目录回退。

启动时创建用户目录，将初始配置复制为 `config/panic.yaml`，分配 `127.0.0.1` 随机可用端口，并传入数据库、配置、日志、客户端版本、随机实例标识及父进程 PID。健康检查同时核对版本、schema 和实例，避免误连被占用端口。启动失败或运行中异常只自动重试一次，之后显示日志路径，用户可手动重试。

窗口与基金初始化独立进行，不等待引擎成功。正常退出等待引擎停止及设置保存；Windows 父进程句柄监测会清理客户端被强制结束后遗留的引擎。

## 接口边界

渲染层仅使用 preload 的类型化 `panic` 接口：

- `getRealtime()` / `getDailyLatest()`
- `getRealtimeHistory(date)` / `getDailyHistory(limit)`
- `getSources()` / `getHealth()`
- `refresh()` / `generateChart(type)`

IPC 校验发送窗口及主框架，日期、条数、图表类型及设置参数均验证后处理。渲染层无法任意发起 HTTP 或访问文件系统。

Python 保留 `/api/v1/realtime`、`/api/v1/realtime/history`、`/api/v1/daily/latest`、`/api/v1/daily/history`、`/api/v1/sources`、`/api/v1/reference`、`/healthz`。新增 POST `/api/v1/realtime/refresh` 真正执行采集，POST `/api/v1/chart?type=intraday|daily` 导出图表。具备当天完整收盘桶、达到正式化时间且尚未正式化时，刷新流程生成 daily。

## 数据与界面

用户数据均保存到 Electron `userData` 的 config、data、backups、logs、reports、cache。基金配置与恐慌配置分开。旧 `state.json` 可迁移至 `config/funds.json`，保留原文件；恐慌实时缓存不从配置恢复。

SQLite 维持 WAL；V5 升级前使用在线备份，包含已提交的 WAL 内容。旧版数据库先完整备份，再按现有 V5 逻辑初始化；备份不删除。

左侧基金、右侧恐慌、下方曲线和来源质量。全局、基金、恐慌刷新入口独立，自动刷新统一开关与周期。加载、无数据、成功、过期、错误均有明确文字。引擎失败清空实时值；正式收盘保留交易日期。

盘中查询和渲染均按上海当天过滤，避免首次加载混入过去日期。日线只显示截至上海当前日期近一年已有记录；无重采样、无补日期、无补值。图表导出到 reports，主进程校验返回路径后只向渲染层提供 PNG dataURL 与文件位置。

## 构建与发布

`packaging/build-engine.ps1` 使用独立虚拟环境，完整收集动态依赖和静态资源。构建前后比较源码哈希并生成 `build-manifest.json`；打包前再验证哈希和内置 EXE 健康检查。

Electron 构建复用 npm 安装的当前版本运行时，避免 Windows 解压临时目录重命名占用问题。安装版和便携版输出到 desktop/dist，源码不包含数据库、缓存、日志、图片报告或二进制包。`packaging/check-source.py` 检查已跟踪文件中的排除项、疑似密钥与编码。

公开仓库为 `hu619340515/fund-and-panic-desktop`，默认分支 main。新仓库从干净初始提交开始，不包含旧项目历史。发布 v2.0.0 的两个 Windows x64 EXE 和 SHA-256 校验文件，认证只使用本机 GitHub 凭据。

验收命令及结果见 [验收记录](verification.md)。
