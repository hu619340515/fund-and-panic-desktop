# 2.0.4 验收记录

日期：2026-09-12；Windows 11 x64，PowerShell 7.6.5。

## 构建和离线测试

- `npm.cmd run dist:win`：类型检查、43 项桌面测试、构建和 Windows 打包通过。
- `npm.cmd run build:engine`：重新生成内置 PyInstaller EXE；版本为客户端 2.0.4 / 引擎 3.0-realtime / 数据库 5。
- 内置 EXE 健康检查和实际 spawn 子进程的成功返回、异常退出、硬超时、回收检查通过。
- `python -X utf8 -m unittest discover -s engine/tests -v`：87 项，86 通过、1 项按条件跳过。复核时单独运行，耗时 42.229 秒。
- 首次使用虚拟环境 Python 时，服务进程号断言遇到 Windows 启动器与实际解释器 PID 不同；改用上述标准命令后通过。一次并行便携版解压期间，1.5 秒子进程诊断超时；停止并行重负载后完整复测通过。这两次失败未通过放宽断言处理。

## 最终便携版图表

`node packaging/smoke-history.cjs` 直接启动最终便携包，在中文独立用户目录运行，PATH 仅保留 Windows System32，并指定不存在的系统 Python。

- SQLite 离线夹具包含 3 条当日盘中、1 条其他日期盘中、3 条有日期缺口的正式记录及 1 条独立旧估计记录。
- 正式曲线只显示 3 条正式记录，旧估计记录仍保存在测试库中但不绘制；历史补全入口和估计状态已移除。
- 正式曲线位于市场数据上方，一级组件位于下方，与盘中曲线并排。已检查最终便携版截图。
- 两类图表均经真实 IPC 导出有效 PNG；导出不增加数据库记录。
- 单元测试另行覆盖只有估计时正式曲线为空，以及过滤暂定/估计记录和保留日期缺口。

测试夹具只写入 `output/` 下的隔离目录，不是实网行情，不随安装包交付。本次改动不涉及行情接口和模型，未重新执行交易时段实网数据完整性验收。

## 文件校验

- `ffc2cfdca7fad131921b0e2d83cdea7235418e0f538d4481a2bed58e032f146e  FundAndPanic-Setup-2.0.4-x64.exe`
- `e089cdb6aabad45c00d00cff2d2f022f397d6c3a867d11bdfe87cb085b0cf54c  FundAndPanic-Portable-2.0.4-x64.exe`

## 安装与退出回归

`pwsh -NoProfile -ExecutionPolicy Bypass -File packaging/smoke-install.ps1` 通过：安装版与便携版均验证设置和自动刷新周期、IPC 参数白名单、关闭驻留及恢复、网络错误、引擎异常退出一次重启与二次失败、基金独立刷新、退出无引擎残留。中文安装目录及无系统 Python 环境通过；卸载保留用户数据标记。

## 本地旧版本清理

`desktop/dist` 中 2.0.0–2.0.3 的 8 个 EXE、4 个 blockmap，以及旧 `win-unpacked.tmp` 已移入回收站。构建目录只保留 2.0.4 安装包、便携包、SHA256 和当前解包产物。用户配置与数据库未删除，GitHub 历史 Release 和源码历史保留。
