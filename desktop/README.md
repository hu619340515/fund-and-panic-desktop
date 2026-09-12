# Windows 桌面客户端

当前版本为 **3.0.0 观察版**，产品为“市场风险与基金仓位决策系统”。完整的下载、模型门槛、升级和故障排查请查看 [项目说明](../README.md)。安装版和便携版均随包提供 Python 引擎，用户无需安装 Python。

开发和构建请在仓库根目录执行：

```powershell
npm.cmd run install:desktop
npm.cmd run dev
npm.cmd run build:engine
npm.cmd run dist:win
```

打包文件位于此目录下的 `dist/`。安装与便携版使用同一 Windows 用户目录，升级保留基金配置和原数据库，首次迁移前备份；卸载保留数据。

界面通过受控 preload/IPC 访问主进程，Python 服务只监听本机。基金估值可独立刷新，行情失败不会重启健康服务。真实收盘回算与正式发布记录分开，未通过验证的概率和操作建议保持关闭。

验收结果见 [Windows 验收记录](../docs/windows-acceptance-v3.md)；算法与数据来源见 [模型说明](../docs/model-v4.md) 和 [来源清单](../docs/data-sources-v4.md)。
