# v2.0.0 基金与 A 股风险看板

将基金估值和 A 股恐慌指数整合为 Windows 本地桌面客户端。安装版与便携版均内置 Python V3 引擎，无需额外安装 Python。

- 左右看板、独立刷新、自动刷新设置、托盘驻留与窗口恢复。
- 盘中 raw/display、收盘正式值、四组件、confidence/coverage 和来源质量。
- 当日盘中与近一年已有收盘曲线，不补历史记录；支持导出图片。
- 统一用户目录、WAL 升级备份、引擎版本检查、一次自动重启和异常退出清理。

下载 `FundAndPanic-Setup-2.0.0-x64.exe` 安装版或 `FundAndPanic-Portable-2.0.0-x64.exe` 便携版。可使用 SHA256SUMS.txt 核对文件。

Windows 11 x64 已实际验证，目标为 Windows 10/11 x64。安装包未签名，系统可能显示未知发布者。首次运行可能暂无恐慌历史，数据源不可用或没有完整收盘桶时不会生成虚构结果。

具体测试范围见仓库 docs/verification.md。
