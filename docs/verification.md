# v2.0.0 验收记录

验收日期：2026-09-10。实际系统：Windows 11 专业版 10.0.22631 x64。目标平台 Windows 10/11 x64；未在独立 Windows 10 机器复测。

## 自动检查

| 检查 | 结果 |
|---|---|
| `npm.cmd run typecheck` | 通过 |
| `npm.cmd test` | 8 个测试文件、37 项通过 |
| `python -m unittest discover -s engine/tests -v` | Ran 63 tests，OK (skipped=1)；62 项通过，1 项需要显式开启的真实网络探测跳过 |
| `npm.cmd run build` | 通过 |
| `npm.cmd run build:engine` | 独立虚拟环境 PyInstaller 构建通过 |
| `npm.cmd run dist:win` | 通过，输出安装版与便携版 |
| `npm.cmd run test:e2e` | 开发模式真实 Electron 通过，含实际 30 秒自动刷新 |
| `node packaging/smoke-charts.cjs` | 开发模式真实图表与 IPC 导出通过 |
| `node packaging/smoke-charts.cjs desktop/dist/win-unpacked/FundAndPanic.exe` | PyInstaller 内置引擎生成与桌面导出通过 |
| `pwsh -File packaging/smoke-install.ps1` | 最终安装包与便携包完整运行、静默安装和卸载保留数据通过 |
| `python packaging/check-source.py` | 源码排除项、密钥模式与 UTF-8 无 BOM 检查通过 |

进程测试覆盖随机端口、中文路径、启动超时、占用端口、实例/版本不匹配、命令缺失、GET 查询参数、POST 方法、异常退出只重启一次和正常停止。Python 子进程测试还覆盖父进程被结束后的清理，以及包含已提交 WAL 数据的升级备份。

桌面验收实际运行安装目录中的 EXE 与便携 EXE。子进程环境的 PATH 仅保留 Windows System32，PYTHON 指向不存在的程序，PYTHONHOME/PYTHONPATH 清空；两者均启动内置引擎并通过健康检查。此测试未卸载开发机本身的 Python。

界面验收覆盖设置保存、独立 IPC、窗口关闭驻留与恢复、断网错误、引擎异常清空实时值、基金独立刷新、退出无引擎残留。卸载验收在两个可能的默认用户目录写入独立标记，确认卸载后均存在，再仅移除测试标记。

## 图表真实性

图表 E2E 使用现有离线 fixture 派生的隔离测试数据库，明确为测试数据，不作为生产行情交付。夹具包含当天 3 条盘中记录、其他日期 1 条记录，以及有日期缺口的 3 条收盘记录。

UI 当日曲线严格等于当天 3 条；无日期 IPC 可读全库 4 条，两者单独断言。近一年曲线严格等于已有 3 条收盘记录，不补缺失日期或数值。两类 PNG 均由内置 PyInstaller 引擎生成，路径位于独立 userData/reports，真实 IPC 返回有效 PNG dataURL，两个界面导出按钮也分别验收。

截图、测试数据库及报告均在忽略目录 output/，不进入源码或安装包。

## 版本与产物

客户端：2.0.0；引擎：3.0-realtime；数据库：5。打包前独立验证当前 EXE 的健康信息和源码指纹。

引擎 sourceSha256：`958b85d08f03ea600a1eb2a5726aef5b84e4e8fb23401e2adb2f5642ead6a957`。

| 文件 | 字节数 | SHA-256 |
|---|---:|---|
| FundAndPanic-Setup-2.0.0-x64.exe | 157912768 | `eb37e8324ee130a3039a12607750128817b5e0bd26d25ae47dce8e477841658b` |
| FundAndPanic-Portable-2.0.0-x64.exe | 157685295 | `97c400c6f0a2f9c84e09c718674d36d1727b81b31836b6f0af33fb6223a2e570` |

安装包未签名（Authenticode: NotSigned）。真实数据源可用性仍受市场时段和上游接口影响；离线测试不代表每个外部来源当前均可用。发布文件包含相同 SHA-256 校验清单。
